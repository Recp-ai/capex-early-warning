"""Taiwan AI-server supply chain: monthly revenue from the TWSE open-data API.

The endpoint returns the latest reported month for every listed company,
including last month and the same month last year, so YoY and MoM growth are
available immediately. History accumulates in data/taiwan/monthly_revenue.csv
as each new month is published (by the 10th of the following month).
"""
from __future__ import annotations

from statistics import median

from . import util

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
FIELDS = ["period", "code", "name", "revenue", "revenue_prev_month",
          "revenue_last_year", "mom_pct", "yoy_pct", "ytd_yoy_pct", "fetched"]


def roc_to_period(roc: str) -> str:
    """'11509' (ROC year 115, month 09) -> '2026-09'."""
    roc = str(roc).strip()
    year, month = int(roc[:-2]) + 1911, int(roc[-2:])
    return f"{year:04d}-{month:02d}"


def parse_records(records: list[dict], wanted: dict[str, str]) -> list[dict]:
    rows = []
    for r in records:
        code = str(r.get("公司代號", "")).strip()
        if code not in wanted:
            continue
        rows.append({
            "period": roc_to_period(r["資料年月"]),
            "code": code,
            "name": wanted[code],
            "revenue": util.to_float(r.get("營業收入-當月營收")),
            "revenue_prev_month": util.to_float(r.get("營業收入-上月營收")),
            "revenue_last_year": util.to_float(r.get("營業收入-去年當月營收")),
            "mom_pct": util.to_float(r.get("營業收入-上月比較增減(%)")),
            "yoy_pct": util.to_float(r.get("營業收入-去年同月增減(%)")),
            "ytd_yoy_pct": util.to_float(r.get("累計營業收入-前期比較增減(%)")),
            "fetched": util.today().isoformat(),
        })
    return rows


def merge_history(existing: list[dict], new: list[dict]) -> list[dict]:
    keyed = {(r["period"], r["code"]): r for r in existing}
    for r in new:
        keyed[(r["period"], r["code"])] = r
    return sorted(keyed.values(), key=lambda r: (r["period"], r["code"]))


def group_stats(history: list[dict]) -> dict:
    """Median YoY by period for the configured group."""
    by_period: dict[str, list[float]] = {}
    for r in history:
        y = util.to_float(r.get("yoy_pct"))
        if y is not None:
            by_period.setdefault(r["period"], []).append(y)
    series = {p: round(median(v), 2) for p, v in sorted(by_period.items())}
    return series


def run(cfg: dict, offline: bool = False) -> dict:
    path = util.DATA / "taiwan" / "monthly_revenue.csv"
    wanted = {str(k): v for k, v in cfg["taiwan_companies"].items()}
    history = util.read_csv(path)
    error = None
    if not offline:
        try:
            records = util.get_json(util.session(), TWSE_URL, timeout=90)
            new_rows = parse_records(records, wanted)
            if not new_rows:
                raise RuntimeError("no matching companies in TWSE response")
            history = merge_history(history, new_rows)
            util.write_csv(path, history, FIELDS)
        except Exception as e:
            error = str(e)
    latest_period = max((r["period"] for r in history), default=None)
    latest = [r for r in history if r["period"] == latest_period]
    return {
        "latest_period": latest_period,
        "latest": latest,
        "median_yoy_by_period": group_stats(history),
        "errors": {"twse": error} if error else {},
    }
