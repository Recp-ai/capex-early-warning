"""End-of-day prices from Tiingo (free tier; key in the STOCK_PRICE_API_KEY secret).

The key goes in the Authorization header, never in a URL, and no error message repeats it. Any failure
(no key, rejected key, unknown ticker, rate limit) is recorded per ticker; price-based factors are then
marked missing. Prices are cached in data/stocks/prices/<TICKER>.csv for offline re-scoring and
scripts/evaluate_stocks.py.
"""
from __future__ import annotations

from datetime import date, timedelta

import requests

from cew import util

from . import config

URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
FIELDS = ["date", "close", "adj_close", "split_factor"]


class PriceError(Exception):
    pass


def symbol(ticker: str) -> str:
    """Tiingo writes share classes with a dash (BRK-B)."""
    return ticker.replace(".", "-").lower()


def _scrub(text: str, key: str | None) -> str:
    return text.replace(key, "***") if key else text


def fetch_daily(sess, ticker: str, key: str, start: date, retries: int = 2) -> list[dict]:
    """Daily rows {date, close, adj_close, split_factor} from `start` to the latest close, oldest first."""
    last = "no response"
    for attempt in range(retries):
        try:
            r = sess.get(URL.format(symbol=symbol(ticker)), timeout=60,
                         params={"startDate": start.isoformat(), "format": "json", "resampleFreq": "daily"},
                         headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
        except requests.RequestException as e:
            last = type(e).__name__
            util.time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise PriceError(f"price API rejected the key (HTTP {r.status_code})")
        if r.status_code == 404:
            raise PriceError(f"price API has no ticker {ticker}")
        if r.status_code == 429:
            raise PriceError("price API rate limit reached (HTTP 429); try again later")
        if r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            util.time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise PriceError(f"price API returned HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise PriceError("price API returned something other than JSON") from None
        if not isinstance(data, list):
            detail = str((data or {}).get("detail", "unexpected response"))[:160] if isinstance(data, dict) else "unexpected response"
            raise PriceError("price API: " + _scrub(detail, key))
        rows = []
        for d in data:
            close, adj = util.to_float(d.get("close")), util.to_float(d.get("adjClose"))
            if close is None or not d.get("date"):
                continue
            rows.append({"date": str(d["date"])[:10], "close": close, "adj_close": adj if adj is not None else close,
                         "split_factor": util.to_float(d.get("splitFactor")) or 1.0})
        rows.sort(key=lambda x: x["date"])
        if not rows:
            raise PriceError(f"price API returned no prices for {ticker}")
        return rows
    raise PriceError(f"price API unreachable ({last})")


def cache_path(ticker: str):
    return config.data_dir() / "prices" / f"{ticker}.csv"


def read_cache(ticker: str) -> list[dict]:
    rows = util.read_csv(cache_path(ticker))
    return [{"date": r["date"], "close": float(r["close"]), "adj_close": float(r["adj_close"]),
             "split_factor": float(r.get("split_factor") or 1)} for r in rows]


def run(ticker: str, cfg: dict, key: str | None, offline: bool = False, sess=None,
        today: date | None = None) -> dict:
    """{"series": {ticker: rows}, "errors": {ticker: reason}} for the ticker and the benchmarks."""
    pc = cfg.get("prices") or {}
    tickers = list(dict.fromkeys([ticker, *(pc.get("benchmarks") or ["SPY", "QQQ"])]))
    start = (today or util.today()) - timedelta(days=int(365.25 * pc.get("years", 5)) + 10)
    out: dict = {"provider": pc.get("provider", "tiingo"), "series": {}, "errors": {}}
    if not offline and not key:
        out["errors"] = {t: "STOCK_PRICE_API_KEY not set" for t in tickers}
        return out
    sess = sess or util.session("stock-research/1.0")
    for t in tickers:
        try:
            if offline:
                rows = read_cache(t)
                if not rows:
                    raise PriceError("offline and no cached prices")
            else:
                rows = fetch_daily(sess, t, key, start)
                util.write_csv(cache_path(t), rows, FIELDS)
            out["series"][t] = rows
        except Exception as e:
            out["errors"][t] = _scrub(str(e), key)[:200] if isinstance(e, PriceError) else type(e).__name__
    return out


def latest_close(sess, ticker: str, key: str, today: date | None = None) -> dict | None:
    """The latest close (for the "now" column of the rated-tickers table), or None on any failure."""
    try:
        rows = fetch_daily(sess, ticker, key, (today or util.today()) - timedelta(days=10), retries=1)
    except Exception:
        return None
    return {"date": rows[-1]["date"], "close": rows[-1]["close"]}
