"""Stock data layer: ticker resolution, SEC fundamentals, prices, the earnings release, price-based metrics.
All HTTP and Claude calls are mocked."""
import json
from datetime import date

import pytest

import stock_fakes as F
from cew import util
from stocks import config, fundamentals as fund, guidance, metrics, prices, resolve

CFG = config.load()


# ---------------------------------------------------------------- ticker -> CIK
def test_lookup_resolves_and_accepts_dot_or_dash_share_classes():
    sess = F.FakeSession(F.sec_routes())
    assert resolve.lookup(sess, "MSFT") == {"ticker": "MSFT", "sec_ticker": "MSFT", "cik": "0000789019", "name": "MICROSOFT CORP"}
    assert resolve.lookup(sess, "BRK.B")["cik"] == "0001067983"
    assert resolve.lookup(sess, "BRK.B")["sec_ticker"] == "BRK-B"


def test_lookup_fails_clearly_for_unknown_tickers():
    with pytest.raises(resolve.TickerNotFound, match="ZZZZ is not in SEC's company_tickers.json"):
        resolve.lookup(F.FakeSession(F.sec_routes()), "ZZZZ")


def test_ticker_validation():
    ok = {"msft": "MSFT", " brk.b ": "BRK.B", "BF-B": "BF-B", "A": "A", "ABCDEFGHIJ": "ABCDEFGHIJ"}
    assert {k: config.normalize_ticker(k) for k in ok} == ok
    for bad in ("", None, "1ABC", ".A", "ABCDEFGHIJK", "MS FT", "MSFT;rm", "$X", "A/B"):
        assert config.normalize_ticker(bad) is None


# ---------------------------------------------------------------- SEC companyfacts
@pytest.fixture(scope="module")
def rec():
    return fund.extract(F.companyfacts(), F.CIK)


def test_quarters_from_ytd_facts_with_q4_from_the_10k(rec):
    rq = rec["flows"]["revenue"]["quarters"]
    q4 = rq["2026-06-30"]
    assert q4["derived"] and q4["val"] == pytest.approx(F.revenue(date(2026, 6, 30)))
    assert q4["filed"] == "2026-08-29"                        # known once the 10-K was filed
    assert rq["2026-03-31"] == {"val": pytest.approx(F.revenue(date(2026, 3, 31))), "filed": "2026-05-05", "derived": False}
    ocf = rec["flows"]["operating_cash_flow"]["quarters"]     # cash flow: year-to-date facts only
    assert ocf["2025-12-31"]["derived"] and ocf["2025-12-31"]["val"] == pytest.approx(round(F.revenue(date(2025, 12, 31)) * 0.45, -6))
    assert ocf["2026-03-31"]["filed"] == "2026-05-05"         # comparatives filed a year later don't move it
    assert rec["flows"]["revenue"]["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert rec["taxonomy"] == "us-gaap" and rec["currency"] == "USD" and rec["foreign"] is False


def test_summary_metrics(rec):
    s = fund.summarize(rec)
    assert s["ttm_end"] == "2026-06-30" and s["latest_quarter_end"] == "2026-06-30"
    assert s["revenue_ttm"] == pytest.approx(sum(F.revenue(e) for _, e in F.fiscal_quarters(2026)))
    assert s["revenue_yoy_q"] == pytest.approx(14.75, abs=0.02) and s["revenue_yoy_ttm"] == pytest.approx(14.75, abs=0.02)
    assert s["acceleration_pp"] == pytest.approx(0, abs=0.02)
    assert (s["gross_margin_pct"], s["operating_margin_pct"], s["fcf_margin_pct"], s["sbc_pct_revenue"],
            s["capex_intensity_pct"]) == pytest.approx((69, 44, 30, 4, 15), abs=0.01)
    bs = s["balance_sheet"]
    assert (bs["cash_and_investments"], bs["debt"], bs["net_debt"]) == (80e9, 43e9, -37e9)
    assert bs["debt_tags"] == ["LongTermDebtNoncurrent", "LongTermDebtCurrent"]
    assert s["diluted_shares_end"] == "2026-06-30"            # the 10-K's annual average is the newest count
    assert s["dilution_yoy_pct"] == pytest.approx((0.998 ** 4 - 1) * 100, abs=0.01)
    assert s["shares_outstanding"]["val"] == 7.43e9
    assert [q["end"] for q in s["quarterly"]][-1] == "2026-06-30" and len(s["quarterly"]) == 12


def test_older_periods_fill_in_from_a_previous_revenue_tag():
    old = [e for e in F.flow(F.revenue) if e["end"] < "2022-07-01"]
    new = [e for e in F.flow(F.revenue) if e["end"] >= "2022-07-01" and e["start"] >= "2022-07-01"]
    facts = F.companyfacts(RevenueFromContractWithCustomerExcludingAssessedTax=new, Revenues=old)
    r = fund.extract(facts, F.CIK)["flows"]["revenue"]
    assert r["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert "2021-12-31" in r["quarters"] and "2026-06-30" in r["quarters"]


def test_debt_prefers_the_total_tag_and_counts_missing_debt_as_zero_with_a_note():
    lt = F.instant(lambda e: 45e9)
    s = fund.summarize(fund.extract(F.companyfacts(LongTermDebt=lt, CommercialPaper=F.instant(lambda e: 2e9)), F.CIK))
    assert s["balance_sheet"]["debt"] == 47e9 and s["balance_sheet"]["debt_tags"] == ["LongTermDebt", "CommercialPaper"]
    s = fund.summarize(fund.extract(F.companyfacts(LongTermDebtNoncurrent=None, LongTermDebtCurrent=None), F.CIK))
    assert s["balance_sheet"]["debt"] == 0 and "no debt tagged" in s["balance_sheet"]["debt_note"]


def test_cover_page_share_classes_are_added_up():
    facts = F.companyfacts()
    facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"] = [
        {"end": "2026-07-20", "val": 5.8e9, "accn": "a2", "filed": "2026-07-25", "form": "10-Q"},
        {"end": "2026-07-20", "val": 0.86e9, "accn": "a2", "filed": "2026-07-25", "form": "10-Q"},
        {"end": "2026-04-20", "val": 6.7e9, "accn": "a1", "filed": "2026-04-25", "form": "10-Q"}]
    out = fund.cover_shares(facts)
    assert out["val"] == pytest.approx(6.66e9) and out["classes"] == 2 and out["end"] == "2026-07-20"


def ifrs_facts():
    rows = [{"start": f"{y}-01-01", "end": f"{y}-12-31", "val": 2e12 * 1.3 ** (y - 2021), "accn": f"a{y}",
             "form": "20-F", "filed": f"{y + 1}-04-15"} for y in range(2021, 2026)]
    ocf = [dict(r, val=r["val"] * 0.6) for r in rows]
    capex = [dict(r, val=r["val"] * 0.35) for r in rows]
    return {"entityName": "TAIWAN SEMICONDUCTOR", "facts": {"ifrs-full": {
        "Revenue": {"units": {"TWD": rows}},
        "CashFlowsFromUsedInOperatingActivities": {"units": {"TWD": ocf}},
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": {"units": {"TWD": capex}},
        "CashAndCashEquivalents": {"units": {"TWD": [{"end": "2025-12-31", "val": 2e12, "form": "20-F", "filed": "2026-04-15"}]}}}}}


def test_foreign_ifrs_filer_uses_annual_figures_and_marks_valuation_missing():
    r = fund.extract(ifrs_facts(), "0001046179")
    assert (r["taxonomy"], r["currency"], r["foreign"]) == ("ifrs-full", "TWD", True)
    s = fund.summarize(r)
    assert s["annual_only"] and s["revenue_yoy_ttm"] == pytest.approx(30) and s["acceleration_pp"] == pytest.approx(0)
    assert s["fcf_margin_pct"] == pytest.approx(25) and s["balance_sheet"]["debt"] == 0
    rows = F.tiingo_rows(100, 0.0002)
    rows = [{"date": x["date"][:10], "close": x["close"], "adj_close": x["adjClose"], "split_factor": 1.0} for x in rows]
    assert "foreign filer" in metrics.valuation(s, r, rows, {"value": 4.0, "date": "x"}, CFG)["missing"]


def test_companyfacts_cache_and_offline(sandbox):
    sess = F.FakeSession(F.sec_routes())
    rec = fund.run(sess, "MSFT", F.CIK)
    assert "error" not in rec and fund.cache_path("MSFT").exists()
    assert fund.run(F.FakeSession({}), "MSFT", F.CIK, offline=True)["flows"] == json.loads(json.dumps(rec["flows"]))
    assert "offline and no cached" in fund.run(F.FakeSession({}), "AAPL", "1", offline=True)["error"]
    assert "RuntimeError" in fund.run(F.FakeSession({}), "AAPL", "0000320193")["error"]  # 404 -> error, no crash


# ---------------------------------------------------------------- prices (Tiingo)
def test_prices_send_the_key_in_a_header_never_the_url(sandbox):
    sess = F.FakeSession(F.price_routes())
    out = prices.run("MSFT", CFG, "SECRETKEY", sess=sess, today=date(2026, 10, 14))
    assert out["errors"] == {} and set(out["series"]) == {"MSFT", "SPY", "QQQ"}
    for c in sess.calls:
        assert "SECRETKEY" not in c["url"] and "SECRETKEY" not in json.dumps(c["params"])
        assert c["headers"]["Authorization"] == "Token SECRETKEY"
    assert sess.calls[0]["params"]["startDate"] == "2021-10-04"
    rows = out["series"]["MSFT"]
    assert rows[0]["date"] < rows[-1]["date"] == "2026-10-09" and set(rows[0]) == set(prices.FIELDS)
    assert prices.read_cache("SPY")[-1]["date"] == "2026-10-09"


@pytest.mark.parametrize("resp,msg", [
    (F.FakeResponse(401, "no"), "rejected the key"), (F.FakeResponse(404, "{}"), "no ticker MSFT"),
    (F.FakeResponse(429, "slow down"), "rate limit"), (F.as_json({"detail": "Bad token SECRETKEY"}), "Bad token ***"),
    (F.as_json([]), "no prices"), (F.FakeResponse(503, "x"), "unreachable (HTTP 503)")])
def test_price_failures_are_recorded_without_the_key(sandbox, resp, msg):
    sess = F.FakeSession({**F.price_routes(), F.tiingo_url("MSFT"): resp})
    out = prices.run("MSFT", CFG, "SECRETKEY", sess=sess)
    assert msg in out["errors"]["MSFT"] and "SECRETKEY" not in json.dumps(out["errors"])
    assert "MSFT" not in out["series"] and "SPY" in out["series"]


def test_no_key_marks_prices_missing_without_calling(sandbox):
    sess = F.FakeSession(F.price_routes())
    out = prices.run("MSFT", CFG, None, sess=sess)
    assert out["series"] == {} and set(out["errors"].values()) == {"STOCK_PRICE_API_KEY not set"} and not sess.calls


def test_tiingo_symbols_and_latest_close():
    assert prices.symbol("BRK.B") == "brk-b"
    sess = F.FakeSession(F.price_routes())
    assert prices.latest_close(sess, "SPY", "k") == {"date": "2026-10-09", "close": pytest.approx(400 * 2.718281828 ** (0.0003 * 1309), rel=1e-3)}
    assert prices.latest_close(sess, "NOPE", "k") is None


# ---------------------------------------------------------------- price metrics
def rows_of(raw):
    return [{"date": x["date"][:10], "close": x["close"], "adj_close": x["adjClose"], "split_factor": x["splitFactor"]} for x in raw]


def test_split_adjustment_and_share_units():
    raw = rows_of(F.tiingo_rows(100, 0.0, split=(date(2024, 6, 10), 10.0)))
    adj = dict(metrics.split_adjusted(raw))
    assert raw[0]["close"] == pytest.approx(1000) and adj[raw[0]["date"]] == pytest.approx(100)
    assert adj["2024-06-10"] == pytest.approx(100) and metrics.splits_after(raw, "2024-06-07") == 10
    # a pre-split count (not restated) is scaled up; a restated one is kept
    assert metrics.current_units(1e9, "2024-03-31", raw, 1e10) == 1e10
    assert metrics.current_units(1e10, "2024-03-31", raw, 1e10) == 1e10
    assert metrics.current_units(1e9, "2024-09-30", raw, 1e10) == 1e9


def test_valuation_history_only_uses_filings_already_public():
    rec = fund.extract(F.companyfacts(), F.CIK)
    s = fund.summarize(rec)
    raw = rows_of(F.tiingo_rows(300, 0.0004, wiggle=0.02))
    v = metrics.valuation(s, rec, raw, {"value": 4.5, "date": "2026-10-08"}, CFG)
    hist = {h["date"][:7]: h for h in v["history"]}
    ttm = fund.ttm_points(rec["flows"]["revenue"])
    close = dict((d, c) for d, c in metrics.split_adjusted(raw))
    jul, aug = hist["2026-07"], hist["2026-08"]
    sh_q3 = rec["shares"]["diluted_q"]["2026-03-31"]["val"]
    # July 31: the 10-K (to June 30) isn't filed yet, so the TTM to March 31 and the Q3 share count apply
    assert jul["price_to_sales"] == pytest.approx(close[jul["date"]] * sh_q3 / ttm["2026-03-31"]["val"], rel=1e-3)
    sh_fy = rec["shares"]["diluted_a"]["2026-06-30"]["val"]
    assert aug["price_to_sales"] == pytest.approx(close[aug["date"]] * sh_fy / ttm["2026-06-30"]["val"], rel=1e-3)
    assert v["vs_history"]["price_to_sales"]["points"] >= 59 and v["vs_history"]["price_to_sales"]["percentile"] is not None
    assert v["shares"]["source"] == "cover page shares outstanding"
    assert v["fcf_yield_vs_10y_pp"] == pytest.approx(v["fcf_yield_pct"] - 4.5)
    assert v["price_to_sales"] == pytest.approx(raw[-1]["close"] * 7.43e9 / s["revenue_ttm"], rel=1e-3)
    assert v["enterprise_value"] == pytest.approx(v["market_cap"] + 43e9 - 80e9)


def test_valuation_missing_reasons_and_negative_multiples():
    rec = fund.extract(F.companyfacts(), F.CIK)
    s = fund.summarize(rec)
    raw = rows_of(F.tiingo_rows(300, 0.0004))
    assert metrics.valuation(s, rec, None, None, CFG) == {"missing": "no price data"}
    assert "reported in EUR" in metrics.valuation({**s, "currency": "EUR"}, rec, raw, None, CFG)["missing"]
    v = metrics.valuation({**s, "fcf_ttm": -1e9, "operating_income_ttm": -5e8}, rec, raw, None, CFG)
    assert v["price_to_fcf"] is None and v["fcf_negative"] and v["ev_to_ebit"] is None and v["ebit_negative"]
    assert v["fcf_yield_vs_10y_pp"] is None  # no 10-year yield
    assert metrics.percentile(5, [1, 2, 5, 9]) == 62.5 and metrics.percentile(None, [1]) is None


def test_trend_metrics():
    flat = rows_of(F.tiingo_rows(100, 0.0))
    spy = rows_of(F.tiingo_rows(400, 0.001))
    t = metrics.trend(flat, {"SPY": spy, "QQQ": flat}, CFG)
    assert (t["vs_ma50_pct"], t["vs_ma200_pct"], t["momentum_12_1_pct"], t["drawdown_52w_pct"], t["rs_qqq_pp"]) == (0, 0, 0, 0, 0)
    assert t["rs_spy_pp"] == pytest.approx(-((2.718281828 ** (0.001 * 126)) - 1) * 100, abs=0.05)
    up = rows_of(F.tiingo_rows(100, 0.001))
    t = metrics.trend(up, {}, CFG)
    assert t["vs_ma50_pct"] > 0 and t["vs_ma200_pct"] > t["vs_ma50_pct"] and t["rs_spy_pp"] is None
    assert t["momentum_12_1_pct"] == pytest.approx((2.718281828 ** (0.001 * 231) - 1) * 100, abs=0.05)
    assert metrics.trend(None, {}, CFG) == {"missing": "no price data"}
    ch = metrics.chart(up)
    assert ch[48][2] is None and ch[49][2] is not None and ch[199][3] is not None and len(ch) == len(up)


def test_context_files(sandbox):
    util.write_csv(util.DATA / "fred" / "DGS10.csv", [{"date": "2026-10-07", "value": "4.1"}, {"date": "2026-10-08", "value": "."}],
                   ["date", "value"])
    assert metrics.ust10y() == {"value": 4.1, "date": "2026-10-07"}
    assert metrics.capex_context() is None
    util.write_json(util.SITE_DATA / "latest.json", {"run_id": "2026-10-13T20:12:21Z", "composite": {
        "level": "RED", "total": 10.0, "max": 18.0, "red_confirmed": True}})
    assert metrics.capex_context(today=date(2026, 10, 14)) == {"level": "RED", "total": 10.0, "max": 18.0, "red_confirmed": True,
                                                               "run_id": "2026-10-13T20:12:21Z", "age_days": 1}


# ---------------------------------------------------------------- earnings release
def test_guidance_is_verbatim_checked():
    src = "We expect revenue between $10 billion and $11 billion. Margins fell."
    out = guidance.verify([
        {"metric": "revenue", "period": "Q1", "amount_text": "between $10 billion and $11 billion", "stated_change": "raised",
         "sentence": "We expect revenue between $10 billion and $11 billion."},
        {"metric": "revenue", "period": "Q1", "amount_text": "$12 billion", "stated_change": "raised",
         "sentence": "We expect revenue between $10 billion and $11 billion."},
        {"metric": "eps", "period": "FY", "amount_text": "", "stated_change": "bogus", "sentence": "We expect EPS to rise."},
        {"metric": "weird", "period": "FY", "amount_text": "", "stated_change": "not stated", "sentence": "Margins fell."}], src)
    assert [g["verified"] for g in out] == [True, False, False, True]
    assert out[1]["problem"] == "amount not found in its sentence" and out[2]["problem"] == "sentence not found in the release"
    assert out[2]["stated_change"] == "not stated" and out[3]["metric"] == "other"


def test_latest_release_read_once_and_compared_with_the_previous(sandbox, monkeypatch):
    sess = F.FakeSession(F.sec_routes())
    fake = F.fake_claude(monkeypatch, [F.guidance_reply()])
    usage = {}
    g = guidance.run(sess, F.CIK, "MSFT", "Microsoft", F.submissions(), "claude-opus-5-5", True, False, usage, CFG)
    assert len(fake.calls) == 1 and usage["guidance"]["requests"] == 1
    assert g["filing_date"] == "2026-07-29" and g["period_end"] == "2026-06-30" and g["read"]
    assert [x["sentence"] for x in g["guidance"]] == [F.GUIDE_SENTENCE] and g["dropped"] == 0
    assert g["phrases"]["headwind"] == {"count": 1, "previous": 1, "sentences": [g["phrases"]["headwind"]["sentences"][0]]}
    assert g["phrases"]["pacing"]["count"] == 1 and g["phrases"]["pacing"]["previous"] == 0
    assert g["phrases"]["uncertain"]["count"] == 1  # the forward-looking-statements section isn't counted
    assert g["phrases"]["softness"]["count"] == 1   # text after the next heading is
    prompt = fake.calls[0]["messages"][0]["content"]
    assert F.GUIDE_SENTENCE in prompt and "forward-looking statements that" not in prompt
    assert {p.name for p in guidance.calls_dir("MSFT").glob("*.json")} == {"0000950170-26-000101.json", "0000950170-26-000050.json"}
    n = len(sess.calls)
    again = guidance.run(sess, F.CIK, "MSFT", "Microsoft", F.submissions(), "claude-opus-5-5", True, False, {}, CFG)
    assert again["guidance"] == g["guidance"] and len(sess.calls) == n  # cached: no fetch, no Claude call


def test_safe_harbor_section_is_left_out_of_phrase_counts():
    text = ("Outlook\nWe see macro headwinds.\nFORWARD-LOOKING STATEMENTS:\nStatements here are uncertain.\n"
            "• uncertainties relating to government customers;\n• economic uncertainties; and\nchanges in tax law\n"
            "About Microsoft\nMicrosoft builds software.\nFor more information, contact us.")
    assert guidance.without_boilerplate(text) == ("Outlook\nWe see macro headwinds.\nAbout Microsoft\n"
                                                  "Microsoft builds software.\nFor more information, contact us.")
    assert guidance.without_boilerplate("Cautionary Note Regarding Forward-Looking Statements\nRisky; uncertain.") == ""
    assert guidance.without_boilerplate("Safe Harbor Statement\n• headwinds;\nConsolidated Statements\nok") == "Consolidated Statements\nok"


def test_cached_release_from_an_older_counter_is_recounted_without_claude(sandbox, monkeypatch):
    acc = "0000950170-26-000101"
    url = "https://www.sec.gov/Archives/edgar/data/789019/000095017026000101/ex991.htm"
    old = {"ticker": "MSFT", "accession": acc, "filing_date": "2026-07-29", "period_end": "2026-06-30", "url": url,
           "phrases": {"uncertain": {"count": 9, "sentences": []}}, "guidance": [], "summary": "None given.", "model": "m"}
    util.write_json(guidance.calls_dir("MSFT") / f"{acc}.json", old)
    fake = F.fake_claude(monkeypatch, [])
    g = guidance.run(F.FakeSession(F.sec_routes()), F.CIK, "MSFT", "Microsoft", F.submissions(), "m", True, False, {}, CFG)
    assert not fake.calls and g["phrases"]["uncertain"]["count"] == 1 and g["summary"] == "None given."
    assert util.read_json(guidance.calls_dir("MSFT") / f"{acc}.json")["version"] == guidance.VERSION


def test_misquoted_guidance_dropped_and_claude_off_counts_phrases_only(sandbox, monkeypatch):
    F.fake_claude(monkeypatch, [F.guidance_reply(sentence="We expect revenue to soar to $200 billion.", amount="$200 billion")])
    g = guidance.run(F.FakeSession(F.sec_routes()), F.CIK, "MSFT", "Microsoft", F.submissions(), "m", True, False, {}, CFG)
    assert g["guidance"] == [] and g["dropped"] == 1
    for p in guidance.calls_dir("MSFT").glob("*.json"):
        p.unlink()
    off = guidance.run(F.FakeSession(F.sec_routes()), F.CIK, "MSFT", "Microsoft", F.submissions(), "m", False, False, {}, CFG)
    assert off["read"] is False and off["guidance"] == [] and "Claude step off" in off["note"] and off["phrases"]["pacing"]["count"] == 1


def test_no_earnings_8k_is_an_error_not_a_crash(sandbox):
    subs = F.submissions([{"accessionNumber": "x", "filingDate": "2026-08-29", "reportDate": "2026-06-30", "form": "20-F",
                           "primaryDocument": "a.htm", "items": ""}])
    g = guidance.run(F.FakeSession({}), F.CIK, "TSM", "TSMC", subs, "m", True, False, {}, CFG)
    assert "no 8-K with Item 2.02" in g["error"]
    assert "offline" in guidance.run(None, F.CIK, "TSM", "TSMC", None, "m", True, True, {}, CFG)["error"]
