"""Stock pipeline end to end: Claude steps, source checks, publishing (append-only), email, evaluation.
HTTP and Claude are mocked."""
import copy
import csv
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

import stock_fakes as F
from cew import sources, util
from stocks import config, notify, publish, research, run, scoring

CFG = config.load()
ROOT = util.ROOT
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_stocks  # noqa: E402

RECENT = (util.today() - timedelta(days=3)).isoformat()  # inside the 30-day window whenever the tests run
EVIDENCE = {
    "guidance": [{"claim": "Microsoft guided revenue to $122 billion", "url": "https://www.reuters.com/a", "event_date": RECENT},
                 {"claim": "Microsoft guided revenue to $122 billion", "url": "https://blog.example/b", "event_date": "2026-10-01"}],
    "analyst_actions": [{"claim": "Mizuho cut Microsoft to Neutral", "url": "https://www.cnbc.com/c", "event_date": RECENT}],
}
PAGES = {"https://www.reuters.com/a": "Microsoft guided revenue to $122 billion.",
         "https://blog.example/b": "Something else entirely.",
         "https://www.cnbc.com/c": "Mizuho cut Microsoft to Neutral on Thursday."}


def fetch(url):
    return (PAGES[url], None) if url in PAGES else (None, "HTTP 404")


QUOTES = F.as_json([F.iex_row("MSFT", 310.5, 305.0)])


def setup(monkeypatch, sandbox, claude=None, key="k", llm=True, latest=None, routes=None):
    sess = F.FakeSession({**F.sec_routes(), **F.price_routes(), F.iex_url("MSFT"): QUOTES, **(routes or {})})
    monkeypatch.setattr(util, "session", lambda ua=None: sess)
    monkeypatch.setattr(sources, "make_fetcher", lambda *a, **k: fetch)
    if key:
        monkeypatch.setenv("STOCK_PRICE_API_KEY", key)
    if llm:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    util.write_csv(util.DATA / "fred" / "DGS10.csv", [{"date": "2026-10-08", "value": "4.5"}], ["date", "value"])
    if latest:
        util.write_json(util.SITE_DATA / "latest.json", latest)
    if claude is None:
        claude = [F.guidance_reply(), F.research_reply("Notes https://www.reuters.com/a https://www.cnbc.com/c",
                                                       ["https://www.reuters.com/a"]),
                  F.topics_reply({"guidance": 2, "analyst_actions": -1}, EVIDENCE), F.writeup_reply()]
    fake = F.fake_claude(monkeypatch, claude)
    return sess, fake


def archived(ticker="MSFT"):
    """Oldest first; "<stamp>-2.json" after "<stamp>.json" (as cew.publish.run_files orders them)."""
    def key(p):
        m = re.fullmatch(r"(.+?)(?:-(\d+))?", p.stem)
        return m.group(1), int(m.group(2) or 1)
    return sorted((util.DATA / "stocks" / "runs" / ticker).glob("*.json"), key=key)


def latest_run(ticker="MSFT"):
    return json.loads(archived(ticker)[-1].read_text())


RED = {"run_id": "2026-10-13T20:12:21Z", "composite": {"level": "RED", "total": 10.0, "max": 18.0, "red_confirmed": True}}


def test_full_run(sandbox, monkeypatch, capsys):
    sess, fake = setup(monkeypatch, sandbox)
    assert run.main(["msft"]) == 0
    r = latest_run()
    assert r["ticker"] == "MSFT" and r["cik"] == "0000789019" and r["sector"] == "Services-Prepackaged Software"
    assert r["confidence"] == "High" and set(r["composite"]["available"]) == set(r["factors"])
    assert r["rating"] == r["rating_before_overlay"] and r["score"] == r["composite"]["score"]
    # topics: dated, tiered, checked, capped by checkable sources
    g, a = r["news"]["topics"]["guidance"], r["news"]["topics"]["analyst_actions"]
    assert [e["status"] for e in g["evidence"]] == ["verified", "unverified"] and g["rating"] == 1 and g["rating_before_cap"] == 2
    assert [e["tier"] for e in g["evidence"]] == ["major", "other"] and g["evidence"][1]["event_date"] == "2026-10-01"
    assert a["rating"] == -1 and "cap_note" not in a and a["label"] == "Analyst actions"
    assert r["factors"]["news_risk"]["score"] == round(50 + 0.25 * 25 - 0.15 * 25)
    # Claude calls: guidance, research (8 searches, 30 days), topic ratings, write-up given the code's rating
    assert len(fake.calls) == 4
    rc = fake.calls[1]
    assert rc["tools"][0]["max_uses"] == 8 and "MICROSOFT CORP (MSFT), Services-Prepackaged Software" in rc["messages"][0]["content"]
    assert f"to {util.today().isoformat()} on each topic" in rc["messages"][0]["content"]
    assert "Guides Q1 FY2027" in fake.calls[2]["messages"][0]["content"]
    wp = fake.calls[3]["messages"][0]["content"]
    assert f'"rating": "{r["rating"]}"' in wp and f'"score": {r["score"]}' in wp
    assert "never set or change the rating" in fake.calls[3]["system"]
    assert r["writeup"]["headline"] == "High-quality compounder at a full price"
    # cost from every step
    assert set(r["cost"]["steps"]) == {"guidance", "research", "scoring", "writeup"} and r["cost"]["total_usd"] > 0
    assert r["cost"]["steps"]["research"]["web_searches"] == 3
    # published files
    rows = util.read_csv(publish.ratings_csv())
    assert len(rows) == 1 and rows[0]["rating"] == r["rating"] and rows[0]["growth"] == str(r["factors"]["growth"]["score"])
    assert list(rows[0]) == publish.RATING_FIELDS and rows[0]["price"] == str(r["price"]["close"])
    assert rows[0]["research_model"] == "claude-sonnet-5-5" and rows[0]["cost_usd"] == str(r["cost"]["total_usd"])
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert len(detail["chart"]) > 1000 and "notes" not in detail["claude"]["research"] and len(detail["history"]) == 1
    assert "chart" not in r and r["claude"]["research"]["notes"].startswith("Notes")
    index = util.read_json(util.SITE_DATA / "stocks" / "index.json")
    assert [e["ticker"] for e in index["tickers"]] == ["MSFT"]
    assert index["tickers"][0]["change_pct"] == round((310.5 / r["price"]["close"] - 1) * 100, 2)
    assert index["settings"] == {"stale_after_days": 14, "max_batch": 256, "max_tickers_per_day": 0, "max_rescore_tickers_per_day": 0}
    assert index["recent_cost"] == {"avg_usd": r["cost"]["total_usd"], "runs": 1}
    assert "Not investment advice" in index["disclaimer"] and index["last_run"]["steps"]["research"]["web_searches"] == 3
    assert r["email"] == "skipped (email off)"
    printed = capsys.readouterr().out
    summary = json.loads(printed[printed.index("{\n"):])
    assert summary["rating"] == r["rating"] and summary["factors"] == {k: v["score"] for k, v in r["factors"].items()}


def test_weak_sources_cap_topics_and_show_on_page_data_and_email(sandbox, monkeypatch):
    ev = {"product_competitive": [{"claim": "Microsoft launched Copilot Max at $99", "url": "https://blog.example/p", "event_date": RECENT},
                                  {"claim": "Microsoft launched Copilot Max at $99", "url": "https://forum.example/q", "event_date": None}],
          "insider_activity": [{"claim": "Satya Nadella sold 10,000 shares", "url": "https://gone.example/x", "event_date": None}],
          "guidance": EVIDENCE["guidance"][:1]}
    pages = {**PAGES, "https://blog.example/p": "Microsoft launched Copilot Max at $99 a month.",
             "https://forum.example/q": "Microsoft launched Copilot Max at $99."}
    _, fake = setup(monkeypatch, sandbox, claude=[
        F.guidance_reply(), F.research_reply("n"),
        F.topics_reply({"product_competitive": 2, "insider_activity": -1, "guidance": 1}, ev), F.writeup_reply()])
    monkeypatch.setattr(sources, "make_fetcher", lambda *a, **k: lambda u: (pages[u], None) if u in pages else (None, "HTTP 404"))
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    t = r["news"]["topics"]
    pc, ia, g = t["product_competitive"], t["insider_activity"], t["guidance"]
    assert [e["status"] for e in pc["evidence"]] == ["verified", "verified"] and {e["tier"] for e in pc["evidence"]} == {"other"}
    # only the dated blog post counts toward the limits: the count cap and the quality cap both give +1
    assert pc["rating"] == 1 and pc["rating_before_cap"] == 2
    assert pc["cap_note"] == ("Capped at +1 (was +2): 1 of 2 sources verified or unreachable and dated within the last 30 "
                              "days (1 more checked out but is older or undated)")
    assert pc["weak_sources_note"].startswith("Capped at +1 (was +2): a rating above ±1 needs 1 verified primary- or major-tier")
    assert ia["evidence"][0]["status"] == "unreachable" and ia["rating"] == 0 and "cap_note" not in ia
    assert ia["weak_sources_note"] == "Capped at 0 (was -1): only other-tier sources, none verified"
    assert g["rating"] == 1 and "weak_sources_note" not in g
    assert r["factors"]["news_risk"]["score"] == round(50 + 0.20 * 25 + 0.25 * 25)
    # Claude is told the rule (from the config); the write-up sees the cap
    assert scoring.source_quality_rule(CFG) in fake.calls[2]["system"] and "{quality_rule}" not in fake.calls[2]["system"]
    assert "only other-tier sources, none verified" in fake.calls[3]["messages"][0]["content"]
    # the stocks page reads it from the site JSON; the email shows it
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert detail["news"]["topics"]["insider_activity"]["weak_sources_note"] == ia["weak_sources_note"]
    _, body_html, text = notify.build_email(r, None)
    assert body_html.count(scoring.WEAK_SOURCES) == 2 and "only other-tier sources, none verified" in body_html
    assert "Product and competitive news: +1, capped: weak sources (Capped at +1 (was +2)" in text
    assert "Insider activity: +0, capped: weak sources" in text and "Guidance changes: +1, capped" not in text


def test_only_older_evidence_caps_a_topic_at_zero_and_shows_on_page_data_and_email(sandbox, monkeypatch):
    d41, d45 = ((util.today() - timedelta(days=n)).isoformat() for n in (41, 45))
    ev = {"insider_activity": [{"claim": "Satya Nadella sold 10,000 shares", "url": "https://www.sec.gov/f4a", "event_date": d41},
                               {"claim": "Satya Nadella sold 10,000 shares", "url": "https://www.sec.gov/f4b", "event_date": d45}],
          "product_competitive": [{"claim": "Microsoft launched Copilot Max at $99", "url": "https://www.reuters.com/p", "event_date": None}],
          "guidance": EVIDENCE["guidance"][:1]}
    pages = {**PAGES, "https://www.sec.gov/f4a": "Satya Nadella sold 10,000 shares.", "https://www.sec.gov/f4b": "Satya Nadella sold 10,000 shares.",
             "https://www.reuters.com/p": "Microsoft launched Copilot Max at $99 a month."}
    _, fake = setup(monkeypatch, sandbox, claude=[
        F.guidance_reply(), F.research_reply("n"),
        F.topics_reply({"insider_activity": -1, "product_competitive": 1, "guidance": 1}, ev), F.writeup_reply()])
    monkeypatch.setattr(sources, "make_fetcher", lambda *a, **k: lambda u: (pages[u], None) if u in pages else (None, "HTTP 404"))
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    ia, pc, g = (r["news"]["topics"][k] for k in ("insider_activity", "product_competitive", "guidance"))
    assert [(e["status"], e["tier"], e["background"]) for e in ia["evidence"]] == [("verified", "primary", True)] * 2
    assert ia["rating"] == 0 and ia["rating_before_cap"] == -1 and "cap_note" not in ia and "weak_sources_note" not in ia
    assert ia["old_evidence_note"] == ("Capped at 0 (was -1): no source that checked out is dated within the last 30 days; "
                                       f"of the 2 that did, 2 dated more than 30 days before the run ({d45}, {d41})")
    assert pc["rating"] == 0 and pc["old_evidence_note"].endswith("of the 1 that did, 1 undated")
    assert g["rating"] == 1 and "old_evidence_note" not in g  # dated inside the window
    assert r["factors"]["news_risk"]["score"] == round(50 + 0.25 * 25)
    # Claude is told the rule; the write-up sees the cap
    assert "AND are dated within the last 30 days count; undated" in fake.calls[2]["system"] and "{window}" not in fake.calls[2]["system"]
    assert "no source that checked out is dated within the last 30 days" in fake.calls[3]["messages"][0]["content"]
    # the page reads it from the site JSON; the email shows it like the weak-sources cap
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert detail["news"]["topics"]["insider_activity"]["old_evidence_note"] == ia["old_evidence_note"]
    _, body_html, text = notify.build_email(r, None)
    assert body_html.count(scoring.OLD_EVIDENCE) == 2 and f"before the run ({d45}, {d41})" in body_html
    assert "Insider activity: +0, capped: only older evidence (Capped at 0 (was -1)" in text
    assert "Product and competitive news: +0, capped: only older evidence" in text and "Guidance changes: +1, capped" not in text



def test_direction_labels_cap_topics_resting_on_evidence_pointing_the_other_way(sandbox, monkeypatch):
    d40 = (util.today() - timedelta(days=40)).isoformat()
    ev = {"guidance": [  # ORCL-like: the raise is older; the one recent item is a delay (negative)
              {"claim": "Microsoft raised revenue guidance to $122 billion", "url": "https://www.reuters.com/g1",
               "event_date": d40, "direction": "positive"},
              {"claim": "Microsoft's Fairwater site is at risk of delay", "url": "https://www.reuters.com/g2",
               "event_date": RECENT, "direction": "negative"}],
          "analyst_actions": [  # RBRK-like: +1 on a reiterated Sell
              {"claim": "Weiss Ratings reiterated a Sell (D-) rating on Microsoft", "url": "https://blog.example/w",
               "event_date": RECENT, "direction": "negative"}],
          "product_competitive": [{"claim": "Microsoft launched Copilot Max at $99", "url": "https://www.reuters.com/p",
                                   "event_date": RECENT, "direction": "positive"}]}
    pages = {"https://www.reuters.com/g1": "Microsoft raised revenue guidance to $122 billion.",
             "https://www.reuters.com/g2": "Microsoft's Fairwater site is at risk of delay.",
             "https://blog.example/w": "Weiss Ratings reiterated a Sell (D-) rating on Microsoft.",
             "https://www.reuters.com/p": "Microsoft launched Copilot Max at $99 a month."}
    _, fake = setup(monkeypatch, sandbox, claude=[
        F.guidance_reply(), F.research_reply("n"),
        F.topics_reply({"guidance": 1, "analyst_actions": 1, "product_competitive": 1}, ev), F.writeup_reply()])
    monkeypatch.setattr(sources, "make_fetcher", lambda *a, **k: lambda u: (pages[u], None) if u in pages else (None, "HTTP 404"))
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    g, a, pc = (r["news"]["topics"][k] for k in ("guidance", "analyst_actions", "product_competitive"))
    assert [e["direction"] for e in g["evidence"]] == ["positive", "negative"]  # labels kept, as Claude gave them
    assert g["rating"] == 0 and g["rating_before_cap"] == 1 and set(g) & set(scoring.CAP_NOTES) == {"direction_note"}
    assert g["direction_note"] == (f"Capped at 0 (was +1): no positive source that checked out is dated within the last 30 "
                                   f"days; the 1 in the window is negative ({RECENT}); 1 positive one is older, undated or "
                                   "didn't check out")
    assert a["rating"] == 0 and a["direction_note"].startswith("Capped at 0 (was +1): no positive source")
    assert pc["rating"] == 1 and not set(pc) & set(scoring.CAP_NOTES)
    assert r["factors"]["news_risk"]["score"] == round(50 + 0.20 * 25)
    assert "news" not in r or "direction_unchecked" not in r["news"]  # a fresh run checks every direction
    # Claude is told to label each item and why; the write-up sees the cap
    system = fake.calls[2]["system"]
    assert "direction for every evidence item" in system and "a Sell rating (new or reiterated) is negative" in system
    assert "{window}" not in system and "dated within the last 30 days; with none it is capped at 0" in system
    assert "no positive source that checked out" in fake.calls[3]["messages"][0]["content"]
    # the page reads it from the site JSON; the email shows the tag and each item's direction
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert detail["news"]["topics"]["guidance"]["direction_note"] == g["direction_note"]
    assert detail["news"]["topics"]["guidance"]["evidence"][1]["direction"] == "negative"
    _, body_html, text = notify.build_email(r, None)
    assert body_html.count(scoring.NO_DIRECTION) == 2 and "· negative</span>" in body_html and "· positive</span>" in body_html
    assert "Guidance changes: +0, capped: no recent evidence in this direction (Capped at 0 (was +1)" in text
    assert "Analyst actions: +0, capped: no recent evidence in this direction" in text
    assert "Product and competitive news: +1, capped" not in text
    page = (ROOT / "site" / "stocks.html").read_text()
    for note, tag in scoring.CAP_TAGS:  # the page's tags are the module's
        assert f'["{note}", "{tag}"]' in page


def cheap_below(monkeypatch, share):
    """config.load() with valuation.history_label.cheap_below = share (the synthetic MSFT sits at ~40%)."""
    cfg = copy.deepcopy(CFG)
    cfg["valuation"]["history_label"]["cheap_below"] = share
    monkeypatch.setattr(config, "load", lambda: cfg)


def note(headline="Microsoft rates on code factors", thesis="Growth is steady.", risks=("AI capex digestion",)):
    return F.reply({"headline": headline, "thesis": thesis, "key_risks": list(risks), "what_would_change": ["Faster growth"]})


BAD = "Valuation is stretched against its own history, more expensive than at most of the last 61 month-ends."
GOOD = "P/FCF is cheaper than at 37 of the last 61 month-ends."


def brief_of(call):
    text = call["messages"][0]["content"]
    return json.loads(text[text.index("```json\n") + 8:text.rindex("\n```")])


def test_write_up_gets_valuation_history_in_words_and_a_conflict_is_regenerated_once(sandbox, monkeypatch):
    cheap_below(monkeypatch, 45)
    _, fake = setup(monkeypatch, sandbox, claude=[
        F.guidance_reply(), F.research_reply("n"), F.topics_reply(), note(thesis=f"Growth is steady. {BAD}"),
        note(thesis=f"Growth is steady. {GOOD}")])
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    hv = r["valuation"]["vs_own_history"]
    assert hv["label"] == "cheap" and hv["multiples"]["price_to_fcf"] == {"label": "cheap", "phrase": GOOD[9:-1]}
    assert [r["valuation"]["vs_history"]["price_to_fcf"][k] for k in ("points", "cheaper_points", "dearer_points")] == [61, 24, 37]
    # the write-up input: the phrases and label, no percentile anywhere; the absolute inputs labelled absolute
    v = brief_of(fake.calls[3])["factors"]["valuation"]
    assert set(v["inputs"]) == {"price_to_sales", "price_to_fcf", "ev_to_ebit", "fcf_yield_vs_10y_pp"}
    assert "percentile" not in json.dumps(v) and v["inputs_are"].startswith("absolute levels today")
    assert v["vs_own_history"]["label"] == "cheap" and v["vs_own_history"]["overall"] == "cheap against its own history"
    assert v["vs_own_history"]["multiples"]["P/FCF"] == {"label": "cheap", "phrase": GOOD[9:-1]}
    assert "word for word as given" in fake.calls[3]["system"] and "Never restate it as a percentile" in fake.calls[3]["system"]
    # one regeneration, naming the conflict, with the first draft as the assistant turn
    assert len(fake.calls) == 5
    msgs = fake.calls[4]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"] and BAD in msgs[1]["content"]
    assert f'"{BAD}" calls valuation expensive against its own history, but code labels it cheap' in msgs[2]["content"]
    assert r["writeup"]["thesis"] == f"Growth is steady. {GOOD}"
    c = r["writeup_check"]
    assert c["history_label"] == "cheap" and c["regenerated"] and c["dropped"] == [] and c["conflicts"][0]["sentence"] == BAD
    assert c["note"] == ("Write-up check: 1 sentence of the first draft called valuation expensive against its own history, "
                         "but code labels it cheap; regenerated once and the new draft passed.")
    assert len(r["cost"]["steps"]["writeup"]["per_request"]) == 2  # both drafts are paid for



def test_write_up_numbers_are_rounded_in_its_input_not_in_the_stored_data(sandbox, monkeypatch):
    _, fake = setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    brief = brief_of(fake.calls[3])
    days = r["trend"]["rs_days"]
    rounded = phrased = 0
    for k, f in brief["factors"].items():
        for m, v in f["inputs"].items():
            stored = r["factors"][k]["metrics"][m]["value"]
            if m in run.SIGNED and stored is not None:  # a signed change: words plus its size, no signed number
                assert v == run.brief_value(m, stored, days) and isinstance(v, str), (k, m)
                assert f"{abs(round(stored, 1)):.1f}" in v or v == run.SIGNED[m][2].format(days=days), (k, m)
                assert not re.search(r"-\d", v), (k, m, v)
                phrased += 1
            elif m in run.ONE_DECIMAL and stored is not None:
                assert v == round(stored, 1), (k, m)
                rounded += v != stored
            else:
                assert v == stored, (k, m)  # balance-sheet ratios and news ratings as they are
    assert phrased == 11 and rounded >= 3  # the synthetic company's margins and multiples carry more decimals
    assert set(run.SIGNED) <= set(run.ONE_DECIMAL)
    assert set(run.ONE_DECIMAL) >= {"price_to_sales", "price_to_fcf", "ev_to_ebit", "fcf_yield_vs_10y_pp", "revenue_yoy_q",
                                     "acceleration_pp", "operating_margin_pct", "rs_spy_pp", "drawdown_52w_pct"}
    assert "net_debt_to_fcf" not in run.ONE_DECIMAL and brief["price"] == r["price"]  # dollar amounts as now
    stored = [m["value"] for f in r["factors"].values() for m in (f.get("metrics") or {}).values()
              if isinstance(m.get("value"), float)]
    assert any(round(v, 1) != v for v in stored)  # the archive keeps full precision
    assert run.brief_value("price_to_sales", 8.4523) == 8.5 and run.brief_value("operating_margin_pct", -18.349) == -18.3
    assert run.brief_value("price_to_fcf", None) is None and run.brief_value("cash_to_debt", 0.34) == 0.34
    system = fake.calls[3]["system"]
    assert "multiples to one decimal (8.5x), percentages and\n  percentage points to one decimal" in system
    assert "Keep that precision" in system
    assert "Changes and gaps come with their subject and a direction word" in system and "never put a minus sign on it" in system
    assert 'never write "of up" or "of\n  down"' in system and '"TTM revenue growth of 12.2%"' in system


@pytest.mark.parametrize("key, value, words", [
    ("acceleration_pp", -1.1, "growth slowed by 1.1 pp from the previous quarter"),       # RBRK's "slowed by -1.1 pp"
    ("acceleration_pp", 8.98, "growth sped up by 9.0 pp from the previous quarter"),
    ("acceleration_pp", -0.04, "growth unchanged from the previous quarter"),               # rounds to zero
    ("fcf_yield_vs_10y_pp", -3.94, "FCF yield 3.9 pp below the 10-year Treasury yield"),   # RBRK's "-3.9 pp below"
    ("fcf_yield_vs_10y_pp", -16.168, "FCF yield 16.2 pp below the 10-year Treasury yield"),
    ("fcf_yield_vs_10y_pp", 2.06, "FCF yield 2.1 pp above the 10-year Treasury yield"),
    ("fcf_yield_vs_10y_pp", 0.0, "FCF yield level with the 10-year Treasury yield"),
    ("revenue_yoy_ttm", 12.24, "TTM revenue growth of 12.2%"),                            # NTAP's "of up 12.2%"
    ("revenue_yoy_q", 37.92, "latest-quarter revenue growth of 37.9% year on year"),
    ("revenue_yoy_ttm", -12.349, "a TTM revenue decline of 12.3%"),
    ("revenue_yoy_q", 0.03, "latest-quarter revenue flat year on year"),
    ("dilution_yoy_pct", 5.8, "diluted share count up 5.8% year on year"),
    ("dilution_yoy_pct", -1.24, "diluted share count down 1.2% year on year"),
    ("vs_ma50_pct", -10.35, "price 10.3% below its 50-day average"),
    ("vs_ma200_pct", 54.72, "price 54.7% above its 200-day average"),
    ("momentum_12_1_pct", -53.99, "shares down 54.0% from 12 months ago to 1 month ago"),
    ("rs_spy_pp", -33.08, "shares 33.1 pp behind SPY over the last 126 trading days"),
    ("rs_qqq_pp", 4.0, "shares 4.0 pp ahead of QQQ over the last 126 trading days"),
    ("drawdown_52w_pct", -58.29, "price 58.3% below its 52-week high"),
    ("drawdown_52w_pct", 0.0, "price at its 52-week high"),
])
def test_signed_changes_reach_the_write_up_as_words_and_a_size(key, value, words):
    assert run.brief_value(key, value) == words
    assert not re.search(r"-\d", words)


def test_no_phrase_starts_with_a_bare_direction_word():
    # "TTM revenue growth of" + "up 12.2%" gave NTAP's "growth of up 12.2%": every phrase has its own subject
    for key, forms in run.SIGNED.items():
        for form in forms:
            words = form.format(x="1.0", days=126).split()
            assert words[0] not in ("up", "down", "above", "below", "ahead", "behind", "flat"), (key, form)
            assert not re.search(r"-\d", form.format(x="1.0", days=126))
    assert run.brief_value("revenue_yoy_ttm", 12.2).startswith("TTM revenue growth of 12.2%")
    assert run.brief_value("acceleration_pp", -1.1) == "growth slowed by 1.1 pp from the previous quarter"  # a change: up/down


def test_signed_changes_use_the_runs_relative_strength_window():
    assert run.brief_value("rs_spy_pp", 12.0, 63) == "shares 12.0 pp ahead of SPY over the last 63 trading days"
    r = {"factors": {k: {"score": 50, "metrics": {}} for k in scoring.FACTORS}, "trend": {"rs_days": 63},
         "ticker": "X", "name": "X", "rating": "Hold", "score": 50, "confidence": "High", "bands": {},
         "rating_before_overlay": "Hold", "composite": {}}
    r["factors"]["trend"]["metrics"] = {"rs_qqq_pp": {"value": -7.26}, "vs_ma200_pct": {"value": None}}
    inputs = run.writeup_brief(r)["factors"]["trend"]["inputs"]
    assert inputs == {"rs_qqq_pp": "shares 7.3 pp behind QQQ over the last 63 trading days", "vs_ma200_pct": None}


def test_a_conflict_left_after_the_rewrite_is_dropped_and_noted_in_run_details(sandbox, monkeypatch):
    cheap_below(monkeypatch, 45)
    rich = "Shares look rich versus their five-year history."
    setup(monkeypatch, sandbox, claude=[
        F.guidance_reply(), F.research_reply("n"), F.topics_reply(), note(thesis=BAD),
        note(headline="Microsoft looks expensive against its own history", thesis=f"Growth is steady. {BAD}",
             risks=("AI capex digestion", rich))])
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    w, c = r["writeup"], r["writeup_check"]
    assert w["headline"] == "" and w["thesis"] == "Growth is steady." and w["key_risks"] == ["AI capex digestion"]
    assert c["regenerated"] and [x["field"] for x in c["dropped"]] == ["headline", "thesis", "key_risks"]
    assert c["note"].endswith(f'regenerated once; 3 sentences still in conflict were dropped: "Microsoft looks expensive '
                              f'against its own history" / "{BAD}" / "{rich}".')
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert detail["writeup_check"]["note"] == c["note"]  # the page's Run details
    subject, body_html, text = notify.build_email(r, None)
    assert subject.endswith("— MICROSOFT CORP rated " + r["rating"]) and c["note"] in text
    assert "3 sentences still in conflict were dropped" in body_html


def test_a_failed_rewrite_drops_the_conflicting_sentences_of_the_first_draft(sandbox, monkeypatch):
    cheap_below(monkeypatch, 45)
    setup(monkeypatch, sandbox, claude=[F.guidance_reply(), F.research_reply("n"), F.topics_reply(),
                                        note(thesis=f"{BAD} Growth is steady."), RuntimeError("overloaded")])
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    assert r["writeup"]["thesis"] == "Growth is steady." and not r["writeup_check"]["regenerated"]
    assert "regenerating failed (RuntimeError: overloaded); 1 sentence still in conflict was dropped" in r["writeup_check"]["note"]
    assert "claude:writeup" not in r["errors"]


def test_a_multiple_not_comparable_with_its_history_reaches_the_write_up_as_that_phrase(sandbox, monkeypatch):
    # INOD-like: capex above operating cash flow until 2024, so FCF was negative in most month-ends
    capex = F.flow(lambda e: round(F.revenue(e) * (0.6 if e.year < 2024 else 0.15), -6), True)
    facts = F.companyfacts(PaymentsToAcquirePropertyPlantAndEquipment=capex)
    wrong = "P/FCF is cheaper than at 50 of the last 61 month-ends."
    _, fake = setup(monkeypatch, sandbox, routes=F.sec_routes(facts=facts), claude=[
        F.guidance_reply(), F.research_reply("n"), F.topics_reply(), note(thesis=f"Growth is steady. {wrong}"),
        note(thesis="Growth is steady.")])
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    h = r["valuation"]["vs_history"]["price_to_fcf"]
    n, m = (int(x) for x in re.fullmatch(r"negative in (\d+) of (\d+) month-ends", h["not_comparable"]).groups())
    assert n == h["negative_points"] and m == h["points"] and n / m > 0.2 and h["percentile"] is None
    phrase = f"not comparable (negative in {n} of {m} month-ends)"
    assert r["factors"]["valuation"]["metrics"]["price_to_fcf_percentile"] == {"value": None, "score": None, "weight": 0.0, "note": phrase}
    assert r["valuation"]["vs_own_history"]["text"].endswith("(P/FCF not comparable)")
    v = brief_of(fake.calls[3])["factors"]["valuation"]["vs_own_history"]
    assert v["multiples"]["P/FCF"] == {"label": "not comparable", "phrase": phrase} and "percentile" not in json.dumps(v)
    assert 'labelled "not comparable"' in fake.calls[3]["system"]
    # calling it cheap against its history is a conflict: rewritten once
    assert len(fake.calls) == 5 and r["writeup"]["thesis"] == "Growth is steady."
    assert r["writeup_check"]["conflicts"][0]["label"] == "not comparable"
    assert f'- P/FCF: not comparable, "{phrase}"' in fake.calls[4]["messages"][2]["content"]
    assert not [x for x in r["confidence_detail"]["reasons"] if "P/FCF vs history" in x["text"]]
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert detail["valuation"]["vs_history"]["price_to_fcf"]["not_comparable"] == h["not_comparable"]


def test_a_middle_label_is_not_checked(sandbox, monkeypatch):
    _, fake = setup(monkeypatch, sandbox, claude=[F.guidance_reply(), F.research_reply("n"), F.topics_reply(), note(thesis=BAD)])
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    assert r["valuation"]["vs_own_history"]["label"] == "middle" and len(fake.calls) == 4
    assert r["writeup"]["thesis"] == BAD and r["writeup_check"]["note"] is None


def test_overlay_caps_ai_exposed_ticker_when_capex_is_red_confirmed(sandbox, monkeypatch):
    setup(monkeypatch, sandbox, latest=RED, claude=[F.guidance_reply(), F.research_reply("n"),
                                                    F.topics_reply({"guidance": 2, "product_competitive": 2},
                                                                   {"guidance": EVIDENCE["guidance"][:1] * 2,
                                                                    "product_competitive": EVIDENCE["guidance"][:1] * 2}),
                                                    F.writeup_reply()])
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    assert r["rating_before_overlay"] in ("Buy", "Strong Buy") and r["rating"] == "Hold"
    assert r["overlay"]["capped"] and r["overlay"]["note"].startswith(f"Capped at Hold (was {r['rating_before_overlay']})")
    assert util.read_csv(publish.ratings_csv())[0]["overlay"] == f"capped from {r['rating_before_overlay']}"


def test_no_price_key_marks_price_factors_missing(sandbox, monkeypatch):
    sess, _ = setup(monkeypatch, sandbox, key=None)
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    assert r["factors"]["valuation"]["missing"] == "no price data" and r["factors"]["trend"]["missing"] == "no price data"
    assert r["confidence"] == "Medium" and r["price"] == {} and r["errors"]["prices:MSFT"] == "STOCK_PRICE_API_KEY not set"
    assert not [c for c in sess.calls if "tiingo" in c["url"]]
    assert util.read_csv(publish.ratings_csv())[0]["price"] == ""


def test_no_llm_run_rates_on_code_factors_only(sandbox, monkeypatch):
    _, fake = setup(monkeypatch, sandbox, llm=False, claude=[])
    assert run.main(["MSFT", "--no-llm"]) == 0
    r = latest_run()
    assert not fake.calls and r["factors"]["news_risk"]["score"] is None and "Claude step off" in r["factors"]["news_risk"]["missing"]
    assert r["confidence"] == "Medium" and r["cost"] is None and "writeup" not in r
    assert r["guidance"]["read"] is False
    row = util.read_csv(publish.ratings_csv())[0]
    assert row["news_risk"] == "" and row["research_model"] == "" and row["cost_usd"] == ""


def test_claude_failures_leave_a_rated_run(sandbox, monkeypatch):
    setup(monkeypatch, sandbox, claude=[RuntimeError("boom"), RuntimeError("search down"), RuntimeError("no json"),
                                        RuntimeError("nope")])
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    assert "guidance" in r["errors"] and "claude:research" in r["errors"] and "topic ratings failed" in r["errors"]["claude"]
    assert "claude:writeup" in r["errors"] and r["factors"]["news_risk"]["score"] is None and r["confidence"] == "Medium"


def test_unknown_or_invalid_ticker_fails_clearly_and_writes_nothing(sandbox, monkeypatch, capsys):
    setup(monkeypatch, sandbox, claude=[])
    assert run.main(["ZZZZ"]) == 2
    assert "ZZZZ is not in SEC's company_tickers.json" in capsys.readouterr().err
    assert run.main(["1BAD"]) == 2 and "Invalid ticker" in capsys.readouterr().err
    monkeypatch.setenv("INPUT_TICKER", "msft;rm -rf")
    assert run.main([]) == 2
    assert not (util.DATA / "stocks" / "runs").exists() and not publish.ratings_csv().exists()


def test_nothing_to_rate_exits_nonzero(sandbox, monkeypatch, capsys):
    routes = {"https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json": F.FakeResponse(404, "x")}
    setup(monkeypatch, sandbox, key=None, llm=False, claude=[], routes=routes)
    assert run.main(["MSFT", "--no-llm"]) == 1
    assert "nothing to rate" in capsys.readouterr().err and not publish.ratings_csv().exists()


def test_runs_are_append_only_and_offline_rescore_works(sandbox, monkeypatch):
    setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    first = archived()[0].read_text()
    first_csv = publish.ratings_csv().read_bytes()
    monkeypatch.setattr(util, "now_utc", lambda: __import__("datetime").datetime.fromisoformat(json.loads(first)["run_id"].replace("Z", "+00:00")))
    F.fake_claude(monkeypatch, [])
    assert run.main(["MSFT", "--offline", "--no-llm"]) == 0  # same second: a "-2" file, the first untouched
    files = archived()
    assert len(files) == 2 and files[1].name.endswith("-2.json") and files[0].read_text() == first
    assert publish.ratings_csv().read_bytes().startswith(first_csv) and len(util.read_csv(publish.ratings_csv())) == 2
    r = json.loads(files[1].read_text())
    assert r["news"]["source_check"] is None and r["factors"]["valuation"]["score"] is not None  # cached prices
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert len(detail["history"]) == 2 and util.read_json(util.SITE_DATA / "stocks" / "index.json")["tickers"][0]["runs"] == 2


def test_new_ratings_column_extends_the_header_and_leaves_rows_alone(sandbox):
    path = publish.ratings_csv()
    path.parent.mkdir(parents=True)
    old_fields = publish.RATING_FIELDS[:-1]
    path.write_text(",".join(old_fields) + "\n" + ",".join(["2026-10-01T00:00:00Z", "AAPL", "Hold"] + [""] * (len(old_fields) - 3)) + "\n")
    body = path.read_bytes().split(b"\n", 1)[1]
    publish.append_rating({"timestamp": "2026-10-02T00:00:00Z", "ticker": "MSFT", "rating": "Buy", "overlay": "note"})
    data = path.read_bytes()
    assert data.split(b"\n", 1)[0].decode() == ",".join(publish.RATING_FIELDS)
    assert data.split(b"\n", 1)[1].startswith(body)
    rows = list(csv.DictReader(path.open()))
    assert rows[0]["overlay"] in ("", None) and rows[1]["overlay"] == "note"


def test_index_keeps_names_and_refreshes_every_rated_ticker_in_one_quote_request(sandbox, monkeypatch):
    quotes = F.as_json([F.iex_row("AAPL", 200.0, 198.0), F.iex_row("MSFT", 310.5, 305.0)])
    sess, _ = setup(monkeypatch, sandbox, claude=[], llm=False, routes={F.iex_url("MSFT", "AAPL"): quotes})
    publish.append_rating({"timestamp": "2026-09-01T00:00:00Z", "ticker": "AAPL", "rating": "Hold", "score": 50,
                           "confidence": "High", "price": 180, "price_date": "2026-08-29"})
    util.write_json(util.SITE_DATA / "stocks" / "index.json", {"tickers": [{"ticker": "AAPL", "name": "APPLE INC"}]})
    assert run.main(["MSFT", "--no-llm"]) == 0
    iex = [c for c in sess.calls if "/iex" in c["url"]]
    assert [c["url"] for c in iex] == [F.iex_url("MSFT", "AAPL")] and iex[0]["headers"]["Authorization"] == "Token k"
    assert not [c for c in sess.calls if c["url"] == F.tiingo_url("AAPL")]  # no per-ticker daily refresh
    idx = {e["ticker"]: e for e in util.read_json(util.SITE_DATA / "stocks" / "index.json")["tickers"]}
    assert idx["AAPL"]["name"] == "APPLE INC" and idx["AAPL"]["price_now"] == pytest.approx(200)
    assert idx["AAPL"]["price_now_date"] == "2026-10-14" and idx["AAPL"]["price_now_as_of"] == "2026-10-14T20:00:00Z"
    assert idx["AAPL"]["change_pct"] == pytest.approx(11.11, abs=0.01) and idx["AAPL"]["quote"]["price"] == 200
    assert idx["MSFT"]["name"] == "MICROSOFT CORP" and list(idx) == ["MSFT", "AAPL"]
    r = latest_run()
    assert r["quote"] == {"price": 310.5, "as_of": "2026-10-14T20:00:00Z", "prev_close": 305.0}
    assert r["factors"]["valuation"]["score"] is not None and r["price"]["date"] == "2026-10-09"  # scored on the close
    assert idx["MSFT"]["price"] == r["price"]["close"] and idx["MSFT"]["price_now"] == 310.5  # quote newer than the close
    row = util.read_csv(publish.ratings_csv())[-1]
    assert row["quote_price"] == "310.5" and row["quote_as_of"] == "2026-10-14T20:00:00Z"
    assert r["close_note"]["note"] == "today's close not yet published; scored on 2026-10-09"


def test_models_come_from_inputs_then_config(monkeypatch):
    sel = config.resolve_models(CFG, {"INPUT_RESEARCH_MODEL": "claude-haiku-5-5", "INPUT_SCORING_MODEL": "bad model; rm"})
    assert (sel["research"], sel["sources"]["research"]) == ("claude-haiku-5-5", "input")
    assert (sel["scoring"], sel["sources"]["scoring"]) == ("claude-opus-5-5", "config")
    assert config.resolve_models({}, {})["research"] == "claude-sonnet-5-5"


def test_research_continues_through_pause_turn(monkeypatch):
    fake = F.fake_claude(monkeypatch, [F.research_reply("part 1 https://a.example/x", ["https://a.example/x"], 2, "pause_turn"),
                                       F.research_reply("part 2", ["https://b.example/y"], 1)])
    usage = __import__("cew").models.new_usage("claude-sonnet-5-5")
    out = research.research("claude-sonnet-5-5", "MSFT", "Microsoft", "Software", date(2026, 10, 14), 30, 8, usage)
    assert out == {"notes": "part 1 https://a.example/x\npart 2", "urls": ["https://a.example/x", "https://b.example/y"], "searches": 3}
    assert usage["requests"] == 2 and len(fake.calls[1]["messages"]) == 2
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "from 2026-09-14 to 2026-10-14" in prompt and "Microsoft (MSFT), Software" in prompt
    for topic in ("Guidance changes", "Analyst actions", "Litigation and regulation", "Customer concentration",
                  "Insider activity", "Product and competitive news"):
        assert topic in prompt


def test_research_cap_leaves_room_for_thinking_and_flags_a_cut_off_reply(monkeypatch):
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "stocks.yaml").read_text())
    assert cfg["research_max_tokens"] == 16000
    fake = F.fake_claude(monkeypatch, [F.research_reply("1. Guidance changes: raised. 2. Analyst act", [], 8, "max_tokens")])
    usage = __import__("cew").models.new_usage("claude-sonnet-5-5")
    out = research.research("claude-sonnet-5-5", "MSFT", "Microsoft", "Software", date(2026, 10, 14), 30, 8, usage)
    assert fake.calls[0]["max_tokens"] == 16000 and out["truncated"]
    assert "output limit" in __import__("cew").analyst.notes_for_model(out)


def test_insider_items_are_dated_by_their_form_4_transaction_date(sandbox, monkeypatch):
    _, fake = setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    research_prompt, topics_system = fake.calls[1]["messages"][0]["content"], fake.calls[2]["system"]
    assert "For SEC Form 4 and other insider\nitems, the event date is the transaction date" in research_prompt
    assert "never leave an insider item undated" in research_prompt
    assert "For a Form 4 or other insider item it is the transaction date" in topics_system and "not the filing date" in topics_system


def test_write_up_risks_are_about_the_company_not_the_screens_data():
    for words in ("key_risks and what_would_change are about the company and its stock",
                  "Leave out caveats about this screen's own data", "news ratings reused from an earlier run",
                  "directions not checked", "a short valuation history"):
        assert words in research.WRITEUP_SYSTEM.replace("\n  ", " "), words


def test_email_quotes_guidance_without_doubled_quote_marks():
    r = {"run_id": "2026-10-15T13:48:51Z", "ticker": "INOD", "name": "INNODATA", "rating": "Hold", "score": 62,
         "confidence": "Medium", "guidance": {"guidance": [{"metric": "revenue", "period": "FY2026",
                                                             "sentence": "\u201cWe are reiterating our full-year 2026 revenue growth guidance."}]}}
    _, body_html, _ = notify.build_email(r, None)
    assert "\u201cWe are reiterating" in body_html and "\u201c\u201c" not in body_html
    assert notify.unquote('"Quoted," he said.') == 'Quoted," he said.' and notify.unquote(None) == ""


def test_confidence_is_labelled_data_confidence_with_what_it_means(sandbox, monkeypatch):
    note = "How complete and clean the inputs are, not the chance the rating is right. The reasons listed lower it."
    assert scoring.CONFIDENCE_NOTE == note
    page = (ROOT / "site" / "stocks.html").read_text()
    assert f'const CONF_NOTE = "{note}";' in page  # the page's hover and note are the module's
    assert page.count('title="${esc(CONF_NOTE)}">Data confidence</th>') == 2  # the list and History
    assert "${esc(D.confidence)} data confidence</span>" in page and '<div class="sub confnote">' in page
    assert ">Confidence<" not in page
    _, fake = setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    r = latest_run()
    assert r["confidence"] == "High"  # the stored field keeps its name
    subject, body_html, text = notify.build_email(r, None)
    assert "(High data confidence)" in subject and "High data confidence (6 of 6 factors had data)" in body_html
    assert "How complete and clean the inputs are, not the chance the rating is right." in body_html
    assert "(High data confidence)" in text and ">Confidence<" not in body_html
    brief = brief_of(fake.calls[3])
    assert brief["data_confidence"] == "High" and "confidence" not in brief
    assert brief["data_confidence_means"] == "how complete and clean the inputs are, not the chance the rating is right"
    assert "call it data confidence, never confidence in the rating" in fake.calls[3]["system"].replace("\n  ", " ")


def test_claude_output_schemas_hold_no_rating_or_score():
    topics = research.TOPICS_SCHEMA["properties"]["topics"]["properties"]
    assert list(topics) == research.TOPIC_KEYS
    assert topics["guidance"]["properties"]["rating"]["enum"] == [-2, -1, 0, 1, 2]
    item = topics["guidance"]["properties"]["evidence"]["items"]
    assert item["required"] == ["claim", "url", "event_date", "direction"]
    assert item["properties"]["direction"] == {"type": "string", "enum": ["positive", "negative", "neutral"]}
    assert list(research.WRITEUP_SCHEMA["properties"]) == ["headline", "thesis", "key_risks", "what_would_change"]
    for word in ("+2", "-2", "two credible sources", "don't re-rate valuation"):
        assert word in research.TOPICS_SYSTEM


def test_email_build_and_send_skipped_without_secrets(sandbox, monkeypatch):
    setup(monkeypatch, sandbox, latest=RED)
    assert run.main(["MSFT", "--email"]) == 0
    r = latest_run()
    assert r["email"].startswith("skipped: ALERT_EMAIL_TO")
    subject, body_html, text = notify.build_email(r, "https://dash.example")
    assert subject.startswith(f"[Stock] MSFT {r['rating']} {r['score']}/100 (High data confidence)")
    assert "Not investment advice" in body_html and "Not investment advice" in text
    assert "Capped at Hold" in body_html and "https://dash.example/stocks.html#MSFT" in body_html
    assert "Microsoft guided revenue to $122 billion" in body_html and "We expect revenue for the first quarter" in body_html


def test_email_sent_over_smtp(sandbox, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            sent["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            sent["login"] = u

        def sendmail(self, frm, to, msg):
            sent["to"], sent["msg"] = to, msg

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    for k, v in {"ALERT_EMAIL_TO": "a@x.com, b@x.com", "SMTP_USERNAME": "me@x.com", "SMTP_PASSWORD": "pw"}.items():
        monkeypatch.setenv(k, v)
    r = {"ticker": "MSFT", "name": "Microsoft", "rating": "Buy", "score": 70, "confidence": "High", "run_id": "2026-10-14T00:00:00Z",
         "factors": {}, "composite": {"available": []}}
    assert notify.send(r) == "sent to 2 recipient(s)"
    assert sent["host"] == ("smtp.gmail.com", 465) and sent["to"] == ["a@x.com", "b@x.com"] and "pw" not in sent["msg"]


# ---------------------------------------------------------------- scripts/evaluate_stocks.py
def test_forward_returns_by_band_and_vs_spy():
    def series(base, daily, start="2026-01-02", n=250):
        from datetime import timedelta
        d0 = date.fromisoformat(start)
        return [{"date": (d0 + timedelta(days=i)).isoformat(), "adj_close": base * (1 + daily) ** i} for i in range(n)]
    data = {"SPY": series(100, 0.0), "AAA": series(10, 0.001), "BBB": series(10, -0.001)}
    ratings = [{"timestamp": "2026-01-05T10:00:00Z", "ticker": "AAA", "rating": "Buy"},
               {"timestamp": "2026-01-05T10:00:00Z", "ticker": "BBB", "rating": "Sell"},
               {"timestamp": "2026-08-30T10:00:00Z", "ticker": "AAA", "rating": "Buy"},  # horizons not reached yet
               {"timestamp": "2026-01-05T10:00:00Z", "ticker": "AAA", "rating": ""}]
    res = evaluate_stocks.evaluate(ratings, load=lambda t: data.get(t, []))
    assert res["Buy"]["1m"] == {"n": 1, "return": pytest.approx(((1.001) ** 30 - 1) * 100, abs=0.01),
                                "vs_spy": pytest.approx(((1.001) ** 30 - 1) * 100, abs=0.01), "beat_spy": 100}
    assert res["Sell"]["6m"]["return"] < 0 and res["Sell"]["6m"]["beat_spy"] == 0 and res["Buy"]["6m"]["n"] == 1
    assert evaluate_stocks.forward_return(data["AAA"], date(2026, 8, 30), 30) is None


def test_evaluate_script_prints_a_table(sandbox, capsys):
    evaluate_stocks.main([])
    assert "No stock ratings yet" in capsys.readouterr().out
    publish.append_rating({"timestamp": "2026-01-05T10:00:00Z", "ticker": "AAA", "rating": "Buy"})
    evaluate_stocks.main([])
    out = capsys.readouterr().out
    assert "Strong Buy" in out and "Not investment advice" in out


# ---------------------------------------------------------------- tiered prices (Haiku 5.5 above 100K prompt tokens)
def test_tiered_prices_charge_each_request_by_its_prompt_size():
    from types import SimpleNamespace as NS
    from cew import models
    prices = models.load_prices()
    assert models.tiers_of(prices, "claude-haiku-5-5") == [{"above_input_tokens": 100000, "input": 0.5, "output": 2.5}]
    assert models.tiers_of(prices, "claude-opus-5-5") == []
    u = models.new_usage("claude-haiku-5-5")
    models.add_request(u, NS(usage=F._usage(150_000, 1000)))   # above 100K: $0.50 / $2.50
    models.add_request(u, NS(usage=F._usage(20_000, 1000)))    # base: $0.10 / $0.50
    cost, high = models.step_cost_tiered(u, prices)
    assert high == 1 and cost == round((150_000 * 0.5 + 1000 * 2.5 + 20_000 * 0.1 + 1000 * 0.5) / 1e6, 4)
    assert models.step_cost(u, prices) == round((170_000 * 0.1 + 2000 * 0.5) / 1e6, 4)  # the capex estimate: unchanged
    # cache reads count toward the prompt size (and are billed at 0.1x the tier's input price)
    u2 = models.new_usage("claude-haiku-5-5")
    models.add_request(u2, NS(usage=NS(input_tokens=10_000, output_tokens=0, cache_creation_input_tokens=0,
                                       cache_read_input_tokens=95_000, server_tool_use=None)))
    assert models.step_cost_tiered(u2, prices) == (round((10_000 + 95_000 * 0.1) * 0.5 / 1e6, 4), 1)
    s = models.cost_summary_tiered({"research": u, "writeup": u2}, prices)
    assert s["steps"]["research"]["tiered_requests"] == 1 and s["total_usd"] == round(cost + s["steps"]["writeup"]["cost_usd"], 4)
    # a model without tiers costs the same either way
    o = models.new_usage("claude-opus-5-5")
    models.add_request(o, NS(usage=F._usage(150_000, 1000)))
    assert models.step_cost_tiered(o, prices) == (models.step_cost(o, prices), 0)


def test_stock_runs_record_every_request_for_tiered_prices(sandbox, monkeypatch):
    setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    steps = latest_run()["cost"]["steps"]
    assert {k: len(v["per_request"]) for k, v in steps.items()} == {"guidance": 1, "research": 1, "scoring": 1, "writeup": 1}
    assert all(v["tiered_requests"] == 0 for v in steps.values())


# ---------------------------------------------------------------- --reuse-news
def test_reuse_news_rescores_with_the_latest_news_and_only_a_new_write_up(sandbox, monkeypatch):
    setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    first = latest_run()

    def no_check(*a, **k):
        raise AssertionError("reused news is not checked again")
    monkeypatch.setattr(sources, "check_evidence", no_check)
    fake = F.fake_claude(monkeypatch, [F.writeup_reply()])  # the write-up only: no research, topics or guidance
    assert run.main(["MSFT", "--reuse-news"]) == 0
    r = latest_run()
    assert len(fake.calls) == 1 and r["writeup"]["headline"] and set(r["cost"]["steps"]) == {"writeup"}
    assert r["news"]["topics"] == first["news"]["topics"] and r["news"]["source_check"] == first["news"]["source_check"]
    assert r["news_from"] == first["run_id"] and r["factors"]["news_risk"]["score"] == first["factors"]["news_risk"]["score"]
    assert f"reused from the run of {first['run_id']}" in fake.calls[0]["messages"][0]["content"]
    assert r["guidance"]["guidance"] == first["guidance"]["guidance"]  # the cached release, no new extraction
    rows = util.read_csv(publish.ratings_csv())
    assert [x["news_from"] for x in rows] == ["", first["run_id"]] and rows[1]["research_model"] == "claude-sonnet-5-5"
    index = util.read_json(util.SITE_DATA / "stocks" / "index.json")
    assert index["recent_cost"] == {"avg_usd": first["cost"]["total_usd"], "runs": 1}  # a re-score isn't a run-cost sample
    assert index["recent_rescore_cost"] == {"avg_usd": r["cost"]["total_usd"], "runs": 1}  # it has its own
    assert list(rows[0])[-1] == "news_from"
    # a reuse of a reuse still points at the original research (here via the workflow's env input)
    monkeypatch.setenv("INPUT_REUSE_NEWS", "true")
    F.fake_claude(monkeypatch, [F.writeup_reply()])
    assert run.main(["MSFT"]) == 0 and latest_run()["news_from"] == first["run_id"]
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert detail["news_from"] == first["run_id"] and detail["history"][0]["news_from"] == first["run_id"]
    _, body_html, text = notify.build_email(latest_run(), None)
    assert f"News &amp; risk reused from the run of {first['run_id']}" in body_html and "News & risk reused" in text


def test_reuse_news_applies_the_code_caps_again_to_claudes_archived_ratings(sandbox, monkeypatch):
    setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    # an archive written before the date cap: Claude's insider -1 on two old Form 4s, left uncapped
    old = latest_run()
    d40, d44 = ((util.today() - timedelta(days=n)).isoformat() for n in (40, 44))
    old["news"]["topics"]["insider_activity"].update(rating=-1, evidence=[
        {"claim": "Satya Nadella sold 10,000 shares", "url": f"https://www.sec.gov/{n}", "event_date": d, "status": "verified",
         "tier": "primary"} for n, d in (("a", d40), ("b", d44))])
    # and one written before only in-window sources counted: +2 on two Reuters items, one of them older
    d5 = (util.today() - timedelta(days=5)).isoformat()
    old["news"]["topics"]["product_competitive"] = {"label": "Product and competitive news", "rating": 2, "summary": "Launch.", "evidence": [
        {"claim": "Microsoft launched Copilot Max", "url": f"https://www.reuters.com/{n}", "event_date": d, "status": "verified",
         "tier": "major"} for n, d in (("a", d5), ("b", d40))]}
    publish.archive_run(old)
    before = {p.name: p.read_bytes() for p in archived()}
    monkeypatch.setattr(sources, "check_evidence", lambda *a, **k: pytest.fail("reused news is not checked again"))
    fake = F.fake_claude(monkeypatch, [F.writeup_reply()])
    assert run.main(["MSFT", "--reuse-news"]) == 0
    r = latest_run()
    ia, g = r["news"]["topics"]["insider_activity"], r["news"]["topics"]["guidance"]
    assert ia["rating"] == 0 and ia["rating_before_cap"] == -1 and ia["old_evidence_note"].startswith("Capped at 0 (was -1)")
    assert [e["background"] for e in ia["evidence"]] == [True, True] and [e["age_days"] for e in ia["evidence"]] == [40, 44]
    assert g == old["news"]["topics"]["guidance"]  # Claude's +2, capped to +1 by the count rule exactly as before
    pc = r["news"]["topics"]["product_competitive"]
    assert pc["rating"] == 1 and pc["rating_before_cap"] == 2 and "old_evidence_note" not in pc
    assert pc["cap_note"].endswith("1 of 2 sources verified or unreachable and dated within the last 30 days "
                                   "(1 more checked out but is older or undated)")
    assert r["news"]["caps_reapplied"] == [{"topic": "insider_activity", "label": "Insider activity", "was": -1, "now": 0},
                                           {"topic": "product_competitive", "label": "Product and competitive news", "was": 2, "now": 1}]
    assert r["factors"]["news_risk"]["score"] == round(50 + 0.25 * 25 - 0.15 * 25 + 0.20 * 25)
    assert r["news"]["source_check"] == old["news"]["source_check"] and len(fake.calls) == 1
    assert {p.name: p.read_bytes() for p in archived()[:-1]} == before  # earlier runs untouched
    _, body_html, text = notify.build_email(r, None)
    assert "Code caps applied again to the reused ratings: Insider activity -1 → 0" in body_html
    assert "Insider activity: +0, capped: only older evidence" in text
    # re-scoring the re-score: Claude's rating is still -1 underneath, the same caps give the same result
    F.fake_claude(monkeypatch, [F.writeup_reply()])
    assert run.main(["MSFT", "--reuse-news"]) == 0
    again = latest_run()
    assert again["news"]["topics"] == r["news"]["topics"] and again["news"]["caps_reapplied"] == []



def test_reuse_news_treats_unlabelled_evidence_as_matching_and_lists_what_a_fresh_run_would_check(sandbox, monkeypatch):
    setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    old = latest_run()  # archived before direction labels: the fakes' evidence has none
    assert not any("direction" in e for t in old["news"]["topics"].values() for e in t["evidence"])
    # one topic of the archive does carry labels, pointing against its rating: the direction cap applies on reuse
    old["news"]["topics"]["product_competitive"] = {"label": "Product and competitive news", "rating": 1, "summary": "Delay.",
                                                   "evidence": [{"claim": "Copilot Max slipped to 2027", "url": "https://www.reuters.com/d",
                                                                 "event_date": RECENT, "status": "verified", "tier": "major",
                                                                 "direction": "negative"}]}
    publish.archive_run(old)
    F.fake_claude(monkeypatch, [F.writeup_reply()])
    assert run.main(["MSFT", "--reuse-news"]) == 0
    r = latest_run()
    t = r["news"]["topics"]
    assert t["guidance"] == old["news"]["topics"]["guidance"] and t["analyst_actions"] == old["news"]["topics"]["analyst_actions"]
    assert t["product_competitive"]["rating"] == 0 and t["product_competitive"]["direction_note"].startswith("Capped at 0 (was +1)")
    assert r["news"]["caps_reapplied"] == [{"topic": "product_competitive", "label": "Product and competitive news", "was": 1, "now": 0}]
    assert r["news"]["direction_unchecked"] == [{"topic": "guidance", "label": "Guidance changes", "rating": 1},
                                                {"topic": "analyst_actions", "label": "Analyst actions", "rating": -1}]
    assert run.summary(r)["direction_unchecked"] == r["news"]["direction_unchecked"]
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert detail["news"]["direction_unchecked"] == r["news"]["direction_unchecked"]
    _, body_html, text = notify.build_email(r, None)
    unchecked = ("Direction not checked for Guidance changes +1, Analyst actions -1: the reused news has no direction labels; "
                 "a fresh run checks it")
    assert unchecked in text and unchecked in body_html
    assert "Product and competitive news: +0, capped: no recent evidence in this direction" in text



def test_reuse_news_retags_tiers_with_the_companys_own_domains(sandbox, monkeypatch):
    setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    old = latest_run()
    # archived before own domains counted: news.microsoft.com tagged "other", so +2 was capped at +1
    ev = [{"claim": "Microsoft launched Copilot Max", "url": "https://news.microsoft.com/copilot-max", "event_date": RECENT,
           "status": "verified", "tier": "other"},
          {"claim": "Microsoft launched Copilot Max", "url": "https://blog.example/copilot", "event_date": RECENT,
           "status": "verified", "tier": "other"}]
    old["news"]["topics"]["product_competitive"] = {"label": "Product and competitive news", "rating": 1, "rating_before_cap": 2,
                                                   "weak_sources_note": "Capped at +1 (was +2): ...", "summary": "Launch.",
                                                   "evidence": ev}
    publish.archive_run(old)
    F.fake_claude(monkeypatch, [F.writeup_reply()])
    assert run.main(["MSFT", "--reuse-news"]) == 0
    pc = latest_run()["news"]["topics"]["product_competitive"]
    assert [e["tier"] for e in pc["evidence"]] == ["primary", "other"]
    assert pc["rating"] == 2 and "weak_sources_note" not in pc
    assert {"topic": "product_competitive", "label": "Product and competitive news", "was": 1, "now": 2} in latest_run()["news"]["caps_reapplied"]


def test_reuse_news_without_an_earlier_run_rates_without_news(sandbox, monkeypatch):
    _, fake = setup(monkeypatch, sandbox, claude=[F.writeup_reply()])
    assert run.main(["MSFT", "--reuse-news"]) == 0
    r = latest_run()
    assert r["factors"]["news_risk"]["missing"] == "--reuse-news: no earlier run of this ticker with news topics"
    assert r["news_from"] is None and len(fake.calls) == 1  # no guidance extraction either: only the write-up
    assert "1 of 6 factors had no data: News & risk" in [x["text"] for x in r["confidence_detail"]["reasons"]]
