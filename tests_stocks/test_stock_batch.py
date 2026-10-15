"""Batch runs (plan -> ticker jobs -> collect), near-live quotes and the scored-on-close note.
HTTP, SMTP and Claude are mocked."""
import csv
import email
import json
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

import pytest

import stock_fakes as F
from cew import sources, util
from stocks import batch, config, notify, prices, publish, run

CFG = config.load()
PAGES = {"https://www.reuters.com/a": "Microsoft guided revenue to $122 billion."}
EVIDENCE = {"guidance": [{"claim": "Microsoft guided revenue to $122 billion", "url": "https://www.reuters.com/a",
                          "event_date": (util.today() - timedelta(days=3)).isoformat()}]}


def claude_run():
    return [F.guidance_reply(), F.research_reply("Notes https://www.reuters.com/a", ["https://www.reuters.com/a"]),
            F.topics_reply({"guidance": 1}, EVIDENCE), F.writeup_reply()]


def world(monkeypatch, routes=None, key="k-SECRET-KEY", claude=None):
    sess = F.FakeSession({**F.sec_routes(), **F.price_routes(), **(routes or {})})
    monkeypatch.setattr(util, "session", lambda ua=None: sess)
    monkeypatch.setattr(sources, "make_fetcher", lambda *a, **k: lambda u: (PAGES[u], None) if u in PAGES else (None, "HTTP 404"))
    if key:
        monkeypatch.setenv("STOCK_PRICE_API_KEY", key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    util.write_csv(util.DATA / "fred" / "DGS10.csv", [{"date": "2026-10-08", "value": "4.5"}], ["date", "value"])
    fake = F.fake_claude(monkeypatch, claude if claude is not None else claude_run())
    return sess, fake


def shared_benchmarks(path):
    for t, base, drift in (("SPY", 400, 0.0003), ("QQQ", 350, 0.00035)):
        rows = [{"date": x["date"][:10], "close": x["close"], "adj_close": x["adjClose"], "split_factor": 1.0}
                for x in F.tiingo_rows(base, drift)]
        util.write_csv(path / f"{t}.csv", rows, prices.FIELDS)
    return path


class FakeSMTP:
    sent = []

    def __init__(self, host, port, context=None, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        pass

    def sendmail(self, frm, to, msg):
        FakeSMTP.sent.append(msg)


def smtp(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    for k, v in {"ALERT_EMAIL_TO": "me@x.com", "SMTP_USERNAME": "bot@x.com", "SMTP_PASSWORD": "pw"}.items():
        monkeypatch.setenv(k, v)
    return FakeSMTP.sent


def subject_of(msg):
    return str(make_header(decode_header(email.message_from_string(msg)["Subject"])))


def text_of(msg):
    part = next(p for p in email.message_from_string(msg).walk() if p.get_content_type() == "text/plain")
    return part.get_payload(decode=True).decode("utf-8")


# ---------------------------------------------------------------- plan
def test_plan_validates_dedupes_and_caps_the_batch():
    assert batch.parse("msft, NVDA msft,,brk.b\nTIC") == (["MSFT", "NVDA", "BRK.B", "TIC"], [])
    assert batch.plan("msft,nvda,MSFT", CFG) == {"tickers": ["MSFT", "NVDA"], "error": None}
    assert "Invalid ticker(s): 1BAD, $X" in batch.plan("MSFT,1BAD,$X", CFG)["error"]
    assert batch.plan(" , ", CFG)["error"] == "No ticker given"
    assert batch.plan(",".join(f"T{i}" for i in range(16)), CFG)["error"] == "16 tickers; a batch takes at most 15"
    assert len(batch.plan(",".join(f"T{i}" for i in range(15)), CFG)["tickers"]) == 15


def test_plan_step_writes_the_matrix_and_fetches_the_benchmarks_once(sandbox, monkeypatch, tmp_path, capsys):
    sess, _ = world(monkeypatch, claude=[])
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setenv("INPUT_TICKERS", "msft, nvda,msft")
    shared = tmp_path / "shared"
    assert batch.main(["plan", "--shared", str(shared)]) == 0
    assert out_file.read_text().splitlines() == ['tickers=["MSFT", "NVDA"]', "count=2", "max_parallel=5"]
    assert [c["url"] for c in sess.calls] == [F.tiingo_url("SPY"), F.tiingo_url("QQQ")]
    assert all(c["headers"]["Authorization"] == "Token k-SECRET-KEY" for c in sess.calls)
    assert prices.read_rows(shared / "SPY.csv")[-1]["date"] == "2026-10-09" and (shared / "QQQ.csv").exists()
    monkeypatch.setenv("INPUT_TICKERS", "MSFT;rm -rf /")
    assert batch.main(["plan", "--shared", str(shared)]) == 2
    assert "::error::Invalid ticker(s)" in capsys.readouterr().out


# ---------------------------------------------------------------- a ticker's job
def test_ticker_job_writes_its_result_and_cache_files_and_publishes_nothing(sandbox, monkeypatch, tmp_path):
    sess, fake = world(monkeypatch)
    out, shared = tmp_path / "out", shared_benchmarks(tmp_path / "shared")
    assert run.main(["MSFT", "--batch-out", str(out), "--shared", str(shared)]) == 0
    res = json.loads((out / "result" / "MSFT.json").read_text())
    assert res["status"] == "ok" and res["ticker"] == "MSFT" and len(res["chart"]) > 1000
    r = res["report"]
    assert r["rating"] and r["factors"]["trend"]["metrics"]["rs_spy_pp"]["value"] is not None  # shared SPY used
    assert "quote" not in r and "email" not in r and len(fake.calls) == 4
    assert [c["url"] for c in sess.calls if "tiingo" in c["url"]] == [F.tiingo_url("MSFT")]  # no SPY/QQQ, no quotes
    staged = out / "data" / "stocks"
    assert (staged / "fundamentals" / "MSFT.json").exists() and (staged / "prices" / "MSFT.csv").exists()
    assert len(list((staged / "calls" / "MSFT").glob("*.json"))) == 2
    assert not (util.DATA / "stocks" / "runs").exists() and not publish.ratings_csv().exists()
    assert not (util.SITE_DATA / "stocks").exists()


def test_a_failed_ticker_job_records_its_error_without_secrets(sandbox, monkeypatch, tmp_path):
    world(monkeypatch, claude=[])
    out = tmp_path / "out"
    assert run.main(["ZZZZ", "--batch-out", str(out), "--shared", str(tmp_path)]) == 2
    res = json.loads((out / "result" / "ZZZZ.json").read_text())
    assert res["ticker"] == "ZZZZ" and res["status"] == "failed" and res["error"].startswith("Ticker ZZZZ is not in SEC's company_tickers.json")
    assert set(res) == {"ticker", "status", "error"}

    def boom(*a, **k):
        raise RuntimeError("upstream said: bad key k-SECRET-KEY")
    monkeypatch.setattr(run, "research_one", boom)
    assert run.main(["NVDA", "--batch-out", str(out)]) == 1
    res = json.loads((out / "result" / "NVDA.json").read_text())
    assert res["status"] == "failed" and res["error"] == "RuntimeError: upstream said: bad key ***"
    with pytest.raises(RuntimeError):  # outside a batch an unexpected error still surfaces
        run.main(["NVDA"])


# ---------------------------------------------------------------- collect
def ticker_results(monkeypatch, tmp_path):
    """out/ as the download step leaves it: MSFT and NVDA rated, ZZZZ failed, AMD's job left nothing."""
    world(monkeypatch)
    out, shared = tmp_path / "out", shared_benchmarks(tmp_path / "shared")
    monkeypatch.setattr(util, "now_utc", lambda: datetime(2026, 10, 14, 21, 0, 57, tzinfo=timezone.utc))
    assert run.main(["MSFT", "--batch-out", str(out), "--shared", str(shared)]) == 0
    res = json.loads((out / "result" / "MSFT.json").read_text())
    nv = json.loads(json.dumps(res))
    nv["ticker"] = nv["report"]["ticker"] = "NVDA"
    nv["report"].update(run_id="2026-10-14T21:01:30Z", name="NVIDIA CORP", rating="Hold", score=55)
    util.write_json(out / "result" / "NVDA.json", nv)
    batch.write_result(out, "ZZZZ", {"status": "failed", "error": "Ticker ZZZZ is not in SEC's company_tickers.json"})
    for p in (util.DATA / "stocks").rglob("*"):  # the collect job starts from a fresh checkout
        if p.is_file():
            p.unlink()
    return out, shared, res["report"]


def test_collect_writes_once_with_one_quote_request_and_one_email(sandbox, monkeypatch, tmp_path):
    out, shared, msft = ticker_results(monkeypatch, tmp_path)
    publish.append_rating({"timestamp": "2026-09-01T00:00:00Z", "ticker": "AAPL", "rating": "Hold", "score": 50,
                           "confidence": "High", "price": 180, "price_date": "2026-08-29"})
    publish.append_rating({"timestamp": "2026-09-02T00:00:00Z", "ticker": "MSFT", "rating": "Sell", "score": 40,
                           "confidence": "High", "price": 250, "price_date": "2026-08-29"})
    before = publish.ratings_csv().read_bytes()
    util.write_json(util.SITE_DATA / "stocks" / "index.json", {"tickers": [{"ticker": "AAPL", "name": "APPLE INC"},
                                                                          {"ticker": "MSFT", "name": "MICROSOFT CORP"}]})
    quotes = F.as_json([F.iex_row("MSFT", 310.5, 305), F.iex_row("NVDA", 221.3, 228.52), F.iex_row("AAPL", 230.0, 229.0)])
    sess, _ = world(monkeypatch, routes={F.iex_url("MSFT", "NVDA", "AAPL"): quotes}, claude=[])
    sent = smtp(monkeypatch)
    s = batch.collect(out, shared, ["MSFT", "NVDA", "ZZZZ", "AMD"], True, CFG, "123", "https://github.com/o/r/actions/runs/123")
    # one Tiingo request in all: the quotes of every affected and rated ticker
    assert [c["url"] for c in sess.calls] == [F.iex_url("MSFT", "NVDA", "AAPL")]
    assert [(x["ticker"], x["status"]) for x in s["tickers"]] == [("MSFT", "done"), ("NVDA", "done"), ("ZZZZ", "failed"), ("AMD", "failed")]
    assert s["tickers"][2]["error"] == "Ticker ZZZZ is not in SEC's company_tickers.json" and "no result" in s["tickers"][3]["error"]
    assert s["tickers"][0]["previous"] == {"rating": "Sell", "score": 40.0, "timestamp": "2026-09-02T00:00:00Z"}
    assert s["tickers"][1]["previous"] is None and s["done"] == 2 and s["failed"] == 2
    assert s["quotes"] == {"endpoint": "tickers", "requests": 1, "error": None}
    assert s["close_window"].startswith("Run at 5:00 pm ET") and s["tickers"][0]["close_note"] == "today's close not yet published; scored on 2026-10-09"
    # one email for the batch
    assert len(sent) == 1 and s["email"] == "sent to 1 recipient(s)"
    assert subject_of(sent[0]).startswith(f"[Stock] Batch of 4: MSFT {msft['rating']} {msft['score']}, NVDA Hold 55 (2 failed)")
    # append-only files: earlier rows untouched, new rows in run order with the quote columns at the end
    data = publish.ratings_csv().read_bytes()
    assert data.startswith(before)
    rows = util.read_csv(publish.ratings_csv())
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT", "MSFT", "NVDA"] and list(rows[0])[-3:] == ["quote_price", "quote_as_of", "news_from"]
    assert rows[2]["quote_price"] == "310.5" and rows[3]["quote_as_of"] == "2026-10-14T20:00:00Z" and rows[3]["rating"] == "Hold"
    for t in ("MSFT", "NVDA"):
        files = list((util.DATA / "stocks" / "runs" / t).glob("*.json"))
        assert len(files) == 1
        r = json.loads(files[0].read_text())
        assert r["email"] == "sent to 1 recipient(s)" and r["quote"]["as_of"] == "2026-10-14T20:00:00Z"
        assert r["batch"] == {"run_id": "123", "tickers": ["MSFT", "NVDA", "ZZZZ", "AMD"]}
    assert not (util.DATA / "stocks" / "runs" / "ZZZZ").exists()
    # page files, caches and the batch status file
    idx = {e["ticker"]: e for e in util.read_json(util.SITE_DATA / "stocks" / "index.json")["tickers"]}
    assert idx["AAPL"]["price_now"] == 230.0 and idx["NVDA"]["price_now"] == 221.3 and idx["MSFT"]["runs"] == 2
    assert idx["NVDA"]["price"] == msft["price"]["close"] and idx["NVDA"]["name"] == "NVIDIA CORP"
    detail = util.read_json(util.SITE_DATA / "stocks" / "MSFT.json")
    assert [h["rating"] for h in detail["history"]] == [msft["rating"], "Sell"] and len(detail["chart"]) > 1000
    assert util.read_json(util.SITE_DATA / "stocks" / "batch.json")["tickers"][2]["status"] == "failed"
    assert (util.DATA / "stocks" / "fundamentals" / "MSFT.json").exists() and len(list((util.DATA / "stocks" / "calls" / "MSFT").glob("*"))) == 2
    assert prices.read_cache("SPY")[-1]["date"] == "2026-10-09"
    assert batch.commit_message(s) == f"Stock research MSFT, NVDA {util.today().isoformat()} (2 failed: ZZZZ, AMD)"


def test_collect_of_one_ticker_sends_the_full_email_and_handles_email_off(sandbox, monkeypatch, tmp_path):
    out, shared, msft = ticker_results(monkeypatch, tmp_path)
    world(monkeypatch, routes={F.iex_url("MSFT"): F.as_json([F.iex_row("MSFT", 310.5, 305)])}, claude=[])
    sent = smtp(monkeypatch)
    s = batch.collect(out, shared, ["MSFT"], True, CFG)
    assert len(sent) == 1 and subject_of(sent[0]).startswith(f"[Stock] MSFT {msft['rating']} {msft['score']}/100")
    assert "Latest quote 310.50 at 4:00 pm ET on 2026-10-14 (display only, not scored)" in text_of(sent[0])
    assert "Scored on the close of 2026-10-09" in text_of(sent[0])
    sent.clear()
    s = batch.collect(out, shared, ["MSFT", "NVDA"], False, CFG)
    assert not sent and s["email"] == "skipped (email off)"
    assert len(list((util.DATA / "stocks" / "runs" / "MSFT").glob("*.json"))) == 2  # a second archive file, never overwritten


def test_collect_main_reads_the_plan_from_env(sandbox, monkeypatch, tmp_path, capsys):
    out, shared, _ = ticker_results(monkeypatch, tmp_path)
    world(monkeypatch, routes={F.iex_url("MSFT", "NVDA"): F.as_json([])}, claude=[])
    monkeypatch.setenv("PLAN_TICKERS", '["MSFT", "NVDA", "ZZZZ"]')
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    msg = tmp_path / "msg.txt"
    assert batch.main(["collect", "--out", str(out), "--shared", str(shared), "--message-file", str(msg)]) == 0
    assert msg.read_text().startswith("Stock research MSFT, NVDA") and "(1 failed: ZZZZ)" in msg.read_text()
    printed = capsys.readouterr().out
    assert "::warning::ZZZZ failed" in printed
    b = util.read_json(util.SITE_DATA / "stocks" / "batch.json")
    assert b["run_id"] == "555" and b["run_url"] == "https://github.com/o/r/actions/runs/555" and b["email"] == "skipped (email off)"
    monkeypatch.setenv("PLAN_TICKERS", "not json")
    assert batch.main(["collect", "--out", str(out)]) == 2


def test_batch_email_lists_each_rating_its_close_and_the_failures():
    b = {"run_url": "https://github.com/o/r/actions/runs/1", "cost_usd": 0.91,
         "close_window": "Run at 5:00 pm ET, between the 4:00 pm close and about 6:00 pm ET",
         "tickers": [{"ticker": "TIC", "status": "done", "rating": "Sell", "score": 38, "confidence": "High",
                      "previous": {"rating": "Hold", "score": 47.0}, "price": 9.12, "scored_on": "2026-10-13",
                      "quote": {"price": 8.68, "as_of": "2026-10-14T20:00:00Z"}, "cost_usd": 0.38,
                      "close_note": "today's close not yet published; scored on 2026-10-13"},
                     {"ticker": "BAD", "status": "failed", "error": "BAD is not in SEC's company_tickers.json"}]}
    subject, body_html, text = notify.build_batch_email(b, "https://dash.example")
    assert subject == "[Stock] Batch of 2: TIC Sell 38 (1 failed)"
    assert "https://dash.example/stocks.html#TIC" in body_html and "Hold 47" in body_html and "8.68 at 4:00 pm ET on 2026-10-14" in body_html
    assert "BAD is not in SEC" in body_html and "Not investment advice" in body_html and "$0.91" in body_html
    assert ("TIC: Sell 38/100 (High), before: Hold 47; scored on the close of 2026-10-13 (today's close not yet published; "
            "scored on 2026-10-13); latest quote 8.68 at 4:00 pm ET on 2026-10-14") in text
    assert "BAD: FAILED: BAD is not in SEC" in text and "Run at 5:00 pm ET" in text


# ---------------------------------------------------------------- quotes (Tiingo IEX, one request)
ROWS = [F.iex_row("TIC", 8.68, 9.12), F.iex_row("BRK-B", 480.1, 479.0, "2026-10-14T19:59:00.123+00:00"),
        F.iex_row("NODATA", None), F.iex_row("AAPL", 230.0)]


def test_quotes_use_one_request_with_the_key_in_a_header():
    sess = F.FakeSession({F.iex_url("TIC", "BRK.B", "NODATA"): F.as_json(ROWS)})
    q = prices.quotes(["TIC", "BRK.B", "NODATA", "TIC"], "SECRETKEY", sess)
    assert q == {"quotes": {"TIC": {"price": 8.68, "as_of": "2026-10-14T20:00:00Z", "prev_close": 9.12},
                            "BRK.B": {"price": 480.1, "as_of": "2026-10-14T19:59:00Z", "prev_close": 479.0}},
                 "endpoint": "tickers", "requests": 1, "error": None}
    assert [c["url"] for c in sess.calls] == ["https://api.tiingo.com/iex/?tickers=tic,brk-b,nodata"]
    assert sess.calls[0]["headers"]["Authorization"] == "Token SECRETKEY"
    assert "SECRETKEY" not in sess.calls[0]["url"] and not sess.calls[0]["params"]


def test_quotes_fall_back_to_the_all_tickers_endpoint_once():
    sess = F.FakeSession({F.iex_url("TIC"): F.FakeResponse(404, "no"), F.IEX_ALL_URL: F.as_json(ROWS)})
    q = prices.quotes(["TIC"], "k", sess)
    assert q["quotes"] == {"TIC": {"price": 8.68, "as_of": "2026-10-14T20:00:00Z", "prev_close": 9.12}}
    assert q["endpoint"] == "all" and q["requests"] == 2 and q["error"] is None
    assert [c["url"] for c in sess.calls] == [F.iex_url("TIC"), F.IEX_ALL_URL]
    sess = F.FakeSession({F.iex_url("TIC"): F.as_json({"detail": "odd"}), F.IEX_ALL_URL: F.as_json(ROWS)})
    assert prices.quotes(["TIC"], "k", sess)["endpoint"] == "all"  # not a list: the other form
    many = [f"T{i}" for i in range(101)]
    sess = F.FakeSession({F.IEX_ALL_URL: F.as_json(ROWS)})
    q = prices.quotes(many + ["TIC"], "k", sess)
    assert [c["url"] for c in sess.calls] == [F.IEX_ALL_URL] and q["quotes"]["TIC"]["price"] == 8.68  # over 100: all directly


@pytest.mark.parametrize("resp,msg", [(F.FakeResponse(401, "no"), "rejected the key (HTTP 401)"),
                                      (F.FakeResponse(429, "slow"), "rate limit")])
def test_quote_key_or_rate_limit_errors_do_not_retry(resp, msg):
    sess = F.FakeSession({F.iex_url("TIC"): resp, F.IEX_ALL_URL: F.as_json(ROWS)})
    q = prices.quotes(["TIC"], "SECRETKEY", sess)
    assert msg in q["error"] and q["quotes"] == {} and len(sess.calls) == 1


def test_quote_errors_never_repeat_the_key():
    detail = F.as_json({"detail": "Invalid token SECRETKEY"})
    sess = F.FakeSession({F.iex_url("TIC"): detail, F.IEX_ALL_URL: detail})
    q = prices.quotes(["TIC"], "SECRETKEY", sess)
    assert q["error"] == "quote API: Invalid token ***" and q["requests"] == 2
    sess = F.FakeSession({})
    assert prices.quotes(["TIC"], None, sess) == {"quotes": {}, "endpoint": None, "requests": 0, "error": "STOCK_PRICE_API_KEY not set"}
    assert prices.quotes([], "k", sess)["requests"] == 0 and not sess.calls


# ---------------------------------------------------------------- which close a run was scored on
def at(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_close_note_flags_a_close_not_yet_published_and_the_after_close_window():
    q = {"price": 8.68, "as_of": "2026-10-14T20:00:00Z", "prev_close": 9.12}
    n = prices.close_note("2026-10-13", q, at("2026-10-14T21:00:57Z"))  # 5:00 pm ET on a Wednesday
    assert n["note"] == "today's close not yet published; scored on 2026-10-13" and n["latest_from"] == "quote"
    assert n["close_window"].startswith("Run at 5:00 pm ET, between the 4:00 pm close and about 6:00 pm ET")
    assert n["run_et"] == "2026-10-14 17:00" and n["scored_on"] == "2026-10-13"
    no_quote = prices.close_note("2026-10-13", None, at("2026-10-14T21:00:57Z"))
    assert no_quote["note"] == n["note"] and no_quote["latest"] == "2026-10-14" and no_quote["latest_from"] == "weekday"
    assert prices.close_note("2026-10-14", q, at("2026-10-15T01:00:00Z"))["note"] is None  # 9 pm ET: close in
    sun = prices.close_note("2026-10-16", None, at("2026-10-18T15:00:00Z"))
    assert sun["note"] is None and sun["latest"] == "2026-10-16" and sun["close_window"] is None
    morning = prices.close_note("2026-10-14", None, at("2026-10-15T13:00:00Z"))  # 9 am ET: today's close can't exist yet
    assert morning["note"] == "today's close not yet published; scored on 2026-10-14" and morning["close_window"] is None
    assert prices.close_note(None, None, at("2026-10-14T21:00:00Z"))["note"] is None


def test_single_email_says_which_close_was_scored():
    r = {"ticker": "TIC", "name": "TIC Solutions", "rating": "Sell", "score": 38, "confidence": "High", "run_id": "2026-10-14T21:00:57Z",
         "factors": {}, "composite": {"available": []}, "price": {"close": 9.12, "date": "2026-10-13"},
         "quote": {"price": 8.68, "as_of": "2026-10-14T20:00:00Z", "prev_close": 9.12}}
    publish.attach_quote(r, r["quote"])
    _, body_html, text = notify.build_email(r, None)
    assert ("Scored on the close of 2026-10-13 (9.12); today's close not yet published; scored on 2026-10-13") in text
    assert "Latest quote 8.68 at 4:00 pm ET on 2026-10-14 (display only, not scored)" in body_html
    assert "Run at 5:00 pm ET" in text


def test_latest_price_prefers_the_newer_and_a_quote_on_the_same_day():
    pick = publish._latest_price
    assert pick((9.12, "2026-10-13", None), (8.68, "2026-10-14", "2026-10-14T20:00:00Z"))["price_now"] == 8.68
    assert pick((8.70, "2026-10-14", None), (8.68, "2026-10-14", "2026-10-14T20:00:00Z"))["price_now"] == 8.68
    assert pick((8.70, "2026-10-15", None), (8.68, "2026-10-14", "2026-10-14T20:00:00Z"))["price_now"] == 8.70
    assert pick((None, None, None))["price_now"] is None


def test_quote_columns_extend_an_old_ratings_header_without_touching_rows(sandbox):
    path = publish.ratings_csv()
    path.parent.mkdir(parents=True)
    old = publish.RATING_FIELDS[:publish.RATING_FIELDS.index("overlay") + 1]
    path.write_text(",".join(old) + "\n" + ",".join(["2026-10-01T00:00:00Z", "AAPL", "Hold"] + [""] * (len(old) - 3)) + "\n")
    body = path.read_bytes().split(b"\n", 1)[1]
    publish.append_rating({"timestamp": "2026-10-02T00:00:00Z", "ticker": "MSFT", "rating": "Buy", "quote_price": 1.5,
                           "quote_as_of": "2026-10-02T20:00:00Z"})
    head, rest = path.read_bytes().split(b"\n", 1)
    assert head.decode().endswith(",overlay,quote_price,quote_as_of,news_from") and rest.startswith(body)
    rows = list(csv.DictReader(path.open()))
    assert rows[0]["quote_price"] in ("", None) and rows[1]["quote_price"] == "1.5"


def test_a_rescore_batch_says_so_in_its_commit_message():
    rows = [{"ticker": "ORCL", "status": "done", "news_from": "2026-10-15T07:18:37Z"}, {"ticker": "ZZZZ", "status": "failed"}]
    assert batch.commit_message({"tickers": rows}) == f"Stock research (rescore) ORCL {util.today().isoformat()} (1 failed: ZZZZ)"
