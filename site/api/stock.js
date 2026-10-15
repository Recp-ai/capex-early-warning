// Vercel function behind the stocks page. Node, no dependencies. Separate from api/run.js.
//   POST /api/stock {passcode, tickers: [...] | ticker, research_model, scoring_model, send_email, reuse_news}
//                   -> dispatches .github/workflows/stocks.yml on main (one batch: a job per ticker). reuse_news: true
//                   re-scores with each ticker's latest news (write-up only); such runs are titled
//                   "Stock research (rescore) ..." and counted against their own 24-hour cap.
//   GET  /api/stock [?run=<id>] -> {status, conclusion, title, run_url, run_id, tickers: [{ticker, status}]}
//                   for that (or the latest) stocks.yml run; each ticker queued / running / done / failed
//   GET  /api/stock?lists -> the lists document from the STOCK_LISTS repo variable ({lists: []} if unset)
//   POST /api/stock {passcode, action: "save_lists", lists, base_revision, force} -> validates and saves
//                   the whole document (no dispatch; not counted in the ticker cap)
//   GET  /api/stock?quotes=all -> {TICKER: {price, as_of}, ...}: near-live Tiingo IEX quotes (tngoLast) for
//                   every ticker that is rated (site/data/stocks/index.json) or in STOCK_LISTS, in one request
//                   (two past 100 tickers; the all-tickers endpoint past 200). One fixed URL, cached 5 minutes
//                   at the edge, so Tiingo sees at most a fill per 5 minutes. Display only.
// Vercel environment variables: RUN_PASSCODE, GITHUB_TOKEN (fine-grained, this repo only: Actions read and
// write, Variables read and write), STOCK_PRICE_API_KEY (Tiingo; quotes only), optional GITHUB_REPO.
// Never put a secret in a response, an error, or a log line.
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const REPO = process.env.GITHUB_REPO ||
  (process.env.VERCEL_GIT_REPO_OWNER && process.env.VERCEL_GIT_REPO_SLUG
    ? `${process.env.VERCEL_GIT_REPO_OWNER}/${process.env.VERCEL_GIT_REPO_SLUG}`
    : "recp-ai/capex-early-warning");
const WORKFLOW = "stocks.yml";
const REF = "main";
// The same limits as config/stocks.yaml (a test checks they match).
const MAX_BATCH = 15;            // batch.max_batch
const MAX_TICKERS_PER_DAY = 30;  // batch.max_tickers_per_day, counted across stocks.yml runs (re-scores excluded)
const MAX_RESCORE_TICKERS_PER_DAY = 30; // batch.max_rescore_tickers_per_day, counted across re-score runs only
const MAX_LISTS = 30;            // lists.max_lists
const MAX_PER_LIST = 100;        // lists.max_tickers_per_list
const MAX_LIST_BYTES = 40000;    // lists.max_bytes (GitHub's variable limit is 48 KB)
const QUOTES_PER_REQUEST = 100;  // prices.quotes_max_tickers: tickers per iex/?tickers= request
const MAX_QUOTE_REQUESTS = 2;    // more tickers than this many requests hold: the all-tickers endpoint
const LISTS_VAR = "STOCK_LISTS";
const LISTS_VERSION = 1;
const MODEL_RE = /^claude-[a-z0-9.-]+$/;
const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
const LIST_ID_RE = /^[a-z0-9][a-z0-9_-]{0,23}$/;
const STEPS = ["research_model", "scoring_model"];
const IEX = "https://api.tiingo.com/iex";
const ACTIVE = ["queued", "waiting", "pending", "requested"];

class Fail extends Error {
  constructor(status, message, cache) { super(message); this.status = status; this.cache = cache; }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

function scrub(s) {
  let out = String(s || "");
  for (const secret of [process.env.GITHUB_TOKEN, process.env.RUN_PASSCODE, process.env.STOCK_PRICE_API_KEY]) {
    if (secret) out = out.split(secret).join("***");
  }
  return out;
}

// Every string in a response is scrubbed too (secrets of 6+ characters, so a short value can't mangle text).
function scrubDeep(v) {
  if (typeof v === "string") {
    let out = v;
    for (const secret of [process.env.GITHUB_TOKEN, process.env.RUN_PASSCODE, process.env.STOCK_PRICE_API_KEY]) {
      if (secret && secret.length >= 6) out = out.split(secret).join("***");
    }
    return out;
  }
  if (Array.isArray(v)) return v.map(scrubDeep);
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, scrubDeep(x)]));
  return v;
}

function send(res, status, body, cache) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", cache || "no-store");
  res.end(JSON.stringify(scrubDeep(body)));
}

function passcodeOk(given) {
  const expected = process.env.RUN_PASSCODE || "";
  if (!expected || typeof given !== "string" || !given) return false;
  // Hash both sides so timingSafeEqual always compares equal-length buffers.
  const a = crypto.createHash("sha256").update(given, "utf8").digest();
  const b = crypto.createHash("sha256").update(expected, "utf8").digest();
  return crypto.timingSafeEqual(a, b);
}

async function gh(method, url, body) {
  const r = await fetch("https://api.github.com" + url, {
    method,
    headers: {
      Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "capex-early-warning-stocks",
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { /* non-JSON error page */ }
  return { status: r.status, ok: r.ok, data };
}

function ghFail(what, r) {
  const msg = r.data && typeof r.data.message === "string" ? ` (${r.data.message.slice(0, 160)})` : "";
  return new Fail(502, `GitHub returned ${r.status} while ${what}${msg}`);
}

const runsPath = params => `/repos/${REPO}/actions/workflows/${WORKFLOW}/runs?${new URLSearchParams(params)}`;

// The run name is "Stock research <tickers>", or "Stock research (rescore) <tickers>" for a reuse_news re-score
// (stocks.yml run-name), so the tickers of a run, and its kind, come from its title.
const RESCORE_RE = /^Stock research \(rescore\)\s*/i;
const isRescore = title => RESCORE_RE.test(String(title || ""));
function tickersInTitle(title) {
  const rest = String(title || "").replace(RESCORE_RE, "").replace(/^Stock research\s*/i, "");
  return rest.split(/[\s,]+/).filter(Boolean).map(t => t.toUpperCase()).filter(t => t.length <= 12);
}

async function recentRuns(sinceMs) {
  const since = new Date(sinceMs).toISOString().replace(/\.\d{3}Z$/, "Z");
  const r = await gh("GET", runsPath({ event: "workflow_dispatch", created: `>=${since}`, per_page: "100" }));
  if (!r.ok) throw ghFail("checking recent stock runs", r);
  return (r.data.workflow_runs || []).filter(x => Date.parse(x.created_at) >= sinceMs);
}

async function findRun(sinceMs) {
  // The dispatch call returns no run id; the new run shows up in the list within a few seconds.
  for (let i = 0; i < 5; i++) {
    await sleep(1500);
    const r = await gh("GET", runsPath({ event: "workflow_dispatch", per_page: "5" }));
    if (!r.ok) return null;
    const run = (r.data.workflow_runs || []).find(x => Date.parse(x.created_at) >= sinceMs - 10000);
    if (run) return run;
  }
  return null;
}

async function saveVariable(name, value) {
  let r = await gh("PATCH", `/repos/${REPO}/actions/variables/${name}`, { name, value });
  if (r.status === 404) r = await gh("POST", `/repos/${REPO}/actions/variables`, { name, value });
  if (!r.ok) throw ghFail(`saving the ${name} variable`, r);
}

const idsFrom = catalog => new Set(((catalog && catalog.models) || []).map(m => m && m.id).filter(id => MODEL_RE.test(id || "")));

async function knownModels(req) {
  // site/data/models.json is written by the capex workflow; vercel.json bundles it with this function.
  for (const p of [path.join(process.cwd(), "data", "models.json"), path.join(__dirname, "..", "data", "models.json")]) {
    try { return idsFrom(JSON.parse(fs.readFileSync(p, "utf8"))); } catch (e) { /* try the next one */ }
  }
  const host = String(req.headers.host || "");
  if (/^[a-z0-9.-]+(:\d+)?$/i.test(host)) {
    try {
      const r = await fetch(`https://${host}/data/models.json`, { cache: "no-store" });
      if (r.ok) return idsFrom(await r.json());
    } catch (e) { /* fall through */ }
  }
  return null;
}

async function readBody(req) {
  try {
    if (req.body && typeof req.body === "object" && !Buffer.isBuffer(req.body)) return req.body;
    if (typeof req.body === "string" || Buffer.isBuffer(req.body)) return JSON.parse(String(req.body) || "{}");
    const chunks = [];
    let size = 0;
    for await (const c of req) {
      size += c.length;
      if (size > 64000) throw new Fail(413, "Request body too large");
      chunks.push(c);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch (e) {
    if (e instanceof Fail) throw e;
    throw new Fail(400, "Body must be JSON");
  }
}

function configured() {
  if (!process.env.GITHUB_TOKEN || !process.env.RUN_PASSCODE) {
    throw new Fail(503, "Stock runs are not set up yet: add GITHUB_TOKEN and RUN_PASSCODE in Vercel, then redeploy");
  }
}

function normalizeTicker(v) {
  const t = typeof v === "string" ? v.trim().toUpperCase() : "";
  return TICKER_RE.test(t) ? t : null;
}

const query = req => new URL(String(req.url || "/"), "http://x").searchParams;

// ---------------------------------------------------------------- lists (STOCK_LISTS variable)
function validateLists(lists) {
  if (!Array.isArray(lists)) throw new Fail(400, "lists must be an array");
  if (lists.length > MAX_LISTS) throw new Fail(400, `Too many lists: ${lists.length} (max ${MAX_LISTS})`);
  const ids = new Set();
  const out = lists.map((l, i) => {
    if (!l || typeof l !== "object" || Array.isArray(l)) throw new Fail(400, `List ${i + 1} must be an object`);
    const id = typeof l.id === "string" ? l.id : "";
    if (!LIST_ID_RE.test(id)) throw new Fail(400, `List ${i + 1} has an invalid id`);
    if (ids.has(id)) throw new Fail(400, `Two lists share the id ${id}`);
    ids.add(id);
    const name = typeof l.name === "string" ? l.name.trim() : "";
    if (!name || name.length > 40 || /[\u0000-\u001f\u007f]/.test(name)) {
      throw new Fail(400, `List ${i + 1}: the name must be 1-40 characters`);
    }
    if (!Array.isArray(l.tickers)) throw new Fail(400, `List "${name}": tickers must be an array`);
    if (l.tickers.length > MAX_PER_LIST) throw new Fail(400, `List "${name}" has ${l.tickers.length} tickers (max ${MAX_PER_LIST})`);
    const seen = new Set();
    const tickers = l.tickers.map(raw => {
      const t = normalizeTicker(raw);
      if (!t) throw new Fail(400, `List "${name}": invalid ticker "${String(raw).slice(0, 12)}"`);
      if (seen.has(t)) throw new Fail(400, `List "${name}" has ${t} twice`);
      seen.add(t);
      return t;
    });
    return { id, name, tickers };
  });
  return out;
}

function listsDoc(lists, revision) {
  const doc = { version: LISTS_VERSION, revision, updated_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), lists };
  const bytes = Buffer.byteLength(JSON.stringify(doc), "utf8");
  if (bytes > MAX_LIST_BYTES) {
    throw new Fail(413, `Lists too large to save: ${bytes} bytes (max ${MAX_LIST_BYTES}); remove some tickers or lists`);
  }
  return doc;
}

async function readLists() {
  const r = await gh("GET", `/repos/${REPO}/actions/variables/${LISTS_VAR}`);
  if (r.status === 404) return { version: LISTS_VERSION, revision: 0, lists: [] };
  if (!r.ok) throw ghFail(`reading the ${LISTS_VAR} variable`, r);
  let doc;
  try { doc = JSON.parse((r.data && r.data.value) || ""); } catch (e) { doc = null; }
  if (!doc || typeof doc !== "object" || !Array.isArray(doc.lists)) {
    return { version: LISTS_VERSION, revision: 0, lists: [], error: `The saved ${LISTS_VAR} variable is not a lists document; saving replaces it` };
  }
  let lists;
  try { lists = validateLists(doc.lists); } catch (e) {
    return { version: LISTS_VERSION, revision: Number(doc.revision) || 0, lists: [], error: `The saved lists are invalid (${e.message}); saving replaces them` };
  }
  return { version: LISTS_VERSION, revision: Number(doc.revision) || 0, updated_at: doc.updated_at || null, lists };
}

async function handleGetLists(res) {
  configured();
  return send(res, 200, await readLists());
}

async function handleSaveLists(body, res) {
  const lists = validateLists(body.lists);
  const current = await readLists();
  const base = body.base_revision;
  if (body.force !== true && typeof base === "number" && base !== current.revision) {
    throw new Fail(409, "The lists were saved from another tab or device since you loaded them. Reload them, or save again to overwrite.");
  }
  const doc = listsDoc(lists, (current.revision || 0) + 1);
  await saveVariable(LISTS_VAR, JSON.stringify(doc));
  return send(res, 200, { ok: true, ...doc });
}

// ---------------------------------------------------------------- quotes (Tiingo IEX, display only)
const tiingoSymbol = t => t.replace(/\./g, "-").toLowerCase();

async function iex(url) {
  let r;
  try {
    r = await fetch(url, { headers: { Authorization: `Token ${process.env.STOCK_PRICE_API_KEY}`, "Content-Type": "application/json" } });
  } catch (e) {
    return { error: "Quote API unreachable", fallback: true };
  }
  if (r.status === 401 || r.status === 403) return { error: `Quote API rejected the key (HTTP ${r.status})` };
  if (r.status === 429) return { error: "Quote API rate limit reached; try again later", status: 503 };
  if (!r.ok) return { error: `Quote API returned HTTP ${r.status}`, fallback: true };
  let data = null;
  try { data = JSON.parse(await r.text()); } catch (e) { /* not JSON */ }
  if (!Array.isArray(data)) return { error: "Quote API returned an unexpected response", fallback: true };
  return { data };
}

function parseQuotes(data, tickers) {
  const wanted = new Map(tickers.map(t => [tiingoSymbol(t).toUpperCase(), t]));
  const out = {};
  for (const d of data) {
    const t = d && wanted.get(String(d.ticker || "").toUpperCase());
    const price = t ? Number(d.tngoLast) : NaN; // tngoLast: Tiingo's last price, the official close once it prints
    if (!t || d.tngoLast == null || !Number.isFinite(price) || price <= 0) continue;
    const ms = Date.parse(d.timestamp);
    out[t] = { price, as_of: Number.isFinite(ms) ? new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z") : null };
  }
  return out;
}

// The tickers the page can show: rated ones (index.json, written by the stock workflow) and listed ones.
// Nothing else is ever sent to Tiingo.
async function ratedTickers(req) {
  let index = null;
  try { index = JSON.parse(fs.readFileSync(path.join(process.cwd(), "data", "stocks", "index.json"), "utf8")); } catch (e) { /* not bundled */ }
  const host = String(req.headers.host || "");
  if (!index && /^[a-z0-9.-]+(:\d+)?$/i.test(host)) {
    try {
      const r = await fetch(`https://${host}/data/stocks/index.json`, { cache: "no-store" });
      if (r.ok) index = await r.json();
    } catch (e) { /* fall through */ }
  }
  if (!index || !Array.isArray(index.tickers)) return null;
  return index.tickers.map(e => normalizeTicker(e && e.ticker)).filter(Boolean);
}

async function listedTickers() {
  if (!process.env.GITHUB_TOKEN) return null;
  try { return (await readLists()).lists.flatMap(l => l.tickers); } catch (e) { return null; }
}

async function handleQuotes(req, res) {
  // One fixed URL, so the edge cache bounds Tiingo use: anything else is refused before any upstream call.
  const q = [...query(req).entries()];
  if (q.length !== 1 || q[0][0] !== "quotes" || q[0][1] !== "all") throw new Fail(400, "Use ?quotes=all");
  if (!process.env.STOCK_PRICE_API_KEY) throw new Fail(503, "Live quotes are not set up: add STOCK_PRICE_API_KEY in Vercel, then redeploy");
  const [rated, listed] = await Promise.all([ratedTickers(req), listedTickers()]);
  if (!rated && !listed) throw new Fail(503, "Ticker list unavailable; try again shortly", "public, s-maxage=60");
  const tickers = [...new Set([...(rated || []), ...(listed || [])])].sort();
  if (!tickers.length) return send(res, 200, {}, "public, s-maxage=300");
  // iex/?tickers= in chunks of 100 (one request for up to 100 tickers); beyond two chunks, or if that form
  // fails (not a rejected key or the rate limit), the all-tickers endpoint once, filtered here.
  const chunks = [];
  for (let i = 0; i < tickers.length; i += QUOTES_PER_REQUEST) chunks.push(tickers.slice(i, i + QUOTES_PER_REQUEST));
  let rows = [], got = { fallback: chunks.length > MAX_QUOTE_REQUESTS };
  if (!got.fallback) {
    for (const c of chunks) {
      got = await iex(`${IEX}/?tickers=${c.map(tiingoSymbol).join(",")}`);
      if (got.error) break;
      rows = rows.concat(got.data);
    }
  }
  if (got.fallback) { got = await iex(IEX); rows = got.data || []; }
  if (got.error) throw new Fail(got.status || 502, got.error, "public, s-maxage=60");
  return send(res, 200, parseQuotes(rows, tickers), "public, s-maxage=300");
}

// ---------------------------------------------------------------- runs
function jobState(job) {
  if (!job) return { status: "queued" };
  if (job.status !== "completed") return { status: job.status === "in_progress" ? "running" : "queued" };
  if (job.conclusion === "success") return { status: "done" };
  const step = (job.steps || []).find(s => s.conclusion === "failure" || s.conclusion === "cancelled");
  return { status: "failed", reason: job.conclusion || "failed", ...(step ? { step: String(step.name || "").slice(0, 80) } : {}) };
}

async function handleStatus(req, res) {
  configured();
  const id = query(req).get("run");
  let run;
  if (id) {
    if (!/^\d{1,20}$/.test(id)) throw new Fail(400, "Invalid run id");
    const r = await gh("GET", `/repos/${REPO}/actions/runs/${id}`);
    if (!r.ok) throw ghFail("reading the stock run", r);
    run = r.data;
    if (!run || !String(run.path || "").endsWith(`workflows/${WORKFLOW}`)) throw new Fail(404, "Not a stock research run");
  } else {
    const r = await gh("GET", runsPath({ event: "workflow_dispatch", per_page: "1" }));
    if (!r.ok) throw ghFail("reading the latest stock run", r);
    run = (r.data.workflow_runs || [])[0];
  }
  if (!run) return send(res, 200, { status: null, conclusion: null }, "public, s-maxage=5");
  const j = await gh("GET", `/repos/${REPO}/actions/runs/${run.id}/jobs?per_page=100`);
  const jobs = j.ok ? (j.data.jobs || []) : [];
  const byTicker = new Map();
  for (const job of jobs) {
    const m = /^rate (\S+)$/.exec(String(job.name || ""));
    if (m) byTicker.set(m[1].toUpperCase(), job);
  }
  const named = n => jobs.find(x => x.name === n);
  const tickers = tickersInTitle(run.display_title).map(t => {
    const st = byTicker.has(t) ? jobState(byTicker.get(t))
      : run.status === "completed" ? { status: run.conclusion === "success" ? "done" : "failed", reason: run.conclusion } : { status: "queued" };
    return { ticker: t, ...st };
  });
  const state = job => (job ? (job.status === "completed" ? job.conclusion : job.status) : null);
  return send(res, 200, {
    status: run.status, conclusion: run.conclusion, title: String(run.display_title || "").slice(0, 200),
    rescore: isRescore(run.display_title),
    created_at: run.created_at, run_url: run.html_url, run_id: run.id, tickers,
    plan: state(named("plan")), collect: state(named("collect")),
  }, "public, s-maxage=5");
}

async function handleRun(req, res) {
  configured();
  const body = await readBody(req);
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Fail(400, "Body must be a JSON object");
  if (!passcodeOk(typeof body.passcode === "string" ? body.passcode.slice(0, 500) : "")) {
    await sleep(1000); // slow down guessing
    throw new Fail(401, "Wrong passcode");
  }
  if (body.action === "save_lists") return handleSaveLists(body, res);
  if (body.action !== undefined) throw new Fail(400, "Unknown action");

  const raw = Array.isArray(body.tickers) ? body.tickers : typeof body.ticker === "string" ? [body.ticker] : null;
  if (!raw || !raw.length) throw new Fail(400, "Give a ticker or a list of tickers");
  if (raw.length > 100) throw new Fail(400, `A batch takes at most ${MAX_BATCH} tickers`);
  const tickers = [];
  for (const v of raw) {
    const t = normalizeTicker(v);
    if (!t) throw new Fail(400, `Invalid ticker "${String(v).slice(0, 12)}": 1-10 characters, a letter first, then letters, digits, '.' or '-'`);
    if (!tickers.includes(t)) tickers.push(t);
  }
  if (tickers.length > MAX_BATCH) throw new Fail(400, `${tickers.length} tickers; a batch takes at most ${MAX_BATCH}`);

  if (body.reuse_news !== undefined && typeof body.reuse_news !== "boolean") throw new Fail(400, "reuse_news must be true or false");
  const rescore = body.reuse_news === true;

  const chosen = {};
  for (const field of STEPS) {
    const v = body[field];
    if (v === undefined || v === null || v === "") continue;
    if (typeof v !== "string" || !MODEL_RE.test(v)) throw new Fail(400, `Invalid ${field}`);
    chosen[field] = v;
  }
  if (Object.keys(chosen).length) {
    const known = await knownModels(req);
    if (!known) throw new Fail(503, "Model list not available yet; leave both models on their defaults");
    for (const [field, v] of Object.entries(chosen)) {
      if (!known.has(v)) throw new Fail(400, `Unknown ${field}: not in data/models.json`);
    }
  }

  // Full runs and re-scores (write-up only) have separate caps; each counts only its own kind of run.
  const now = Date.now();
  const runs = await recentRuns(now - 24 * 3600 * 1000);
  const cap = rescore ? MAX_RESCORE_TICKERS_PER_DAY : MAX_TICKERS_PER_DAY;
  const used = runs.filter(x => isRescore(x.display_title) === rescore).reduce((n, x) => n + tickersInTitle(x.display_title).length, 0);
  if (used + tickers.length > cap) {
    throw new Fail(429, `Limit: ${used} of ${cap} ${rescore ? "rescored " : ""}tickers used in the last 24 hours; this batch has ${tickers.length}`);
  }
  // GitHub keeps one running and one waiting run per concurrency group and cancels an older waiting one.
  if (runs.some(x => ACTIVE.includes(x.status))) {
    throw new Fail(409, "A stock batch is already waiting to start; run this one when it has started");
  }

  const inputs = { tickers: tickers.join(","), send_email: body.send_email === true ? "true" : "false", ...chosen,
    ...(rescore ? { reuse_news: "true" } : {}) };
  const r = await gh("POST", `/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, { ref: REF, inputs });
  if (!r.ok) throw ghFail("starting the stock workflow", r);
  const run = (r.data && r.data.html_url) ? r.data : await findRun(now);
  return send(res, 200, {
    ok: true, ticker: tickers[0], tickers, rescore, used_24h: used + tickers.length, max_24h: cap,
    run_url: (run && run.html_url) || `https://github.com/${REPO}/actions/workflows/${WORKFLOW}`,
    run_id: (run && run.id) || null, run_found: Boolean(run && run.html_url),
  });
}

module.exports = async function handler(req, res) {
  try {
    if (req.method === "GET") {
      const q = query(req);
      if (q.has("quotes")) return await handleQuotes(req, res);
      if (q.has("lists")) return await handleGetLists(res);
      return await handleStatus(req, res);
    }
    if (req.method === "POST") return await handleRun(req, res);
    res.setHeader("Allow", "GET, POST");
    return send(res, 405, { error: "Method not allowed" });
  } catch (e) {
    if (e instanceof Fail) return send(res, e.status, { error: scrub(e.message) }, e.cache);
    console.error("api/stock failed:", scrub(e && e.message));
    return send(res, 500, { error: "Unexpected server error" });
  }
};
