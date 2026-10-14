"""One stock research run: resolve -> SEC fundamentals -> prices -> earnings release -> factor scores ->
Claude news (research, topic ratings, source checks) -> composite, rating, overlay -> write-up -> publish.

Usage:
  python -m stocks.run MSFT                 # full run (needs secrets in the environment)
  python -m stocks.run MSFT --email         # also email the result
  python -m stocks.run MSFT --no-llm        # rule-based factors only (no news & risk, no write-up)
  python -m stocks.run MSFT --offline       # re-score cached data in data/stocks/ (no network)
The ticker may also come from INPUT_TICKER (the workflow input).
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from urllib.parse import urlsplit

from cew import edgar_text, models, sources, util

from . import config, fundamentals, guidance, metrics, notify, prices, publish, research, resolve, scoring

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
        "factors": {k: {"score": fs[k].get("score"), "missing": fs[k].get("missing"),
                        "inputs": {m: v.get("value") for m, v in (fs[k].get("metrics") or {}).items()}}
                    for k in scoring.FACTORS},
        "factor_weights_used": r["composite"].get("weights_used"),
        "news_topics": {k: {"rating": t.get("rating"), "summary": t.get("summary"),
                            "capped": "; ".join(t[n] for n in ("cap_note", "weak_sources_note") if t.get(n)) or None}
                        for k, t in ((r.get("news") or {}).get("topics") or {}).items()},
        "earnings_release": {k: (r.get("guidance") or {}).get(k) for k in ("filing_date", "summary")},
        "price": r.get("price"),
    }


def refresh_now(ticker: str, cfg: dict, key: str | None) -> dict:
    """Latest closes for other rated tickers (the "now" column), oldest first, at most prices.refresh_now_max."""
    if not key:
        return {}
    index = util.read_json(config.site_dir() / "index.json") or {}
    others = sorted((e for e in index.get("tickers") or [] if e.get("ticker") != ticker),
                    key=lambda e: e.get("price_now_date") or "")
    n = int((cfg.get("prices") or {}).get("refresh_now_max", 10))
    sess = util.session("stock-research/1.0")
    out = {}
    for e in others[:n]:
        p = prices.latest_close(sess, e["ticker"], key)
        if p:
            out[e["ticker"]] = p
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Research one stock")
    p.add_argument("ticker", nargs="?")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--email", action="store_true")
    args = p.parse_args(argv)

    cfg = config.load()
    ticker = config.normalize_ticker(args.ticker or util.env("INPUT_TICKER"))
    if not ticker:
        print("Invalid ticker: use 1-10 characters, a letter first, then letters, digits, '.' or '-'.", file=sys.stderr)
        return 2
    started = util.now_utc()
    run_id = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = started.date()
    selection = config.resolve_models(cfg)
    llm_on = bool(util.env("ANTHROPIC_API_KEY")) and not args.no_llm
    key = util.env("STOCK_PRICE_API_KEY")
    sec_ua = util.env("SEC_USER_AGENT") or f"StockResearch {util.env('SMTP_USERNAME', 'research@example.com')}"
    sec = util.session(sec_ua)
    errors: dict = {}
    print(f"Stock run {run_id}: {ticker}. Models: research={selection['research']} ({selection['sources']['research']}), "
          f"scoring={selection['scoring']} ({selection['sources']['scoring']}); Claude {'on' if llm_on else 'off'}", flush=True)

    # 1. ticker -> CIK
    if args.offline:
        cached = util.read_json(fundamentals.cache_path(ticker))
        if not cached:
            print(f"Offline and no cached data for {ticker}.", file=sys.stderr)
            return 2
        ent = {"ticker": ticker, "sec_ticker": ticker, "cik": cached["cik"], "name": cached.get("entity") or ticker}
    else:
        try:
            ent = resolve.lookup(sec, ticker)
        except resolve.TickerNotFound as e:
            print(str(e), file=sys.stderr)
            return 2
        except Exception as e:
            print(f"Could not read SEC company_tickers.json: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            return 1
    cik, name = ent["cik"], ent["name"]
    print(f"{ticker} -> CIK {cik} ({name})", flush=True)
    subs: dict = {}
    if not args.offline:
        try:
            subs = edgar_text.submissions(sec, cik)
        except Exception as e:
            errors["sec:submissions"] = f"{type(e).__name__}: {str(e)[:200]}"
    name = subs.get("name") or name
    sector, website = subs.get("sicDescription"), subs.get("website") or subs.get("investorWebsite")

    # 2. data
    print("Fetching SEC companyfacts ...", flush=True)
    f = fundamentals.run(sec, ticker, cik, args.offline)
    if f.get("error"):
        errors["sec:companyfacts"] = f["error"]
    s = fundamentals.summarize(f) if not f.get("error") else {}
    if not f.get("error") and not f.get("taxonomy"):
        errors["sec:companyfacts"] = "no revenue in us-gaap or ifrs-full XBRL facts"
    print("Fetching prices ...", flush=True)
    px = prices.run(ticker, cfg, key, args.offline)
    for t, e in px["errors"].items():
        errors[f"prices:{t}"] = e
    rows = px["series"].get(ticker)
    usage_by_step: dict = {}
    print("Reading the latest earnings release ...", flush=True)
    g = guidance.run(sec, cik, ticker, name, subs, selection["scoring"], llm_on, args.offline, usage_by_step, cfg)
    if g.get("error"):
        errors["guidance"] = g["error"]
    dgs10, ctx = metrics.ust10y(), metrics.capex_context(today=today)
    val = metrics.valuation(s, f, rows, dgs10, cfg) if s else {"missing": "no fundamentals"}
    tr = metrics.trend(rows, px["series"], cfg)
    fs = scoring.factors(s, val, tr, cfg)

    # 3. Claude news, checked by code
    claude: dict = {"enabled": llm_on}
    topics = None
    tcfg = tiers_config(cfg, website)
    if llm_on:
        print("Claude research and topic ratings ...", flush=True)
        res = research.run_news(cfg, selection, ticker, name, sector, topic_brief(ticker, name, sector, s, val, tr, fs, g, ctx),
                                tcfg, usage_by_step)
        claude["research"] = res["research"]
        if res["research"].get("error"):
            errors["claude:research"] = res["research"]["error"]
        if res.get("error"):
            errors["claude"] = res["error"]
        topics = res.get("topics")
    source_check = None
    if topics:
        labels = {k: v.get("label", k) for k, v in ((cfg.get("news_risk") or {}).get("topics") or {}).items()}
        for k, t in topics.items():
            t["label"] = labels.get(k, k)
        ev = [e for t in topics.values() for e in t.get("evidence") or []]
        sources.tag_tiers(ev, tcfg)
        sources.tag_dates(ev, today, cfg)
        if args.offline:
            source_check = {"skipped": "offline run"}
        else:
            print("Checking evidence URLs ...", flush=True)
            source_check = {"counts": sources.check_evidence(topics, cfg)}
        for t in topics.values():
            scoring.cap_topic(t)
            scoring.cap_weak_sources(t, cfg)
    fs["news_risk"] = scoring.news_risk(topics, cfg, None if llm_on else "Claude step off (no ANTHROPIC_API_KEY or --no-llm)")

    # 4. composite, rating, overlay
    comp = scoring.composite(fs, cfg)
    if comp["score"] is None:
        print("No factor had data; nothing to rate. " + json.dumps(errors, default=str), file=sys.stderr)
        return 1
    band = scoring.band(comp["score"], cfg)
    ov = scoring.overlay(ticker, band, ctx, cfg)
    report = {
        "run_id": run_id, "generated_at": util.now_iso(), "ticker": ticker, "sec_ticker": ent["sec_ticker"],
        "cik": cik, "name": name, "sector": sector, "website": website,
        "models": selection, "llm": llm_on,
        "rating": ov.get("rating", band), "rating_before_overlay": band, "score": comp["score"],
        "confidence": scoring.confidence(len(comp["available"]), cfg), "bands": scoring.distance(comp["score"], cfg),
        "composite": comp, "factors": fs, "overlay": ov, "capex_context": ctx,
        "price": {"close": rows[-1]["close"], "date": rows[-1]["date"]} if rows else {},
        "fundamentals": s, "valuation": val, "trend": tr, "ust10y": dgs10, "guidance": g,
        "news": {"topics": topics or {}, "source_check": source_check}, "claude": claude,
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
    report["cost"] = models.cost_summary(used, models.load_prices()) if used else None

    if args.email:
        try:
            report["email"] = notify.send(report)
        except Exception as e:
            report["email"] = f"failed: {type(e).__name__}"
            errors["email"] = f"{type(e).__name__}: {str(e)[:200]}"
    else:
        report["email"] = "skipped (email off)"
    now = refresh_now(ticker, cfg, key) if not args.offline else {}
    archived = publish.write_all(report, now, metrics.chart(rows))

    print(json.dumps({"run_id": run_id, "ticker": ticker, "archived": f"data/stocks/runs/{ticker}/{archived}",
                      "rating": report["rating"], "rating_before_overlay": band, "score": comp["score"],
                      "confidence": report["confidence"],
                      "factors": {k: fs[k].get("score") for k in scoring.FACTORS},
                      "overlay": ov.get("note"), "cost_usd": (report["cost"] or {}).get("total_usd"),
                      "source_check": source_check, "email": report["email"], "errors": errors}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
