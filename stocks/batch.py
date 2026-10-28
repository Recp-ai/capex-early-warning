"""Batch stock runs (.github/workflows/stocks.yml): plan -> one job per ticker -> collect.

  python -m stocks.batch plan --shared shared       # validate INPUT_TICKERS, fetch SPY/QQQ once
  python -m stocks.run MSFT --batch-out out --shared shared   # each ticker's job (publishes nothing)
  python -m stocks.batch collect --out out --shared shared     # write everything once, one email

plan     INPUT_TICKERS (comma- or space-separated) -> upper-cased, validated with the ticker regex,
         de-duplicated, at most batch.max_batch; written to $GITHUB_OUTPUT for the matrix. The benchmark
         prices are fetched once here and shared with every ticker's job (Tiingo's free tier allows about
         50 requests an hour: a batch of n tickers costs 2 + n + 1).
collect  reads each ticker job's result, copies its cache files into data/stocks/, refreshes the near-live
         quotes of the batch's and every rated ticker in ONE request, writes the append-only archive and
         ratings rows and the page files, site/data/stocks/batch.json (each ticker's status and error), and
         sends ONE email for the batch when asked. The workflow commits once afterwards.
A failed ticker is reported with its error and never blocks the others.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from cew import util

from . import config, notify, prices, publish

CACHE_DIRS = ("fundamentals", "prices", "calls")  # what a ticker's job may hand back; never runs/ or ratings.csv


def parse(raw: str | None) -> tuple[list[str], list[str]]:
    """(valid tickers in order, de-duplicated; invalid entries as given)."""
    valid, invalid = [], []
    for part in re.split(r"[\s,]+", str(raw or "").strip()):
        if not part:
            continue
        t = config.normalize_ticker(part)
        if t is None:
            invalid.append(part[:20])
        elif t not in valid:
            valid.append(t)
    return valid, invalid


def plan(raw: str | None, cfg: dict) -> dict:
    """{"tickers": [...], "error": None} or {"tickers": [], "error": reason}."""
    tickers, invalid = parse(raw)
    cap = int((cfg.get("batch") or {}).get("max_batch", 256))
    if invalid:
        return {"tickers": [], "error": "Invalid ticker(s): " + ", ".join(invalid)
                + " (1-10 characters, a letter first, then letters, digits, '.' or '-')"}
    if not tickers:
        return {"tickers": [], "error": "No ticker given"}
    if len(tickers) > cap:
        return {"tickers": [], "error": f"{len(tickers)} tickers; a batch takes at most {cap}"}
    return {"tickers": tickers, "error": None}


def fetch_benchmarks(cfg: dict, key: str | None, shared: Path, sess=None) -> dict:
    """SPY/QQQ daily prices once for the batch -> shared/<T>.csv. Returns {ticker: error}."""
    errors = {}
    if not key:
        return {t: "STOCK_PRICE_API_KEY not set" for t in prices.benchmarks(cfg)}
    sess = sess or util.session("stock-research/1.0")
    start = prices.start_date(cfg)
    for t in prices.benchmarks(cfg):
        try:
            util.write_csv(shared / f"{t}.csv", prices.fetch_daily(sess, t, key, start), prices.FIELDS)
        except Exception as e:
            errors[t] = scrub(str(e))[:200] if isinstance(e, prices.PriceError) else type(e).__name__
    return errors


def scrub(text: str) -> str:
    """Remove secret values from a message before it is written anywhere."""
    out = str(text)
    for k in ("STOCK_PRICE_API_KEY", "ANTHROPIC_API_KEY", "SMTP_PASSWORD", "GITHUB_TOKEN"):
        v = util.env(k)
        if v and len(v) >= 4:
            out = out.replace(v, "***")
    return out


# ---------------------------------------------------------------- a ticker's job
def result_path(out: Path, ticker: str) -> Path:
    return out / "result" / f"{ticker}.json"


def write_result(out: Path, ticker: str, result: dict) -> None:
    safe = re.sub(r"[^A-Za-z0-9.\-]", "_", ticker) or "_"
    util.write_json(result_path(out, safe), {"ticker": ticker, **result})


def stage(out: Path, ticker: str) -> list[str]:
    """Copy the ticker's cache files (fundamentals, prices, earnings releases) under out/data/stocks/."""
    src, dst, copied = config.data_dir(), out / "data" / "stocks", []
    for rel in (f"fundamentals/{ticker}.json", f"prices/{ticker}.csv", f"calls/{ticker}"):
        p = src / rel
        if p.is_dir():
            shutil.copytree(p, dst / rel, dirs_exist_ok=True)
        elif p.is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / rel)
        else:
            continue
        copied.append(rel)
    return copied


# ---------------------------------------------------------------- collect
def _copy_caches(out: Path, shared: Path | None) -> None:
    dst = config.data_dir()
    src = out / "data" / "stocks"
    for d in CACHE_DIRS:
        if (src / d).is_dir():
            shutil.copytree(src / d, dst / d, dirs_exist_ok=True)
    if shared and shared.is_dir():
        for p in shared.glob("*.csv"):
            (dst / "prices").mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / "prices" / p.name)


def _read_results(out: Path, tickers: list[str]) -> dict:
    results = {}
    for t in tickers:
        r = util.read_json(result_path(out, t))
        if not isinstance(r, dict) or r.get("status") not in ("ok", "failed"):
            r = {"status": "failed", "error": "no result from this ticker's job (it crashed or was cancelled; see its log)"}
        elif r["status"] == "ok" and not isinstance(r.get("report"), dict):
            r = {"status": "failed", "error": "the ticker's job wrote no report"}
        results[t] = r
    return results


def _previous() -> dict:
    """Latest earlier rating per ticker, from ratings.csv."""
    out = {}
    for row in util.read_csv(publish.ratings_csv()):
        out[row["ticker"]] = {"rating": row.get("rating"), "score": util.to_float(row.get("score")),
                              "timestamp": row.get("timestamp")}
    return out


def collect(out: Path, shared: Path | None, tickers: list[str], email: bool, cfg: dict,
            run_id: str | None = None, run_url: str | None = None) -> dict:
    """Publish every finished ticker once; returns the batch summary (also site/data/stocks/batch.json)."""
    started = util.now_iso()
    _copy_caches(out, shared)
    results = _read_results(out, tickers)
    ok = sorted((r["report"] for r in results.values() if r["status"] == "ok"), key=lambda r: r["run_id"])
    key = util.env("STOCK_PRICE_API_KEY")
    affected = [r["ticker"] for r in ok]
    q = prices.quotes(list(dict.fromkeys([*affected, *publish.rated_tickers()])), key,
                      max_tickers=int((cfg.get("prices") or {}).get("quotes_max_tickers", 100))) if affected else \
        {"quotes": {}, "endpoint": None, "requests": 0, "error": None}
    for r in ok:
        if q.get("error"):
            r.setdefault("errors", {})["quotes"] = q["error"]
        publish.attach_quote(r, q["quotes"].get(r["ticker"]))
    before = _previous()
    rows = []
    for t in tickers:
        res = results[t]
        if res["status"] != "ok":
            rows.append({"ticker": t, "status": "failed", "error": scrub(str(res.get("error") or "failed"))[:500]})
            continue
        r = res["report"]
        cn = r.get("close_note") or {}
        rows.append({"ticker": t, "status": "done", "name": r.get("name"), "rating": r["rating"], "score": r["score"],
                     "confidence": r["confidence"], "previous": before.get(t), "price": (r.get("price") or {}).get("close"),
                     "scored_on": (r.get("price") or {}).get("date"), "quote": r.get("quote"),
                     "close_note": cn.get("note"), "close_window": cn.get("close_window"),
                     "cost_usd": (r.get("cost") or {}).get("total_usd"), "run_id": r["run_id"],
                     "news_from": r.get("news_from"),
                     "direction_unchecked": [c["label"] for c in (r.get("news") or {}).get("direction_unchecked") or []],
                     "errors": sorted((r.get("errors") or {}).keys())})
    costs = [x["cost_usd"] for x in rows if x.get("cost_usd") is not None]
    summary = {"run_id": run_id, "run_url": run_url, "started_at": started, "tickers": rows,
               "done": sum(x["status"] == "done" for x in rows), "failed": sum(x["status"] == "failed" for x in rows),
               "cost_usd": round(sum(costs), 4) if costs else None,
               "quotes": {k: q.get(k) for k in ("endpoint", "requests", "error")},
               "close_window": next((x["close_window"] for x in rows if x.get("close_window")), None)}

    # One email for the whole batch (a batch of one gets the full single-stock email), before the
    # append-only archive is written so each run records what happened to it.
    status = "skipped (email off)"
    if email and (ok or rows):
        try:
            status = notify.send(ok[0]) if len(rows) == 1 and ok else notify.send_batch(summary)
        except Exception as e:
            status = f"failed: {type(e).__name__}"
            summary["email_error"] = scrub(f"{type(e).__name__}: {str(e)[:200]}")
    summary["email"] = status
    for r in ok:
        r["email"] = status
        r["batch"] = {"run_id": run_id, "tickers": tickers}
        res = results[r["ticker"]]
        archived = publish.write_all(r, q["quotes"], res.get("chart"), cfg)
        next(x for x in rows if x["ticker"] == r["ticker"])["archived"] = f"data/stocks/runs/{r['ticker']}/{archived}"
    summary["finished_at"] = util.now_iso()
    util.write_json(config.site_dir() / "batch.json", summary)
    return summary


def commit_message(summary: dict) -> str:
    done = [x["ticker"] for x in summary["tickers"] if x["status"] == "done"]
    failed = [x["ticker"] for x in summary["tickers"] if x["status"] == "failed"]
    rescore = any(x.get("news_from") for x in summary["tickers"] if x["status"] == "done")
    msg = f"Stock research {'(rescore) ' if rescore else ''}{', '.join(done) or 'batch'} {util.today().isoformat()}"
    return msg + (f" ({len(failed)} failed: {', '.join(failed)})" if failed else "")


def _output(**kv) -> None:
    path = util.env("GITHUB_OUTPUT")
    lines = [f"{k}={v}" for k, v in kv.items()]
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Batch stock runs")
    p.add_argument("step", choices=["plan", "collect"])
    p.add_argument("--shared", default="shared", help="folder for the benchmark prices fetched once per batch")
    p.add_argument("--out", default="out", help="folder holding every ticker job's result (collect)")
    p.add_argument("--message-file", help="collect: write the commit message here")
    args = p.parse_args(argv)
    cfg = config.load()
    shared = Path(args.shared)

    if args.step == "plan":
        pl = plan(util.env("INPUT_TICKERS"), cfg)
        if pl["error"]:
            print(f"::error::{pl['error']}")
            return 2
        par = max(1, int((cfg.get("batch") or {}).get("max_parallel", 5)))
        _output(tickers=json.dumps(pl["tickers"]), count=len(pl["tickers"]), max_parallel=par)
        errors = fetch_benchmarks(cfg, util.env("STOCK_PRICE_API_KEY"), shared)
        for t, e in errors.items():
            print(f"::warning::Benchmark {t}: {e}")
        print(f"Plan: {', '.join(pl['tickers'])}; benchmarks fetched once: "
              f"{', '.join(t for t in prices.benchmarks(cfg) if t not in errors) or 'none'}")
        return 0

    try:
        tickers = json.loads(util.env("PLAN_TICKERS") or "[]")
    except ValueError:
        tickers = []
    tickers = [t for t in (config.normalize_ticker(x) for x in tickers if isinstance(x, str)) if t]
    if not tickers:
        print("::error::No planned tickers to collect")
        return 2
    run_id = util.env("GITHUB_RUN_ID")
    server, repo = util.env("GITHUB_SERVER_URL", "https://github.com"), util.env("GITHUB_REPOSITORY")
    summary = collect(Path(args.out), shared, tickers, util.env("INPUT_SEND_EMAIL") == "true", cfg,
                      run_id, f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else None)
    if args.message_file:
        Path(args.message_file).write_text(commit_message(summary) + "\n", encoding="utf-8")
    for x in summary["tickers"]:
        if x["status"] == "failed":
            print(f"::warning::{x['ticker']} failed: {x['error']}")
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
