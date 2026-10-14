import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from cew import util


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """data/ and site/data/ in a temp dir; secrets and inputs cleared."""
    data, site = tmp_path / "data", tmp_path / "site" / "data"
    monkeypatch.setattr(util, "DATA", data)
    monkeypatch.setattr(util, "SITE_DATA", site)
    monkeypatch.setattr(util.time, "sleep", lambda s: None)
    for k in ("ANTHROPIC_API_KEY", "STOCK_PRICE_API_KEY", "SMTP_PASSWORD", "SMTP_USERNAME", "ALERT_EMAIL_TO",
              "INPUT_TICKER", "INPUT_RESEARCH_MODEL", "INPUT_SCORING_MODEL", "SEC_USER_AGENT", "DASHBOARD_URL"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path
