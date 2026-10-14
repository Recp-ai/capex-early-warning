// Vercel function behind the stocks page's Run panel. Node, no dependencies. Separate from api/run.js.
//   POST /api/stock {passcode, ticker, research_model, scoring_model, send_email}
//                   -> dispatches .github/workflows/stocks.yml on main and returns the run URL
//   GET  /api/stock -> {status, conclusion, title, created_at, run_url} of the latest stocks.yml run
// Vercel environment variables (the same ones api/run.js uses): RUN_PASSCODE, GITHUB_TOKEN (fine-grained,
// this repo only, Actions read and write), optional GITHUB_REPO ("owner/name").
// Never put either secret in a response, an error, or a log line.
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
const MAX_RUNS_PER_DAY = 10; // counted on stocks.yml only
const MODEL_RE = /^claude-[a-z0-9.-]+$/;
const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
const STEPS = ["research_model", "scoring_model"];

class Fail extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

function scrub(s) {
  let out = String(s || "");
  for (const secret of [process.env.GITHUB_TOKEN, process.env.RUN_PASSCODE]) {
    if (secret) out = out.split(secret).join("***");
  }
  return out;
}

function send(res, status, body, cache) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", cache || "no-store");
  res.end(JSON.stringify(body));
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

async function runsSince(sinceMs) {
  const since = new Date(sinceMs).toISOString().replace(/\.\d{3}Z$/, "Z");
  const r = await gh("GET", runsPath({ event: "workflow_dispatch", created: `>=${since}`, per_page: "100" }));
  if (!r.ok) throw ghFail("checking recent stock runs", r);
  return (r.data.workflow_runs || []).filter(x => Date.parse(x.created_at) >= sinceMs).length;
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
      if (size > 10000) throw new Fail(413, "Request body too large");
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

async function handleStatus(res) {
  configured();
  const r = await gh("GET", runsPath({ event: "workflow_dispatch", per_page: "1" }));
  if (!r.ok) throw ghFail("reading the latest stock run", r);
  const run = (r.data.workflow_runs || [])[0];
  return send(res, 200, run ? {
    status: run.status, conclusion: run.conclusion, title: String(run.display_title || "").slice(0, 80),
    created_at: run.created_at, run_url: run.html_url,
  } : { status: null, conclusion: null }, "public, s-maxage=5");
}

async function handleRun(req, res) {
  configured();
  const body = await readBody(req);
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Fail(400, "Body must be a JSON object");
  if (!passcodeOk(typeof body.passcode === "string" ? body.passcode.slice(0, 500) : "")) {
    await sleep(1000); // slow down guessing
    throw new Fail(401, "Wrong passcode");
  }
  const ticker = normalizeTicker(body.ticker);
  if (!ticker) throw new Fail(400, "Invalid ticker: 1-10 characters, a letter first, then letters, digits, '.' or '-'");

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

  const now = Date.now();
  const recent = await runsSince(now - 24 * 3600 * 1000);
  if (recent >= MAX_RUNS_PER_DAY) {
    throw new Fail(429, `Limit reached: ${recent} stock runs in the last 24 hours (max ${MAX_RUNS_PER_DAY})`);
  }

  const inputs = { ticker, send_email: body.send_email === true ? "true" : "false", ...chosen };
  const r = await gh("POST", `/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, { ref: REF, inputs });
  if (!r.ok) throw ghFail("starting the stock workflow", r);
  const run = (r.data && r.data.html_url) ? r.data : await findRun(now);
  return send(res, 200, {
    ok: true, ticker,
    run_url: (run && run.html_url) || `https://github.com/${REPO}/actions/workflows/${WORKFLOW}`,
    run_found: Boolean(run && run.html_url),
  });
}

module.exports = async function handler(req, res) {
  try {
    if (req.method === "GET") return await handleStatus(res);
    if (req.method === "POST") return await handleRun(req, res);
    res.setHeader("Allow", "GET, POST");
    return send(res, 405, { error: "Method not allowed" });
  } catch (e) {
    if (e instanceof Fail) return send(res, e.status, { error: scrub(e.message) });
    console.error("api/stock failed:", scrub(e && e.message));
    return send(res, 500, { error: "Unexpected server error" });
  }
};
