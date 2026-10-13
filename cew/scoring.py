"""Rule-based scores for the data-driven categories and the composite."""
from __future__ import annotations

from datetime import date

from . import util

CATEGORIES = [
    ("credit", "Credit markets"),
    ("ai_lab_financing", "AI-lab financing"),
    ("taiwan_supply_chain", "Taiwan supply chain"),
    ("hyperscaler_filings", "Hyperscaler filings"),
    ("supplier_signals", "Supplier signals"),
    ("physical_buildout", "Physical build-out"),
]
NEWS_CATEGORIES = ["ai_lab_financing", "supplier_signals", "physical_buildout"]


def _ladder(value: float | None, steps: list[float], below: bool) -> int:
    """Count how many thresholds are crossed. below=True means lower is worse."""
    if value is None:
        return 0
    if below:
        return sum(1 for s in steps if value < s)
    return sum(1 for s in steps if value >= s)


def score_credit(fred: dict, cfg: dict) -> dict:
    c = cfg["credit"]
    hy = fred["series"].get("BAMLH0A0HYM2", {})
    ig = fred["series"].get("BAMLC0A0CM", {})
    reasons = []
    score = _ladder(hy.get("chg_4w"), c["hy_change_4w"], below=False)
    if hy.get("chg_4w") is not None:
        reasons.append(f"High-yield spread {hy['latest']:.2f}% ({hy['chg_4w']:+.2f} pts over 4 weeks)")
    if hy.get("latest") is not None and hy["latest"] >= c["hy_level_stress"]:
        score = max(score, 2)
        reasons.append(f"High-yield spread at stress level (>= {c['hy_level_stress']}%)")
    if ig.get("chg_4w") is not None:
        reasons.append(f"Investment-grade spread {ig['latest']:.2f}% ({ig['chg_4w']:+.2f} pts over 4 weeks)")
        if ig["chg_4w"] >= c["ig_change_4w_bonus"]:
            score += 1
    if not hy:
        reasons.append("No credit-spread data this week")
    return {"score": min(score, 3), "reasons": reasons, "data_ok": bool(hy)}


STAGE_SERIES = {"hy": "BAMLH0A0HYM2", "ig": "BAMLC0A0CM", "baa": "BAA10Y"}
STAGE_LABELS = {
    "calm": "Calm",
    "junk_only": "Junk only: contained",
    "spreading": "Spreading: junk and investment grade",
    "ig_only": "IG only: financing pressure (possibly bond supply)",
    "easing": "Easing",
}


def credit_stage(fred: dict, cfg: dict) -> dict:
    """Credit contagion stage from the 4-week spread changes, plus the 10-year yield's direction.
    Context only: it never changes the credit score."""
    c = cfg.get("credit_stage") or {}
    series = fred.get("series") or {}
    chg = {k: (series.get(sid) or {}).get("chg_4w") for k, sid in STAGE_SERIES.items()}
    widening = [k for k, v in chg.items() if v is not None and c.get(f"{k}_widen") is not None and v >= c[f"{k}_widen"]]
    narrowing = [k for k, v in chg.items() if v is not None and c.get(f"{k}_narrow") is not None and v <= c[f"{k}_narrow"]]
    out = {"changes_4w": chg, "widening": widening, "narrowing": narrowing,
           "missing": [STAGE_SERIES[k] for k, v in chg.items() if v is None]}
    if chg["hy"] is None:
        return {**out, "stage": "", "label": "Unknown (no high-yield spread data)", "ten_year": {},
                "line": "Unknown (no high-yield spread data)"}
    if "hy" in widening:
        stage = "spreading" if {"ig", "baa"} & set(widening) else "junk_only"
    elif {"ig", "baa"} & set(widening):
        stage = "ig_only"
    elif {"hy", "ig"} <= set(narrowing):
        stage = "easing"
    else:
        stage = "calm"
    t = (series.get("DGS10") or {}).get("chg_4w")
    move = c.get("ust10y_move", 0.10)
    if t is None:
        ten = {"direction": "", "chg_4w": None, "label": ""}
    elif t >= move:
        ten = {"direction": "rising", "chg_4w": t, "label": "10y rising (rate pressure)"}
    elif t <= -move:
        ten = {"direction": "falling", "chg_4w": t,
               "label": "10y falling (growth scare)" if widening else "10y falling"}
    else:
        ten = {"direction": "flat", "chg_4w": t, "label": "10y flat"}
    label = STAGE_LABELS[stage]
    return {**out, "stage": stage, "label": label, "ten_year": ten,
            "line": " · ".join(x for x in (label, ten["label"]) if x)}


def score_taiwan(tw: dict, cfg: dict) -> dict:
    t = cfg["taiwan"]
    med = tw.get("median_yoy_by_period", {})
    if not med:
        return {"score": 0, "reasons": ["No Taiwan revenue data yet"], "data_ok": False}
    periods = list(med)
    latest_p = periods[-1]
    latest = med[latest_p]
    score = _ladder(latest, t["median_yoy"], below=True)
    reasons = [f"Median YoY revenue growth for {latest_p}: {latest:+.1f}% across the AI-server group"]
    # Two consecutive negative months escalates
    if len(periods) >= 2 and latest < 0 and med[periods[-2]] < 0:
        score = max(score, 3)
        reasons.append("Negative YoY two months in a row")
    # Deceleration versus three months earlier
    y, m = map(int, latest_p.split("-"))
    m -= 3
    if m <= 0:
        y, m = y - 1, m + 12
    earlier = med.get(f"{y:04d}-{m:02d}")
    if earlier is not None:
        drop = earlier - latest
        reasons.append(f"3 months earlier: {earlier:+.1f}% (change {-drop:+.1f} pts)")
        if drop >= t["decel_3m_pts"]:
            score += 1
    else:
        reasons.append("Deceleration check needs 3 months of history (builds up automatically)")
    weak = [r for r in tw.get("latest", []) if util.to_float(r.get("yoy_pct")) is not None
            and util.to_float(r["yoy_pct"]) < 0]
    if weak:
        reasons.append("Negative YoY: " + ", ".join(f"{r['name']} {float(r['yoy_pct']):+.1f}%" for r in weak))
    return {"score": min(score, 3), "reasons": reasons, "data_ok": True}


def _cal_quarter(end: str) -> str:
    d = date.fromisoformat(end)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def aggregate_by_calendar_quarter(edgar: dict, metric: str) -> dict[str, dict[str, float]]:
    """{calendar_quarter: {ticker: value}} for one metric (Oracle's offset quarters are bucketed)."""
    buckets: dict[str, dict[str, float]] = {}
    for t, c in edgar["companies"].items():
        m = c.get(metric)
        if not m:
            continue
        for end, val in m["history"]:
            buckets.setdefault(_cal_quarter(end), {})[t] = val
    return dict(sorted(buckets.items()))


def agg_qoq(buckets: dict[str, dict[str, float]]) -> tuple[str | None, float | None, int]:
    """QoQ % of the sum, using only companies reporting in both of the two latest quarters
    that at least two companies share."""
    qs = [q for q, v in buckets.items() if len(v) >= 2]
    if len(qs) < 2:
        return None, None, 0
    cur, prev = qs[-1], qs[-2]
    common = set(buckets[cur]) & set(buckets[prev])
    if not common:
        return cur, None, 0
    a = sum(buckets[cur][t] for t in common)
    b = sum(buckets[prev][t] for t in common)
    pc = util.pct_change(a, b)
    return cur, (None if pc is None else round(pc, 2)), len(common)


def score_filings(edgar: dict, cfg: dict, leases: dict | None = None) -> dict:
    """Capex, free cash flow and lease signals. `leases` is cew.leases.run() output: totals of
    leases signed but not yet commenced, each read from a sentence found verbatim in a filing."""
    h = cfg["hyperscaler_filings"]
    if not edgar["companies"]:
        return {"score": 0, "reasons": ["No EDGAR data this week"], "data_ok": False}
    reasons = []
    q, qoq, n = agg_qoq(aggregate_by_calendar_quarter(edgar, "capex"))
    score = _ladder(qoq, h["agg_capex_qoq"], below=True)
    if qoq is not None:
        reasons.append(f"Aggregate capex {q}: {qoq:+.1f}% QoQ ({n} companies)")
    for t, c in edgar["companies"].items():
        cap = c.get("capex")
        if cap and cap.get("qoq_pct") is not None:
            if cap["qoq_pct"] <= h["single_capex_cut_pct"]:
                score += 1
                reasons.append(f"{c['name']} cut quarterly capex {cap['qoq_pct']:+.1f}% QoQ")
        fcf = c.get("free_cash_flow")
        if fcf and fcf["value"] < 0:
            reasons.append(f"{c['name']} free cash flow negative (${fcf['value']/1e9:.1f}B, quarter ending {fcf['quarter_end']})")
    lease_b = {}
    for metric in ("new_operating_lease_assets", "new_finance_lease_assets"):
        for qtr, vals in aggregate_by_calendar_quarter(edgar, metric).items():
            for t, v in vals.items():
                lease_b.setdefault(qtr, {}).setdefault(t, 0.0)
                lease_b[qtr][t] += v
    lq, lqoq, _ = agg_qoq(lease_b)
    if lqoq is not None:
        reasons.append(f"New lease assets (data-center leases starting) {lq}: {lqoq:+.1f}% QoQ")
        if lqoq <= h["new_lease_assets_qoq"]:
            score += 1
    nq, nqoq, nn = agg_qoq((leases or {}).get("by_quarter") or {})
    threshold = h.get("leases_not_commenced_qoq")
    if nqoq is not None:
        reasons.append(f"Leases signed but not yet commenced {nq}: {nqoq:+.1f}% QoQ ({nn} companies)")
        if threshold is not None and nqoq <= threshold:
            score += 1
            reasons.append(f"Not-yet-commenced leases fell {abs(threshold):.0f}% or more QoQ (+1)")
    return {"score": min(score, 3), "reasons": reasons, "data_ok": True}


def composite(scores: dict[str, dict], cfg: dict) -> dict:
    c = cfg["composite"]
    w = c["weights"]
    total = sum(scores[k]["score"] * w.get(k, 1.0) for k, _ in CATEGORIES if k in scores)
    max_total = sum(3 * w.get(k, 1.0) for k, _ in CATEGORIES)
    level = "GREEN"
    if total >= c["red_at"]:
        level = "RED"
    elif total >= c["amber_at"]:
        level = "AMBER"
    hot = [k for k, _ in CATEGORIES if k in scores and scores[k]["score"] >= c["single_category_alert"]]
    alert = level != "GREEN" or bool(hot)
    if hot and level == "GREEN":
        level = "AMBER"
    return {"total": round(total, 2), "max": max_total, "level": level, "alert": alert, "hot_categories": hot}


LEVELS = ["GREEN", "AMBER", "RED"]


def hysteresis(comp: dict, previous_level: str | None, cfg: dict) -> dict:
    """Hold the previous run's level near a boundary. Once AMBER (or RED), the composite has to fall
    below amber_at (red_at) minus composite.level_hysteresis to drop a level; rises are immediate.
    Sets comp["raw_level"] (before hysteresis) and, when the level was held, comp["held"]. In place."""
    c = cfg["composite"]
    margin = float(c.get("level_hysteresis") or 0)
    comp["raw_level"] = comp["level"]
    if margin <= 0 or previous_level not in LEVELS:
        return comp
    total = comp["total"]
    sticky = "RED" if total >= c["red_at"] - margin else "AMBER" if total >= c["amber_at"] - margin else "GREEN"
    held = min(LEVELS.index(previous_level), LEVELS.index(sticky))
    if held > LEVELS.index(comp["level"]):
        level = LEVELS[held]
        line = c["red_at"] if level == "RED" else c["amber_at"]
        comp.update(level=level, alert=True, held={
            "previous_level": previous_level, "raw_level": comp["raw_level"], "margin": margin,
            "note": f"Held at {level}: composite {total:g} is under the {level.title()} line ({line:g}) but not "
                    f"below {line - margin:g}, so the level stays until it falls further."})
    return comp


# ---------------------------------------------------------------- run-to-run checks
def run_scores(row: dict | None) -> dict[str, int] | None:
    """Category scores from a data/runs.csv row or a report's "scores" dict."""
    if not row:
        return None
    out = {}
    for k, _ in CATEGORIES:
        v = row.get(k)
        v = v.get("score") if isinstance(v, dict) else v
        try:
            out[k] = int(float(v))
        except (TypeError, ValueError):
            pass
    return out or None


def confirmation(scores: dict[str, dict], previous: list[dict[str, int]], cfg: dict) -> dict:
    """Categories at min_score or more in each of the last `consecutive_runs` runs (this one included).
    `previous` holds earlier runs' scores, oldest first."""
    c = cfg.get("confirmation") or {}
    n, min_score, min_cats = c.get("consecutive_runs", 2), c.get("min_score", 2), c.get("min_categories", 2)
    runs = [s for s in previous if s][-(n - 1):] if n > 1 else []
    runs = runs + [{k: v["score"] for k, v in scores.items()}]
    sustained = []
    if len(runs) >= n:
        sustained = [k for k, _ in CATEGORIES if all(r.get(k) is not None and r[k] >= min_score for r in runs)]
    return {"met": len(sustained) >= min_cats, "categories": sustained, "runs": n,
            "min_score": min_score, "min_categories": min_cats}


def jumps(scores: dict[str, dict], previous: dict[str, int] | None, cfg: dict) -> dict[str, int]:
    """Categories that rose `jump_points` or more since the previous run: {category: change}."""
    pts = (cfg.get("confirmation") or {}).get("jump_points", 2)
    if not previous:
        return {}
    return {k: scores[k]["score"] - previous[k] for k, _ in CATEGORIES
            if k in scores and k in previous and scores[k]["score"] - previous[k] >= pts}


def event_email_reason(comp: dict, scores: dict[str, dict], previous: dict[str, int] | None,
                       previous_level: str | None, cfg: dict) -> str | None:
    """Why an event-day run should email (None = stay quiet): level change or a big category move."""
    move = (cfg.get("email") or {}).get("event_category_move", 2)
    if previous_level and comp["level"] != previous_level:
        return f"level changed {previous_level} -> {comp['level']}"
    if previous is None:
        return "no previous run to compare with"
    moved = [f"{k} {scores[k]['score'] - previous[k]:+d}" for k, _ in CATEGORIES
             if k in scores and k in previous and abs(scores[k]["score"] - previous[k]) >= move]
    return ("moved " + ", ".join(moved)) if moved else None
