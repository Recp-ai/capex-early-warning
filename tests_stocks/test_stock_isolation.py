"""The stock module stays separate from the capex pipeline; the workflow, page and Vercel function.
api/stock.js runs under node with fetch mocked (skipped if node is not installed)."""
import json
import re
import shutil
import subprocess

import pytest
import yaml

from cew import util

ROOT = util.ROOT
NODE = shutil.which("node")


def workflow(name):
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
    return wf, wf[True] if True in wf else wf["on"]  # PyYAML reads the "on" key as a boolean


def test_stock_workflow_plans_runs_one_job_per_ticker_and_collects_once():
    wf, on = workflow("stocks.yml")
    assert list(on) == ["workflow_dispatch"] and "schedule" not in on
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"tickers", "research_model", "scoring_model", "send_email", "reuse_news"}
    assert inputs["tickers"]["required"] is True
    assert inputs["send_email"]["default"] is False and inputs["reuse_news"]["default"] is False
    assert wf["run-name"] == "Stock research ${{ inputs.tickers }}"  # api/stock.js counts tickers from run titles
    assert wf["concurrency"] == {"group": "stocks", "cancel-in-progress": False}
    jobs = wf["jobs"]
    assert list(jobs) == ["plan", "rate", "collect"]
    for job in jobs.values():
        for s in job["steps"]:
            assert "${{" not in s.get("run", ""), f"expression inside run: of {s.get('name')}"
    plan = jobs["plan"]["steps"]
    assert next(s for s in plan if s.get("name") == "Unit tests (stock module)")["run"] == "python -m pytest -q tests_stocks"
    step = next(s for s in plan if s.get("name") == "Plan the batch")
    assert step["env"]["INPUT_TICKERS"] == "${{ inputs.tickers }}" and step["run"] == "python -m stocks.batch plan --shared shared"
    rate = jobs["rate"]
    assert rate["strategy"]["fail-fast"] is False and rate["needs"] == "plan"
    assert rate["strategy"]["matrix"]["ticker"] == "${{ fromJSON(needs.plan.outputs.tickers) }}"
    assert rate["strategy"]["max-parallel"] == "${{ fromJSON(needs.plan.outputs.max_parallel) }}"
    assert rate["name"] == "rate ${{ matrix.ticker }}"  # api/stock.js reads per-ticker status from these job names
    env = next(s for s in rate["steps"] if s.get("name") == "Research the stock")["env"]
    assert env["INPUT_TICKER"] == "${{ matrix.ticker }}" and env["STOCK_PRICE_API_KEY"] == "${{ secrets.STOCK_PRICE_API_KEY }}"
    assert env["INPUT_SCORING_MODEL"] == "${{ inputs.scoring_model }}" and "SMTP_PASSWORD" not in env
    assert env["INPUT_REUSE_NEWS"] == "${{ inputs.reuse_news }}"
    assert not any("git " in s.get("run", "") for s in rate["steps"]), "ticker jobs never commit"
    upload = next(s for s in rate["steps"] if s.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["if"] == "always()" and upload["with"]["name"] == "stock-${{ matrix.ticker }}"
    col = jobs["collect"]
    assert col["if"] == "always() && needs.plan.result == 'success'" and col["needs"] == ["plan", "rate"]
    env = next(s for s in col["steps"] if s.get("name") == "Write the results and send one email")["env"]
    assert env["PLAN_TICKERS"] == "${{ needs.plan.outputs.tickers }}" and env["INPUT_SEND_EMAIL"] == "${{ inputs.send_email }}"
    commit = next(s for s in col["steps"] if s.get("name") == "Commit once")["run"]
    assert "data/stocks site/data/stocks" in commit and commit.count("git commit") == 2 and "git pull --rebase" in commit
    assert "for i in 1 2 3 4 5" in commit and "git rebase --abort" in commit
    assert sum("git push" in s.get("run", "") for j in jobs.values() for s in j["steps"]) == 1


def test_function_limits_match_the_config():
    cfg = yaml.safe_load((ROOT / "config" / "stocks.yaml").read_text())
    js = (ROOT / "site" / "api" / "stock.js").read_text()
    const = lambda name: int(re.search(rf"const {name} = +(\d+);", js).group(1))  # noqa: E731
    assert const("MAX_BATCH") == cfg["batch"]["max_batch"] == 15
    assert const("MAX_TICKERS_PER_DAY") == cfg["batch"]["max_tickers_per_day"] == 30
    assert const("MAX_LISTS") == cfg["lists"]["max_lists"] == 30
    assert const("MAX_PER_LIST") == cfg["lists"]["max_tickers_per_list"] == 100
    assert const("MAX_LIST_BYTES") == cfg["lists"]["max_bytes"] and cfg["lists"]["max_bytes"] <= 45000  # GitHub's limit: 48 KB
    assert const("QUOTES_PER_REQUEST") == cfg["prices"]["quotes_max_tickers"] == 100 and const("MAX_QUOTE_REQUESTS") == 2
    assert cfg["batch"]["max_parallel"] == 5 and cfg["page"]["stale_after_days"] == 14
    page = (ROOT / "site" / "stocks.html").read_text()
    assert "const MAX_LISTS = 30, MAX_PER_LIST = 100;" in page
    assert page.count("quotes=") == 1 and 'fetch("api/stock?quotes=all")' in page  # the one cacheable quote URL


def test_capex_workflow_runs_only_the_capex_tests():
    wf, _ = workflow("weekly.yml")
    step = next(s for s in wf["jobs"]["run"]["steps"] if s.get("name") == "Unit tests")
    assert step["run"].split("#")[0].strip() == "python -m pytest -q tests"


def test_capex_code_never_imports_the_stock_module():
    for p in (ROOT / "cew").glob("*.py"):
        assert not re.search(r"^\s*(from|import)\s+stocks\b", p.read_text(), re.M), p.name
    assert "stocks" not in (ROOT / "site" / "api" / "run.js").read_text()


def test_pages_link_each_other_and_say_not_investment_advice():
    stocks = (ROOT / "site" / "stocks.html").read_text()
    index = (ROOT / "site" / "index.html").read_text()
    assert '<a href="./">← Capex dashboard</a>' in stocks and '<a href="stocks.html">Stock research →</a>' in index
    assert "<b>Not investment advice.</b>" in stocks
    assert 'get("data/stocks/index.json")' in stocks and 'get("data/models.json")' in stocks and '"api/stock"' in stocks
    assert 'DEFAULTS = { research: "claude-sonnet-5-5", scoring: "claude-opus-5-5" }' in stocks
    conf = json.loads((ROOT / "site" / "vercel.json").read_text())["functions"]
    assert conf["api/stock.js"]["includeFiles"] == "data/*.json" and "api/run.js" in conf


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_page_script_and_function_parse(tmp_path):
    js = tmp_path / "page.js"
    js.write_text(re.search(r"<script>(.*)</script>", (ROOT / "site" / "stocks.html").read_text(), re.S).group(1))
    assert subprocess.run([NODE, "--check", str(js)], capture_output=True).returncode == 0
    assert subprocess.run([NODE, "--check", str(ROOT / "site" / "api" / "stock.js")], capture_output=True).returncode == 0


def page_function(name):
    """One top-level function of the stocks page's script, as source."""
    page = (ROOT / "site" / "stocks.html").read_text()
    return re.search(rf"\n  function {name}\(.*?\n  }}\n", page, re.S).group(0)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_price_axis_ticks_always_cover_the_data():
    cases = [[15.2, 118.4], [61.2, 118.4], [0.37, 0.52], [100, 100], [99.5, 150], [2.04, 2.04], [-3.2, 7.9], [1234.5, 4321.9], [50, 100]]
    script = page_function("niceTicks") + f"console.log(JSON.stringify({json.dumps(cases)}.map(([lo, hi]) => niceTicks(lo, hi))));"
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    for (lo, hi), ticks in zip(cases, json.loads(p.stdout)):
        assert ticks[0] <= lo and ticks[-1] >= hi, (lo, hi, ticks)
        assert ticks == sorted(ticks) and len(set(ticks)) == len(ticks) and 3 <= len(ticks) <= 8, (lo, hi, ticks)
        steps = {round(b - a, 9) for a, b in zip(ticks, ticks[1:])}
        assert len(steps) == 1, (lo, hi, ticks)  # evenly spaced
    assert json.loads(p.stdout)[0] == [0, 50, 100, 150]  # INOD 2Y (step 50): the top tick was 100, below the ~118 high


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_batch_tags_clear_once_results_are_on_the_page_but_failures_stay_until_dismissed():
    page = (ROOT / "site" / "stocks.html").read_text()
    failed = re.search(r"\n  const batchFailed = .*?;\n", page).group(0)
    script = ("const esc = s => String(s); let state = {};" + failed + page_function("statusTag") + """
      const st = { MSFT: { ticker: "MSFT", status: "done" }, ZZZZ: { ticker: "ZZZZ", status: "failed", step: "Research the stock" },
                   NVDA: { ticker: "NVDA", status: "running" } };
      const out = {};
      for (const phase of ["running", "deploying", "updated", "lost"]) {
        state = { batch: { phase, statuses: st } };
        out[phase] = Object.fromEntries(Object.keys(st).map(t => [t, statusTag(t)]));
      }
      state = { batch: null };
      out.dismissed = statusTag("ZZZZ");
      out.failed = [batchFailed({ statuses: st }), batchFailed({ statuses: { MSFT: st.MSFT } }), batchFailed(null)];
      console.log(JSON.stringify(out));""")
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert all(out["running"].values()) and ">done<" in out["running"]["MSFT"]
    assert out["updated"]["MSFT"] == "" and out["updated"]["NVDA"] == "" and ">failed<" in out["updated"]["ZZZZ"]
    assert "Failed at: Research the stock" in out["updated"]["ZZZZ"] and all(out["lost"].values())
    assert out["dismissed"] == "" and out["failed"] == [True, False, False]
    # the Run panel keeps the summary with a dismiss button; a batch with failures is restored until dismissed
    assert 'data-act="dismiss-batch"' in page and 'store("stocks-batch", null)' in page
    assert "Date.now() - B.started < 2 * 3600e3 || batchFailed(B)" in page


HARNESS = r"""
const world = JSON.parse(process.argv[3]);
process.env.GITHUB_TOKEN = "ghp_TOKEN_SECRET"; process.env.RUN_PASSCODE = "pass-SECRET"; process.env.GITHUB_REPO = "o/r";
if (world.key !== null) process.env.STOCK_PRICE_API_KEY = world.key || "tiingo-KEY-SECRET";
const calls = [];
const now = Date.now();
const resp = (status, body) => ({ status, ok: status < 300, text: async () => (body == null ? "" : typeof body === "string" ? body : JSON.stringify(body)),
  json: async () => body });
let variable = world.variable === undefined ? null : world.variable;
global.fetch = async (url, opts = {}) => {
  const h = opts.headers || {};
  calls.push({ url, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null, auth: h.Authorization });
  if (url === "https://localhost/data/stocks/index.json") return world.hostIndex ? resp(200, world.hostIndex) : resp(404, null);
  if (url.startsWith("https://api.tiingo.com/iex")) {
    const form = url.includes("?tickers=") ? "tickers" : "all";
    const mode = (world.iex || {})[form] || "ok";
    if (mode !== "ok") return resp(Number(mode), { detail: "Error for token tiingo-KEY-SECRET" });
    return resp(200, (world.iexRows || []));
  }
  if (url.includes("/dispatches")) return world.dispatch === "fail" ? resp(403, { message: "Resource not accessible by ghp_TOKEN_SECRET" })
    : resp(200, { html_url: "https://github.com/o/r/actions/runs/9", id: 9 });
  if (url.includes("/actions/variables")) {
    if (opts.method === "PATCH") { if (variable === null) return resp(404, { message: "Not Found" }); variable = JSON.parse(opts.body).value; return resp(204, null); }
    if (opts.method === "POST") { variable = JSON.parse(opts.body).value; return resp(201, null); }
    if (world.variableStatus) return resp(world.variableStatus, { message: "Server Error" });
    return variable === null ? resp(404, { message: "Not Found" }) : resp(200, { name: "STOCK_LISTS", value: variable });
  }
  if (/\/actions\/runs\/\d+\/jobs/.test(url)) return resp(200, { jobs: world.jobs || [] });
  if (/\/actions\/runs\/\d+$/.test(url)) return resp(200, Object.assign({ id: 77, path: ".github/workflows/stocks.yml", html_url: "https://github.com/o/r/actions/runs/77" }, world.run || {}));
  if (url.includes("/actions/workflows/stocks.yml/runs")) {
    const runs = (world.runs || []).map((r, i) => ({ id: 100 + i, created_at: new Date(now - (r.hours_ago ?? i + 1) * 3600e3).toISOString(),
      status: r.status || "completed", conclusion: r.conclusion || "success", display_title: r.title, html_url: "https://github.com/o/r/actions/runs/" + (100 + i) }));
    return resp(200, { workflow_runs: runs });
  }
  return resp(404, {});
};
const handler = require(process.argv[2]);
const call = (method, url, body) => new Promise(resolve => {
  const res = { statusCode: 0, headers: {}, setHeader(k, v) { this.headers[k] = v; },
                end(b) { resolve({ status: this.statusCode, body: JSON.parse(b), cache: this.headers["Cache-Control"] }); } };
  handler({ method, url, headers: { host: "localhost" }, body }, res);
});
(async () => {
  const out = {};
  for (const [name, method, url, body] of JSON.parse(process.argv[4])) out[name] = await call(method, url, body);
  console.log(JSON.stringify({ out, calls, variable }));
})();
"""


def run_function(tmp_path, cases, index=None, **world):
    """index: the rated tickers bundled with the function (data/stocks/index.json under its working dir)."""
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    bundled = tmp_path / "data" / "stocks" / "index.json"
    if index is not None:
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_text(json.dumps({"tickers": [{"ticker": t} for t in index]}))
    elif bundled.exists():
        bundled.unlink()
    p = subprocess.run([NODE, str(h), str(ROOT / "site" / "api" / "stock.js"), json.dumps(world), json.dumps(cases)],
                       capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert "SECRET" not in json.dumps(out["out"]), "a secret leaked into a response"
    return out


node = pytest.mark.skipif(not NODE, reason="node not installed")
GOOD = {"passcode": "pass-SECRET", "research_model": "claude-sonnet-5-5", "scoring_model": "claude-opus-5-5", "send_email": True}


@node
def test_function_validates_and_dispatches_a_single_ticker_and_a_batch(tmp_path):
    r = run_function(tmp_path, [
        ["wrong", "POST", "/api/stock", {"passcode": "nope", "ticker": "MSFT"}],
        ["bad_ticker", "POST", "/api/stock", {"passcode": "pass-SECRET", "ticker": "MSFT; rm -rf /"}],
        ["bad_in_batch", "POST", "/api/stock", {"passcode": "pass-SECRET", "tickers": ["MSFT", "1BAD"]}],
        ["too_many", "POST", "/api/stock", {"passcode": "pass-SECRET", "tickers": [f"T{i}" for i in range(16)]}],
        ["bad_model", "POST", "/api/stock", {"passcode": "pass-SECRET", "ticker": "MSFT", "scoring_model": "gpt-5"}],
        ["unknown_model", "POST", "/api/stock", {"passcode": "pass-SECRET", "ticker": "MSFT", "scoring_model": "claude-nope-1"}],
        ["single", "POST", "/api/stock", {**GOOD, "ticker": " brk.b "}],
        ["batch", "POST", "/api/stock", {**GOOD, "tickers": ["msft", "NVDA", "MSFT", "tic"], "send_email": False}],
        ["unknown_action", "POST", "/api/stock", {"passcode": "pass-SECRET", "action": "nuke"}],
        ["put", "PUT", "/api/stock", None]], runs=[{"title": "Stock research MSFT"}])
    out, calls = r["out"], r["calls"]
    assert out["wrong"] == {"status": 401, "body": {"error": "Wrong passcode"}, "cache": "no-store"}
    assert out["bad_ticker"]["status"] == 400 and "Invalid ticker" in out["bad_ticker"]["body"]["error"]
    assert out["bad_in_batch"]["status"] == 400 and '"1BAD"' in out["bad_in_batch"]["body"]["error"]
    assert out["too_many"]["status"] == 400 and "at most 15" in out["too_many"]["body"]["error"]
    assert out["bad_model"]["body"] == {"error": "Invalid scoring_model"}
    assert out["unknown_model"]["body"]["error"] == "Unknown scoring_model: not in data/models.json"
    assert out["single"]["status"] == 200 and out["single"]["body"]["tickers"] == ["BRK.B"] and out["single"]["body"]["run_id"] == 9
    assert out["batch"]["body"]["tickers"] == ["MSFT", "NVDA", "TIC"] and out["batch"]["body"]["used_24h"] == 4
    dispatches = [c for c in calls if c["url"].endswith("/dispatches")]
    assert [d["body"] for d in dispatches] == [
        {"ref": "main", "inputs": {"tickers": "BRK.B", "send_email": "true", "research_model": "claude-sonnet-5-5", "scoring_model": "claude-opus-5-5"}},
        {"ref": "main", "inputs": {"tickers": "MSFT,NVDA,TIC", "send_email": "false", "research_model": "claude-sonnet-5-5", "scoring_model": "claude-opus-5-5"}}]
    assert dispatches[0]["url"] == "https://api.github.com/repos/o/r/actions/workflows/stocks.yml/dispatches"
    assert all(c["auth"] == "Bearer ghp_TOKEN_SECRET" for c in calls)
    assert out["unknown_action"]["status"] == 400 and out["put"]["status"] == 405


@node
def test_function_caps_tickers_per_day_and_refuses_a_second_waiting_batch(tmp_path):
    runs = [{"title": "Stock research MSFT,NVDA,TIC"}, {"title": "Stock research AAPL"}, {"title": "Stock research " + ",".join(f"A{i}" for i in range(24))},
            {"title": "Stock research OLD,ER", "hours_ago": 30}]  # older than 24 h: not counted
    r = run_function(tmp_path, [["over", "POST", "/api/stock", {"passcode": "pass-SECRET", "tickers": ["X", "Y", "Z"]}],
                                ["fits", "POST", "/api/stock", {"passcode": "pass-SECRET", "tickers": ["X", "Y"]}]], runs=runs)
    assert r["out"]["over"]["status"] == 429
    assert r["out"]["over"]["body"]["error"] == "Limit: 28 of 30 tickers used in the last 24 hours; this batch has 3"
    assert r["out"]["fits"]["status"] == 200 and r["out"]["fits"]["body"]["used_24h"] == 30
    assert sum(c["url"].endswith("/dispatches") for c in r["calls"]) == 1
    r = run_function(tmp_path, [["queued", "POST", "/api/stock", {"passcode": "pass-SECRET", "ticker": "MSFT"}]],
                     runs=[{"title": "Stock research A", "status": "in_progress"}, {"title": "Stock research B", "status": "queued"}])
    assert r["out"]["queued"]["status"] == 409 and "already waiting" in r["out"]["queued"]["body"]["error"]
    r = run_function(tmp_path, [["fail", "POST", "/api/stock", {"passcode": "pass-SECRET", "ticker": "MSFT"}]], dispatch="fail")
    err = r["out"]["fail"]["body"]["error"]
    assert r["out"]["fail"]["status"] == 502 and "GitHub returned 403" in err and "***" in err


@node
def test_function_reports_each_tickers_job(tmp_path):
    jobs = [{"name": "plan", "status": "completed", "conclusion": "success"},
            {"name": "rate MSFT", "status": "completed", "conclusion": "success"},
            {"name": "rate NVDA", "status": "in_progress", "conclusion": None},
            {"name": "rate ZZZZ", "status": "completed", "conclusion": "failure",
             "steps": [{"name": "Install dependencies", "conclusion": "success"}, {"name": "Research the stock", "conclusion": "failure"}]},
            {"name": "collect", "status": "queued", "conclusion": None}]
    r = run_function(tmp_path, [["run", "GET", "/api/stock?run=77", None], ["bad", "GET", "/api/stock?run=x1", None],
                                ["latest", "GET", "/api/stock", None]],
                     run={"display_title": "Stock research MSFT,NVDA,ZZZZ,TIC", "status": "in_progress", "conclusion": None},
                     jobs=jobs, runs=[{"title": "Stock research MSFT,NVDA,ZZZZ,TIC", "status": "in_progress"}])
    body = r["out"]["run"]["body"]
    assert body["run_id"] == 77 and body["status"] == "in_progress" and body["plan"] == "success" and body["collect"] == "queued"
    assert body["tickers"] == [{"ticker": "MSFT", "status": "done"}, {"ticker": "NVDA", "status": "running"},
                               {"ticker": "ZZZZ", "status": "failed", "reason": "failure", "step": "Research the stock"},
                               {"ticker": "TIC", "status": "queued"}]
    assert r["out"]["run"]["cache"] == "public, s-maxage=5" and r["out"]["bad"]["status"] == 400
    assert r["out"]["latest"]["body"]["tickers"][0] == {"ticker": "MSFT", "status": "done"}


def lists_doc(*lists, revision=3):
    return json.dumps({"version": 1, "revision": revision, "lists": [{"id": i, "name": n, "tickers": t} for i, n, t in lists]})


@node
def test_function_reads_and_saves_lists_without_touching_the_run_cap(tmp_path):
    good = [{"id": "ai", "name": " AI names ", "tickers": ["nvda", "MSFT"]}, {"id": "w2", "name": "Watch", "tickers": ["MSFT", "brk.b"]}]
    r = run_function(tmp_path, [
        ["empty", "GET", "/api/stock?lists", None],
        ["no_pass", "POST", "/api/stock", {"passcode": "x", "action": "save_lists", "lists": good}],
        ["save", "POST", "/api/stock", {"passcode": "pass-SECRET", "action": "save_lists", "lists": good, "base_revision": 0}],
        ["after", "GET", "/api/stock?lists", None]])
    out = r["out"]
    assert out["empty"]["body"] == {"version": 1, "revision": 0, "lists": []} and out["empty"]["cache"] == "no-store"
    assert out["no_pass"]["status"] == 401
    saved = out["save"]["body"]
    assert saved["ok"] and saved["revision"] == 1 and saved["version"] == 1
    assert saved["lists"] == [{"id": "ai", "name": "AI names", "tickers": ["NVDA", "MSFT"]}, {"id": "w2", "name": "Watch", "tickers": ["MSFT", "BRK.B"]}]
    assert out["after"]["body"]["lists"] == saved["lists"] and out["after"]["body"]["revision"] == 1
    methods = [(c["method"], c["url"].split("/repos/o/r")[1]) for c in r["calls"]]
    assert ("PATCH", "/actions/variables/STOCK_LISTS") in methods and ("POST", "/actions/variables") in methods  # created on 404
    assert not any("dispatches" in u or "/runs" in u for _, u in methods)  # no dispatch, not counted in the cap
    stored = json.loads(r["variable"])
    assert stored["lists"] == saved["lists"] and "," in r["variable"] and ": " not in r["variable"]  # compact JSON


@node
def test_function_validates_lists_and_guards_the_variable_size(tmp_path):
    bad = lambda lists: {"passcode": "pass-SECRET", "action": "save_lists", "lists": lists}  # noqa: E731
    big = [{"id": f"l{i}", "name": f"List number {i:02d} with a long name", "tickers": [f"T{i:02d}{j:07d}" for j in range(100)]}
           for i in range(30)]
    r = run_function(tmp_path, [
        ["not_array", "POST", "/api/stock", bad({"a": 1})],
        ["bad_ticker", "POST", "/api/stock", bad([{"id": "a", "name": "A", "tickers": ["MSFT", "BAD TICKER"]}])],
        ["dupe", "POST", "/api/stock", bad([{"id": "a", "name": "A", "tickers": ["MSFT", "msft"]}])],
        ["dupe_id", "POST", "/api/stock", bad([{"id": "a", "name": "A", "tickers": []}, {"id": "a", "name": "B", "tickers": []}])],
        ["bad_id", "POST", "/api/stock", bad([{"id": "A B", "name": "A", "tickers": []}])],
        ["no_name", "POST", "/api/stock", bad([{"id": "a", "name": "  ", "tickers": []}])],
        ["31_lists", "POST", "/api/stock", bad([{"id": f"l{i}", "name": "x", "tickers": []} for i in range(31)])],
        ["101", "POST", "/api/stock", bad([{"id": "a", "name": "A", "tickers": [f"T{i}" for i in range(101)]}])],
        ["too_big", "POST", "/api/stock", bad(big)]])
    out = r["out"]
    assert out["not_array"]["body"]["error"] == "lists must be an array"
    assert out["bad_ticker"]["body"]["error"] == 'List "A": invalid ticker "BAD TICKER"'
    assert out["dupe"]["body"]["error"] == 'List "A" has MSFT twice'
    assert out["dupe_id"]["body"]["error"] == "Two lists share the id a" and "invalid id" in out["bad_id"]["body"]["error"]
    assert "1-40 characters" in out["no_name"]["body"]["error"]
    assert out["31_lists"]["body"]["error"] == "Too many lists: 31 (max 30)"
    assert out["101"]["body"]["error"] == 'List "A" has 101 tickers (max 100)'
    assert out["too_big"]["status"] == 413 and "max 40000" in out["too_big"]["body"]["error"]
    assert not any(c["method"] in ("PATCH", "POST") for c in r["calls"])  # nothing saved


@node
def test_function_refuses_a_stale_lists_save_unless_forced(tmp_path):
    lists = [{"id": "a", "name": "A", "tickers": ["MSFT"]}]
    r = run_function(tmp_path, [
        ["stale", "POST", "/api/stock", {"passcode": "pass-SECRET", "action": "save_lists", "lists": lists, "base_revision": 2}],
        ["forced", "POST", "/api/stock", {"passcode": "pass-SECRET", "action": "save_lists", "lists": lists, "base_revision": 2, "force": True}],
        ["broken", "GET", "/api/stock?lists", None]], variable=lists_doc(("a", "A", ["NVDA"]), revision=3))
    assert r["out"]["stale"]["status"] == 409 and "another tab or device" in r["out"]["stale"]["body"]["error"]
    assert r["out"]["forced"]["body"]["revision"] == 4 and json.loads(r["variable"])["lists"] == lists
    r = run_function(tmp_path, [["broken", "GET", "/api/stock?lists", None]], variable="not json")
    assert r["out"]["broken"]["body"]["lists"] == [] and "saving replaces it" in r["out"]["broken"]["body"]["error"]


IEX_ROWS = [{"ticker": "TIC", "tngoLast": 8.68, "last": None, "prevClose": 9.12, "timestamp": "2026-10-14T20:00:00+00:00"},
            {"ticker": "BRK-B", "tngoLast": 480.1, "last": 481, "prevClose": 479, "timestamp": "2026-10-14T19:59:00.123+00:00"},
            {"ticker": "NODATA", "tngoLast": None, "timestamp": "2026-10-14T20:00:00+00:00"},
            {"ticker": "AAPL", "tngoLast": 230.0, "timestamp": "2026-10-14T20:00:00+00:00"}]


def tiingo_urls(r):
    return [c["url"] for c in r["calls"] if "tiingo" in c["url"]]


@node
def test_function_quotes_every_rated_and_listed_ticker_at_one_fixed_url(tmp_path):
    lists = lists_doc(("a", "A", ["NODATA", "TIC"]), ("b", "B", ["BRK.B"]))
    r = run_function(tmp_path, [["ok", "GET", "/api/stock?quotes=all", None]], index=["TIC", "MSFT"], variable=lists, iexRows=IEX_ROWS)
    ok = r["out"]["ok"]
    assert ok["status"] == 200 and ok["cache"] == "public, s-maxage=300"
    assert ok["body"] == {"TIC": {"price": 8.68, "as_of": "2026-10-14T20:00:00Z"}, "BRK.B": {"price": 480.1, "as_of": "2026-10-14T19:59:00Z"}}
    # one request for the rated and listed tickers, sorted; AAPL (in Tiingo's reply, but neither rated nor listed) is dropped
    assert tiingo_urls(r) == ["https://api.tiingo.com/iex/?tickers=brk-b,msft,nodata,tic"]
    tiingo = next(c for c in r["calls"] if "tiingo" in c["url"])
    assert tiingo["auth"] == "Token tiingo-KEY-SECRET" and "SECRET" not in tiingo["url"]
    # without a bundled index.json the function reads the deployed one from its own host
    r = run_function(tmp_path, [["host", "GET", "/api/stock?quotes=all", None]], hostIndex={"tickers": [{"ticker": "TIC"}, {"ticker": "bad ticker"}]})
    assert tiingo_urls(r) == ["https://api.tiingo.com/iex/?tickers=tic"]  # invalid entries never reach Tiingo
    # STOCK_LISTS unreadable: the rated tickers are still quoted; neither source: refused, cached briefly
    r = run_function(tmp_path, [["rated", "GET", "/api/stock?quotes=all", None]], index=["TIC"], variableStatus=500, iexRows=IEX_ROWS)
    assert r["out"]["rated"]["body"] == {"TIC": {"price": 8.68, "as_of": "2026-10-14T20:00:00Z"}}
    r = run_function(tmp_path, [["none", "GET", "/api/stock?quotes=all", None]], variableStatus=500)
    assert r["out"]["none"]["status"] == 503 and r["out"]["none"]["cache"] == "public, s-maxage=60" and not tiingo_urls(r)
    r = run_function(tmp_path, [["empty", "GET", "/api/stock?quotes=all", None]], index=[])
    assert r["out"]["empty"] == {"status": 200, "body": {}, "cache": "public, s-maxage=300"} and not tiingo_urls(r)


@node
def test_function_refuses_any_other_quote_url_before_calling_anyone(tmp_path):
    bad = ["/api/stock?quotes=MSFT", "/api/stock?quotes=MSFT,NVDA", "/api/stock?quotes=", "/api/stock?quotes=ALL",
           "/api/stock?quotes=all&x=1", "/api/stock?quotes=all&quotes=all", "/api/stock?v=1&quotes=all", "/api/stock?quotes"]
    r = run_function(tmp_path, [[u, "GET", u, None] for u in bad], index=["TIC"], iexRows=IEX_ROWS)
    for u in bad:
        assert r["out"][u]["status"] == 400 and r["out"][u]["body"] == {"error": "Use ?quotes=all"}, u
    assert not r["calls"]  # neither Tiingo nor GitHub
    r = run_function(tmp_path, [["nokey", "GET", "/api/stock?quotes=all", None]], index=["TIC"], key=None)
    assert r["out"]["nokey"]["status"] == 503 and "STOCK_PRICE_API_KEY" in r["out"]["nokey"]["body"]["error"] and not r["calls"]


@node
def test_function_splits_past_100_tickers_and_falls_back_to_the_all_tickers_endpoint(tmp_path):
    many = [f"T{i:03d}" for i in range(150)]
    r = run_function(tmp_path, [["two", "GET", "/api/stock?quotes=all", None]], index=many)
    urls = tiingo_urls(r)
    assert len(urls) == 2 and urls[0].count(",") == 99 and urls[1].count(",") == 49
    r = run_function(tmp_path, [["all", "GET", "/api/stock?quotes=all", None]], index=[f"T{i:03d}" for i in range(201)], iexRows=IEX_ROWS)
    assert tiingo_urls(r) == ["https://api.tiingo.com/iex"]  # past two requests: the all-tickers endpoint once
    # the ?tickers= form failing falls back to the all-tickers endpoint once; a rejected key or the rate limit does not
    r = run_function(tmp_path, [["fallback", "GET", "/api/stock?quotes=all", None]], index=["TIC"], iexRows=IEX_ROWS, iex={"tickers": "404"})
    assert r["out"]["fallback"]["body"] == {"TIC": {"price": 8.68, "as_of": "2026-10-14T20:00:00Z"}}
    assert tiingo_urls(r) == ["https://api.tiingo.com/iex/?tickers=tic", "https://api.tiingo.com/iex"]
    for code, status, msg in (("401", 502, "rejected the key"), ("429", 503, "rate limit")):
        r = run_function(tmp_path, [["e", "GET", "/api/stock?quotes=all", None]], index=["TIC"], iex={"tickers": code})
        assert r["out"]["e"]["status"] == status and msg in r["out"]["e"]["body"]["error"] and len(tiingo_urls(r)) == 1
        assert r["out"]["e"]["cache"] == "public, s-maxage=60"
