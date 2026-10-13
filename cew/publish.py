"""Persist the report, the daily log, every run's archive copy, and the dashboard's data files.

data/reports/<date>.json and data/log.csv keep one entry per day (the dashboard's running log).
data/runs/<UTC timestamp>.json and data/runs.csv keep every run; both are append-only.
"""
from __future__ import annotations

import csv
import json
from datetime import date, timedelta

from . import events, scoring, util

REPORTS = util.DATA / "reports"
LOG_PATH = util.DATA / "log.csv"
LOG_FIELDS = ["date", "level", "composite", "credit", "ai_lab_financing", "taiwan_supply_chain",
              "hyperscaler_filings", "supplier_signals", "physical_buildout",
              "sp500", "nasdaq100", "vix", "ust10y", "brent", "hy_spread",
              "research_model", "assessment_model", "cost_usd"]
CHART_SERIES = ["BAMLH0A0HYM2", "BAMLC0A0CM", "DGS10", "DCOILBRENTEU", "VIXCLS", "SP500", "NASDAQ100"]
RUN_FIELDS = ["timestamp", "trigger", "research_model", "assessment_model", "tokens", "searches", "cost_usd",
              "composite", "level", *[k for k, _ in scoring.CATEGORIES]]
RUNS_SHOWN = 20


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


# ---------------------------------------------------------------- every run
def runs_dir():
    return util.DATA / "runs"


def runs_csv():
    return util.DATA / "runs.csv"


def run_stamp(timestamp: str) -> str:
    """'2026-10-13T18:45:13Z' -> '2026-10-13T184513Z' (file-name safe)."""
    return timestamp.replace(":", "")


def run_row(report: dict) -> dict:
    claude = report.get("claude") or {}
    used = (claude.get("models") or {}) if claude.get("enabled") else {}
    cost = report.get("cost") or {}
    steps = (cost.get("steps") or {}).values()
    tokens = sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_write_tokens", 0)
                 + u.get("cache_read_tokens", 0) for u in steps)
    return {
        "timestamp": report["run_id"], "trigger": report.get("trigger", "manual"),
        "research_model": used.get("research", ""), "assessment_model": used.get("assessment", ""),
        "tokens": tokens if cost else "", "searches": sum(u.get("web_searches", 0) for u in steps) if cost else "",
        "cost_usd": "" if cost.get("total_usd") is None else cost["total_usd"],
        "composite": report["composite"]["total"], "level": report["composite"]["level"],
        **{k: report["scores"][k]["score"] for k, _ in scoring.CATEGORIES},
    }


def archive_run(report: dict) -> str:
    """Write data/runs/<timestamp>.json (never overwriting) and append a row to data/runs.csv."""
    d = runs_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp, n = run_stamp(report["run_id"]), 1
    while True:
        path = d / (f"{stamp}.json" if n == 1 else f"{stamp}-{n}.json")
        try:
            with open(path, "x", encoding="utf-8") as f:  # "x": fail rather than replace an earlier run
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            break
        except FileExistsError:
            n += 1
    append_run_row(run_row(report))
    return path.name


def append_run_row(row: dict) -> None:
    path = runs_csv()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not (path.exists() and path.stat().st_size)
    fields, prefix = RUN_FIELDS, ""
    if not new_file:
        with open(path, newline="", encoding="utf-8") as f:
            fields = next(csv.reader(f), None) or RUN_FIELDS  # keep the existing column order
        with open(path, "rb") as f:
            f.seek(-1, 2)
            prefix = "" if f.read(1) == b"\n" else "\n"
    with open(path, "a", newline="", encoding="utf-8") as f:
        f.write(prefix)
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


def previous_runs(n: int = 5) -> list[dict]:
    """The last n rows of data/runs.csv, oldest first. Before the first v2 run, the latest daily
    report stands in so the run-to-run checks have something to compare with."""
    rows = util.read_csv(runs_csv())
    if rows:
        return rows[-n:]
    files = sorted(REPORTS.glob("*.json")) if REPORTS.exists() else []
    if not files:
        return []
    r = util.read_json(files[-1]) or {}
    if not r.get("scores"):
        return []
    return [{"timestamp": r.get("generated_at", r.get("date", "")), "trigger": "report",
             "level": (r.get("composite") or {}).get("level", ""),
             **{k: (r["scores"].get(k) or {}).get("score", "") for k, _ in scoring.CATEGORIES}}]


def runs_site_data(today: date, evts: list[dict]) -> dict:
    rows = util.read_csv(runs_csv())
    spend: dict[str, dict] = {}
    for r in rows:
        m = spend.setdefault(r["timestamp"][:7], {"cost_usd": 0.0, "runs": 0})
        m["runs"] += 1
        m["cost_usd"] = round(m["cost_usd"] + (util.to_float(r.get("cost_usd")) or 0.0), 4)
    return {"generated_at": util.now_iso(), "total_runs": len(rows), "runs": rows[-RUNS_SHOWN:][::-1],
            "spend_by_month": spend, "upcoming_events": events.upcoming(evts, today)}


def update_log(report: dict) -> list[dict]:
    fred = report["data"]["fred"]

    def latest(sid):
        return (fred.get(sid) or {}).get("latest")

    claude = report.get("claude") or {}
    used = (claude.get("models") or {}) if claude.get("enabled") else {}
    cost = (report.get("cost") or {}).get("total_usd")

    row = {
        "date": report["date"], "level": report["composite"]["level"],
        "composite": report["composite"]["total"],
        **{k: report["scores"][k]["score"] for k, _ in scoring.CATEGORIES},
        "sp500": latest("SP500"), "nasdaq100": latest("NASDAQ100"), "vix": latest("VIXCLS"),
        "ust10y": latest("DGS10"), "brent": latest("DCOILBRENTEU"), "hy_spread": latest("BAMLH0A0HYM2"),
        "research_model": used.get("research", ""), "assessment_model": used.get("assessment", ""),
        "cost_usd": "" if cost is None else cost,
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


def finish_run(report: dict, evts: list[dict]) -> str:
    """Final write after the email step: report files, the run archive, and site/data/runs.json."""
    write_report(report)
    name = archive_run(report)
    util.write_json(util.SITE_DATA / "runs.json", runs_site_data(date.fromisoformat(report["date"]), evts))
    return name
