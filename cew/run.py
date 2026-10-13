"""Weekly pipeline: fetch -> score -> Claude digest -> publish -> email.

Usage:
  python -m cew.run                 # full run (needs secrets in the environment)
  python -m cew.run --no-email      # everything except sending the email
  python -m cew.run --no-llm        # skip Claude (rule-based scores only)
  python -m cew.run --offline       # reuse cached data in data/ (no network fetches)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import analyst, fetch_edgar, fetch_fred, fetch_taiwan, models, notify, publish, scoring, util


def data_brief(fred: dict, edgar: dict, tw: dict, rule_scores: dict) -> dict:
    """Compact, history-free summary of the official data for Claude."""
    companies = {}
    for t, c in edgar["companies"].items():
        companies[t] = {
            k: ({kk: vv for kk, vv in v.items() if kk != "history"} if isinstance(v, dict) else v)
            for k, v in c.items() if k != "tags"
        }
    return {
        "rule_scores": {k: {"score": v["score"], "reasons": v["reasons"]} for k, v in rule_scores.items()},
        "macro_and_credit": fred["series"],
        "hyperscalers": companies,
        "taiwan_latest_period": tw.get("latest_period"),
        "taiwan_latest": tw.get("latest"),
        "taiwan_median_yoy_recent": dict(list(tw.get("median_yoy_by_period", {}).items())[-6:]),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--no-email", action="store_true")
    args = p.parse_args(argv)

    cfg = util.load_config()
    today = util.today().isoformat()
    selection = models.resolve(cfg)
    print(f"Models: research={selection['research']} ({selection['sources']['research']}), "
          f"assessment={selection['assessment']} ({selection['sources']['assessment']})", flush=True)
    sec_ua = util.env("SEC_USER_AGENT") or f"CapexEarlyWarning {util.env('SMTP_USERNAME', 'research@example.com')}"

    print("Fetching FRED ...", flush=True)
    fred = fetch_fred.run(cfg, util.env("FRED_API_KEY"), offline=args.offline)
    print("Fetching SEC EDGAR ...", flush=True)
    edgar = fetch_edgar.run(cfg, sec_ua, offline=args.offline)
    print("Fetching Taiwan monthly revenue ...", flush=True)
    tw = fetch_taiwan.run(cfg, offline=args.offline)

    rule = {
        "credit": scoring.score_credit(fred, cfg),
        "taiwan_supply_chain": scoring.score_taiwan(tw, cfg),
        "hyperscaler_filings": scoring.score_filings(edgar, cfg),
    }

    previous = publish.previous_report(today)
    prev_summary = None
    if previous:
        prev_summary = {"date": previous.get("date"), "composite": previous.get("composite"),
                        "scores": {k: v.get("score") for k, v in previous.get("scores", {}).items()},
                        "headline": ((previous.get("claude") or {}).get("assessment") or {}).get("headline")}

    llm_on = bool(util.env("ANTHROPIC_API_KEY")) and not args.no_llm
    print(f"Claude step: {'on' if llm_on else 'off'} ...", flush=True)
    claude = analyst.run(cfg, data_brief(fred, edgar, tw, rule), prev_summary, llm_on, selection)
    prices = models.load_prices()
    cost = models.cost_summary(claude.pop("usage"), prices) if claude.get("usage") else None
    models.write_catalog(prices, models.resolve(cfg, use_inputs=False), use_api=llm_on and not args.offline)

    scores = dict(rule)
    a = claude.get("assessment") or {}
    for k in scoring.NEWS_CATEGORIES:
        n = (a.get("news_scores") or {}).get(k)
        if n:
            scores[k] = {"score": int(n["score"]), "reasons": [n["summary"]], "evidence": n.get("evidence", []),
                         "data_ok": True}
        else:
            scores[k] = {"score": 0, "reasons": ["Not scored this week (Claude step unavailable)"],
                         "evidence": [], "data_ok": False}
    adj = a.get("credit_news_adjustment") or {}
    if adj.get("delta"):
        scores["credit"]["score"] = max(0, min(3, scores["credit"]["score"] + int(adj["delta"])))
        scores["credit"]["reasons"].append(f"News adjustment {int(adj['delta']):+d}: {adj.get('reason', '')}")

    comp = scoring.composite(scores, cfg)
    errors = {}
    for name, src in (("fred", fred), ("edgar", edgar), ("taiwan", tw)):
        for k, v in (src.get("errors") or {}).items():
            errors[f"{name}:{k}"] = v
    if claude.get("error") and llm_on:
        errors["claude"] = claude["error"]
    if (claude.get("research") or {}).get("error"):
        errors["claude:research"] = claude["research"]["error"]

    report = {
        "date": today,
        "generated_at": util.now_iso(),
        "categories": scoring.CATEGORIES,
        "scores": scores,
        "composite": comp,
        "claude": claude,
        "cost": cost,
        "data": {"fred": fred["series"], "edgar": edgar["companies"], "taiwan": tw},
        "errors": errors,
    }
    publish.write_all(report, cfg)

    if args.no_email:
        report["email"] = "skipped (--no-email)"
    elif not cfg["email"].get("send_every_week", True) and not comp["alert"]:
        report["email"] = "skipped (no alert and send_every_week is false)"
    else:
        try:
            report["email"] = notify.send(report)
        except Exception as e:
            report["email"] = f"failed: {e}"
            errors["email"] = str(e)
    publish.write_report(report)

    print(json.dumps({"date": today, "composite": comp,
                      "scores": {k: v["score"] for k, v in scores.items()},
                      "cost_usd": (cost or {}).get("total_usd"),
                      "email": report["email"], "errors": errors}, indent=1))
    # Fail the workflow only if nothing at all could be fetched.
    if not fred["series"] and not edgar["companies"] and not tw.get("latest"):
        print("No data sources succeeded.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
