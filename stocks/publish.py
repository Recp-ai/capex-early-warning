"""Persist a stock run: the append-only archive and ratings log, and the stocks page's data files.

data/stocks/runs/<TICKER>/<UTC timestamp>.json is opened with mode "x" (never overwritten) and
data/stocks/ratings.csv is only appended to; a column added to RATING_FIELDS goes on the end of the
header line and earlier rows stay byte for byte (they read as blank in it).
site/data/stocks/index.json lists the latest rating per ticker; site/data/stocks/<TICKER>.json holds the
latest run's detail for the page.
"""
from __future__ import annotations

import csv
import json

from cew import publish as cew_publish
from cew import util

from . import config, scoring

RATING_FIELDS = ["timestamp", "ticker", "rating", "score", "confidence", *scoring.FACTORS, "price", "price_date",
                 "research_model", "scoring_model", "cost_usd", "overlay"]
DISCLAIMER = "Automated research screen from public data and Claude's news reading. Not investment advice."


def runs_dir(ticker: str):
    return config.data_dir() / "runs" / ticker


def ratings_csv():
    return config.data_dir() / "ratings.csv"


def archive_run(report: dict) -> str:
    d = runs_dir(report["ticker"])
    d.mkdir(parents=True, exist_ok=True)
    stamp, n = cew_publish.run_stamp(report["run_id"]), 1
    while True:
        path = d / (f"{stamp}.json" if n == 1 else f"{stamp}-{n}.json")
        try:
            with open(path, "x", encoding="utf-8") as f:  # "x": fail rather than replace an earlier run
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            return path.name
        except FileExistsError:
            n += 1


def rating_row(report: dict) -> dict:
    fs = report.get("factors") or {}
    sel = report.get("models") or {}
    price = report.get("price") or {}
    cost = (report.get("cost") or {}).get("total_usd")
    ov = report.get("overlay") or {}
    return {"timestamp": report["run_id"], "ticker": report["ticker"], "rating": report["rating"],
            "score": report["score"], "confidence": report["confidence"],
            **{k: "" if (fs.get(k) or {}).get("score") is None else fs[k]["score"] for k in scoring.FACTORS},
            "price": "" if price.get("close") is None else price["close"], "price_date": price.get("date") or "",
            "research_model": sel.get("research", "") if report.get("llm") else "",
            "scoring_model": sel.get("scoring", "") if report.get("llm") else "",
            "cost_usd": "" if cost is None else cost,
            "overlay": f"capped from {ov['rating_before']}" if ov.get("capped") else ("note" if ov.get("applies") else "")}


def append_rating(row: dict) -> None:
    path = ratings_csv()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not (path.exists() and path.stat().st_size)
    fields, prefix = RATING_FIELDS, ""
    if not new_file:
        with open(path, newline="", encoding="utf-8") as f:
            fields = next(csv.reader(f), None) or RATING_FIELDS
        missing = [k for k in RATING_FIELDS if k not in fields]
        if missing:
            fields = fields + missing
            cew_publish._extend_header(path, fields)
        with open(path, "rb") as f:
            f.seek(-1, 2)
            prefix = "" if f.read(1) == b"\n" else "\n"
    with open(path, "a", newline="", encoding="utf-8") as f:
        f.write(prefix)
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


def site_report(report: dict, chart: list | None = None) -> dict:
    """The latest run for the page: research notes left out, price chart and rating history added."""
    r = json.loads(json.dumps(report, default=str))
    r["chart"] = chart or []
    research = (r.get("claude") or {}).get("research")
    if isinstance(research, dict):
        research.pop("notes", None)  # full notes stay in the archive
    r["history"] = [row for row in util.read_csv(ratings_csv()) if row.get("ticker") == report["ticker"]][-20:][::-1]
    return r


def update_index(report: dict, now_prices: dict | None = None) -> dict:
    """Latest rating per ticker (from ratings.csv), names and "now" prices kept from the previous index."""
    path = config.site_dir() / "index.json"
    old = {e["ticker"]: e for e in (util.read_json(path) or {}).get("tickers") or []}
    latest: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for row in util.read_csv(ratings_csv()):
        latest[row["ticker"]] = row
        counts[row["ticker"]] = counts.get(row["ticker"], 0) + 1
    out = []
    for t, row in latest.items():
        prev = old.get(t) or {}
        e = {"ticker": t, "name": prev.get("name") or t, "rating": row["rating"], "score": int(util.to_float(row["score"]) or 0),
             "confidence": row["confidence"], "timestamp": row["timestamp"], "runs": counts[t],
             "price": util.to_float(row.get("price")), "price_date": row.get("price_date") or None,
             "price_now": prev.get("price_now"), "price_now_date": prev.get("price_now_date"),
             "overlay": row.get("overlay") or ""}
        if t == report["ticker"]:
            e["name"] = report.get("name") or t
            e["price_now"], e["price_now_date"] = e["price"], e["price_date"]
        now = (now_prices or {}).get(t)
        if now and (not e["price_now_date"] or now["date"] >= e["price_now_date"]):
            e["price_now"], e["price_now_date"] = now["close"], now["date"]
        e["change_pct"] = (round((e["price_now"] / e["price"] - 1) * 100, 2)
                           if e["price"] and e["price_now"] is not None else None)
        out.append(e)
    out.sort(key=lambda e: e["timestamp"], reverse=True)
    steps = (report.get("cost") or {}).get("steps") or {}
    index = {"generated_at": util.now_iso(), "disclaimer": DISCLAIMER, "tickers": out,
             "last_run": {"ticker": report["ticker"], "timestamp": report["run_id"],
                          "cost_usd": (report.get("cost") or {}).get("total_usd"),
                          "steps": {k: {x: v.get(x) for x in ("model", "input_tokens", "output_tokens", "cache_write_tokens",
                                                              "cache_read_tokens", "web_searches")}
                                    for k, v in steps.items()}}}
    util.write_json(path, index)
    return index


def write_all(report: dict, now_prices: dict | None = None, chart: list | None = None) -> str:
    """Archive, ratings row, then the page files. Returns the archive file name."""
    name = archive_run(report)
    append_rating(rating_row(report))
    util.write_json(config.site_dir() / f"{report['ticker']}.json", site_report(report, chart))
    update_index(report, now_prices)
    return name
