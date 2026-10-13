"""v2: every run kept, event-day runs, EDGAR filing text, source checks, confirmation. HTTP is mocked."""
import json
import re
import shutil
import subprocess
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import pytest
import requests
import yaml

from cew import (analyst, calls, edgar_text, events, fetch_edgar, fetch_fred, fetch_taiwan, leases, models,
                 notify, publish, run, scoring, sources, util)

CFG = util.load_config()
ROOT = util.ROOT


# ---------------------------------------------------------------- fakes
class FakeResponse:
    def __init__(self, status=200, body="", headers=None):
        self.status_code = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.encoding = "utf-8"

    @property
    def text(self):
        return self.body.decode()

    def json(self):
        return json.loads(self.body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        pass


class FakeSession:
    """requests.Session stand-in: url -> FakeResponse, or an exception to raise."""

    def __init__(self, routes):
        self.routes, self.calls = dict(routes), []

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append(url)
        r = self.routes.get(url, FakeResponse(404, "not found"))
        if isinstance(r, Exception):
            raise r
        return r


def page(html):
    return FakeResponse(200, f"<html><body>{html}</body></html>")


def as_json(obj):
    return FakeResponse(200, json.dumps(obj), {"Content-Type": "application/json"})


def _usage(inp=1000, out=200):
    return NS(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0,
              server_tool_use=None)


def reply(obj, inp=1000, out=200):
    return NS(stop_reason="end_turn", usage=_usage(inp, out), content=[NS(type="text", text=json.dumps(obj), citations=[])])


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def create(self, **kw):
        self.calls.append(kw)
        if not self.responses:
            raise AssertionError("unexpected Claude call")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(util.time, "sleep", lambda s: None)


def fake_claude(monkeypatch, responses):
    fake = FakeMessages(responses)
    monkeypatch.setattr(analyst, "_client", lambda: NS(messages=fake))
    return fake


def public_dns(host, port):
    ip = "10.0.0.5" if host == "intranet.example" else "93.184.216.34"
    return [(2, 1, 6, "", (ip, 0))]


# ---------------------------------------------------------------- 1. every run kept
def _report(run_id="2026-10-13T18:45:13Z", trigger="manual", scores=None, level="GREEN", total=0.0):
    scores = scores or {k: 0 for k, _ in scoring.CATEGORIES}
    return {"date": run_id[:10], "run_id": run_id, "trigger": trigger, "composite": {"total": total, "level": level},
            "scores": {k: {"score": v} for k, v in scores.items()},
            "claude": {"enabled": True, "models": {"research": "claude-sonnet-5-5", "assessment": "claude-opus-5-5"}},
            "cost": {"total_usd": 0.5, "steps": {
                "research": dict(models.new_usage("claude-sonnet-5-5"), input_tokens=100, output_tokens=10, web_searches=3),
                "assessment": dict(models.new_usage("claude-opus-5-5"), input_tokens=50, output_tokens=5)}}}


def test_archive_never_overwrites_and_appends(sandbox):
    a, b = _report(trigger="schedule"), _report(trigger="event:CPI", total=4.0)  # same second on purpose
    assert publish.archive_run(a) == "2026-10-13T184513Z.json"
    assert publish.archive_run(b) == "2026-10-13T184513Z-2.json"
    first = (sandbox / "data" / "runs" / "2026-10-13T184513Z.json").read_text()
    assert json.loads(first)["trigger"] == "schedule"
    lines = (sandbox / "data" / "runs.csv").read_text().splitlines()
    assert lines[0] == ",".join(publish.RUN_FIELDS) and len(lines) == 3
    rows = util.read_csv(sandbox / "data" / "runs.csv")
    assert [r["trigger"] for r in rows] == ["schedule", "event:CPI"]
    assert rows[0]["tokens"] == "165" and rows[0]["searches"] == "3" and rows[0]["cost_usd"] == "0.5"
    assert rows[1]["composite"] == "4.0" and rows[1]["research_model"] == "claude-sonnet-5-5"
    # a file without a trailing newline still gets a clean new row; earlier rows untouched
    path = sandbox / "data" / "runs.csv"
    path.write_text(path.read_text().rstrip("\n"))
    publish.archive_run(_report(run_id="2026-10-14T23:30:05Z", trigger="event:CPI"))
    rows = util.read_csv(path)
    assert [r["timestamp"] for r in rows] == ["2026-10-13T18:45:13Z"] * 2 + ["2026-10-14T23:30:05Z"]
    assert (sandbox / "data" / "runs" / "2026-10-13T184513Z.json").read_text() == first


def test_runs_site_data_last_20_and_month_spend(sandbox):
    for i in range(23):
        r = _report(run_id=f"2026-{9 + (i >= 3):02d}-{1 + i:02d}T01:17:00Z", trigger="schedule")
        r["cost"]["total_usd"] = 0.25
        publish.archive_run(r)
    evts = [{"date": "2026-10-28", "name": "FOMC", "type": "fomc", "verified": True},
            {"date": "2026-09-01", "name": "CPI", "type": "cpi", "verified": True}]
    d = publish.runs_site_data(date(2026, 10, 20), evts)
    assert d["total_runs"] == 23 and len(d["runs"]) == 20
    assert d["runs"][0]["timestamp"] == "2026-10-23T01:17:00Z"  # newest first
    assert d["spend_by_month"] == {"2026-09": {"cost_usd": 0.75, "runs": 3}, "2026-10": {"cost_usd": 5.0, "runs": 20}}
    assert [e["name"] for e in d["upcoming_events"]] == ["FOMC"]


def test_offline_runs_kept_and_daily_log_single_row(sandbox, monkeypatch):
    monkeypatch.setenv("RUN_TRIGGER", "schedule")
    assert run.main(["--offline", "--no-llm", "--no-email"]) == 0
    monkeypatch.setenv("RUN_TRIGGER", "event:CPI")
    assert run.main(["--offline", "--no-llm", "--no-email"]) == 0
    files = sorted((sandbox / "data" / "runs").glob("*.json"))
    assert len(files) == 2
    rows = util.read_csv(sandbox / "data" / "runs.csv")
    assert [r["trigger"] for r in rows] == ["schedule", "event:CPI"]
    assert len(util.read_csv(sandbox / "data" / "log.csv")) == 1  # still one row per day
    reports = [json.loads(f.read_text()) for f in files]
    ev = next(r for r in reports if r["trigger"] == "event:CPI")
    assert ev["events"] and ev["events"][0]["name"] == "CPI" and ev["source_check"] == {"skipped": "offline run"}
    assert "confirmation" in ev["composite"] and "jumps" in ev["composite"] and ev["run_id"].endswith("Z")
    site_runs = json.loads((sandbox / "site" / "data" / "runs.json").read_text())
    assert len(site_runs["runs"]) == 2 and site_runs["runs"][0]["trigger"] in ("event:CPI", "schedule")
    latest = json.loads((sandbox / "site" / "data" / "latest.json").read_text())
    assert latest["trigger"] == "event:CPI" and latest["email"] == "skipped (--no-email)"


# ---------------------------------------------------------------- 2. events
OFFICIAL = {"fomc": ("federalreserve.gov",), "cpi": ("bls.gov",), "MSFT": ("microsoft.com",),
            "GOOGL": ("abc.xyz",), "META": ("atmeta.com",), "AMZN": ("aboutamazon.com",),
            "ORCL": ("oracle.com",), "NVDA": ("nvidia.com",), "AMD": ("amd.com",),
            "AVGO": ("broadcom.com",), "TSMC": ("tsmc.com",)}


def test_events_yaml_is_complete_and_valid():
    evts = events.load()
    assert evts == sorted(evts, key=lambda e: (e["date"], e["name"]))
    for e in evts:
        assert "2026-10-01" <= e["date"] <= "2026-12-31", e
        assert e["type"] in events.TYPES and events.NAME_RE.fullmatch(e["name"])
        assert isinstance(e["verified"], bool) and e["source"].startswith("https://") and e.get("title")
        if e["type"] == "earnings":
            assert e["name"] == f"{e['ticker']}-earnings"
        else:
            assert e["name"] == e["type"].upper()
        if e["verified"]:  # verified dates cite the official source
            key = e["type"] if e["type"] != "earnings" else e["ticker"]
            assert any(d in e["source"] for d in OFFICIAL[key]), e
        else:
            assert e.get("note"), "unverified dates explain where the date came from"
    tickers = {e["ticker"] for e in evts if e["type"] == "earnings"}
    assert tickers == {"MSFT", "GOOGL", "META", "AMZN", "ORCL", "NVDA", "AMD", "AVGO", "TSMC"}
    assert {e["type"] for e in evts} == {"fomc", "cpi", "earnings"}
    assert len({(e["date"], e["name"]) for e in evts}) == len(evts)


def test_workflow_has_event_cron_and_no_inline_expressions():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "weekly.yml").read_text())
    on = wf[True] if True in wf else wf["on"]  # PyYAML reads the "on" key as a boolean
    crons = [s["cron"] for s in on["schedule"]]
    assert events.EVENT_CHECK_CRON in crons and "17 1 * * 6" in crons
    assert "event" in on["workflow_dispatch"]["inputs"]
    steps = wf["jobs"]["run"]["steps"]
    plan = next(s for s in steps if s.get("id") == "plan")
    assert plan["env"]["SCHEDULE"] == "${{ github.event.schedule }}" and plan["env"]["INPUT_EVENT"] == "${{ inputs.event }}"
    for s in steps:
        assert "${{" not in s.get("run", ""), f"expression inside run: of {s.get('name')}"
    pipeline = next(s for s in steps if s.get("name") == "Run pipeline")
    assert pipeline["env"]["RUN_TRIGGER"] == "${{ steps.plan.outputs.trigger }}"
    assert "steps.plan.outputs.run == 'true'" in pipeline["if"]


EVTS = [
    {"date": "2026-10-28", "name": "FOMC", "type": "fomc", "verified": True},
    {"date": "2026-10-28", "name": "MSFT-earnings", "type": "earnings", "ticker": "MSFT", "verified": True},
    {"date": "2026-10-29", "name": "AMZN-earnings", "type": "earnings", "ticker": "AMZN", "verified": False},
    {"date": "2027-01-28", "name": "MSFT-earnings", "type": "earnings", "ticker": "MSFT", "verified": False},
]
CHECK = {"EVENT_NAME": "schedule", "SCHEDULE": events.EVENT_CHECK_CRON}
AT_0730_UTC8 = datetime(2026, 10, 28, 23, 30, tzinfo=timezone.utc)


def test_plan_runs_after_an_event_day():
    p = events.plan(CHECK, AT_0730_UTC8, EVTS, [])
    assert p["run"] and p["trigger"] == "event:FOMC+MSFT-earnings"
    assert events.us_date(AT_0730_UTC8) == date(2026, 10, 28)  # 19:30 in New York
    assert events.us_date(datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc)) == date(2026, 11, 1)  # after DST


def test_plan_skips_quiet_days_and_days_already_run():
    quiet = events.plan(CHECK, datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc), EVTS, [])
    assert not quiet["run"] and "No listed event" in quiet["reason"]
    ran = [{"timestamp": "2026-10-28T14:02:00Z", "trigger": "manual"}]  # 10:02 New York, same day
    p = events.plan(CHECK, AT_0730_UTC8, EVTS, ran)
    assert not p["run"] and "already" in p["reason"]
    late_prev_day = [{"timestamp": "2026-10-28T02:00:00Z", "trigger": "schedule"}]  # 22:00 on Oct 27 in New York
    assert events.plan(CHECK, AT_0730_UTC8, EVTS, late_prev_day)["run"]


def test_plan_weekly_and_manual_triggers(capsys):
    assert events.plan({"EVENT_NAME": "schedule", "SCHEDULE": "17 1 * * 6"}, AT_0730_UTC8, EVTS, [])["trigger"] == "schedule"
    assert events.plan({"EVENT_NAME": "workflow_dispatch"}, AT_0730_UTC8, EVTS, [])["trigger"] == "manual"
    p = events.plan({"EVENT_NAME": "workflow_dispatch", "INPUT_EVENT": "MSFT-earnings, $(id);x+AMZN-earnings"},
                    AT_0730_UTC8, EVTS, [])
    assert p == {"run": True, "trigger": "event:MSFT-earnings+AMZN-earnings", "reason": "Manual run tagged as an event run"}
    assert "Ignoring unknown event name" in capsys.readouterr().out
    assert events.names_in("event:FOMC+MSFT-earnings") == ["FOMC", "MSFT-earnings"]
    assert events.names_in("schedule") == [] and events.names_in("event:bad name") == []


def test_for_trigger_uses_that_days_events_or_nearest():
    got = events.for_trigger("event:MSFT-earnings", EVTS, date(2026, 10, 28))
    assert got[0]["date"] == "2026-10-28"
    assert events.for_trigger("event:MSFT-earnings", EVTS, date(2027, 1, 20))[0]["date"] == "2027-01-28"
    assert events.for_trigger("manual", EVTS, date(2026, 10, 28)) == []


def test_plan_cli_writes_github_output(sandbox, monkeypatch, tmp_path):
    out = tmp_path / "gh_output"
    path = tmp_path / "events.yaml"
    path.write_text(yaml.safe_dump({"events": EVTS}))
    monkeypatch.setattr(events, "EVENTS_PATH", path)
    monkeypatch.setattr(util, "now_utc", lambda: AT_0730_UTC8)
    for k, v in {**CHECK, "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    assert events.main(["plan"]) == 0
    assert out.read_text() == "run=true\ntrigger=event:FOMC+MSFT-earnings\n"
    util.write_csv(sandbox / "data" / "runs.csv", [{"timestamp": "2026-10-28T20:00:00Z"}], ["timestamp"])
    assert events.main(["plan"]) == 0
    assert out.read_text().endswith("run=false\ntrigger=\n")


def test_horizon_warning():
    assert events.horizon_warning(EVTS, date(2026, 10, 1)) is None
    assert "after 2027-01-28" in events.horizon_warning(EVTS, date(2027, 1, 5))


def test_event_email_only_on_level_change_or_big_move():
    prev = {k: 1 for k, _ in scoring.CATEGORIES}
    same = {k: {"score": 1} for k, _ in scoring.CATEGORIES}
    comp = {"level": "AMBER"}
    assert run.email_decision(False, "event:CPI", comp, same, prev, "AMBER", CFG) == (
        "event run: no level change and no category moved 2+ points", None)
    assert run.email_decision(False, "event:CPI", {"level": "RED"}, same, prev, "AMBER", CFG) == (
        None, "level changed AMBER -> RED")
    moved = dict(same, physical_buildout={"score": 3})
    assert run.email_decision(False, "event:CPI", comp, moved, prev, "AMBER", CFG) == (None, "moved physical_buildout +2")
    down = dict(same, credit={"score": 3})
    assert run.email_decision(False, "event:CPI", comp, down, {**prev, "credit": 3}, "AMBER", CFG)[0]  # +0
    fell = dict(same, credit={"score": 0})
    assert run.email_decision(False, "event:CPI", comp, fell, {**prev, "credit": 2}, "AMBER", CFG) == (None, "moved credit -2")
    assert run.email_decision(True, "event:CPI", {"level": "RED"}, same, prev, "AMBER", CFG)[0] == "--no-email"
    assert run.email_decision(False, "schedule", comp, same, prev, "AMBER", CFG) == (None, None)  # weekly: always


# ---------------------------------------------------------------- 6. confirmation and jumps
def _scores(**kw):
    return {k: {"score": kw.get(k, 0)} for k, _ in scoring.CATEGORIES}


def test_red_confirmed_needs_same_categories_two_runs_running():
    now = _scores(credit=2, supplier_signals=3, physical_buildout=2)
    prev = {k: 0 for k, _ in scoring.CATEGORIES} | {"credit": 2, "supplier_signals": 2}
    c = scoring.confirmation(now, [prev], CFG)
    assert c["met"] and c["categories"] == ["credit", "supplier_signals"]
    other = {k: 0 for k, _ in scoring.CATEGORIES} | {"ai_lab_financing": 3, "physical_buildout": 2}
    assert scoring.confirmation(now, [other], CFG)["categories"] == ["physical_buildout"]  # only one sustained
    assert not scoring.confirmation(now, [], CFG)["met"]  # first run: nothing to confirm against
    three = dict(CFG, confirmation=dict(CFG["confirmation"], consecutive_runs=3))
    assert not scoring.confirmation(now, [prev], three)["met"]
    assert scoring.confirmation(now, [prev, prev], three)["met"]


def test_jumps_flag_rises_of_two_or_more():
    prev = {k: 1 for k, _ in scoring.CATEGORIES}
    now = _scores(credit=3, ai_lab_financing=2, taiwan_supply_chain=1, hyperscaler_filings=0, physical_buildout=3)
    assert scoring.jumps(now, prev, CFG) == {"credit": 2, "physical_buildout": 2}
    assert scoring.jumps(now, None, CFG) == {}
    assert scoring.run_scores({"credit": "2", "ai_lab_financing": "", "level": "RED"}) == {"credit": 2}
    assert scoring.run_scores({"credit": {"score": 1}}) == {"credit": 1}


def test_previous_runs_falls_back_to_latest_report(sandbox):
    util.write_json(sandbox / "data" / "reports" / "2026-10-13.json",
                    {"date": "2026-10-13", "generated_at": "2026-10-13T18:45:13+00:00", "composite": {"level": "AMBER"},
                     "scores": {k: {"score": 2} for k, _ in scoring.CATEGORIES}})
    rows = publish.previous_runs()
    assert len(rows) == 1 and rows[0]["level"] == "AMBER" and scoring.run_scores(rows[0])["credit"] == 2
    publish.archive_run(_report(trigger="schedule"))
    assert publish.previous_runs()[-1]["trigger"] == "schedule"


# ---------------------------------------------------------------- 5. source checks
def test_key_terms_and_claim_check():
    nums, names = sources.key_terms("OpenAI's revenue was near $50B, about $20B below the ~$70B reported (FT via Dealroom) on Oct 12.")
    assert nums == ["50", "20", "70", "12"] and names == ["OpenAI", "FT", "Dealroom"]
    good = "OpenAI annualized revenue is about $20 billion below the $70 billion reported, near $50 billion. Oct 12. Dealroom"
    assert sources.check_claim("OpenAI revenue near $50B, $20B below the $70B reported (FT via Dealroom), Oct 12", good)[0] == "verified"
    st, detail = sources.check_claim("OpenAI revenue near $45B (FT via Dealroom)", good)
    assert st == "unverified" and "45" in detail
    assert sources.check_claim("Revenue was 1,200 million", "revenue 1200 million")[0] == "verified"
    assert sources.check_claim("nothing checkable here", good)[0] == "unverified"


def test_fetcher_statuses_with_mocked_http():
    sess = FakeSession({
        "https://news.example/ok": page("<p>CoreWeave spreads widened 70 basis points</p><script>var x=1</script>"),
        "https://news.example/moved": FakeResponse(301, "", {"Location": "/ok"}),
        "https://news.example/gone": FakeResponse(404, "missing"),
        "https://news.example/slow": requests.Timeout("read timed out"),
        "https://news.example/pdf": FakeResponse(200, b"%PDF-1.7", {"Content-Type": "application/pdf"}),
        "https://news.example/to-intranet": FakeResponse(302, "", {"Location": "http://intranet.example/admin"}),
    })
    fetch = sources.make_fetcher(10, session=sess, resolver=public_dns)
    assert fetch("https://news.example/ok") == ("CoreWeave spreads widened 70 basis points", None)
    assert fetch("https://news.example/moved")[0].startswith("CoreWeave")
    assert fetch("https://news.example/gone") == (None, "HTTP 404")
    assert fetch("https://news.example/slow") == (None, "Timeout")
    assert fetch("https://news.example/pdf") == (None, "not a text page (application/pdf)")
    assert fetch("https://news.example/to-intranet") == (None, "host not public or not resolvable")
    assert fetch("http://127.0.0.1:8080/x")[1] == "host not public or not resolvable"
    assert fetch("file:///etc/passwd")[1].startswith("unsupported URL scheme")
    assert "http://intranet.example/admin" not in sess.calls


def test_check_evidence_and_caps():
    sess = FakeSession({
        "https://a.example/1": page("Lab X delayed its $10 billion round, Reuters said."),
        "https://b.example/2": page("Something unrelated."),
        "https://c.example/3": FakeResponse(403, "blocked"),
        "https://d.example/4": page("Nvidia said demand for Blackwell is 3 times supply."),
    })
    edgar_item = {"claim": "Meta capex guidance", "url": "https://www.sec.gov/x.htm", "status": "verified", "source": "edgar"}
    scores = {
        "credit": {"score": 2, "reasons": []},
        "ai_lab_financing": {"score": 2, "reasons": ["s"], "evidence": [
            {"claim": "Lab X delayed a $10 billion round (Reuters)", "url": "https://a.example/1"},
            {"claim": "Lab Y cut its target by 30%", "url": "https://b.example/2"}]},
        "supplier_signals": {"score": 3, "reasons": ["s"], "evidence": [
            {"claim": "Nvidia: demand 3 times supply for Blackwell", "url": "https://d.example/4"},
            {"claim": "Broadcom orders slowed 12%", "url": "https://c.example/3"}, edgar_item]},
        "physical_buildout": {"score": 1, "reasons": ["s"], "evidence": [
            {"claim": "Site Z cancelled 2 GW", "url": "https://b.example/2"}]},
    }
    counts = sources.check_evidence(scores, CFG, fetch=sources.make_fetcher(10, session=sess, resolver=public_dns))
    assert counts["ai_lab_financing"] == {"verified": 1, "unverified": 1, "unreachable": 0}
    assert counts["supplier_signals"] == {"verified": 2, "unverified": 0, "unreachable": 1}
    assert sorted(sess.calls) == ["https://a.example/1", "https://b.example/2", "https://c.example/3", "https://d.example/4"]
    assert scores["supplier_signals"]["evidence"][1]["check"] == "HTTP 403"
    capped = sources.apply_caps(scores, CFG)
    assert capped == ["ai_lab_financing"]
    ai = scores["ai_lab_financing"]
    assert ai["score"] == 1 and ai["score_before_cap"] == 2 and "Capped at 1 (was 2)" in ai["reasons"][-1]
    assert scores["supplier_signals"]["score"] == 3 and scores["physical_buildout"]["score"] == 1
    assert scores["credit"]["score"] == 2  # rule-based categories are never capped here


# ---------------------------------------------------------------- 4. leases not yet commenced
MSFT_CIK, META_CIK = "0000789019", "0001326801"


def subs(cik, rows):
    keys = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument", "items")
    return as_json({"cik": cik, "filings": {"recent": {k: [r.get(k, "") for r in rows] for k in keys}}})


def tenq(acc, filed, period, doc, form="10-Q"):
    return {"accessionNumber": acc, "filingDate": filed, "reportDate": period, "form": form, "primaryDocument": doc}


LEASE_DOC = """<html><head><style>p{{}}</style></head><body><ix:header><div>hidden facts {amt1}</div></ix:header>
<p>Note 12 — Leases</p><p>As of {asof}, we have additional operating and finance leases, primarily for datacenters,
that have not yet commenced of {amt1} and {amt2}, respectively. These leases will commence between fiscal year
2027 and fiscal year 2032.</p><p>Other text.</p></body></html>"""


def lease_routes():
    def doc(cik, acc, name, asof, a1, a2):
        url = edgar_text.doc_url(cik, acc, name)
        return url, page(LEASE_DOC.format(asof=asof, amt1=a1, amt2=a2))
    routes = dict([
        doc(MSFT_CIK, "0000950170-26-000100", "msft-20260930.htm", "September 30, 2026", "$100.0 billion", "$10.0 billion"),
        doc(MSFT_CIK, "0000950170-26-000050", "msft-20260630.htm", "June 30, 2026", "$150.0 billion", "$20.0 billion"),
        doc(META_CIK, "0001628280-26-000200", "meta-20260930.htm", "September 30, 2026", "$40.0 billion", "$5.0 billion"),
        doc(META_CIK, "0001628280-26-000150", "meta-20260630.htm", "June 30, 2026", "$45.0 billion", "$5.0 billion"),
    ])
    routes[edgar_text.SUBMISSIONS_URL.format(cik=MSFT_CIK)] = subs(MSFT_CIK, [
        tenq("0000950170-26-000100", "2026-10-29", "2026-09-30", "msft-20260930.htm"),
        {"accessionNumber": "0001193125-26-000099", "filingDate": "2026-10-28", "form": "8-K", "items": "2.02,9.01"},
        tenq("0000950170-26-000050", "2026-07-30", "2026-06-30", "msft-20260630.htm", "10-K"),
        tenq("0000950170-26-000010", "2026-04-30", "2026-03-31", "msft-20260331.htm")])
    routes[edgar_text.SUBMISSIONS_URL.format(cik=META_CIK)] = subs(META_CIK, [
        tenq("0001628280-26-000200", "2026-10-30", "2026-09-30", "meta-20260930.htm"),
        tenq("0001628280-26-000150", "2026-07-31", "2026-06-30", "meta-20260630.htm")])
    return routes


def lease_reply(asof, a1, a2):
    s = (f"As of {asof}, we have additional operating and finance leases, primarily for datacenters, that have not "
         f"yet commenced of {a1} and {a2}, respectively.")
    return reply({"amounts": [{"amount_text": a1, "kind": "operating", "sentence": s, "commencement": "2027-2032"},
                              {"amount_text": a2, "kind": "finance", "sentence": s, "commencement": ""}], "note": ""})


TWO = dict(CFG, hyperscalers={"MSFT": {"cik": MSFT_CIK, "name": "Microsoft"}, "META": {"cik": META_CIK, "name": "Meta"}})


def test_parse_usd_and_verbatim_checks():
    assert edgar_text.parse_usd("$12.3 billion") == 12.3e9
    assert edgar_text.parse_usd("$1,234 million") == 1.234e9
    assert edgar_text.parse_usd("$4.5bn") == 4.5e9
    assert edgar_text.parse_usd("$12,345") is None  # no scale word: not trusted
    assert edgar_text.parse_usd_range("$70-72 billion") == (70e9, 72e9)
    assert edgar_text.parse_usd_range("$70 billion to $72 billion") == (70e9, 72e9)
    assert edgar_text.parse_usd_range("between $115 and $135 billion") == (115e9, 135e9)
    assert edgar_text.found_in("leases  that have not yet commenced of $1 billion",
                               "Leases that have not yet\ncommenced of $1 billion.")
    assert not edgar_text.found_in("leases of $2 billion", "leases of $1 billion")
    assert edgar_text.period_end("results for the quarter ended September 30, 2026 were") == date(2026, 9, 30)
    assert edgar_text.quarter_before(date(2026, 10, 28)) == date(2026, 9, 30)
    assert edgar_text.quarter_before(date(2027, 1, 28)) == date(2026, 12, 31)


def test_verify_amounts_rejects_unquoted_numbers():
    src = "We have leases that have not yet commenced of $5.0 billion and $1.0 billion, respectively."
    raw = [{"amount_text": "$5.0 billion", "kind": "operating", "sentence": src},
           {"amount_text": "$1.0 billion", "kind": "finance", "sentence": src},
           {"amount_text": "$5.0 billion", "kind": "other", "sentence": src},           # duplicate
           {"amount_text": "$9.9 billion", "kind": "operating", "sentence": src},        # not in the sentence
           {"amount_text": "$3.0 billion", "kind": "total", "sentence": "Invented sentence with $3.0 billion."}]
    got = leases.verify_amounts(raw, src)
    assert [a["verified"] for a in got] == [True, True, False, False]
    assert [a["problem"] for a in got][2:] == ["amount not found in its sentence", "sentence not found in the filing"]
    assert leases.total(got) == 6.0e9
    assert leases.total([{"verified": True, "kind": "total", "amount_usd": 7e9},
                         {"verified": True, "kind": "operating", "amount_usd": 5e9}]) == 7e9
    assert leases.total(got[2:]) is None


def test_leases_run_extracts_stores_and_scores(sandbox, monkeypatch):
    sess = FakeSession(lease_routes())
    fake = fake_claude(monkeypatch, [
        lease_reply("September 30, 2026", "$100.0 billion", "$10.0 billion"),
        lease_reply("June 30, 2026", "$150.0 billion", "$20.0 billion"),
        lease_reply("September 30, 2026", "$40.0 billion", "$5.0 billion"),
        lease_reply("June 30, 2026", "$45.0 billion", "$5.0 billion")])
    usage = {}
    out = leases.run(TWO, sess, "claude-opus-5-5", True, False, usage, {})
    assert out["errors"] == {} and len(fake.calls) == 4 and usage["leases"]["requests"] == 4
    assert "not yet commenced" in fake.calls[0]["messages"][0]["content"]
    assert fake.calls[0]["output_config"]["format"]["schema"] == leases.SCHEMA
    stored = json.loads((sandbox / "data" / "edgar" / "MSFT_leases.json").read_text())
    assert [r["quarter"] for r in stored["filings"]] == ["2026Q2", "2026Q3"]
    assert stored["filings"][1]["total_usd"] == 110e9 and stored["filings"][1]["amounts"][0]["verified"]
    assert "hidden facts" not in " ".join(stored["filings"][1]["paragraphs"])
    msft = out["companies"]["MSFT"]
    assert msft["total_usd"] == 110e9 and msft["previous_total_usd"] == 170e9 and msft["qoq_pct"] == -35.29
    assert out["by_quarter"] == {"2026Q2": {"MSFT": 170e9, "META": 50e9}, "2026Q3": {"MSFT": 110e9, "META": 45e9}}
    # aggregate 220B -> 155B is -29.6%: past the -25% threshold, so filings gets +1
    edgar = {"companies": {"MSFT": {"name": "Microsoft", "capex": None, "free_cash_flow": None}}}
    s = scoring.score_filings(edgar, CFG, out)
    assert s["score"] == 1 and any("not yet commenced 2026Q3: -29.6% QoQ" in r for r in s["reasons"])
    looser = dict(CFG, hyperscaler_filings=dict(CFG["hyperscaler_filings"], leases_not_commenced_qoq=-40.0))
    assert scoring.score_filings(edgar, looser, out)["score"] == 0  # threshold is configurable
    # a second run reuses the stored extraction: no Claude call, no document download
    sess2 = FakeSession(lease_routes())
    again = leases.run(TWO, sess2, "claude-opus-5-5", True, False, {}, {})
    assert again["by_quarter"] == out["by_quarter"] and fake.calls and len(fake.calls) == 4
    assert all("submissions" in u for u in sess2.calls)


def test_leases_wait_when_claude_is_off(sandbox):
    out = leases.run(TWO, FakeSession(lease_routes()), "claude-opus-5-5", False, False, {}, {})
    assert out["companies"] == {} and "not read (Claude step off)" in out["errors"]["MSFT"]
    assert not (sandbox / "data" / "edgar" / "MSFT_leases.json").exists()


# ---------------------------------------------------------------- 3. earnings press releases
Q3_ACC, Q2_ACC = "0001628280-26-000300", "0001628280-26-000250"
RELEASE = """<html><body><p>MENLO PARK, Calif. — Meta Platforms, Inc. today reported financial results for the
quarter ended {period}.</p><p>"We remain capacity constrained, and we are pacing our build-out carefully," said
the CEO.</p><table><tr><td>Revenue</td><td>51,242</td><td>40,589</td></tr></table>
<p>CFO Outlook Commentary</p><p>We expect capital expenditures, including principal payments on finance leases, to
be in the range of {guide} for the full year 2026.</p><p>We continue to optimize our infrastructure for
efficiency; we are not seeing digestion.</p></body></html>"""
INDEX = """<table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/ix?doc=/Archives/edgar/data/1326801/{acc}/meta-8k.htm">meta-8k.htm</a></td><td>8-K</td></tr>
<tr class="evenRow"><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1326801/{acc}/{ex}">{ex}</a></td><td>EX-99.1</td></tr></table>"""


def release_routes():
    routes = {edgar_text.SUBMISSIONS_URL.format(cik=META_CIK): subs(META_CIK, [
        {"accessionNumber": "0001326801-26-000400", "filingDate": "2026-11-05", "form": "8-K", "items": "5.07"},
        {"accessionNumber": Q3_ACC, "filingDate": "2026-10-21", "form": "8-K", "items": "2.02,9.01"},
        tenq("0001628280-26-000200", "2026-10-30", "2026-09-30", "meta-20260930.htm"),
        {"accessionNumber": Q2_ACC, "filingDate": "2026-07-29", "form": "8-K", "items": "2.02, 9.01"}])}
    for acc, ex, period, guide in ((Q3_ACC, "meta-09302026xexhibit991.htm", "September 30, 2026", "$70-72 billion"),
                                   (Q2_ACC, "meta-06302026xexhibit991.htm", "June 30, 2026", "$66-72 billion")):
        nodash = acc.replace("-", "")
        routes[edgar_text.INDEX_URL.format(cik=1326801, acc=nodash, acc_dashed=acc)] = page(INDEX.format(acc=nodash, ex=ex))
        routes[f"https://www.sec.gov/Archives/edgar/data/1326801/{nodash}/{ex}"] = page(RELEASE.format(period=period, guide=guide))
    return routes


def guidance_reply(guide, sentence_override=None):
    sent = sentence_override or (f"We expect capital expenditures, including principal payments on finance leases, to be "
                                 f"in the range of {guide} for the full year 2026.")
    return reply({"capex_guidance": [{"period": "FY 2026", "amount_text": guide, "stated_change": "not stated",
                                      "sentence": sent}], "summary": "Capacity constrained."})


def test_press_release_extracted_compared_and_turned_into_evidence(sandbox, monkeypatch):
    sess = FakeSession(release_routes())
    fake = fake_claude(monkeypatch, [guidance_reply("$70-72 billion"), guidance_reply("$66-72 billion")])
    usage = {}
    out = calls.run(TWO, sess, ["META", "NVDA"], "claude-opus-5-5", True, False, usage, {}, since=date(2026, 10, 21))
    assert out["errors"] == {} and list(out["companies"]) == ["META"] and usage["earnings"]["requests"] == 2
    assert "51,242" not in fake.calls[0]["messages"][0]["content"]  # numeric table rows are not sent
    q3 = json.loads((sandbox / "data" / "calls" / "META" / "2026Q3.json").read_text())
    q2 = json.loads((sandbox / "data" / "calls" / "META" / "2026Q2.json").read_text())
    assert q3["accession"] == Q3_ACC and q3["url"].endswith("meta-09302026xexhibit991.htm") and q2["quarter"] == "2026Q2"
    g = q3["capex_guidance"][0]
    assert g["verified"] and g["period"] == "FY2026" and (g["low_usd"], g["high_usd"]) == (70e9, 72e9)
    assert q3["phrases"]["capacity constrained"]["count"] == 1 and q3["phrases"]["pacing"]["count"] == 1
    assert q3["phrases"]["optimize"]["count"] == 1 and q3["phrases"]["digestion"]["count"] == 1
    assert q3["phrases"]["fungible"]["count"] == 0
    assert "capacity constrained" in q3["phrases"]["capacity constrained"]["sentences"][0]
    comp = out["companies"]["META"]["comparison"]
    assert comp["previous_quarter"] == "2026Q2"
    assert comp["guidance"][0]["before"] == "$66-72 billion" and comp["guidance"][0]["midpoint_change_usd"] == 2e9
    claims = [e["claim"] for e in out["evidence"]]
    assert any("$70-72 billion (previous quarter's release: $66-72 billion; midpoint +2.0B)" in c for c in claims)
    assert any("cautious-phrase counts" in c and "pacing 1 (prev. 1)" in c for c in claims)
    assert all(e["status"] == "verified" and e["source"] == "edgar" and "sec.gov" in e["url"] for e in out["evidence"])
    # stored releases are reused: no new Claude calls
    again = calls.run(TWO, FakeSession(release_routes()), ["META"], "claude-opus-5-5", True, False, {}, {})
    assert again["companies"]["META"]["quarter"] == "2026Q3" and not fake.responses and len(fake.calls) == 2


def test_press_release_not_yet_filed_or_misquoted(sandbox, monkeypatch):
    fake_claude(monkeypatch, [])
    out = calls.run(TWO, FakeSession(release_routes()), ["META"], "claude-opus-5-5", True, False, {}, {},
                    since=date(2027, 1, 28))
    assert "not on EDGAR yet" in out["errors"]["META"] and out["evidence"] == []
    fake_claude(monkeypatch, [guidance_reply("$80-90 billion", "We expect capex of $80-90 billion."),
                              guidance_reply("$66-72 billion")])
    out = calls.run(TWO, FakeSession(release_routes()), ["META"], "claude-opus-5-5", True, False, {}, {})
    q3 = json.loads((sandbox / "data" / "calls" / "META" / "2026Q3.json").read_text())
    assert not q3["capex_guidance"][0]["verified"] and q3["capex_guidance"][0]["problem"] == "sentence not found in the release"
    claims = [e["claim"] for e in out["evidence"]]
    assert not any("$80-90" in c for c in claims)
    assert any("gives no capex guidance; the previous quarter's release gave guidance for FY2026" in c for c in claims)


def test_exhibit_lookup_falls_back_to_any_ex99():
    acc = "0000789019-26-000001"
    idx = edgar_text.INDEX_URL.format(cik=789019, acc=acc.replace("-", ""), acc_dashed=acc)
    sess = FakeSession({idx: page('<tr><td><a href="/Archives/edgar/data/789019/x/msft-ex99.htm">x</a></td><td>EX-99</td></tr>')})
    assert edgar_text.exhibit_99_1(sess, "0000789019", acc) == "https://www.sec.gov/Archives/edgar/data/789019/x/msft-ex99.htm"
    assert edgar_text.exhibit_99_1(FakeSession({idx: page("<tr><td>8-K</td></tr>")}), "0000789019", acc) is None


# ---------------------------------------------------------------- end to end: three runs through run.main
NEWS = {
    "https://news.example/lab-ok": page("Lab X delayed its $10 billion round, Reuters reported."),
    "https://news.example/lab-bad": page("An unrelated page."),
    "https://news.example/nvda": page("Nvidia said Blackwell demand is 3 times supply."),
    "https://news.example/dc1": page("Project Alpha, a 2 GW campus, was cancelled."),
    "https://news.example/dc2": FakeResponse(403, "blocked"),
}


def assessment(build=0):
    ev = lambda claim, url: {"claim": claim, "url": url}  # noqa: E731
    return {"news_scores": {
        "ai_lab_financing": {"score": 2, "summary": "Two labs wobble.", "evidence": [
            ev("Lab X delayed a $10 billion round (Reuters)", "https://news.example/lab-ok"),
            ev("Lab Y cut its target by 30%", "https://news.example/lab-bad")]},
        "supplier_signals": {"score": 2, "summary": "Mixed.", "evidence": [
            ev("Nvidia: Blackwell demand 3 times supply", "https://news.example/nvda")]},
        "physical_buildout": {"score": build, "summary": "Cancellations." if build else "Quiet.", "evidence": [
            ev("Project Alpha 2 GW campus cancelled", "https://news.example/dc1"),
            ev("Utility withdrew 5 GW of requests", "https://news.example/dc2")] if build else []}},
        "credit_news_adjustment": {"delta": 0, "reason": ""}, "headline": "h", "digest_markdown": "d", "watch_next": []}


def research_reply():
    return NS(stop_reason="end_turn", usage=_usage(20_000, 1_000), content=[NS(type="text", text="notes", citations=[])])


def test_three_runs_end_to_end(sandbox, monkeypatch):
    for mod in (fetch_fred, fetch_edgar, fetch_taiwan):  # cached data from the sandbox, no network
        orig = mod.run
        monkeypatch.setattr(mod, "run", lambda cfg, *a, _o=orig, **k: _o(cfg, *a, **{**k, "offline": True}))
    monkeypatch.setattr(leases, "run", lambda *a, **k: {"companies": {}, "by_quarter": {}, "errors": {}})
    monkeypatch.setattr(util, "session", lambda ua=None: FakeSession({**release_routes(), **NEWS}))
    monkeypatch.setattr(sources, "public_host", lambda host, resolver=None: True)
    monkeypatch.setattr(models, "_client", lambda: (_ for _ in ()).throw(ConnectionError("no network in tests")))
    sent = []
    monkeypatch.setattr(notify, "send", lambda report: sent.append(report["trigger"]) or "sent to 1 recipient(s)")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = fake_claude(monkeypatch, [
        research_reply(), reply(assessment()),                                            # run 1
        guidance_reply("$70-72 billion"), guidance_reply("$66-72 billion"),               # run 2: press releases
        research_reply(), reply(assessment()),
        research_reply(), reply(assessment(build=2)),                                     # run 3 (releases reused)
    ])

    monkeypatch.setenv("RUN_TRIGGER", "schedule")
    monkeypatch.setenv("EVENT_NAME", "schedule")
    assert run.main([]) == 0
    monkeypatch.setenv("RUN_TRIGGER", "event:META-earnings")
    monkeypatch.setenv("EVENT_NAME", "workflow_dispatch")
    assert run.main([]) == 0
    assert run.main([]) == 0
    assert not fake.responses

    rows = util.read_csv(sandbox / "data" / "runs.csv")
    assert [r["trigger"] for r in rows] == ["schedule", "event:META-earnings", "event:META-earnings"]
    assert [(r["ai_lab_financing"], r["supplier_signals"], r["physical_buildout"]) for r in rows] == [
        ("1", "1", "0"), ("1", "2", "0"), ("1", "2", "2")]
    assert [r["level"] for r in rows] == ["GREEN", "GREEN", "AMBER"]
    assert sent == ["schedule", "event:META-earnings"]  # run 2 moved too little to email
    reports = {json.loads(p.read_text())["email"]: json.loads(p.read_text()) for p in (sandbox / "data" / "runs").glob("*.json")}
    assert len(list((sandbox / "data" / "runs").glob("*.json"))) == 3
    quiet = reports["skipped (event run: no level change and no category moved 2+ points)"]
    assert quiet["cost"]["steps"]["earnings"]["requests"] == 2 and quiet["filing_text"]["earnings"]["companies"]["META"]
    assert quiet["events"][0]["name"] == "META-earnings"

    latest = json.loads((sandbox / "site" / "data" / "latest.json").read_text())
    comp = latest["composite"]
    assert latest["email_reason"] == "level changed GREEN -> AMBER" and latest["email"].startswith("sent")
    assert comp["jumps"] == {"physical_buildout": 2} and comp["confirmation"]["categories"] == ["supplier_signals"]
    assert not comp["red_confirmed"] and comp["previous_run"]["level"] == "GREEN"
    s = latest["scores"]
    assert s["ai_lab_financing"]["score"] == 1 and "Capped at 1 (was 2)" in s["ai_lab_financing"]["reasons"][-1]
    assert latest["source_check"]["capped"] == ["ai_lab_financing"]
    assert [e["status"] for e in s["physical_buildout"]["evidence"]] == ["verified", "unreachable"]
    edgar_ev = [e for e in s["supplier_signals"]["evidence"] if e.get("source") == "edgar"]
    assert edgar_ev and edgar_ev == [e for e in s["hyperscaler_filings"]["evidence"] if e.get("source") == "edgar"]
    assert "Blackwell" not in json.dumps(s["hyperscaler_filings"])
    site_runs = json.loads((sandbox / "site" / "data" / "runs.json").read_text())
    assert site_runs["runs"][0]["level"] == "AMBER" and site_runs["total_runs"] == 3
    assert "event-day run" in fake.calls[-2]["messages"][0]["content"]  # research focused on the event
    assert "earnings_press_releases" in fake.calls[-1]["messages"][0]["content"]


# ---------------------------------------------------------------- email, HTML text, dashboard
def test_event_email_subject_and_flags():
    report = _report(trigger="event:META-earnings", level="RED", total=10.0)
    report.update(categories=scoring.CATEGORIES, events=[{"date": "2026-10-21", "name": "META-earnings", "title": "Meta Q3"}])
    report["composite"].update(max=18.0, alert=True, red_confirmed=True, jumps={"credit": 2},
                               confirmation={"met": True, "categories": ["credit", "supplier_signals"], "runs": 2,
                                             "min_score": 2, "min_categories": 2})
    report["scores"]["supplier_signals"]["evidence"] = [{"status": "verified"}, {"status": "unreachable"}]
    report["claude"]["assessment"] = {"headline": "Two signals held", "digest_markdown": "x", "watch_next": []}
    subject, body, text = notify.build_email(report, None)
    assert subject == "[Capex EW] Event META-earnings · ALERT RED (confirmed) 10/18 — Two signals held"
    assert "rose +2 since last run" in body and "1/2 sources verified" in body and "credit, supplier_signals" in body
    report["composite"]["red_confirmed"] = False
    assert "RED (unconfirmed)" in notify.build_email(report, None)[0]


def test_html_to_text_joins_wrapped_source_lines():
    html = "<p>We have leases that have\n  not yet commenced of $1.2&nbsp;billion.</p><script>x()</script><p>Next</p>"
    assert util.html_to_text(html) == "We have leases that have not yet commenced of $1.2 billion.\nNext"


def test_dashboard_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = (ROOT / "site" / "index.html").read_text()
    js = tmp_path / "page.js"
    js.write_text(re.search(r"<script>(.*)</script>", html, re.S).group(1))
    assert subprocess.run([node, "--check", str(js)], capture_output=True).returncode == 0
    assert 'get("data/runs.json")' in html


def test_fiscal_period_fallback_for_offset_quarters():
    orcl = [tenq("a", "2026-09-10", "2026-08-31", "x.htm"), tenq("b", "2026-06-20", "2026-05-31", "y.htm", "10-K"),
            {"accessionNumber": "c", "filingDate": "2026-09-09", "form": "8-K", "items": "2.02", "reportDate": "2026-09-09"}]
    months = edgar_text.quarter_end_months(orcl)
    assert months == {5, 8}
    assert edgar_text.period_before(date(2026, 12, 10), {2, 5, 8, 11}) == date(2026, 11, 30)
    assert edgar_text.period_before(date(2027, 2, 4), {3, 6, 9, 12}) == date(2026, 12, 31)
    assert edgar_text.period_before(date(2026, 10, 28), set()) == date(2026, 9, 30)
    assert edgar_text.period_end("for its second quarter ending November 30, 2026") == date(2026, 11, 30)
