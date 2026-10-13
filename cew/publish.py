"""Persist the weekly report, the running log, and the dashboard's data files."""
from __future__ import annotations

from datetime import date, timedelta

from . import scoring, util

REPORTS = util.DATA / "reports"
LOG_PATH = util.DATA / "log.csv"
LOG_FIELDS = ["date", "level", "composite", "credit", "ai_lab_financing", "taiwan_supply_chain",
              "hyperscaler_filings", "supplier_signals", "physical_buildout",
              "sp500", "nasdaq100", "vix", "ust10y", "brent", "hy_spread"]
CHART_SERIES = ["BAMLH0A0HYM2", "BAMLC0A0CM", "DGS10", "DCOILBRENTEU", "VIXCLS", "SP500", "NASDAQ100"]


def previous_report(today: str) -> dict | None:
    if not REPORTS.exists():
        return None
    files = sorted(p for p in REPORTS.glob("*.json") if p.stem < today)
    return util.read_json(files[-1]) if files else None


def write_report(report: dict) -> None:
    util.write_json(REPORTS / f"{report['date']}.json", report)
    util.write_json(util.SITE_DATA / "latest.json", _site_report(report))


def _site_report(report: dict) -> dict:
    r = dict(report)
    claude = dict(r.get("claude") or {})
    research = dict(claude.get("research") or {})
    research.pop("notes", None)  # keep the public page light; full notes stay in data/reports
    claude["research"] = research
    r["claude"] = claude
    return r


def update_log(report: dict) -> list[dict]:
    fred = report["data"]["fred"]

    def latest(sid):
        return (fred.get(sid) or {}).get("latest")

    row = {
        "date": report["date"], "level": report["composite"]["level"],
        "composite": report["composite"]["total"],
        **{k: report["scores"][k]["score"] for k, _ in scoring.CATEGORIES},
        "sp500": latest("SP500"), "nasdaq100": latest("NASDAQ100"), "vix": latest("VIXCLS"),
        "ust10y": latest("DGS10"), "brent": latest("DCOILBRENTEU"), "hy_spread": latest("BAMLH0A0HYM2"),
    }
    rows = [r for r in util.read_csv(LOG_PATH) if r["date"] != report["date"]]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    util.write_csv(LOG_PATH, rows, LOG_FIELDS)
    return rows


def _weekly(rows: list[dict], years: int = 3) -> list[list]:
    """Down-sample daily observations to one point per week (last obs of each ISO week)."""
    cutoff = util.today() - timedelta(days=365 * years)
    by_week: dict[tuple[int, int], dict] = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        if d < cutoff:
            continue
        by_week[d.isocalendar()[:2]] = r
    return [[r["date"], float(r["value"])] for r in sorted(by_week.values(), key=lambda x: x["date"])]


def chart_data(report: dict) -> dict:
    series = {}
    for sid in CHART_SERIES:
        rows = util.read_csv(util.DATA / "fred" / f"{sid}.csv")
        if rows:
            series[sid] = _weekly(rows)
    edgar = report["data"]["edgar"]
    capex = scoring.aggregate_by_calendar_quarter({"companies": edgar}, "capex")
    return {
        "fred": series,
        "taiwan_median_yoy": report["data"]["taiwan"].get("median_yoy_by_period", {}),
        "capex_by_quarter": capex,
        "tickers": list(edgar.keys()),
    }


def write_all(report: dict, cfg: dict) -> None:
    rows = update_log(report)
    util.write_json(util.SITE_DATA / "log.json", rows)
    util.write_json(util.SITE_DATA / "charts.json", chart_data(report))
    write_report(report)
