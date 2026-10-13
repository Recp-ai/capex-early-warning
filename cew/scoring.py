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
    return cur, round(util.pct_change(a, b), 2), len(common)


def score_filings(edgar: dict, cfg: dict) -> dict:
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
