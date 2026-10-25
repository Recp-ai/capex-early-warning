"""Fetch full daily adjusted OHLCV history from Tiingo for research (key from STOCK_PRICE_API_KEY, header only)."""
import csv, os, sys
import requests

def main(ticker: str, out: str) -> None:
    key = os.environ.get("STOCK_PRICE_API_KEY", "")
    if not key:
        sys.exit("STOCK_PRICE_API_KEY not set")
    r = requests.get(f"https://api.tiingo.com/tiingo/daily/{ticker.lower()}/prices",
                     params={"startDate": "1990-01-01", "format": "json", "resampleFreq": "daily"},
                     headers={"Authorization": f"Token {key}"}, timeout=120)
    if r.status_code != 200:
        sys.exit(f"Tiingo HTTP {r.status_code}")
    rows = r.json()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "adj_open", "adj_high", "adj_low", "adj_close", "volume"])
        for d in rows:
            w.writerow([d["date"][:10], d["open"], d["high"], d["low"], d["close"],
                        d["adjOpen"], d["adjHigh"], d["adjLow"], d["adjClose"], d["adjVolume"]])
    print(f"{ticker}: {len(rows)} rows, {rows[0]['date'][:10]} to {rows[-1]['date'][:10]}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
