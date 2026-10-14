"""Ticker -> SEC CIK and company name, from SEC's company_tickers.json."""
from __future__ import annotations

from cew import util

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class TickerNotFound(Exception):
    pass


def variants(ticker: str) -> list[str]:
    """SEC writes share classes with a dash (BRK-B); accept BRK.B and BRKB too."""
    out = [ticker, ticker.replace(".", "-"), ticker.replace("-", "."), ticker.replace(".", "").replace("-", "")]
    return list(dict.fromkeys(out))


def lookup(sess, ticker: str) -> dict:
    """{"ticker", "sec_ticker", "cik" (10 digits), "name"}; raises TickerNotFound."""
    data = util.get_json(sess, TICKERS_URL, timeout=60)
    rows = data.values() if isinstance(data, dict) else data
    by_ticker = {str(r.get("ticker", "")).upper(): r for r in rows if isinstance(r, dict)}
    for v in variants(ticker):
        r = by_ticker.get(v)
        if r and r.get("cik_str") is not None:
            return {"ticker": ticker, "sec_ticker": v, "cik": str(int(r["cik_str"])).zfill(10),
                    "name": r.get("title") or ticker}
    raise TickerNotFound(f"Ticker {ticker} is not in SEC's company_tickers.json (checked {', '.join(variants(ticker))}). "
                         "Only SEC-registered companies can be researched.")
