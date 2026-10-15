"""Stock pipeline end to end: Claude steps, source checks, publishing (append-only), email, evaluation.
HTTP and Claude are mocked."""
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

import stock_fakes as F
from cew import sources, util
from stocks import config, notify, publish, research, run, scoring

CFG = config.load()
ROOT = util.ROOT
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_stocks  # noqa: E402

EVIDENCE = {
    "guidance": [{"claim": "Microsoft guided revenue to $122 billion", "url": "https://www.reuters.com/a", "event_date": None},
                 {"claim": "Microsoft guided revenue to $122 billion", "url": "https://blog.example/b", "event_date": "2026-10-01"}],
    "analyst_actions": [{"claim": "Mizuho cut Microsoft to Neutral", "url": "https://www.cnbc.com/c", "event_date": "2026-10-02"}],
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
    assert index["settings"] == {"stale_after_days": 14, "max_batch": 15, "max_tickers_per_day": 30}
    assert index["recent_cost"] == {"avg_usd": r["cost"]["total_usd"], "runs": 1}
    assert "Not investment advice" in index["disclaimer"] and index["last_run"]["steps"]["research"]["web_searches"] == 3
    assert r["email"] == "skipped (email off)"
    printed = capsys.readouterr().out
    summary = json.loads(printed[printed.index("{\n"):])
    assert summary["rating"] == r["rating"] and summary["factors"] == {k: v["score"] for k, v in r["factors"].items()}


def test_weak_sources_cap_topics_and_show_on_page_data_and_email(sandbox, monkeypatch):
    ev = {"product_competitive": [{"claim": "Microsoft launched Copilot Max at $99", "url": "https://blog.example/p", "event_date": None},
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
    assert pc["rating"] == 1 and pc["rating_before_cap"] == 2 and "cap_note" not in pc
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


def test_claude_output_schemas_hold_no_rating_or_score():
    topics = research.TOPICS_SCHEMA["properties"]["topics"]["properties"]
    assert list(topics) == research.TOPIC_KEYS
    assert topics["guidance"]["properties"]["rating"]["enum"] == [-2, -1, 0, 1, 2]
    assert topics["guidance"]["properties"]["evidence"]["items"]["required"] == ["claim", "url", "event_date"]
    assert list(research.WRITEUP_SCHEMA["properties"]) == ["headline", "thesis", "key_risks", "what_would_change"]
    for word in ("+2", "-2", "two credible sources", "don't re-rate valuation"):
        assert word in research.TOPICS_SYSTEM


def test_email_build_and_send_skipped_without_secrets(sandbox, monkeypatch):
    setup(monkeypatch, sandbox, latest=RED)
    assert run.main(["MSFT", "--email"]) == 0
    r = latest_run()
    assert r["email"].startswith("skipped: ALERT_EMAIL_TO")
    subject, body_html, text = notify.build_email(r, "https://dash.example")
    assert subject.startswith(f"[Stock] MSFT {r['rating']} {r['score']}/100 (High confidence)")
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
