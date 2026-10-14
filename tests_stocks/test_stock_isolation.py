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


def test_stock_workflow_is_dispatch_only_and_passes_inputs_through_env():
    wf, on = workflow("stocks.yml")
    assert list(on) == ["workflow_dispatch"] and "schedule" not in on
    assert set(on["workflow_dispatch"]["inputs"]) == {"ticker", "research_model", "scoring_model", "send_email"}
    assert on["workflow_dispatch"]["inputs"]["send_email"]["default"] is False
    steps = wf["jobs"]["research"]["steps"]
    for s in steps:
        assert "${{" not in s.get("run", ""), f"expression inside run: of {s.get('name')}"
    assert next(s for s in steps if s.get("name") == "Unit tests (stock module)")["run"] == "python -m pytest -q tests_stocks"
    env = next(s for s in steps if s.get("name") == "Research the stock")["env"]
    assert env["INPUT_TICKER"] == "${{ inputs.ticker }}" and env["STOCK_PRICE_API_KEY"] == "${{ secrets.STOCK_PRICE_API_KEY }}"
    assert env["INPUT_SCORING_MODEL"] == "${{ inputs.scoring_model }}" and env["INPUT_SEND_EMAIL"] == "${{ inputs.send_email }}"
    save = next(s for s in steps if s.get("name") == "Save results")
    assert "data/stocks site/data/stocks" in save["run"] and "tr -cd" in save["run"]
    assert wf["concurrency"]["group"] != "weekly-run"


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


HARNESS = r"""
process.env.GITHUB_TOKEN = "ghp_TOKEN_SECRET"; process.env.RUN_PASSCODE = "pass-SECRET"; process.env.GITHUB_REPO = "o/r";
const calls = [];
const recent = Number(process.argv[3]);
const now = Date.now();
const resp = (status, body) => ({ status, ok: status < 300, text: async () => (body == null ? "" : JSON.stringify(body)),
  json: async () => body });
global.fetch = async (url, opts = {}) => {
  calls.push({ url, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null,
               auth: (opts.headers || {}).Authorization });
  if (url.includes("/dispatches")) return process.argv[4] === "fail" ? resp(403, { message: "Resource not accessible by ghp_TOKEN_SECRET" })
    : resp(200, { html_url: "https://github.com/o/r/actions/runs/9" });
  if (url.includes("/actions/workflows/stocks.yml/runs")) {
    const runs = Array.from({ length: recent }, (_, i) => ({ created_at: new Date(now - (i + 1) * 3600e3).toISOString(),
      status: "in_progress", conclusion: null, display_title: "Stock research MSFT", html_url: "https://github.com/o/r/actions/runs/" + i }));
    return resp(200, { workflow_runs: runs });
  }
  return resp(404, {});
};
const handler = require(process.argv[2]);
const call = (method, body) => new Promise(resolve => {
  const res = { statusCode: 0, headers: {}, setHeader(k, v) { this.headers[k] = v; },
                end(b) { resolve({ status: this.statusCode, body: JSON.parse(b) }); } };
  handler({ method, url: "/api/stock", headers: { host: "localhost" }, body }, res);
});
(async () => {
  const out = {};
  for (const [name, method, body] of JSON.parse(process.argv[5])) out[name] = await call(method, body);
  console.log(JSON.stringify({ out, calls }));
})();
"""


def run_function(tmp_path, cases, recent=0, mode="ok"):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    p = subprocess.run([NODE, str(h), str(ROOT / "site" / "api" / "stock.js"), str(recent), mode, json.dumps(cases)],
                       capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_function_validates_caps_and_dispatches(tmp_path):
    good = {"passcode": "pass-SECRET", "ticker": " brk.b ", "research_model": "claude-sonnet-5-5",
            "scoring_model": "claude-opus-5-5", "send_email": True}
    r = run_function(tmp_path, [
        ["wrong", "POST", {"passcode": "nope", "ticker": "MSFT"}],
        ["bad_ticker", "POST", {"passcode": "pass-SECRET", "ticker": "MSFT; rm -rf /"}],
        ["bad_model", "POST", {"passcode": "pass-SECRET", "ticker": "MSFT", "scoring_model": "gpt-5"}],
        ["unknown_model", "POST", {"passcode": "pass-SECRET", "ticker": "MSFT", "scoring_model": "claude-nope-1"}],
        ["ok", "POST", good],
        ["status", "GET", None],
        ["put", "PUT", None]], recent=9)
    out, calls = r["out"], r["calls"]
    assert out["wrong"] == {"status": 401, "body": {"error": "Wrong passcode"}}
    assert out["bad_ticker"]["status"] == 400 and "Invalid ticker" in out["bad_ticker"]["body"]["error"]
    assert out["bad_model"] == {"status": 400, "body": {"error": "Invalid scoring_model"}}
    assert out["unknown_model"]["body"]["error"] == "Unknown scoring_model: not in data/models.json"
    assert out["ok"] == {"status": 200, "body": {"ok": True, "ticker": "BRK.B", "run_url": "https://github.com/o/r/actions/runs/9", "run_found": True}}
    dispatch = next(c for c in calls if c["url"].endswith("/dispatches"))
    assert dispatch["url"] == "https://api.github.com/repos/o/r/actions/workflows/stocks.yml/dispatches"
    assert dispatch["body"] == {"ref": "main", "inputs": {"ticker": "BRK.B", "send_email": "true",
                                                          "research_model": "claude-sonnet-5-5", "scoring_model": "claude-opus-5-5"}}
    assert all("workflows/stocks.yml" in c["url"] for c in calls) and all(c["auth"] == "Bearer ghp_TOKEN_SECRET" for c in calls)
    assert out["status"]["body"]["status"] == "in_progress" and out["status"]["body"]["title"] == "Stock research MSFT"
    assert out["put"]["status"] == 405
    assert "SECRET" not in json.dumps(out)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_function_caps_at_ten_runs_a_day_and_never_echoes_secrets(tmp_path):
    r = run_function(tmp_path, [["capped", "POST", {"passcode": "pass-SECRET", "ticker": "MSFT"}]], recent=10)
    assert r["out"]["capped"]["status"] == 429 and "10 stock runs in the last 24 hours" in r["out"]["capped"]["body"]["error"]
    assert not any(c["url"].endswith("/dispatches") for c in r["calls"])
    r = run_function(tmp_path, [["fail", "POST", {"passcode": "pass-SECRET", "ticker": "MSFT"}]], recent=0, mode="fail")
    err = r["out"]["fail"]["body"]["error"]
    assert r["out"]["fail"]["status"] == 502 and "GitHub returned 403" in err and "ghp_TOKEN_SECRET" not in err and "***" in err
