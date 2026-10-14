"""Did the stock ratings work? Forward 1/3/6-month returns by rating band, and versus SPY.

Reads data/stocks/ratings.csv and the price caches in data/stocks/prices/ (each stock run refreshes the
rated ticker and SPY). --refresh first fetches the latest prices for every rated ticker and SPY
(needs STOCK_PRICE_API_KEY). Returns use split- and dividend-adjusted closes, from the last close on or
before the rating date.

  python scripts/evaluate_stocks.py [--refresh]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cew import util  # noqa: E402
from stocks import config, prices, scoring  # noqa: E402

HORIZONS = {"1m": 30, "3m": 91, "6m": 182}
STALE_DAYS = 5  # a start or end price more than this many days before its date doesn't count


def close_on_or_before(rows: list[dict], day: date) -> float | None:
    best = None
    for r in rows:
        if r["date"] <= day.isoformat():
            best = r
        else:
            break
    if best is None or date.fromisoformat(best["date"]) < day - timedelta(days=STALE_DAYS):
        return None
    return best["adj_close"]


def forward_return(rows: list[dict], start: date, days: int) -> float | None:
    """% return from the close on/before `start` to the close on/before start + days; None until reached."""
    end = start + timedelta(days=days)
    if not rows or rows[-1]["date"] < end.isoformat():
        return None
    p0, p1 = close_on_or_before(rows, start), close_on_or_before(rows, end)
    return None if not p0 or p1 is None else (p1 / p0 - 1) * 100


def evaluate(ratings: list[dict], load=prices.read_cache) -> dict:
    """{rating: {horizon: {"n", "return", "vs_spy", "beat_spy"}}} from rating rows."""
    cache: dict = {}
    get = lambda t: cache.setdefault(t, load(t))  # noqa: E731
    spy = get("SPY")
    out: dict = {}
    for row in ratings:
        if not row.get("rating"):
            continue
        start = date.fromisoformat(row["timestamp"][:10])
        rows = get(row["ticker"])
        for h, days in HORIZONS.items():
            ret, bench = forward_return(rows, start, days), forward_return(spy, start, days)
            if ret is None or bench is None:
                continue
            cell = out.setdefault(row["rating"], {}).setdefault(h, {"returns": [], "excess": []})
            cell["returns"].append(ret)
            cell["excess"].append(ret - bench)
    return {r: {h: {"n": len(c["returns"]), "return": round(mean(c["returns"]), 2),
                    "vs_spy": round(mean(c["excess"]), 2),
                    "beat_spy": round(100 * sum(1 for x in c["excess"] if x > 0) / len(c["excess"]))}
                for h, c in hs.items()} for r, hs in out.items()}


def refresh(tickers: list[str]) -> None:
    key = util.env("STOCK_PRICE_API_KEY")
    if not key:
        print("STOCK_PRICE_API_KEY not set; using cached prices.")
        return
    sess = util.session("stock-research/1.0")
    start = util.today() - timedelta(days=int(365.25 * 5) + 10)
    for t in dict.fromkeys([*tickers, "SPY"]):
        try:
            util.write_csv(prices.cache_path(t), prices.fetch_daily(sess, t, key, start), prices.FIELDS)
        except Exception as e:
            print(f"{t}: {str(e).replace(key, '***')[:120]}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--refresh", action="store_true", help="fetch the latest prices first (STOCK_PRICE_API_KEY)")
    args = p.parse_args(argv)
    ratings = util.read_csv(config.data_dir() / "ratings.csv")
    if not ratings:
        print("No stock ratings yet.")
        return
    if args.refresh:
        refresh([r["ticker"] for r in ratings])
    res = evaluate(ratings)
    print(f"{len(ratings)} ratings. Average forward return, return minus SPY's, and share beating SPY:\n")
    print(f"  {'Rating':<12}" + "".join(f"{h:>30}" for h in HORIZONS))
    for rating in reversed(scoring.order(config.load())):
        cells = []
        for h in HORIZONS:
            c = (res.get(rating) or {}).get(h)
            cells.append(f"{c['return']:+.1f}% / {c['vs_spy']:+.1f} pp / {c['beat_spy']}% (n={c['n']})" if c else "n/a")
        print(f"  {rating:<12}" + "".join(f"{x:>30}" for x in cells))
    print("\nSmall samples mean little; wait for dozens of ratings per band before trusting a pattern. Not investment advice.")


if __name__ == "__main__":
    main()
