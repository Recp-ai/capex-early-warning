"""End-of-day prices and near-live quotes from Tiingo (free tier; key in the STOCK_PRICE_API_KEY secret).

The key goes in the Authorization header, never in a URL, and no error message repeats it. Any failure
(no key, rejected key, unknown ticker, rate limit) is recorded per ticker; price-based factors are then
marked missing. Prices are cached in data/stocks/prices/<TICKER>.csv for offline re-scoring and
scripts/evaluate_stocks.py.

Quotes (`quotes`) are display only: Tiingo IEX tngoLast (the official close once it prints), never a score
input. One request covers every ticker.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from cew import util

from . import config

URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"
IEX_URL = "https://api.tiingo.com/iex/"      # ?tickers=a,b,c
IEX_ALL_URL = "https://api.tiingo.com/iex"   # every ticker (~10 MB), filtered in code
FIELDS = ["date", "close", "adj_close", "split_factor"]

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # no tz database: fixed EST is close enough for labels
    ET = timezone(timedelta(hours=-5))


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


def read_rows(path) -> list[dict]:
    rows = util.read_csv(Path(path))
    return [{"date": r["date"], "close": float(r["close"]), "adj_close": float(r["adj_close"]),
             "split_factor": float(r.get("split_factor") or 1)} for r in rows]


def read_cache(ticker: str) -> list[dict]:
    return read_rows(cache_path(ticker))


def benchmarks(cfg: dict) -> list[str]:
    return list((cfg.get("prices") or {}).get("benchmarks") or ["SPY", "QQQ"])


def start_date(cfg: dict, today: date | None = None) -> date:
    return (today or util.today()) - timedelta(days=int(365.25 * (cfg.get("prices") or {}).get("years", 5)) + 10)


def run(ticker: str, cfg: dict, key: str | None, offline: bool = False, sess=None,
        today: date | None = None, shared_dir=None) -> dict:
    """{"series": {ticker: rows}, "errors": {ticker: reason}} for the ticker and the benchmarks.
    With `shared_dir` (a batch's ticker job) the benchmarks come from the CSVs the plan job fetched once
    for the whole batch, never from Tiingo again."""
    pc = cfg.get("prices") or {}
    bench = benchmarks(cfg)
    tickers = list(dict.fromkeys([ticker, *bench]))
    start = start_date(cfg, today)
    out: dict = {"provider": pc.get("provider", "tiingo"), "series": {}, "errors": {}}
    if shared_dir is not None:
        for t in bench:
            if t == ticker:
                continue
            rows = read_rows(Path(shared_dir) / f"{t}.csv")
            if rows:
                out["series"][t] = rows
            else:
                out["errors"][t] = "no shared benchmark prices from the batch's plan job"
        tickers = [ticker]
    if not offline and not key:
        out["errors"].update({t: "STOCK_PRICE_API_KEY not set" for t in tickers})
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


# ---------------------------------------------------------------- near-live quotes (display only)
def _as_of(v) -> str | None:
    """Tiingo's timestamp as UTC ISO ("2026-10-14T20:00:00Z"), or None."""
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_quotes(data, tickers: list[str]) -> dict:
    """{TICKER: {price, as_of, prev_close}} for the wanted tickers. price is tngoLast (Tiingo's last price,
    the official close once it prints); `last` needs an IEX entitlement and is ignored."""
    if not isinstance(data, list):
        raise PriceError("quote API returned something other than a list")
    wanted = {symbol(t).upper(): t for t in tickers}
    out = {}
    for d in data:
        t = wanted.get(str((d or {}).get("ticker") or "").upper()) if isinstance(d, dict) else None
        price = util.to_float(d.get("tngoLast")) if t else None
        if t is None or price is None or price <= 0:
            continue
        out[t] = {"price": price, "as_of": _as_of(d.get("timestamp")), "prev_close": util.to_float(d.get("prevClose"))}
    return out


def _get_quotes(sess, url: str, key: str):
    """(data, None) or (None, (reason, fallback_ok)). fallback_ok: the all-tickers form may still work."""
    try:
        r = sess.get(url, timeout=90, headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
    except requests.RequestException as e:
        return None, (f"quote API unreachable ({type(e).__name__})", True)
    if r.status_code in (401, 403):
        return None, (f"quote API rejected the key (HTTP {r.status_code})", False)
    if r.status_code == 429:
        return None, ("quote API rate limit reached (HTTP 429); try again later", False)
    if r.status_code >= 400:
        return None, (f"quote API returned HTTP {r.status_code}", True)
    try:
        data = r.json()
    except ValueError:
        return None, ("quote API returned something other than JSON", True)
    if not isinstance(data, list):
        detail = str(data.get("detail", "unexpected response"))[:160] if isinstance(data, dict) else "unexpected response"
        return None, ("quote API: " + _scrub(detail, key), True)
    return data, None


def quotes(tickers, key: str | None, sess=None, max_tickers: int = 100) -> dict:
    """Near-live quotes for any number of tickers in ONE request (never one per ticker):
    {"quotes": {TICKER: {price, as_of, prev_close}}, "endpoint": "tickers" | "all" | None, "requests": n,
     "error": reason or None}. Up to `max_tickers` uses iex/?tickers=a,b,c; more, or if that form fails
    (not a rejected key or the rate limit), the all-tickers endpoint, filtered here."""
    wanted = list(dict.fromkeys(t for t in (tickers or []) if t))
    out: dict = {"quotes": {}, "endpoint": None, "requests": 0, "error": None}
    if not wanted:
        return out
    if not key:
        out["error"] = "STOCK_PRICE_API_KEY not set"
        return out
    sess = sess or util.session("stock-research/1.0")
    forms = [("all", IEX_ALL_URL)]
    if len(wanted) <= max_tickers:
        forms.insert(0, ("tickers", IEX_URL + "?tickers=" + ",".join(symbol(t) for t in wanted)))
    for form, url in forms:
        out["requests"] += 1
        data, err = _get_quotes(sess, url, key)
        if err is None:
            try:
                out["quotes"], out["endpoint"], out["error"] = parse_quotes(data, wanted), form, None
            except PriceError as e:
                out["error"] = str(e)
                continue
            return out
        out["error"] = _scrub(err[0], key)
        if not err[1]:
            break
    return out


# ---------------------------------------------------------------- which close a run was scored on
def et(moment: datetime) -> datetime:
    return moment.astimezone(ET)


def quote_date(quote: dict | None) -> str | None:
    """US Eastern date of a quote's as_of."""
    try:
        return et(datetime.fromisoformat(str((quote or {})["as_of"]).replace("Z", "+00:00"))).date().isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def latest_weekday(moment: datetime) -> str:
    """The US Eastern date of `moment`, moved back to Friday on a weekend (holidays not known)."""
    d = et(moment).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def clock_et(moment: datetime) -> str:
    t = et(moment)
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'} ET"


def close_note(price_date: str | None, quote: dict | None, run_at: datetime) -> dict:
    """Which official close the run was scored on, and whether a newer one existed then: the quote's date
    (or, without a quote, the latest weekday at run time) after the scoring date means today's close was
    not yet published. Also flags runs between 16:00 and 18:00 ET (after the close, before Tiingo's daily
    update). Display only."""
    newer = quote_date(quote) or latest_weekday(run_at)
    t = et(run_at)
    out = {"scored_on": price_date, "latest": newer, "latest_from": "quote" if quote_date(quote) else "weekday",
           "run_et": t.strftime("%Y-%m-%d %H:%M"), "note": None, "close_window": None}
    if price_date and newer > price_date:
        out["note"] = f"today's close not yet published; scored on {price_date}"
    if t.weekday() < 5 and 16 <= t.hour < 18:
        out["close_window"] = (f"Run at {clock_et(run_at)}, between the 4:00 pm close and about 6:00 pm ET, when "
                               "Tiingo may not have published today's daily close yet")
    return out
