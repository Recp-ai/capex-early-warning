"""v4: event dates on evidence and follow-ups ("background" when old), conflicting figures across sources.
Claude and HTTP are mocked."""
import json
from datetime import date, datetime, timezone

from cew import (analyst, calls, fetch_edgar, fetch_fred, fetch_taiwan, followups, leases, models, notify, publish,
                 run, scoring, sources, util)
from test_v2 import FakeSession, fake_claude, reply
from test_v3 import ORCL, ROUTES, _research, _run_report, _sample

CFG = util.load_config()
DATE = {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]}


# ---------------------------------------------------------------- prompts and schema
def test_schema_requires_an_event_date_on_every_evidence_item_and_follow_up():
    props = analyst.ASSESS_SCHEMA["properties"]
    for k in ("ai_lab_financing", "supplier_signals", "physical_buildout"):
        ev = props["news_scores"]["properties"][k]["properties"]["evidence"]["items"]
        assert ev["required"] == ["claim", "url", "event_date"] and ev["properties"]["event_date"] == DATE
    fu = props["follow_ups"]["items"]
    assert "event_date" in fu["required"] and fu["properties"]["event_date"] == DATE


def test_prompts_ask_for_event_dates_and_ranges_and_rule_out_other_tier_statuses():
    for p in (analyst.RESEARCH_PROMPT, analyst.ASSESS_SYSTEM):
        p = " ".join(p.split())
        assert "YYYY-MM-DD" in p and "not when the article was published" in p
    assert "event date" in analyst.FOLLOW_UP_PROMPT
    assert "list each figure with its own URL" in analyst.RESEARCH_PROMPT
    s = " ".join(analyst.ASSESS_SYSTEM.split())
    assert "event_date, for every evidence item and every follow-up" in s and "null when the notes don't" in s
    assert "report the range" in s and "prefer the primary- or major-tier figure" in s
    assert 'Never base a follow-up status on a number from "other"-tier sources alone' in s


def test_hosts_by_tier_lists_each_site_once():
    notes = ("- Oracle CDS 150 bp — 2026-10-13 — https://www.reuters.com/a.\n"
             "- Same, 145 bp — date unknown — (https://finance.yahoo.com/b) and https://www.reuters.com/c,\n"
             "- 10-Q — https://www.sec.gov/Archives/x.htm; IR: https://investor.nvidia.com/q")
    assert sources.hosts_by_tier(notes, CFG) == {"primary": ["sec.gov", "investor.nvidia.com"],
                                                 "major": ["reuters.com"], "other": ["finance.yahoo.com"]}
    assert sources.hosts_by_tier("no links", CFG) == {} and sources.hosts_by_tier(None, CFG) == {}


def test_assessment_prompt_lists_the_tier_of_each_site_in_the_notes(monkeypatch):
    notes = "Oracle CDS 150 bp — https://www.reuters.com/a; 145 bp — https://finance.yahoo.com/b"
    fake = fake_claude(monkeypatch, [_research(notes), reply(_sample(0, 0, 0)), reply(_sample(0, 0, 0)),
                                     _research(""), reply(_sample(0, 0, 0)), reply(_sample(0, 0, 0))])
    analyst.run(CFG, {}, None, True, models.resolve(CFG, {}))
    a1, a2 = (c["messages"][0]["content"] for c in fake.calls[1:3])
    assert "## Source tier of each site in the notes (from its domain)\nmajor: reuters.com\nother: finance.yahoo.com\n" in a1
    assert a1 == a2  # still one cached prompt for both samples
    analyst.run(CFG, {}, None, True, models.resolve(CFG, {}))
    assert "## Source tier of each site in the notes (from its domain)\n(no URLs in the notes)" in fake.calls[4]["messages"][0]["content"]


# ---------------------------------------------------------------- dates checked by code
def test_event_date_keeps_only_real_dates_up_to_tomorrow():
    today = date(2026, 10, 17)
    ok = ["2026-10-09", " 2026-10-09 ", "2026-10-18", "2025-02-28"]
    assert [sources.event_date(v, today) for v in ok] == ["2026-10-09", "2026-10-09", "2026-10-18", "2025-02-28"]
    bad = [None, "", "null", "2026-10-19", "2026-10-32", "2026-02-29", "2026-10", "20261009", "2026-W41-5",
           "Oct 9, 2026", "2026-10-09T12:00:00Z", 20261009]
    assert [sources.event_date(v, today) for v in bad] == [None] * len(bad)


def test_tag_dates_labels_items_older_than_ten_days_background():
    today = date(2026, 10, 17)
    items = [{"event_date": "2026-10-07"}, {"event_date": "2026-10-06"}, {"event_date": None}, {},
             {"event_date": "2026-10-18"}, {"event_date": "soon"}]
    sources.tag_dates(items, today, CFG)
    assert [(e["event_date"], e["age_days"], e["background"]) for e in items] == [
        ("2026-10-07", 10, False), ("2026-10-06", 11, True), (None, None, False), (None, None, False),
        ("2026-10-18", -1, False), (None, None, False)]
    one = [{"event_date": "2026-10-15"}]
    sources.tag_dates(one, today, dict(CFG, evidence_dates={"background_after_days": 1}))
    assert one[0]["background"] is True and CFG["evidence_dates"]["background_after_days"] == 10


def test_follow_ups_keep_claudes_event_date():
    items = [{"item": "Oracle CDS", "runs": 1, "listed": "R"}, {"item": "OpenAI round", "runs": 2, "listed": "Q"}]
    oracle, openai = followups.merge(items, [
        {"item": "Oracle CDS", "status": "worsened", "note": "150 bp", "url": "https://x.example/1", "event_date": "2026-10-13"}])
    assert oracle["event_date"] == "2026-10-13" and oracle["source"] == {"url": "https://x.example/1", "claim": "150 bp"}
    assert openai["event_date"] is None and openai["status"] == "no_news"


def test_press_release_evidence_is_dated_by_the_8k_filing():
    cur = {"quarter": "2026Q3", "name": "Meta", "url": "https://www.sec.gov/ex99.htm", "filing_date": "2026-10-29",
           "phrases": {"pacing": {"count": 1}}, "capex_guidance": []}
    ev = calls.evidence(cur, calls.compare(cur, None))
    assert ev and {e["event_date"] for e in ev} == {"2026-10-29"}


# ---------------------------------------------------------------- email
def _email_report():
    ev = lambda claim, url, d, bg=False, **k: {"claim": claim, "url": url, "event_date": d, "background": bg, **k}  # noqa: E731
    scores = {k: {"score": 0, "evidence": []} for k, _ in scoring.CATEGORIES}
    scores["ai_lab_financing"]["evidence"] = [
        ev("Lab X delayed a $10 billion round", "https://www.reuters.com/lab", "2026-10-14", status="verified", tier="major"),
        ev("Lab Y repriced <its> round", "https://news.example/y", "2026-09-30", True, status="unreachable", tier="other")]
    scores["hyperscaler_filings"]["evidence"] = [
        ev("Meta capex guidance", "https://www.sec.gov/ex99.htm", "2025-12-30", True, status="verified", source="edgar"),
        ev("Undated claim", "https://news.example/z", None)]
    return {"date": "2026-10-17", "run_id": "2026-10-17T01:17:00Z", "trigger": "schedule", "categories": scoring.CATEGORIES,
            "scores": scores, "composite": {"total": 0, "max": 18, "level": "GREEN", "alert": False},
            "claude": {"assessment": {"headline": "h", "digest_markdown": "d", "watch_next": ["w"]}},
            "follow_ups": [
                {"item": "Oracle CDS", "status": "worsened", "note": "150 bp", "url": ORCL, "event_date": "2026-10-13",
                 "background": False, "source": {"url": ORCL, "claim": "150 bp", "status": "verified", "tier": "major"}},
                {"item": "Old item", "status": "still_open", "note": "n", "url": "https://news.example/o",
                 "event_date": "2026-09-01", "background": True, "source": {"url": "https://news.example/o", "claim": "n"}},
                {"item": "Quiet item", "status": "no_news", "note": "", "url": "", "event_date": None, "source": None}]}


def test_email_shows_each_items_event_date_and_labels_old_ones_background():
    subject, body, text = notify.build_email(_email_report(), None)
    ev = body[body.index("<h3>Evidence</h3>"):]
    assert body.index("Watch next") < body.index("<h3>Evidence</h3>")
    assert ev.index("AI-lab financing") < ev.index("Lab X") < ev.index("Hyperscaler filings") < ev.index("Meta capex")
    lab_x = ev[ev.index("Lab X") - 120:ev.index("</li>", ev.index("Lab X"))]
    assert ">Oct 14</span> Lab X delayed a $10 billion round <a href='https://www.reuters.com/lab'>reuters.com</a>" in lab_x
    assert "(verified, major)" in lab_x and "background" not in lab_x
    lab_y = ev[ev.index("Lab Y") - 200:ev.index("Lab Y")]
    assert ">Sep 30</span><span" in lab_y and ">background</span>" in lab_y and "Lab Y repriced &lt;its&gt; round" in ev
    assert ">Dec 30, 2025</span>" in ev and ">SEC filing</a>" in ev and ">undated</span> Undated claim" in ev
    fu = body[body.index("Follow-ups from last run"):body.index("Watch next")]
    assert "<b>Worsened</b> · Oracle CDS: 150 bp <span style='color:#777;font-size:12px'>Oct 13</span> <a" in fu
    assert ">Sep 1</span><span" in fu and fu.count(">background</span>") == 1
    assert "Quiet item</li>" in fu  # no news, no URL: no date shown
    assert "- Worsened: Oracle CDS (150 bp) [Oct 13]" in text and "- Still open: Old item (n) [Sep 1 (background)]" in text
    assert "- No news: Quiet item\n" in text
    assert "- AI-lab financing: [Sep 30 (background)] Lab Y repriced <its> round https://news.example/y" in text
    assert "- Hyperscaler filings: [undated] Undated claim https://news.example/z" in text


# ---------------------------------------------------------------- end to end
def test_run_dates_evidence_and_follow_ups_end_to_end(sandbox, monkeypatch):
    for mod in (fetch_fred, fetch_edgar, fetch_taiwan):  # cached data from the sandbox, no network
        orig = mod.run
        monkeypatch.setattr(mod, "run", lambda cfg, *a, _o=orig, **k: _o(cfg, *a, **{**k, "offline": True}))
    monkeypatch.setattr(leases, "run", lambda *a, **k: {"companies": {}, "by_quarter": {}, "errors": {}})
    monkeypatch.setattr(util, "session", lambda ua=None: FakeSession(ROUTES))
    monkeypatch.setattr(sources, "public_host", lambda host, resolver=None: True)
    monkeypatch.setattr(models, "_client", lambda: (_ for _ in ()).throw(ConnectionError("no network in tests")))
    monkeypatch.setattr(util, "now_utc", lambda: datetime(2026, 10, 17, 1, 17, tzinfo=timezone.utc))
    sent = []
    monkeypatch.setattr(notify, "send", lambda report: sent.append(json.loads(json.dumps(report, default=str))) or "sent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("RUN_TRIGGER", "schedule")
    prev = _run_report("2026-10-10T01:17:00Z")
    prev["claude"]["assessment"]["watch_next"] = ["Oracle CDS and bond deals"]
    publish.archive_run(prev)

    ev = lambda claim, url, d: {"claim": claim, "url": url, "event_date": d}  # noqa: E731
    assessment = {
        "news_scores": {
            "ai_lab_financing": {"score": 1, "summary": "Labs.", "evidence": [
                ev("Lab X delayed a $10 billion round (Reuters)", "https://www.reuters.com/lab", "2026-10-14"),
                ev("Utility withdrew 5 GW of requests", "https://news.example/blocked", "2026-09-30")]},
            "supplier_signals": {"score": 0, "summary": "Suppliers.", "evidence": [
                ev("Nvidia: Blackwell demand 3 times supply", "https://investor.nvidia.com/q", None),
                ev("Broadcom orders slowed 12%", "https://news.example/blocked", "2026-13-01")]},
            "physical_buildout": {"score": 0, "summary": "Build-out.", "evidence": [
                ev("Project Alpha 2 GW campus cancelled", "https://finance.yahoo.com/dc1", "2026-10-20")]}},
        "credit_news_adjustment": {"delta": 0, "reason": ""}, "headline": "h", "digest_markdown": "d",
        "follow_ups": [{"item": "Oracle CDS and bond deals", "status": "worsened",
                        "note": "Oracle 5-year CDS widened to 150 basis points", "url": ORCL, "event_date": "2026-10-13"}],
        "watch_next": []}
    notes = f"Oracle CDS 150 bp — 2026-10-13 — {ORCL}\nLab X — 2026-10-14 — https://www.reuters.com/lab"
    fake = fake_claude(monkeypatch, [_research(notes), reply(assessment), reply(assessment)])
    assert run.main([]) == 0
    assert "major: reuters.com\n" in fake.calls[1]["messages"][0]["content"]

    latest = json.loads((sandbox / "site" / "data" / "latest.json").read_text())
    got = {e["claim"][:12]: (e["event_date"], e["age_days"], e["background"])
           for s in latest["scores"].values() for e in s.get("evidence") or []}
    assert got == {"Lab X delaye": ("2026-10-14", 3, False), "Utility with": ("2026-09-30", 17, True),
                   "Nvidia: Blac": (None, None, False), "Broadcom ord": (None, None, False),
                   "Project Alph": (None, None, False)}  # a month 13 and a future date are dropped
    fu = latest["follow_ups"][0]
    assert (fu["status"], fu["event_date"], fu["age_days"], fu["background"]) == ("worsened", "2026-10-13", 4, False)
    assert fu["source"]["tier"] == "major" and fu["source"]["status"] == "verified"
    assert latest["scores"]["ai_lab_financing"]["score"] == 1  # dates are labels: scores unchanged
    body = notify.build_email(sent[0], None)[1]
    assert ">Oct 14</span> Lab X delayed" in body and ">Sep 30</span><span" in body and ">undated</span> Nvidia" in body
