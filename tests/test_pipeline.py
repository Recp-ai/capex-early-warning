import json
from datetime import date, timedelta

import pytest

from cew import fetch_edgar, fetch_fred, fetch_taiwan, publish, run, scoring, util


# ---------------------------------------------------------------- EDGAR
AMZN_ENTRIES = [  # shape copied from data.sec.gov companyconcept output
    {"start": "2025-01-01", "end": "2025-12-31", "val": 131819000000, "form": "10-K", "filed": "2026-02-06"},
    {"start": "2025-04-01", "end": "2026-03-31", "val": 151003000000, "form": "10-Q", "filed": "2026-04-30"},
    {"start": "2026-01-01", "end": "2026-03-31", "val": 44203000000, "form": "10-Q", "filed": "2026-04-30"},
    {"start": "2025-07-01", "end": "2026-06-30", "val": 173028000000, "form": "10-Q", "filed": "2026-07-31"},
    {"start": "2026-01-01", "end": "2026-06-30", "val": 98411000000, "form": "10-Q", "filed": "2026-07-31"},
    {"start": "2026-04-01", "end": "2026-06-30", "val": 54208000000, "form": "10-Q", "filed": "2026-07-31"},
]

YTD_ONLY = [  # Microsoft-style: Q1 three-month, then year-to-date only
    {"start": "2025-07-01", "end": "2025-09-30", "val": 100, "form": "10-Q", "filed": "2025-10-29"},
    {"start": "2025-07-01", "end": "2025-12-31", "val": 230, "form": "10-Q", "filed": "2026-01-28"},
    {"start": "2025-07-01", "end": "2026-03-31", "val": 380, "form": "10-Q", "filed": "2026-04-29"},
    {"start": "2025-07-01", "end": "2026-06-30", "val": 560, "form": "10-K", "filed": "2026-07-30"},
    {"start": "2025-07-01", "end": "2026-06-30", "val": 561, "form": "10-K/A", "filed": "2026-08-15"},
    {"start": "2025-07-01", "end": "2025-09-30", "val": 999, "form": "8-K", "filed": "2025-10-29"},
]


def test_quarterly_three_month_values_win():
    q = fetch_edgar.quarterly_from_entries(AMZN_ENTRIES)
    assert q["2026-03-31"] == 44203000000
    assert q["2026-06-30"] == 54208000000
    assert "2025-12-31" not in q  # only an annual figure; no 9-month YTD to difference


def test_quarterly_from_ytd_differencing_and_amendments():
    q = fetch_edgar.quarterly_from_entries(YTD_ONLY)
    assert q == {"2025-09-30": 100.0, "2025-12-31": 130.0, "2026-03-31": 150.0, "2026-06-30": 181.0}


def test_pick_metric_prefers_most_recent_tag():
    facts = {"facts": {"us-gaap": {
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": YTD_ONLY[:2]}},
        "PaymentsToAcquireProductiveAssets": {"units": {"USD": AMZN_ENTRIES}},
    }}}
    tag, q = fetch_edgar.pick_metric(facts, fetch_edgar.METRIC_TAGS["capex"])
    assert tag == "PaymentsToAcquireProductiveAssets"
    assert max(q) == "2026-06-30"


# ---------------------------------------------------------------- Taiwan
def test_roc_period():
    assert fetch_taiwan.roc_to_period("11509") == "2026-09"
    assert fetch_taiwan.roc_to_period("11412") == "2025-12"


def test_parse_twse_records_filters_and_converts():
    rec = {"出表日期": "1151012", "資料年月": "11509", "公司代號": "2382", "公司名稱": "廣達",
           "營業收入-當月營收": "150000000", "營業收入-上月營收": "140000000",
           "營業收入-去年當月營收": "120000000", "營業收入-上月比較增減(%)": "7.14",
           "營業收入-去年同月增減(%)": "25.00", "累計營業收入-前期比較增減(%)": "30.1"}
    other = dict(rec, **{"公司代號": "1101"})
    rows = fetch_taiwan.parse_records([rec, other], {"2382": "Quanta"})
    assert len(rows) == 1 and rows[0]["period"] == "2026-09" and rows[0]["yoy_pct"] == 25.0


# ---------------------------------------------------------------- scoring
CFG = util.load_config()


def test_taiwan_score_ladder_and_consecutive_negative():
    tw = {"median_yoy_by_period": {"2026-06": 30.0, "2026-07": 12.0, "2026-08": -2.0, "2026-09": -12.0},
          "latest": []}
    s = scoring.score_taiwan(tw, CFG)
    assert s["score"] == 3


def test_credit_score():
    fred = {"series": {"BAMLH0A0HYM2": {"latest": 3.9, "chg_4w": 0.55},
                       "BAMLC0A0CM": {"latest": 1.1, "chg_4w": 0.05}}}
    assert scoring.score_credit(fred, CFG)["score"] == 2


def test_composite_levels():
    calm = {k: {"score": 0} for k, _ in scoring.CATEGORIES}
    assert scoring.composite(calm, CFG)["level"] == "GREEN"
    one_hot = dict(calm, credit={"score": 3})
    c = scoring.composite(one_hot, CFG)
    assert c["alert"] and c["level"] == "AMBER" and c["hot_categories"] == ["credit"]
    hot = {k: {"score": 2} for k, _ in scoring.CATEGORIES}
    assert scoring.composite(hot, CFG)["level"] == "RED"


def test_fred_summary_lookbacks():
    start = date(2025, 1, 1)
    rows = [{"date": (start + timedelta(days=i)).isoformat(), "value": float(i)} for i in range(400)]
    s = fetch_fred.summarize(rows, "daily")
    assert s["chg_4w"] == 28.0 and s["chg_52w"] == 365.0


# ---------------------------------------------------------------- end to end (offline)
@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    data, site = tmp_path / "data", tmp_path / "site" / "data"
    monkeypatch.setattr(util, "DATA", data)
    monkeypatch.setattr(util, "SITE_DATA", site)
    monkeypatch.setattr(publish, "REPORTS", data / "reports")
    monkeypatch.setattr(publish, "LOG_PATH", data / "log.csv")
    for k in ("ANTHROPIC_API_KEY", "FRED_API_KEY", "SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    # cached FRED data
    end = util.today()
    for sid in CFG["fred_series"]:
        rows = [{"date": (end - timedelta(days=d)).isoformat(), "value": 3.0 + d / 1000}
                for d in range(800, -1, -1)]
        util.write_csv(data / "fred" / f"{sid}.csv", rows, ["date", "value"])
    # cached EDGAR data
    for t in CFG["hyperscalers"]:
        m = {"entity": t, "tags": {"capex": "X"}, "metrics": {
            "capex": {"2026-03-31": 30e9, "2026-06-30": 33e9},
            "operating_cash_flow": {"2026-03-31": 35e9, "2026-06-30": 30e9},
            "new_operating_lease_assets": {}, "new_finance_lease_assets": {}, "debt_issued": {}}}
        util.write_json(data / "edgar" / f"{t}.json", m)
    util.write_csv(data / "taiwan" / "monthly_revenue.csv",
                   [{"period": "2026-09", "code": "2382", "name": "Quanta", "yoy_pct": "25"}],
                   fetch_taiwan.FIELDS)
    return tmp_path


def test_offline_run_writes_outputs(sandbox):
    assert run.main(["--offline", "--no-llm", "--no-email"]) == 0
    latest = json.loads((sandbox / "site" / "data" / "latest.json").read_text())
    assert latest["composite"]["level"] in {"GREEN", "AMBER", "RED"}
    assert set(latest["scores"]) == {k for k, _ in scoring.CATEGORIES}
    log = json.loads((sandbox / "site" / "data" / "log.json").read_text())
    assert len(log) == 1 and log[0]["sp500"]
    charts = json.loads((sandbox / "site" / "data" / "charts.json").read_text())
    assert "SP500" in charts["fred"] and charts["capex_by_quarter"]
    # a second run on the same day replaces, not duplicates, the log row
    assert run.main(["--offline", "--no-llm", "--no-email"]) == 0
    assert len(json.loads((sandbox / "site" / "data" / "log.json").read_text())) == 1
