"""FRED: macro context, credit spreads and index levels."""
from __future__ import annotations

from datetime import date, timedelta

from . import util

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def fetch_series(sess, api_key: str, series_id: str, start: date) -> list[dict]:
    data = util.get_json(sess, FRED_URL, params={
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start.isoformat(),
    })
    rows = []
    for ob in data.get("observations", []):
        v = util.to_float(ob.get("value"))
        if v is not None:
            rows.append({"date": ob["date"], "value": v})
    return rows


def value_on_or_before(rows: list[dict], target: date) -> dict | None:
    """Last observation dated on or before target (rows sorted ascending)."""
    best = None
    for r in rows:
        if date.fromisoformat(r["date"]) <= target:
            best = r
        else:
            break
    return best


def summarize(rows: list[dict], freq: str) -> dict:
    if not rows:
        return {}
    last = rows[-1]
    d = date.fromisoformat(last["date"])
    out = {"latest": last["value"], "latest_date": last["date"]}
    lookbacks = {"4w": 28, "13w": 91, "52w": 365}
    for k, days in lookbacks.items():
        prev = value_on_or_before(rows, d - timedelta(days=days))
        out[f"chg_{k}"] = None if prev is None else round(last["value"] - prev["value"], 4)
        out[f"prev_{k}"] = None if prev is None else prev["value"]
    if freq == "monthly":
        prev12 = value_on_or_before(rows, d - timedelta(days=360))
        pc = None if prev12 is None else util.pct_change(last["value"], prev12["value"])
        out["yoy_pct"] = None if pc is None else round(pc, 2)
    return out


def run(cfg: dict, api_key: str | None, offline: bool = False) -> dict:
    """Fetch every configured series, persist CSVs, return summaries keyed by series id."""
    out_dir = util.DATA / "fred"
    sess = util.session()
    start = util.today() - timedelta(days=5 * 365)
    summaries, errors = {}, {}
    for sid, meta in cfg["fred_series"].items():
        path = out_dir / f"{sid}.csv"
        try:
            if offline or not api_key:
                rows = [{"date": r["date"], "value": float(r["value"])} for r in util.read_csv(path)]
                if not rows:
                    raise RuntimeError("no API key and no cached data")
            else:
                rows = fetch_series(sess, api_key, sid, start)
                util.write_csv(path, rows, ["date", "value"])
            summaries[sid] = {"label": meta["label"], **summarize(rows, meta.get("freq", "daily"))}
        except Exception as e:  # one bad series must not sink the run
            errors[sid] = str(e)
    return {"series": summaries, "errors": errors}
