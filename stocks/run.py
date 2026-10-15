"""One stock research run: resolve -> SEC fundamentals -> prices -> earnings release -> factor scores ->
Claude news (research, topic ratings, source checks) -> composite, rating, overlay -> write-up -> publish.

Usage:
  python -m stocks.run MSFT                 # full run (needs secrets in the environment)
  python -m stocks.run MSFT --email         # also email the result
  python -m stocks.run MSFT --no-llm        # rule-based factors only (no news & risk, no write-up)
  python -m stocks.run MSFT --offline       # re-score cached data in data/stocks/ (no network)
  python -m stocks.run MSFT --reuse-news    # new SEC data and prices, the latest run's news; new write-up only
  python -m stocks.run MSFT --batch-out out --shared shared   # one ticker of a batch (stocks/batch.py)
The ticker may also come from INPUT_TICKER, and --reuse-news from INPUT_REUSE_NEWS=true (stocks.yml).

--reuse-news re-fetches SEC data and prices and re-scores every code factor, but keeps the latest archived
run's news topic ratings and source checks as they were (no research, no topic rating, no source check, no
guidance extraction; labelled "news from <time>"). The only Claude call is the write-up.

A batch's ticker job (--batch-out) rates the ticker with the benchmark prices the plan job fetched once
(--shared), then writes its result and cache files under the --batch-out folder and nothing else: no quote,
no email, no append-only files. The batch's collect job does those once for every ticker.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from cew import edgar_text, models, sources, util

from . import batch, config, fundamentals, guidance, metrics, notify, prices, publish, research, resolve, scoring

BRIEF_SUMMARY_KEYS = ["latest_quarter_end", "ttm_end", "currency", "foreign", "revenue_ttm", "revenue_yoy_q",
                      "revenue_yoy_ttm", "acceleration_pp", "gross_margin_pct", "operating_margin_pct", "fcf_ttm",
                      "fcf_margin_pct", "capex_intensity_pct", "sbc_pct_revenue", "dilution_yoy_pct"]


def _host(url: str | None) -> str | None:
    u = (url or "").strip()
    if not u:
        return None
    host = (urlsplit(u if "://" in u else "https://" + u).hostname or "").lower().removeprefix("www.")
    return host or None


def tiers_config(cfg: dict, website: str | None) -> dict:
    """cfg's source tiers, with the company's own website added to primary."""
    t = copy.deepcopy(cfg)
    host = _host(website)
    if host:
        t.setdefault("source_tiers", {}).setdefault("primary", []).append(host)
    return t


def topic_brief(ticker, name, sector, s, val, tr, fs, g, ctx) -> dict:
    return {
        "company": {"ticker": ticker, "name": name, "sector": sector},
        "factor_scores_by_code": {k: (fs.get(k) or {}).get("score") for k in scoring.FACTORS if k != "news_risk"},
        "fundamentals": {k: s.get(k) for k in BRIEF_SUMMARY_KEYS if k in s},
        "valuation": {k: val.get(k) for k in ("price_to_sales", "price_to_fcf", "ev_to_ebit", "fcf_yield_pct",
                                              "fcf_yield_vs_10y_pp") if k in val} or {"missing": val.get("missing")},
        "trend": {k: tr.get(k) for k in ("vs_ma200_pct", "momentum_12_1_pct", "drawdown_52w_pct") if k in tr},
        "latest_earnings_release": None if g.get("error") else {
            "filed": g.get("filing_date"), "url": g.get("url"), "summary": g.get("summary"),
            "guidance_quotes_verified": [{k: x[k] for k in ("metric", "period", "sentence", "stated_change")}
                                         for x in g.get("guidance") or []]},
        "capex_dashboard": ctx,
    }


def writeup_brief(r: dict) -> dict:
    fs = r["factors"]
    return {
        "company": {"ticker": r["ticker"], "name": r["name"], "sector": r.get("sector")},
        "rating": r["rating"], "score": r["score"], "confidence": r["confidence"],
        "band_distance": r["bands"], "rating_before_overlay": r["rating_before_overlay"],
        "capex_overlay_note": (r.get("overlay") or {}).get("note"),
        "confidence_reasons": [x["text"] for x in (r.get("confidence_detail") or {}).get("reasons") or []],
        "factors": {k: {"score": fs[k].get("score"), "missing": fs[k].get("missing"),
                        "inputs": {m: v.get("value") for m, v in (fs[k].get("metrics") or {}).items()},
                        "notes": {m: v["note"] for m, v in (fs[k].get("metrics") or {}).items() if v.get("note")}}
                    for k in scoring.FACTORS},
        "factor_weights_used": r["composite"].get("weights_used"),
        "news_topics": {k: {"rating": t.get("rating"), "summary": t.get("summary"),
                            "capped": "; ".join(t[n] for n in ("cap_note", "weak_sources_note") if t.get(n)) or None}
                        for k, t in ((r.get("news") or {}).get("topics") or {}).items()},
        "earnings_release": {k: (r.get("guidance") or {}).get(k) for k in ("filing_date", "summary")},
        "price": r.get("price"),
        **({"news_from": f"the news ratings are reused from the run of {r['news_from']} (not refreshed this run)"}
           if r.get("news_from") else {}),
    }


class RunError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def research_one(ticker: str, cfg: dict, offline: bool = False, no_llm: bool = False, shared=None,
                 reuse_news: bool = False) -> tuple[dict, list]:
    """Rate one ticker: everything up to (not including) the quote, email and publish steps.
    Returns (report, price chart); raises RunError when there is nothing to rate."""
    started = util.now_utc()
    run_id = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = started.date()
    selection = config.resolve_models(cfg)
    llm_on = bool(util.env("ANTHROPIC_API_KEY")) and not no_llm
    key = util.env("STOCK_PRICE_API_KEY")
    sec_ua = util.env("SEC_USER_AGENT") or f"StockResearch {util.env('SMTP_USERNAME', 'research@example.com')}"
    sec = util.session(sec_ua)
    errors: dict = {}
    print(f"Stock run {run_id}: {ticker}. Models: research={selection['research']} ({selection['sources']['research']}), "
          f"scoring={selection['scoring']} ({selection['sources']['scoring']}); Claude {'on' if llm_on else 'off'}"
          f"{'; reusing the latest news' if reuse_news else ''}", flush=True)

    # 1. ticker -> CIK
    if offline:
        cached = util.read_json(fundamentals.cache_path(ticker))
        if not cached:
            raise RunError(f"Offline and no cached data for {ticker}.", 2)
        ent = {"ticker": ticker, "sec_ticker": ticker, "cik": cached["cik"], "name": cached.get("entity") or ticker}
    else:
        try:
            ent = resolve.lookup(sec, ticker)
        except resolve.TickerNotFound as e:
            raise RunError(str(e), 2) from None
        except Exception as e:
            raise RunError(f"Could not read SEC company_tickers.json: {type(e).__name__}: {str(e)[:200]}") from None
    cik, name = ent["cik"], ent["name"]
    print(f"{ticker} -> CIK {cik} ({name})", flush=True)
    subs: dict = {}
    if not offline:
        try:
            subs = edgar_text.submissions(sec, cik)
        except Exception as e:
            errors["sec:submissions"] = f"{type(e).__name__}: {str(e)[:200]}"
    name = subs.get("name") or name
    sector, website = subs.get("sicDescription"), subs.get("website") or subs.get("investorWebsite")

    # 2. data
    print("Fetching SEC companyfacts ...", flush=True)
    f = fundamentals.run(sec, ticker, cik, offline)
    if f.get("error"):
        errors["sec:companyfacts"] = f["error"]
    s = fundamentals.summarize(f, cfg) if not f.get("error") else {}
    if not f.get("error") and not f.get("taxonomy"):
        errors["sec:companyfacts"] = "no revenue in us-gaap or ifrs-full XBRL facts"
    print("Fetching prices ...", flush=True)
    px = prices.run(ticker, cfg, key, offline, shared_dir=shared)
    for t, e in px["errors"].items():
        errors[f"prices:{t}"] = e
    rows = px["series"].get(ticker)
    usage_by_step: dict = {}
    print("Reading the latest earnings release ...", flush=True)
    g = guidance.run(sec, cik, ticker, name, subs, selection["scoring"], llm_on and not reuse_news, offline, usage_by_step, cfg)
    if g.get("error"):
        errors["guidance"] = g["error"]
    dgs10, ctx = metrics.ust10y(), metrics.capex_context(today=today)
    val = metrics.valuation(s, f, rows, dgs10, cfg) if s else {"missing": "no fundamentals"}
    tr = metrics.trend(rows, px["series"], cfg)
    fs = scoring.factors(s, val, tr, cfg)

    # 3. Claude news, checked by code (or, with --reuse-news, the latest run's as it was)
    claude: dict = {"enabled": llm_on}
    topics, source_check, news_from, news_models, news_missing = None, None, None, None, None
    tcfg = tiers_config(cfg, website)
    if reuse_news:
        prev = publish.latest_news(ticker)
        if prev:
            topics, source_check, news_from, news_models = prev["topics"], prev["source_check"], prev["news_from"], prev["models"]
            claude["news_reused"] = {"news_from": news_from, "archived": prev["archived"], "models": news_models}
            print(f"Reusing the news topics of {news_from} ({prev['archived']})", flush=True)
        else:
            news_missing = "--reuse-news: no earlier run of this ticker with news topics"
            errors["news"] = news_missing
    elif llm_on:
        print("Claude research and topic ratings ...", flush=True)
        res = research.run_news(cfg, selection, ticker, name, sector, topic_brief(ticker, name, sector, s, val, tr, fs, g, ctx),
                                tcfg, usage_by_step)
        claude["research"] = res["research"]
        if res["research"].get("error"):
            errors["claude:research"] = res["research"]["error"]
        if res.get("error"):
            errors["claude"] = res["error"]
        topics = res.get("topics")
    if topics and not reuse_news:
        labels = {k: v.get("label", k) for k, v in ((cfg.get("news_risk") or {}).get("topics") or {}).items()}
        for k, t in topics.items():
            t["label"] = labels.get(k, k)
        ev = [e for t in topics.values() for e in t.get("evidence") or []]
        sources.tag_tiers(ev, tcfg)
        sources.tag_dates(ev, today, cfg)
        if offline:
            source_check = {"skipped": "offline run"}
        else:
            print("Checking evidence URLs ...", flush=True)
            source_check = {"counts": sources.check_evidence(topics, cfg)}
        for t in topics.values():
            scoring.cap_topic(t)
            scoring.cap_weak_sources(t, cfg)
    fs["news_risk"] = scoring.news_risk(topics, cfg, news_missing or (None if llm_on else "Claude step off (no ANTHROPIC_API_KEY or --no-llm)"))

    # 4. composite, rating, overlay
    comp = scoring.composite(fs, cfg)
    if comp["score"] is None:
        raise RunError("No factor had data; nothing to rate. " + json.dumps(errors, default=str))
    band = scoring.band(comp["score"], cfg)
    ov = scoring.overlay(ticker, band, ctx, cfg)
    conf = scoring.confidence_detail(fs, comp["score"], s, val, topics, cfg)
    report = {
        "run_id": run_id, "generated_at": util.now_iso(), "ticker": ticker, "sec_ticker": ent["sec_ticker"],
        "cik": cik, "name": name, "sector": sector, "website": website,
        "models": selection, "llm": llm_on,
        "rating": ov.get("rating", band), "rating_before_overlay": band, "score": comp["score"],
        "confidence": conf["level"], "confidence_detail": conf, "bands": scoring.distance(comp["score"], cfg),
        "composite": comp, "factors": fs, "overlay": ov, "capex_context": ctx,
        "price": {"close": rows[-1]["close"], "date": rows[-1]["date"]} if rows else {},
        "fundamentals": s, "valuation": val, "trend": tr, "ust10y": dgs10, "guidance": g,
        "news": {"topics": topics or {}, "source_check": source_check, "reused_from": news_from}, "claude": claude,
        "news_from": news_from, "news_models": news_models,
        "subject_prefix": (cfg.get("email") or {}).get("subject_prefix", "[Stock]"),
        "disclaimer": publish.DISCLAIMER, "errors": errors,
    }

    # 5. write-up (Claude is given the rating; it never sets it)
    if llm_on:
        print("Claude write-up ...", flush=True)
        usage_by_step["writeup"] = models.new_usage(selection["scoring"])
        try:
            report["writeup"] = research.writeup(selection["scoring"], today, writeup_brief(report), usage_by_step["writeup"])
        except Exception as e:
            errors["claude:writeup"] = f"{type(e).__name__}: {str(e)[:300]}"
    used = {k: u for k, u in usage_by_step.items() if u["requests"]}
    report["cost"] = models.cost_summary_tiered(used, models.load_prices()) if used else None
    return report, metrics.chart(rows)


def summary(report: dict, archived: str | None = None) -> dict:
    fs = report.get("factors") or {}
    cn = report.get("close_note") or {}
    return {"run_id": report["run_id"], "ticker": report["ticker"],
            **({"archived": f"data/stocks/runs/{report['ticker']}/{archived}"} if archived else {}),
            "rating": report["rating"], "rating_before_overlay": report.get("rating_before_overlay"), "score": report["score"],
            "confidence": report["confidence"],
            "confidence_reasons": [x["text"] for x in (report.get("confidence_detail") or {}).get("reasons") or []],
            "news_from": report.get("news_from"),
            "factors": {k: (fs.get(k) or {}).get("score") for k in scoring.FACTORS},
            "overlay": (report.get("overlay") or {}).get("note"), "cost_usd": (report.get("cost") or {}).get("total_usd"),
            "scored_on": (report.get("price") or {}).get("date"), "quote": report.get("quote"),
            "close_note": cn.get("note"), "close_window": cn.get("close_window"),
            "source_check": (report.get("news") or {}).get("source_check"), "email": report.get("email"),
            "errors": report.get("errors")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Research one stock")
    p.add_argument("ticker", nargs="?")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--email", action="store_true")
    p.add_argument("--reuse-news", action="store_true",
                   help="keep the latest run's news ratings and source checks; new SEC data, prices and write-up")
    p.add_argument("--batch-out", help="one ticker of a batch: write the result here, publish nothing")
    p.add_argument("--shared", help="benchmark price CSVs fetched once by the batch's plan job")
    args = p.parse_args(argv)

    cfg = config.load()
    raw = args.ticker or util.env("INPUT_TICKER")
    ticker = config.normalize_ticker(raw)
    if not ticker:
        msg = "Invalid ticker: use 1-10 characters, a letter first, then letters, digits, '.' or '-'."
        print(msg, file=sys.stderr)
        if args.batch_out:
            batch.write_result(Path(args.batch_out), str(raw or "")[:12], {"status": "failed", "error": msg})
        return 2
    try:
        reuse = args.reuse_news or util.env("INPUT_REUSE_NEWS") == "true"
        report, chart = research_one(ticker, cfg, args.offline, args.no_llm, args.shared, reuse)
    except RunError as e:
        print(str(e), file=sys.stderr)
        if args.batch_out:
            batch.write_result(Path(args.batch_out), ticker, {"status": "failed", "error": batch.scrub(str(e))[:500]})
        return e.code
    except Exception as e:
        if not args.batch_out:
            raise
        msg = batch.scrub(f"{type(e).__name__}: {str(e)[:400]}")
        print(f"Run failed: {msg}", file=sys.stderr)
        batch.write_result(Path(args.batch_out), ticker, {"status": "failed", "error": msg})
        return 1

    if args.batch_out:
        batch.write_result(Path(args.batch_out), ticker, {"status": "ok", "report": report, "chart": chart})
        batch.stage(Path(args.batch_out), ticker)
        print(json.dumps(summary(report), indent=1, default=str))
        return 0

    # A single run: one quote request for this ticker and every rated ticker (the list's latest prices)
    q: dict = {"quotes": {}, "error": None}
    if not args.offline:
        q = prices.quotes([ticker, *publish.rated_tickers()], util.env("STOCK_PRICE_API_KEY"),
                          max_tickers=int((cfg.get("prices") or {}).get("quotes_max_tickers", 100)))
    if q.get("error"):
        report["errors"]["quotes"] = q["error"]
    publish.attach_quote(report, q["quotes"].get(ticker))
    if args.email:
        try:
            report["email"] = notify.send(report)
        except Exception as e:
            report["email"] = f"failed: {type(e).__name__}"
            report["errors"]["email"] = f"{type(e).__name__}: {str(e)[:200]}"
    else:
        report["email"] = "skipped (email off)"
    archived = publish.write_all(report, q["quotes"], chart, cfg)
    print(json.dumps(summary(report, archived), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
