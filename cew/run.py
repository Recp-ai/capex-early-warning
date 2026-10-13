"""One pipeline run: fetch -> filing text -> score -> Claude -> source checks -> publish -> email.

Usage:
  python -m cew.run                 # full run (needs secrets in the environment)
  python -m cew.run --no-email      # everything except sending the email
  python -m cew.run --no-llm        # skip Claude (rule-based scores only)
  python -m cew.run --offline       # reuse cached data in data/ (no network fetches)

RUN_TRIGGER (set by the workflow's plan step) tags the run: schedule, manual or event:<names>.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import date

from . import (analyst, calls, events, fetch_edgar, fetch_fred, fetch_taiwan, leases, models, notify, publish,
               scoring, sources, util)


def data_brief(fred: dict, edgar: dict, tw: dict, rule_scores: dict, filing_text: dict | None = None,
               run_events: list[dict] | None = None) -> dict:
    """Compact, history-free summary of the official data for Claude."""
    companies = {}
    for t, c in edgar["companies"].items():
        companies[t] = {
            k: ({kk: vv for kk, vv in v.items() if kk != "history"} if isinstance(v, dict) else v)
            for k, v in c.items() if k != "tags"
        }
    brief = {
        "rule_scores": {k: {"score": v["score"], "reasons": v["reasons"]} for k, v in rule_scores.items()},
        "macro_and_credit": fred["series"],
        "hyperscalers": companies,
        "taiwan_latest_period": tw.get("latest_period"),
        "taiwan_latest": tw.get("latest"),
        "taiwan_median_yoy_recent": dict(list(tw.get("median_yoy_by_period", {}).items())[-6:]),
    }
    ft = filing_text or {}
    if (ft.get("leases") or {}).get("companies"):
        brief["leases_not_yet_commenced"] = {
            t: {k: c[k] for k in ("quarter", "total_usd", "previous_quarter", "previous_total_usd", "qoq_pct")}
            for t, c in ft["leases"]["companies"].items()}
    if (ft.get("earnings") or {}).get("companies"):
        brief["earnings_press_releases"] = ft["earnings"]["companies"]
    if run_events:
        brief["events_this_run"] = [{k: e.get(k) for k in ("date", "name", "title")} for e in run_events]
    return brief


def email_decision(no_email: bool, trigger: str, comp: dict, scores: dict, previous: dict | None,
                   previous_level: str | None, cfg: dict) -> tuple[str | None, str | None]:
    """(reason to skip or None, reason an event run emails or None)."""
    if no_email:
        return "--no-email", None
    if events.names_in(trigger):
        why = scoring.event_email_reason(comp, scores, previous, previous_level, cfg)
        if not why:
            move = cfg["email"].get("event_category_move", 2)
            return f"event run: no level change and no category moved {move}+ points", None
        return None, why
    if not cfg["email"].get("send_every_week", True) and not comp["alert"]:
        return "no alert and send_every_week is false", None
    return None, None


def resolve_trigger() -> str:
    t = (util.env("RUN_TRIGGER") or "").strip()
    if t in ("schedule", "manual") or events.names_in(t):
        return t
    return "schedule" if util.env("EVENT_NAME") == "schedule" else "manual"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--no-email", action="store_true")
    args = p.parse_args(argv)

    cfg = util.load_config()
    started = util.now_utc()
    run_id = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = started.date().isoformat()
    trigger = resolve_trigger()
    all_events = events.load()
    us_day = events.us_date(started)
    run_events = events.for_trigger(trigger, all_events, us_day)
    selection = models.resolve(cfg)
    print(f"Run {run_id} ({trigger}). Models: research={selection['research']} ({selection['sources']['research']}), "
          f"assessment={selection['assessment']} ({selection['sources']['assessment']})", flush=True)
    sec_ua = util.env("SEC_USER_AGENT") or f"CapexEarlyWarning {util.env('SMTP_USERNAME', 'research@example.com')}"
    llm_on = bool(util.env("ANTHROPIC_API_KEY")) and not args.no_llm

    print("Fetching FRED ...", flush=True)
    fred = fetch_fred.run(cfg, util.env("FRED_API_KEY"), offline=args.offline)
    print("Fetching SEC EDGAR ...", flush=True)
    edgar = fetch_edgar.run(cfg, sec_ua, offline=args.offline)
    print("Fetching Taiwan monthly revenue ...", flush=True)
    tw = fetch_taiwan.run(cfg, offline=args.offline)

    sec = util.session(sec_ua)
    subs_cache: dict = {}
    extra_usage: dict = {}
    print("Reading lease footnotes (not yet commenced) ...", flush=True)
    lease_data = leases.run(cfg, sec, selection["assessment"], llm_on, args.offline, extra_usage, subs_cache)
    earn_tickers = [e["ticker"] for e in run_events if e.get("type") == "earnings" and e.get("ticker") in cfg["hyperscalers"]]
    earnings = None
    if earn_tickers:
        print(f"Reading earnings press releases: {', '.join(earn_tickers)} ...", flush=True)
        # The daily event check runs after the release; a manual event run takes whatever is latest.
        since = us_day if util.env("EVENT_NAME") == "schedule" else None
        earnings = calls.run(cfg, sec, earn_tickers, selection["assessment"], llm_on, args.offline, extra_usage,
                             subs_cache, since)
    filing_text = {"leases": lease_data, "earnings": earnings}

    rule = {
        "credit": scoring.score_credit(fred, cfg),
        "taiwan_supply_chain": scoring.score_taiwan(tw, cfg),
        "hyperscaler_filings": scoring.score_filings(edgar, cfg, lease_data),
    }

    previous = publish.previous_report(today)
    prev_summary = None
    if previous:
        prev_summary = {"date": previous.get("date"), "composite": previous.get("composite"),
                        "scores": {k: v.get("score") for k, v in previous.get("scores", {}).items()},
                        "headline": ((previous.get("claude") or {}).get("assessment") or {}).get("headline")}
    prev_runs = publish.previous_runs()
    prev_scores = [scoring.run_scores(r) for r in prev_runs]
    last_run = prev_runs[-1] if prev_runs else None

    print(f"Claude step: {'on' if llm_on else 'off'} ...", flush=True)
    recent = [e for e in run_events if 0 <= (us_day - date.fromisoformat(e["date"])).days <= 3]  # not future dates
    focus = "; ".join(f"{e.get('title') or e['name']} ({e['date']})" for e in recent) or None
    claude = analyst.run(cfg, data_brief(fred, edgar, tw, rule, filing_text, run_events), prev_summary, llm_on,
                         selection, focus)
    usage = claude.pop("usage", None) or {}
    usage.update({k: u for k, u in extra_usage.items() if u["requests"]})
    prices = models.load_prices()
    cost = models.cost_summary(usage, prices) if usage else None
    models.write_catalog(prices, models.resolve(cfg, use_inputs=False), use_api=llm_on and not args.offline)

    scores = dict(rule)
    a = claude.get("assessment") or {}
    for k in scoring.NEWS_CATEGORIES:
        n = (a.get("news_scores") or {}).get(k)
        if n:
            scores[k] = {"score": int(n["score"]), "reasons": [n["summary"]], "evidence": n.get("evidence", []),
                         "data_ok": True}
        else:
            scores[k] = {"score": 0, "reasons": ["Not scored this run (Claude step unavailable)"],
                         "evidence": [], "data_ok": False}
    if earnings and earnings["evidence"]:  # verified press-release comparison, quoted from the 8-K
        for k in ("hyperscaler_filings", "supplier_signals"):
            scores[k].setdefault("evidence", []).extend(copy.deepcopy(earnings["evidence"]))

    if args.offline:
        source_check = {"skipped": "offline run"}
    else:
        print("Checking evidence URLs ...", flush=True)
        source_check = {"counts": sources.check_evidence(scores, cfg)}
        source_check["capped"] = sources.apply_caps(scores, cfg)

    adj = a.get("credit_news_adjustment") or {}
    if adj.get("delta"):
        scores["credit"]["score"] = max(0, min(3, scores["credit"]["score"] + int(adj["delta"])))
        scores["credit"]["reasons"].append(f"News adjustment {int(adj['delta']):+d}: {adj.get('reason', '')}")

    comp = scoring.composite(scores, cfg)
    comp["confirmation"] = scoring.confirmation(scores, prev_scores, cfg)
    comp["red_confirmed"] = comp["level"] == "RED" and comp["confirmation"]["met"]
    comp["jumps"] = scoring.jumps(scores, prev_scores[-1] if prev_scores else None, cfg)
    comp["previous_run"] = last_run and {"timestamp": last_run.get("timestamp"), "level": last_run.get("level")}

    errors = {}
    for name, src in (("fred", fred), ("edgar", edgar), ("taiwan", tw), ("leases", lease_data),
                      ("earnings", earnings or {})):
        for k, v in (src.get("errors") or {}).items():
            errors[f"{name}:{k}"] = v
    if claude.get("error") and llm_on:
        errors["claude"] = claude["error"]
    if (claude.get("research") or {}).get("error"):
        errors["claude:research"] = claude["research"]["error"]
    note = events.horizon_warning(all_events, us_day)
    if note:
        errors["events"] = note

    report = {
        "date": today,
        "run_id": run_id,
        "generated_at": util.now_iso(),
        "trigger": trigger,
        "events": run_events,
        "categories": scoring.CATEGORIES,
        "scores": scores,
        "composite": comp,
        "claude": claude,
        "cost": cost,
        "source_check": source_check,
        "filing_text": filing_text,
        "data": {"fred": fred["series"], "edgar": edgar["companies"], "taiwan": tw},
        "errors": errors,
    }
    publish.write_all(report, cfg)

    skip, report["email_reason"] = email_decision(args.no_email, trigger, comp, scores,
                                                  prev_scores[-1] if prev_scores else None,
                                                  (last_run or {}).get("level"), cfg)
    if skip:
        report["email"] = f"skipped ({skip})"
    else:
        try:
            report["email"] = notify.send(report)
        except Exception as e:
            report["email"] = f"failed: {e}"
            errors["email"] = str(e)
    archived = publish.finish_run(report, all_events)

    print(json.dumps({"run_id": run_id, "trigger": trigger, "archived": f"data/runs/{archived}",
                      "composite": comp, "scores": {k: v["score"] for k, v in scores.items()},
                      "cost_usd": (cost or {}).get("total_usd"), "source_check": source_check,
                      "email": report["email"], "errors": errors}, indent=1, default=str))
    # Fail the workflow only if nothing at all could be fetched.
    if not fred["series"] and not edgar["companies"] and not tw.get("latest"):
        print("No data sources succeeded.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
