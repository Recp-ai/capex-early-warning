"""v3: follow-ups, source tiers, sampled news scores, level hysteresis, credit contagion stage.
Claude and HTTP are mocked."""
import json
from datetime import datetime, timezone
from fractions import Fraction
from types import SimpleNamespace as NS

import pytest

from cew import (analyst, fetch_edgar, fetch_fred, fetch_taiwan, followups, leases, models, notify, publish, run,
                 scoring, sources, util)
from test_v2 import FakeResponse, FakeSession, _usage, fake_claude, page, reply

CFG = util.load_config()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(util.time, "sleep", lambda s: None)


# ---------------------------------------------------------------- 1. follow-ups
def test_pending_carries_unresolved_items_and_drops_them_after_three_runs():
    prev = {"run_id": "R3", "claude": {"assessment": {"watch_next": ["New item", "Old open item", " ", "Expired"]}},
            "follow_ups": [
                {"item": "Old open item", "status": "still_open", "runs": 1, "listed": "R1"},
                {"item": "Answered", "status": "resolved", "runs": 1, "listed": "R1"},
                {"item": "Third time", "status": "no_news", "runs": 2, "listed": "R0"},
                {"item": "Expired", "status": "worsened", "runs": 3, "listed": "R0"}]}
    got = followups.pending(prev, CFG)
    assert [(f["item"], f["runs"], f["listed"]) for f in got] == [
        ("New item", 1, "R3"), ("Old open item", 2, "R1"), ("Third time", 3, "R0")]  # listed again: keeps its count
    assert [f["item"] for f in followups.pending(prev, dict(CFG, follow_ups={"max_items": 2}))] == ["New item", "Old open item"]
    assert followups.pending(None, CFG) == []
    assert followups.pending({"run_id": "R", "claude": {}}, CFG) == []


def test_unresolved_item_is_followed_up_in_three_runs_then_drops_off():
    prev = {"run_id": "R0", "claude": {"assessment": {"watch_next": ["Oracle CDS"]}}}
    counts = []
    for i in range(1, 5):
        items = followups.pending(prev, CFG)
        counts.append([f["runs"] for f in items])
        merged = followups.merge(items, [{"item": "Oracle CDS", "status": "still_open", "note": "n", "url": ""}])
        prev = {"run_id": f"R{i}", "claude": {"assessment": {"watch_next": []}}, "follow_ups": merged}
    assert counts == [[1], [2], [3], []]


def test_merge_matches_claudes_entries_by_text():
    items = [{"item": "Oracle CDS and bond deals", "runs": 1, "listed": "R"},
             {"item": "OpenAI round terms", "runs": 2, "listed": "Q"},
             {"item": "Taiwan October revenue", "runs": 1, "listed": "R"}]
    returned = [
        {"item": "OpenAI funding round terms", "status": "resolved", "note": "Closed at $40 billion", "url": "https://x.example/1"},
        {"item": "oracle CDS and bond deals.", "status": "bogus", "note": "", "url": "javascript:alert(1)"},
        {"item": "Something else entirely", "status": "worsened", "note": "x", "url": "https://x.example/2"}]
    oracle, openai, taiwan = followups.merge(items, returned)
    assert openai["status"] == "resolved" and openai["runs"] == 2
    assert openai["source"] == {"url": "https://x.example/1", "claim": "Closed at $40 billion"}
    assert oracle["status"] == "still_open" and oracle["url"] == "" and oracle["source"] is None
    assert taiwan["status"] == "no_news" and taiwan["note"] == "Not covered in this run's assessment."
    assert followups.merge(items[:1], None)[0]["status"] == "no_news"


# ---------------------------------------------------------------- 4a. sampled news scores
def _sample(ai, sup, build, delta=0, tag=""):
    news = lambda score, k: {"score": score, "summary": f"{k}{tag}", "evidence": [{"claim": tag, "url": f"https://e/{k}{tag}"}]}  # noqa: E731
    return {"news_scores": {"ai_lab_financing": news(ai, "ai"), "supplier_signals": news(sup, "sup"),
                            "physical_buildout": news(build, "build")},
            "credit_news_adjustment": {"delta": delta, "reason": f"r{tag}"}, "headline": f"h{tag}",
            "digest_markdown": "", "follow_ups": [], "watch_next": [tag]}


def test_round_half_down():
    assert [analyst.round_half_down(Fraction(n, d)) for n, d in ((3, 2), (5, 2), (7, 4), (0, 1), (1, 2), (2, 3), (1, 3))] \
        == [1, 2, 2, 0, 0, 1, 0]


def test_combine_averages_rounds_half_down_and_flags_unstable():
    c = analyst.combine([_sample(1, 2, 0, delta=1, tag="a"), _sample(2, 3, 2, delta=0, tag="b")])
    ns = c["news_scores"]
    assert {k: v["score"] for k, v in ns.items()} == {"ai_lab_financing": 1, "supplier_signals": 2, "physical_buildout": 1}
    assert ns["physical_buildout"]["samples"] == [0, 2] and ns["physical_buildout"]["spread"] == 2
    assert ns["physical_buildout"]["unstable"] and not ns["ai_lab_financing"]["unstable"]
    assert c["samples"]["unstable"] == ["physical_buildout"] and c["samples"]["used"] == 0
    assert c["headline"] == "ha" and c["watch_next"] == ["a"] and ns["supplier_signals"]["summary"] == "supa"
    assert c["credit_news_adjustment"] == {"delta": 0, "reason": "rb"}  # a 1 / 0 split makes no adjustment
    three = analyst.combine([_sample(0, 0, 0, 1), _sample(1, 0, 3, 1), _sample(1, 1, 3, 0)], unstable_spread=3)
    assert {k: v["score"] for k, v in three["news_scores"].items()} == {
        "ai_lab_financing": 1, "supplier_signals": 0, "physical_buildout": 2}
    assert three["samples"]["unstable"] == ["physical_buildout"] and three["credit_news_adjustment"]["delta"] == 1
    assert analyst.combine([_sample(0, 0, 0, 1), _sample(0, 0, 0, -1)])["credit_news_adjustment"] == {"delta": 0, "reason": ""}
    # the sample closest to the final scores supplies the text
    assert analyst.combine([_sample(2, 2, 2, tag="far"), _sample(1, 1, 1, tag="near")])["headline"] == "hnear"


ASSESS = _sample(1, 1, 0)


def _research(text="notes"):
    return NS(stop_reason="end_turn", usage=_usage(20_000, 1_000), content=[NS(type="text", text=text, citations=[])])


def test_run_samples_follow_ups_and_prompt_caching(monkeypatch):
    fake = fake_claude(monkeypatch, [_research(), reply(_sample(1, 0, 0)), reply(_sample(2, 2, 0))])
    out = analyst.run(CFG, {"x": 1}, None, True, models.resolve(CFG, {}), follow_ups=["Oracle CDS", "OpenAI round"])
    research_call, a1, a2 = fake.calls
    assert research_call["tools"][0]["max_uses"] == 16
    assert "Follow-ups from the last run" in research_call["messages"][0]["content"]
    assert "1. Oracle CDS\n2. OpenAI round" in research_call["messages"][0]["content"]
    assert "Reuters" in research_call["system"] and "aggregator" in research_call["system"]
    for call in (a1, a2):
        assert call["cache_control"] == {"type": "ephemeral"}
        assert "## Follow-ups from the last run\n1. Oracle CDS\n2. OpenAI round" in call["messages"][0]["content"]
    assert a1["messages"] == a2["messages"]  # same prompt both times, so the second reads the cache
    fu = a1["output_config"]["format"]["schema"]["properties"]["follow_ups"]["items"]
    assert fu["required"] == ["item", "status", "note", "url", "event_date"]
    assert fu["properties"]["status"]["enum"] == ["resolved", "improved", "worsened", "still_open", "no_news"]
    assert out["usage"]["assessment"]["requests"] == 2 and out["usage"]["assessment"]["input_tokens"] == 2_000
    ns = out["assessment"]["news_scores"]
    assert ns["ai_lab_financing"]["score"] == 1 and ns["supplier_signals"]["samples"] == [0, 2]
    assert ns["supplier_signals"]["score"] == 1 and ns["supplier_signals"]["unstable"]


def test_failed_sample_leaves_the_others_and_one_sample_skips_caching(monkeypatch):
    early = NS(stop_reason="max_tokens", usage=_usage(9_000, 8_000), content=[])
    fake_claude(monkeypatch, [_research(), reply(ASSESS), early])
    out = analyst.run(CFG, {}, None, True, models.resolve(CFG, {}))
    assert out["assessment"]["samples"]["n"] == 1 and "error" not in out
    assert out["sample_errors"] == ["sample 2: assessment stopped early (max_tokens)"]
    fake_claude(monkeypatch, [_research(), early, early])
    assert analyst.run(CFG, {}, None, True, models.resolve(CFG, {}))["error"].startswith("assessment failed: sample 1")
    fake = fake_claude(monkeypatch, [_research(), reply(ASSESS)])
    one = analyst.run(dict(CFG, assessment_samples=1), {}, None, True, models.resolve(CFG, {}))
    assert "cache_control" not in fake.calls[1] and "(none)" in fake.calls[1]["messages"][0]["content"]
    assert one["assessment"]["samples"]["n"] == 1
    assert "Follow-ups from the last run" not in fake.calls[0]["messages"][0]["content"]


# ---------------------------------------------------------------- 4b. level hysteresis
def _scores_totalling(total):
    return {k: {"score": min(2, max(0, total - 2 * i))} for i, (k, _) in enumerate(scoring.CATEGORIES)}


def _level(total, previous, cfg=CFG):
    return scoring.hysteresis(scoring.composite(_scores_totalling(total), cfg), previous, cfg)


def test_hysteresis_holds_a_level_until_one_point_past_the_line():
    held = _level(4, "AMBER")
    assert held["level"] == "AMBER" and held["raw_level"] == "GREEN" and held["alert"]
    assert held["held"]["previous_level"] == "AMBER" and "Held at AMBER" in held["held"]["note"]
    assert _level(3, "AMBER")["level"] == "GREEN" and "held" not in _level(3, "AMBER")
    assert _level(8, "RED")["level"] == "RED" and _level(8, "RED")["raw_level"] == "AMBER"
    assert _level(7, "RED")["level"] == "AMBER" and "held" not in _level(7, "RED")
    assert _level(4, "RED")["level"] == "AMBER" and _level(3, "RED")["level"] == "GREEN"
    assert _level(4, "GREEN")["level"] == "GREEN" and _level(4, None)["level"] == "GREEN"
    assert _level(9, "AMBER")["level"] == "RED" and _level(5, "GREEN")["level"] == "AMBER"  # rises are immediate
    off = dict(CFG, composite=dict(CFG["composite"], level_hysteresis=0))
    assert _level(4, "AMBER", off)["level"] == "GREEN"


def test_event_email_compares_the_held_level():
    scores = _scores_totalling(4)
    prev = {k: v["score"] for k, v in _scores_totalling(5).items()}  # one category one point higher
    comp = _level(4, "AMBER")
    assert run.email_decision(False, "event:CPI", comp, scores, prev, "AMBER", CFG)[0].startswith("event run: no level change")
    off = dict(CFG, composite=dict(CFG["composite"], level_hysteresis=0))
    raw = _level(4, "AMBER", off)
    assert run.email_decision(False, "event:CPI", raw, scores, prev, "AMBER", off) == (None, "level changed AMBER -> GREEN")


# ---------------------------------------------------------------- 6. credit contagion stage
def _fred(hy=None, ig=None, baa=None, ust=None):
    s = {}
    for sid, chg in (("BAMLH0A0HYM2", hy), ("BAMLC0A0CM", ig), ("BAA10Y", baa), ("DGS10", ust)):
        if chg is not None:
            s[sid] = {"latest": 3.0, "chg_4w": chg}
    return {"series": s}


@pytest.mark.parametrize("args,stage,line", [
    ((0.1, 0.05, 0.1, 0.0), "calm", "Calm · 10y flat"),
    ((0.40, 0.03, 0.05, 0.27), "junk_only", "Junk only: contained · 10y rising (rate pressure)"),
    ((0.5, 0.02, 0.15, -0.2), "spreading", "Spreading: junk and investment grade · 10y falling (growth scare)"),
    ((0.1, 0.12, None, -0.15), "ig_only", "IG only: financing pressure (possibly bond supply) · 10y falling (growth scare)"),
    ((0.39, 0.09, 0.16, None), "ig_only", "IG only: financing pressure (possibly bond supply)"),
    ((-0.45, -0.10, 0.0, -0.3), "easing", "Easing · 10y falling"),
    ((-0.45, -0.05, 0.0, 0.05), "calm", "Calm · 10y flat"),
])
def test_credit_stage_classes(args, stage, line):
    s = scoring.credit_stage(_fred(*args), CFG)
    assert (s["stage"], s["line"]) == (stage, line)


def test_credit_stage_is_context_only():
    fred = _fred(0.40, 0.03, 0.30, 0.27)
    s = scoring.credit_stage(fred, CFG)
    assert s["widening"] == ["hy", "baa"] and s["stage"] == "spreading" and s["changes_4w"]["baa"] == 0.30
    without_baa = {"series": {k: v for k, v in fred["series"].items() if k != "BAA10Y"}}
    assert scoring.score_credit(fred, CFG) == scoring.score_credit(without_baa, CFG)  # BAA never scores
    none = scoring.credit_stage(_fred(ig=0.2), CFG)
    assert none["stage"] == "" and "no high-yield" in none["line"] and "BAMLH0A0HYM2" in none["missing"]


# ---------------------------------------------------------------- 3. source tiers
def test_source_tiers_by_host():
    tier = lambda u: sources.tier(u, CFG)  # noqa: E731
    assert tier("https://www.sec.gov/Archives/edgar/x.htm") == "primary"
    assert tier("https://www.federalreserve.gov/newsevents.htm") == "primary"
    assert tier("https://investor.nvidia.com/news") == "primary"
    assert tier("https://www.businesswire.com/news/home/x") == "primary"
    assert tier("https://www.reuters.com/x") == "major" and tier("https://asia.nikkei.com/x") == "major"
    assert tier("https://finance.yahoo.com/x") == "other" and tier("https://daily.dev/posts/x") == "other"
    assert tier("https://evilreuters.com/x") == "other" and tier("https://notgov.com/x") == "other"
    assert tier("") == "other"
    items = [{"url": "https://www.cnbc.com/a"}, {"url": ""}, {"claim": "no url"}]
    sources.tag_tiers(items, CFG)
    assert items[0]["tier"] == "major" and "tier" not in items[1] and "tier" not in items[2]


# ---------------------------------------------------------------- runs.csv / run files
def test_new_runs_csv_column_extends_the_header_and_leaves_rows_alone(sandbox):
    path = sandbox / "data" / "runs.csv"
    old_fields = [f for f in publish.RUN_FIELDS if f != "credit_stage"]
    old_row = b"2026-10-13T19:20:32Z,event:MSFT-earnings,claude-sonnet-5-5,claude-opus-5-5,231024,12,0.8591,5.0,AMBER,2,1,0,0,0,2"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(",".join(old_fields).encode() + b"\r\n" + old_row + b"\r\n")
    row = dict(publish.run_row(_run_report()), timestamp="2026-10-17T01:17:00Z")
    publish.append_run_row(row)
    publish.append_run_row(dict(row, timestamp="2026-10-18T01:17:00Z"))
    lines = path.read_bytes().split(b"\r\n")
    assert lines[0] == ",".join(publish.RUN_FIELDS).encode() and lines[1] == old_row and lines[-1] == b""
    rows = util.read_csv(path)
    assert rows[0].get("credit_stage") is None and [r["credit_stage"] for r in rows[1:]] == ["junk_only"] * 2
    assert publish.previous_runs()[0]["level"] == "AMBER"


def _run_report(run_id="2026-10-17T01:17:00Z", assessed=True):
    return {"date": run_id[:10], "run_id": run_id, "trigger": "schedule", "composite": {"total": 4, "level": "GREEN"},
            "scores": {k: {"score": 0} for k, _ in scoring.CATEGORIES}, "credit_stage": {"stage": "junk_only"},
            "claude": {"enabled": True, "models": {}, **({"assessment": {"watch_next": [run_id]}} if assessed else {})}}


def test_last_assessed_run_orders_files_and_skips_runs_without_an_assessment(sandbox):
    for rid in ("2026-10-13T18:45:13Z", "2026-10-13T18:45:13Z", "2026-10-14T01:00:00Z"):
        publish.archive_run(_run_report(rid))
    publish.archive_run(_run_report("2026-10-14T01:00:00Z", assessed=False))
    for n in range(3, 11):  # -10 sorts after -9
        publish.archive_run(_run_report("2026-10-13T18:45:13Z"))
    names = [p.name for p in publish.run_files()]
    assert names[:3] == ["2026-10-13T184513Z.json", "2026-10-13T184513Z-2.json", "2026-10-13T184513Z-3.json"]
    assert names[9:] == ["2026-10-13T184513Z-10.json", "2026-10-14T010000Z.json", "2026-10-14T010000Z-2.json"]
    last = publish.last_assessed_run()
    assert last["claude"]["assessment"]["watch_next"] == ["2026-10-14T01:00:00Z"]


# ---------------------------------------------------------------- end to end: three weekly runs
W1, W2, W3 = "Oracle CDS and bond deals", "OpenAI round terms", "Taiwan October revenue"
ORCL = "https://www.reuters.com/orcl"
ROUTES = {
    "https://www.reuters.com/lab": page("Lab X delayed its $10 billion round, Reuters reported."),
    "https://news.example/blocked": FakeResponse(403, "blocked"),
    "https://investor.nvidia.com/q": page("Nvidia said Blackwell demand is 3 times supply."),
    "https://finance.yahoo.com/dc1": page("Project Alpha, a 2 GW campus, was cancelled."),
    ORCL: page("Oracle's 5-year CDS widened to 150 basis points on Tuesday."),
}


def weekly(ai=2, sup=1, build=1, watch=(), follow=()):
    ev = lambda claim, url: {"claim": claim, "url": url}  # noqa: E731
    return {"news_scores": {
        "ai_lab_financing": {"score": ai, "summary": "Labs.", "evidence": [
            ev("Lab X delayed a $10 billion round (Reuters)", "https://www.reuters.com/lab"),
            ev("Utility withdrew 5 GW of requests", "https://news.example/blocked")]},
        "supplier_signals": {"score": sup, "summary": "Suppliers.", "evidence": [
            ev("Nvidia: Blackwell demand 3 times supply", "https://investor.nvidia.com/q"),
            ev("Broadcom orders slowed 12%", "https://news.example/blocked")]},
        "physical_buildout": {"score": build, "summary": "Build-out.", "evidence": [
            ev("Project Alpha 2 GW campus cancelled", "https://finance.yahoo.com/dc1")]}},
        "credit_news_adjustment": {"delta": 0, "reason": ""}, "headline": "h", "digest_markdown": "d",
        "follow_ups": list(follow), "watch_next": list(watch)}


def test_follow_ups_samples_tiers_hysteresis_and_stage_end_to_end(sandbox, monkeypatch):
    for mod in (fetch_fred, fetch_edgar, fetch_taiwan):  # cached data from the sandbox, no network
        orig = mod.run
        monkeypatch.setattr(mod, "run", lambda cfg, *a, _o=orig, **k: _o(cfg, *a, **{**k, "offline": True}))
    monkeypatch.setattr(leases, "run", lambda *a, **k: {"companies": {}, "by_quarter": {}, "errors": {}})
    monkeypatch.setattr(util, "session", lambda ua=None: FakeSession(ROUTES))
    monkeypatch.setattr(sources, "public_host", lambda host, resolver=None: True)
    monkeypatch.setattr(models, "_client", lambda: (_ for _ in ()).throw(ConnectionError("no network in tests")))
    sent = []
    monkeypatch.setattr(notify, "send", lambda report: sent.append(json.loads(json.dumps(report, default=str))) or "sent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("RUN_TRIGGER", "schedule")
    monkeypatch.setenv("EVENT_NAME", "schedule")
    fu2 = [{"item": W1, "status": "worsened", "note": "Oracle 5-year CDS widened to 150 basis points", "url": ORCL},
           {"item": W2, "status": "resolved", "note": "The round closed.", "url": ""}]
    fake = fake_claude(monkeypatch, [
        _research(), reply(weekly(sup=0, build=1, watch=[W1, W2])), reply(weekly(sup=2, build=2, watch=[W1, W2, "x"])),
        _research(), reply(weekly(sup=2, watch=[W3], follow=fu2)), reply(weekly(sup=2, watch=[W3], follow=fu2)),
        _research(), reply(weekly(sup=1)), reply(weekly(sup=1)),
    ])
    for day in (17, 24, 31):
        monkeypatch.setattr(util, "now_utc", lambda d=day: datetime(2026, 10, d, 1, 17, tzinfo=timezone.utc))
        assert run.main([]) == 0
    assert not fake.responses and len(sent) == 3
    prompts = [c["messages"][0]["content"] for c in fake.calls]

    # run 1: nothing to follow up; scores averaged over two samples
    assert "Follow-ups from the last run" not in prompts[0] and "## Follow-ups from the last run\n(none)" in prompts[1]
    r1 = sent[0]
    sup = r1["scores"]["supplier_signals"]
    assert (sup["score"], sup["samples"], sup["unstable"]) == (1, [0, 2], True)
    assert r1["scores"]["physical_buildout"]["score"] == 1 and r1["scores"]["physical_buildout"]["samples"] == [1, 2]
    assert r1["cost"]["steps"]["assessment"]["requests"] == 2 and r1["composite"]["level"] == "GREEN"
    assert all(c.get("cache_control") for c in (fake.calls[1], fake.calls[2]))
    tiers = {e["url"]: e["tier"] for s in r1["scores"].values() for e in s.get("evidence") or []}
    assert tiers == {"https://www.reuters.com/lab": "major", "https://news.example/blocked": "other",
                     "https://investor.nvidia.com/q": "primary", "https://finance.yahoo.com/dc1": "other"}
    assert r1["credit_stage"]["line"] == "Calm · 10y flat" and r1["follow_ups"] == []
    assert "unstable: scoring samples 0, 2" in notify.build_email(r1, None)[1]

    # run 2: last run's watch-next items are searched for and get a status
    assert f"1. {W1}\n2. {W2}" in prompts[3] and f"1. {W1}\n2. {W2}" in prompts[4]
    r2 = sent[1]
    orcl, openai = r2["follow_ups"]
    assert (orcl["status"], orcl["runs"], orcl["listed"]) == ("worsened", 1, "2026-10-17T01:17:00Z")
    assert orcl["source"]["status"] == "verified" and orcl["source"]["tier"] == "major"
    assert openai["status"] == "resolved" and openai["source"] is None
    assert r2["composite"]["level"] == "AMBER"
    subject, body, text = notify.build_email(r2, None)
    assert body.index("Follow-ups from last run") < body.index("Watch next")
    assert "<b>Worsened</b> · Oracle CDS and bond deals" in body and "(verified, major)" in body
    assert "Credit contagion stage (context, not scored): Calm · 10y flat" in body
    assert "1 primary, 1 other" in body and "Worsened: Oracle CDS and bond deals" in text

    # run 3: the resolved item is gone, the open one carries forward; composite 4 holds AMBER
    assert f"1. {W3}\n2. {W1}" in prompts[6] and W2 not in prompts[6]
    r3 = sent[2]
    assert [(f["item"], f["runs"], f["status"]) for f in r3["follow_ups"]] == [(W3, 1, "no_news"), (W1, 2, "no_news")]
    comp = r3["composite"]
    assert comp["total"] == 4 and comp["level"] == "AMBER" and comp["raw_level"] == "GREEN"
    assert "Held at AMBER" in notify.build_email(r3, None)[1]

    rows = util.read_csv(sandbox / "data" / "runs.csv")
    assert [r["level"] for r in rows] == ["GREEN", "AMBER", "AMBER"] and {r["credit_stage"] for r in rows} == {"calm"}
    assert util.read_csv(sandbox / "data" / "log.csv")[-1]["credit_stage"] == "calm"
    charts = json.loads((sandbox / "site" / "data" / "charts.json").read_text())
    assert "BAA10Y" in charts["fred"]
    latest = json.loads((sandbox / "site" / "data" / "latest.json").read_text())
    assert latest["follow_ups"][1]["item"] == W1 and latest["credit_stage"]["stage"] == "calm"
