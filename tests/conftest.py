import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import timedelta

import pytest

from cew import fetch_taiwan, publish, util

CFG = util.load_config()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    data, site = tmp_path / "data", tmp_path / "site" / "data"
    monkeypatch.setattr(util, "DATA", data)
    monkeypatch.setattr(util, "SITE_DATA", site)
    monkeypatch.setattr(publish, "REPORTS", data / "reports")
    monkeypatch.setattr(publish, "LOG_PATH", data / "log.csv")
    for k in ("ANTHROPIC_API_KEY", "FRED_API_KEY", "SMTP_PASSWORD", "INPUT_RESEARCH_MODEL",
              "INPUT_ASSESSMENT_MODEL", "VAR_RESEARCH_MODEL", "VAR_ASSESSMENT_MODEL", "RUN_TRIGGER",
              "EVENT_NAME", "SCHEDULE", "INPUT_EVENT", "GITHUB_OUTPUT"):
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
