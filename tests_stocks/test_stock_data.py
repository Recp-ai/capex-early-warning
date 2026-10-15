"""Stock data layer: ticker resolution, SEC fundamentals, prices, the earnings release, price-based metrics.
All HTTP and Claude calls are mocked."""
import copy
import json
from datetime import date

import pytest

import stock_fakes as F
from cew import util
from stocks import config, fundamentals as fund, guidance, metrics, prices, resolve, scoring

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


def test_debt_prefers_the_total_tag_and_counts_no_debt_tags_as_zero_with_a_note():
    lt = F.instant(lambda e: 45e9)
    s = fund.summarize(fund.extract(F.companyfacts(LongTermDebt=lt, CommercialPaper=F.instant(lambda e: 2e9)), F.CIK))
    assert s["balance_sheet"]["debt"] == 47e9 and s["balance_sheet"]["debt_tags"] == ["LongTermDebt", "CommercialPaper"]
    s = fund.summarize(fund.extract(F.companyfacts(LongTermDebtNoncurrent=None, LongTermDebtCurrent=None), F.CIK))
    assert s["balance_sheet"]["debt"] == 0 and s["balance_sheet"]["debt_note"] == "no debt: no debt tag with a value in the last 8 quarters"
    assert s["balance_sheet"]["unknown"] == [] and s["balance_sheet"]["net_debt"] == -80e9


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


def test_tiingo_symbols():
    assert prices.symbol("BRK.B") == "brk-b" and prices.symbol("MSFT") == "msft"


def test_batch_ticker_jobs_read_benchmarks_from_the_shared_folder_never_tiingo(sandbox, tmp_path):
    shared = tmp_path / "shared"
    util.write_csv(shared / "SPY.csv", [{"date": "2026-10-09", "close": 1, "adj_close": 1, "split_factor": 1}], prices.FIELDS)
    sess = F.FakeSession(F.price_routes())
    out = prices.run("MSFT", CFG, "k", sess=sess, shared_dir=shared)
    assert [c["url"] for c in sess.calls] == [F.tiingo_url("MSFT")]
    assert out["series"]["SPY"][0]["close"] == 1 and "MSFT" in out["series"]
    assert out["errors"] == {"QQQ": "no shared benchmark prices from the batch's plan job"}
    assert not prices.cache_path("SPY").exists()  # the collect job copies the shared files into the cache


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


# ---------------------------------------------------------------- balance-sheet tags and the stopped-reporting guard
def bs_of(**over):
    return fund.summarize(fund.extract(F.companyfacts(**over), F.CIK), CFG)["balance_sheet"]


def upto(rows, end):
    return [e for e in rows if e["end"] <= end]


def since(rows, start):
    return [e for e in rows if e["end"] >= start]


NO_LTD = {"LongTermDebtNoncurrent": None, "LongTermDebtCurrent": None}


def test_debt_families_read_notes_convertibles_and_all_current_debt():
    b = bs_of(**NO_LTD, NotesPayableCurrent=F.instant(lambda e: 7.6e9), LongTermNotesAndLoans=F.instant(lambda e: 117.7e9))
    assert b["debt"] == pytest.approx(125.3e9) and b["debt_tags"] == ["LongTermNotesAndLoans", "NotesPayableCurrent"]  # Oracle
    b = bs_of(**NO_LTD, NotesPayableCurrent=F.instant(lambda e: 7e9), LongTermNotesPayable=F.instant(lambda e: 90e9))
    assert b["debt"] == 97e9 and b["debt_tags"] == ["LongTermNotesPayable", "NotesPayableCurrent"]
    assert b["debt_parts"] == [{"tag": "LongTermNotesPayable", "val": 90e9}, {"tag": "NotesPayableCurrent", "val": 7e9}]
    # DebtCurrent holds every current borrowing (the commercial paper too); families add their non-current parts
    b = bs_of(**NO_LTD, DebtCurrent=F.instant(lambda e: 5e9), LongTermNotesPayable=F.instant(lambda e: 90e9),
              ConvertibleLongTermNotesPayable=F.instant(lambda e: 1e9), CommercialPaper=F.instant(lambda e: 2e9))
    assert b["debt"] == 96e9 and b["debt_tags"] == ["DebtCurrent", "LongTermNotesPayable", "ConvertibleLongTermNotesPayable"]
    assert bs_of(DebtLongtermAndShorttermCombinedAmount=F.instant(lambda e: 50e9))["debt"] == 50e9
    b = bs_of(**NO_LTD, LongTermDebt=F.instant(lambda e: 45e9), DebtCurrent=F.instant(lambda e: 4e9),
              ShortTermBorrowings=F.instant(lambda e: 1e9))
    assert b["debt"] == 46e9 and b["debt_tags"] == ["LongTermDebt", "ShortTermBorrowings"]  # LongTermDebt includes current maturities
    b = bs_of(**NO_LTD, OtherLongTermDebtNoncurrent=F.instant(lambda e: 3e9), OtherLongTermDebtCurrent=F.instant(lambda e: 1e9))
    assert b["debt"] == 4e9 and b["unknown"] == []
    # LongTermDebt's current part alone doesn't hide the other families' non-current notes
    b = bs_of(LongTermDebtNoncurrent=None, LongTermNotesPayable=F.instant(lambda e: 90e9))
    assert b["debt"] == 93e9 and b["debt_tags"] == ["LongTermDebtCurrent", "LongTermNotesPayable"]


def test_a_tag_that_stops_reporting_makes_the_figure_unknown_not_zero():
    # investments (NVDA: MarketableSecuritiesCurrent stopped two quarters ago)
    b = bs_of(ShortTermInvestments=None, MarketableSecuritiesCurrent=upto(F.instant(lambda e: 49e9), "2025-12-31"))
    assert b["short_term_investments"] is None and b["cash_and_investments"] is None and b["net_debt"] is None
    assert b["unknown"] == ["short-term investments unknown: MarketableSecuritiesCurrent was $49.00B on 2025-12-31, "
                            "and no short-term investments tag covers 2026-06-30"]
    assert b["debt"] == 43e9  # debt itself is still known
    # debt (RBRK, ZS: the old tag stopped 5 quarters ago): unknown within no_debt_quarters (8)
    b = bs_of(LongTermDebtNoncurrent=upto(F.instant(lambda e: 40e9), "2025-03-31"), LongTermDebtCurrent=None)
    assert b["debt"] is None and b["net_debt"] is None and b["cash_and_investments"] == 80e9
    assert b["unknown"][0].startswith("debt unknown: LongTermDebtNoncurrent was $40.00B on 2025-03-31")
    # ... unless a new tag covers the date (new convertible notes)
    b = bs_of(LongTermDebtNoncurrent=upto(F.instant(lambda e: 40e9), "2025-03-31"), LongTermDebtCurrent=None,
              ConvertibleLongTermNotesPayable=since(F.instant(lambda e: 1.1e9), "2025-06-30"))
    assert b["debt"] == 1.1e9 and b["unknown"] == []
    # "no debt" only when no debt tag had a value in the last 8 quarters, or the last value was zero (repaid)
    b = bs_of(LongTermDebtNoncurrent=upto(F.instant(lambda e: 40e9), "2024-03-31"), LongTermDebtCurrent=None)
    assert b["debt"] == 0 and b["unknown"] == [] and b["debt_note"].startswith("no debt")
    b = bs_of(**NO_LTD, ConvertibleDebtCurrent=upto(F.instant(lambda e: 0 if e.isoformat() >= "2025-12-31" else 9.2e8), "2025-12-31"))
    assert b["debt"] == 0 and b["unknown"] == []
    # Shopify: the notes' total tag stops at $920M, the next quarter's current tag says 0 (matured): no debt
    b = bs_of(**NO_LTD, ConvertibleNotesPayable=upto(F.instant(lambda e: 9.2e8), "2025-09-30"),
              ConvertibleDebtCurrent=since(upto(F.instant(lambda e: 0.0), "2025-12-31"), "2025-12-31"))
    assert b["debt"] == 0 and b["unknown"] == []


def test_cash_and_investments_reported_together_and_tags_the_screen_does_not_read():
    b = bs_of(ShortTermInvestments=None, CashCashEquivalentsAndShortTermInvestments=F.instant(lambda e: 75e9))
    assert b["short_term_investments"] == 45e9 and b["investment_note"] == "CashCashEquivalentsAndShortTermInvestments minus cash"
    # only an unlisted tag carries the figure on the latest balance sheet: unknown, named
    rec = fund.extract(F.companyfacts(**NO_LTD, SubordinatedDebt=F.instant(lambda e: 2e9),
                                      ShortTermInvestments=None, HeldToMaturitySecuritiesFairValueCurrent=F.instant(lambda e: 9e9)), F.CIK)
    assert rec["scan"]["balance_sheet_date"] == "2026-06-30" and rec["scan"]["instants"]["SubordinatedDebt"] == 2e9
    b = fund.summarize(rec, CFG)["balance_sheet"]
    assert b["debt"] is None and b["short_term_investments"] is None
    assert "only tags this screen doesn't read carry a value on 2026-06-30 (SubordinatedDebt $2.00B)" in b["unknown"][1]
    assert "HeldToMaturitySecuritiesFairValueCurrent $9.00B" in b["unknown"][0]
    assert "SubordinatedDebt $2.00B" in fund.tag_report("X", rec, CFG)
    # an unread line bigger than everything read: unknown, not the small part (Oracle before LongTermNotesAndLoans)
    rec = fund.extract(F.companyfacts(**NO_LTD, NotesPayableCurrent=F.instant(lambda e: 7.6e9),
                                      FutureNotesAndLoansPayableNoncurrent=F.instant(lambda e: 117.7e9)), F.CIK)
    b = fund.summarize(rec, CFG)["balance_sheet"]
    assert b["debt"] is None and b["unknown"] == [
        "debt unknown: FutureNotesAndLoansPayableNoncurrent is $117.70B on 2026-06-30, more than the $7.60B the debt "
        "tags read add up to (NotesPayableCurrent)"]
    assert bs_of(**NO_LTD, NotesPayableCurrent=F.instant(lambda e: 7.6e9), NotesAndLoansReceivableNetCurrent=F.instant(
        lambda e: 9e9))["debt"] == 7.6e9  # receivables never count


def test_unknown_cash_or_debt_leaves_the_balance_sheet_and_ev_out_with_a_note():
    rec = fund.extract(F.companyfacts(LongTermDebtNoncurrent=upto(F.instant(lambda e: 40e9), "2025-03-31"), LongTermDebtCurrent=None), F.CIK)
    s = fund.summarize(rec, CFG)
    raw = rows_of(F.tiingo_rows(300, 0.0004))
    v = metrics.valuation(s, rec, raw, {"value": 4.5, "date": "x"}, CFG)
    assert v["enterprise_value"] is None and v["ev_to_ebit"] is None and v["ev_note"].startswith("EV unknown: debt unknown")
    assert v["price_to_sales"] is not None and v["price_to_fcf"] is not None  # market-cap multiples unaffected
    fs = scoring.factors(s, v, {"missing": "no price data"}, CFG)
    assert fs["balance_sheet"]["score"] is None and fs["balance_sheet"]["missing"].startswith("debt unknown")
    assert fs["valuation"]["metrics"]["ev_to_ebit"]["note"].startswith("missing: EV unknown")


# ---------------------------------------------------------------- margins, prepayments, share counts, yields
def summ(**over):
    return fund.summarize(fund.extract(F.companyfacts(**over), F.CIK), CFG)


def test_margins_fall_back_to_revenue_minus_costs_and_say_when_missing():
    cost = F.flow(lambda e: round(F.revenue(e) * 0.31, -6))
    s = summ(GrossProfit=None, CostOfServices=cost)
    assert s["gross_margin_pct"] == pytest.approx(69, abs=0.01) and s["gross_profit_source"] == "revenue - CostOfServices"
    assert summ(GrossProfit=None, DirectOperatingCosts=cost)["gross_profit_source"] == "revenue - DirectOperatingCosts"  # INOD
    s = summ(OperatingIncomeLoss=None, CostsAndExpenses=F.flow(lambda e: round(F.revenue(e) * 0.56, -6)))
    assert s["operating_margin_pct"] == pytest.approx(44, abs=0.01) and s["operating_income_source"] == "revenue - CostsAndExpenses"
    s = summ(OperatingIncomeLoss=None, OperatingExpenses=F.flow(lambda e: round(F.revenue(e) * 0.25, -6)))
    assert s["operating_margin_pct"] == pytest.approx(44, abs=0.01) and s["operating_income_source"] == "gross profit - OperatingExpenses"
    # INOD: GrossProfit tagged until the 10-K, the 2026 quarters only through a cost tag
    s = summ(GrossProfit=[e for e in F.flow(lambda e: round(F.revenue(e) * 0.69, -6)) if e["end"] < "2026-01-01"], CostOfServices=cost)
    assert s["gross_margin_pct"] == pytest.approx(69, abs=0.01) and s["gross_profit_source"] == "GrossProfit; revenue - CostOfServices"
    assert s["quarterly"][-1]["operating_margin_pct"] == pytest.approx(44, abs=0.01)
    # neither: missing, and the factor's inputs say so (no silent 90 on FCF alone)
    s = summ(GrossProfit=None, OperatingIncomeLoss=None)
    assert s["gross_margin_pct"] is None and set(s["missing_inputs"]) == {"gross_margin_pct", "operating_margin_pct"}
    q = scoring.factors(s, {"missing": "x"}, {"missing": "x"}, CFG)["quality"]["metrics"]
    assert q["gross_margin_pct"]["note"] == "missing: no GrossProfit or cost-of-revenue tag covers the TTM to 2026-06-30"
    assert q["operating_margin_pct"]["value"] is None and q["operating_margin_pct"]["note"].startswith("missing: no OperatingIncomeLoss")


def ttm_rev(e):
    """The synthetic filer's TTM revenue to quarter end `e` (unrounded)."""
    return sum(50e9 * 1.035 ** (F.qindex(e) - k) for k in range(4))


def test_prepayments_count_only_the_excess_over_revenue_growth(sandbox):
    # subscription: deferred revenue at 1.4x TTM revenue grows with sales -> no excess, reported FCF (RBRK, ZS)
    s = summ(ContractWithCustomerLiability=F.instant(lambda e: 1.4 * ttm_rev(e)))
    pp = s["prepayments"]
    assert pp["increase"] == pytest.approx(pp["base"] * pp["revenue_growth_pct"] / 100, rel=1e-3) and pp["revenue_growth_pct"] > 10
    assert abs(pp["excess"]) < 1e-3 * pp["increase"] and pp["applies"] is False and s["fcf_basis"] == "reported"
    # the same growth from the cash-flow line: the old rule (44% of operating cash flow) would have applied
    s = summ(ContractWithCustomerLiability=F.instant(lambda e: 1.4 * ttm_rev(e)),
             IncreaseDecreaseInContractWithCustomerLiability=F.flow(lambda e: round(F.revenue(e) * 0.20, -6), True))
    pp = s["prepayments"]
    assert pp["source"] == "cash flow statement: IncreaseDecreaseInContractWithCustomerLiability"
    assert pp["increase"] / s["operating_cash_flow_ttm"] > 0.4 and pp["share_of_ocf"] < 0.25 and not pp["applies"]
    # a jump (ORCL, INOD): $100B of advances on top -> the excess applies, and only the excess is subtracted
    jump = lambda e: 0.1 * ttm_rev(e) + (100e9 if e.isoformat() > "2025-06-30" else 0)  # noqa: E731
    rec = fund.extract(F.companyfacts(ContractWithCustomerLiabilityCurrent=F.instant(jump)), F.CIK)
    s = fund.summarize(rec, CFG)
    pp = s["prepayments"]
    assert pp["source"] == "balance sheet: contract 2025-06-30 to 2026-06-30" and pp["base_date"] == "2025-06-30"
    assert pp["expected"] == pytest.approx(pp["base"] * pp["revenue_growth_pct"] / 100, rel=1e-3)
    assert pp["excess"] == pytest.approx(100e9, rel=1e-3) and pp["applies"]
    assert pp["share_of_ocf"] == pytest.approx(pp["excess"] / s["operating_cash_flow_ttm"], abs=1e-4)
    assert s["fcf_basis"] == "ex-prepayments" and s["fcf_scored_ttm"] == pytest.approx(s["fcf_ttm"] - pp["excess"])
    q = scoring.factors(s, {"missing": "x"}, {"missing": "x"}, CFG)["quality"]["metrics"]["fcf_margin_pct"]
    assert q["value"] == s["fcf_ex_prepayments_margin_pct"] and q["note"] == f"FCF ex-prepayments; reported FCF margin {s['fcf_margin_pct']:.1f}%"
    raw = rows_of(F.tiingo_rows(300, 0.0004))
    v = metrics.valuation(s, rec, raw, {"value": 4.5, "date": "x"}, CFG)
    assert v["fcf_basis"] == "ex-prepayments" and v["price_to_fcf"] == pytest.approx(v["market_cap"] / s["fcf_scored_ttm"], rel=1e-3)
    assert v["price_to_fcf_reported"] == pytest.approx(v["market_cap"] / s["fcf_ttm"], rel=1e-3)
    m = scoring.valuation(v, CFG)
    assert m["price_to_fcf"]["note"].startswith("FCF ex-prepayments; reported P/FCF") and m["fcf_yield_vs_10y_pp"]["note"] == "FCF ex-prepayments"
    assert fs_conf(s, v, "fcf_ex_prepayments")["text"].startswith("excess prepayments: contract liabilities rose $")
    # no balance a year earlier: the excess can't be measured, so nothing is subtracted
    s = summ(IncreaseDecreaseInContractWithCustomerLiability=F.flow(lambda e: round(F.revenue(e) * 0.20, -6), True))
    assert s["prepayments"]["excess"] is None and not s["prepayments"]["applies"] and "can't be measured" in s["prepayments"]["note"]


def ocf_with(fn):
    return F.flow(lambda e: round(F.revenue(e) * fn(e), -6), True)


def test_fcf_spike_guard_scores_normalised_fcf_without_any_tag(sandbox):
    assert summ()["fcf_spike"]["triggered"] is False  # steady FCF: reported
    # a quarter with FCF above its revenue (INOD Q2 2026: $124M on $92M): median margin of 8 quarters x TTM revenue
    rec = fund.extract(F.companyfacts(NetCashProvidedByUsedInOperatingActivities=ocf_with(
        lambda e: 1.4 if e == date(2026, 6, 30) else 0.45)), F.CIK)
    s = fund.summarize(rec, CFG)
    sp = s["fcf_spike"]
    assert sp["triggered"] and sp["reasons"][0].startswith("FCF $") and "in the quarter to 2026-06-30 on revenue of $" in sp["reasons"][0]
    assert sp["median_margin_pct"] == pytest.approx(30, abs=0.01) and sp["quarters"] == 8
    assert s["fcf_basis"] == "normalised" and s["fcf_scored_ttm"] == pytest.approx(0.30 * s["revenue_ttm"], rel=1e-3)
    assert s["fcf_margin_pct"] > 50 and s["fcf_scored_margin_pct"] == pytest.approx(30, abs=0.01)  # both kept
    q = scoring.factors(s, {"missing": "x"}, {"missing": "x"}, CFG)["quality"]["metrics"]["fcf_margin_pct"]
    assert q["value"] == pytest.approx(30, abs=0.01) and q["note"].startswith("normalised FCF; reported FCF margin")
    v = metrics.valuation(s, rec, rows_of(F.tiingo_rows(300, 0.0004)), {"value": 4.5, "date": "x"}, CFG)
    assert v["fcf_basis"] == "normalised" and v["price_to_fcf"] == pytest.approx(v["market_cap"] / s["fcf_scored_ttm"], rel=1e-3)
    assert v["history"][-1]["fcf_yield_pct"] == pytest.approx(v["history"][-1]["sales_yield_pct"] * 0.30, rel=1e-2)
    r = fs_conf(s, v, "fcf_spike")
    assert r["points"] == 1 and r["text"].startswith("FCF spike (cause not in tagged data): FCF $")
    assert "scored on normalised FCF (median quarterly margin 30.0% of the last 8 quarters x TTM revenue)" in r["text"]
    # a jump in the TTM margin: more than 2x a year earlier and above 30%
    s = summ(NetCashProvidedByUsedInOperatingActivities=ocf_with(lambda e: 0.2 if e <= date(2025, 6, 30) else 0.75))
    sp = s["fcf_spike"]
    assert sp["triggered"] and sp["reasons"] == [f"TTM FCF margin {sp['fcf_margin_pct']:.1f}% vs {sp['fcf_margin_year_earlier_pct']:.1f}% a year earlier"]
    assert sp["median_margin_pct"] == pytest.approx(32.5, abs=0.01)
    # thresholds come from the config
    cfg = copy.deepcopy(CFG)
    cfg["fcf_spike"].update(quarter_fcf_to_revenue=2.0)
    assert fund.summarize(rec, cfg)["fcf_spike"]["triggered"] is False
    # spike and excess prepayments together: the lower FCF is scored
    rec = fund.extract(F.companyfacts(NetCashProvidedByUsedInOperatingActivities=ocf_with(lambda e: 1.4 if e == date(2026, 6, 30) else 0.45),
                                      ContractWithCustomerLiabilityCurrent=F.instant(
                                          lambda e: 0.1 * ttm_rev(e) + (300e9 if e.isoformat() > "2025-06-30" else 0))), F.CIK)
    s = fund.summarize(rec, CFG)
    assert s["prepayments"]["applies"] and s["fcf_spike"]["triggered"]
    assert s["fcf_scored_ttm"] == min(s["fcf_ex_prepayments_ttm"], s["fcf_normalised_ttm"]) and s["fcf_basis"] == "ex-prepayments"


def fs_conf(s, v, issue):
    fs = scoring.factors(s, v, {"missing": "x"}, CFG)
    reasons = scoring.confidence_detail(fs, 72, s, v, {}, CFG)["reasons"]
    return next(r for r in reasons if r["issue"] == issue)


def test_share_counts_off_by_more_than_10x_are_dropped_but_splits_are_kept():
    pts = lambda vals: {f"20{20 + i // 4}-{3 * (i % 4) + 3:02d}-28": {"val": v} for i, v in enumerate(vals)}  # noqa: E731
    kept, dropped = fund.clean_shares(pts([25.6e6, 25.7e6, 29452, 26522, 26.9e6, 27e6, 27.2e6]))
    assert [x["val"] for x in dropped] == [29452, 26522] and len(kept) == 5
    kept, dropped = fund.clean_shares(pts([1e9, 1e9, 1e9, 20e9, 20e9, 20e9]))   # a 20-for-1 split
    assert dropped == [] and len(kept) == 6
    assert [x["val"] for x in fund.clean_shares(pts([30e6, 30e6, 30e6, 30e3]))[1]] == [30e3]  # the newest one wrong
    # INOD: three filings gave thousands for millions; the valuation history leaves them out
    facts = F.companyfacts()
    sh = facts["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"]["units"]["shares"]
    for e in sh:
        if e["end"] in ("2023-03-31", "2023-09-30"):
            e["val"] = e["val"] / 1e6
    rec = fund.extract(facts, F.CIK)
    s = fund.summarize(rec, CFG)
    assert [x["end"] for x in s["share_outliers"]] == ["2023-03-31", "2023-09-30"]
    v = metrics.valuation(s, rec, rows_of(F.tiingo_rows(300, 0.0004)), None, CFG)
    assert v["vs_history"]["price_to_sales"]["low"] > 1 and v["share_outliers"] == s["share_outliers"]


def test_history_percentiles_rank_yields_so_negative_periods_count_as_expensive():
    assert metrics.cheaper_share(5.0, [1, 2, 5, 9]) == 37.5 and metrics.cheaper_share(None, [1]) is None
    assert metrics.cheaper_share(-1.0, [-5, -2, 0.001, 3]) == 50.0
    # capex above operating cash flow before 2024: negative FCF months rank as dearest, not dropped
    capex = F.flow(lambda e: round(F.revenue(e) * (0.6 if e.year < 2024 else 0.15), -6), True)
    rec = fund.extract(F.companyfacts(PaymentsToAcquirePropertyPlantAndEquipment=capex), F.CIK)
    v = metrics.valuation(fund.summarize(rec, CFG), rec, rows_of(F.tiingo_rows(300, 0.0004)), None, CFG)
    h = v["vs_history"]["price_to_fcf"]
    assert h["negative_points"] > 0 and h["points"] == v["vs_history"]["price_to_sales"]["points"]
    assert h["high"] is None and h["low"] is not None and 0 <= h["percentile"] < 50
    assert all(x["price_to_fcf"] is None for x in v["history"] if x["fcf_yield_pct"] <= 0)
