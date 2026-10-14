"""Rule-based factor scores (0-100), the composite, confidence, rating band and the capex overlay.

Every threshold and weight comes from config/stocks.yaml. Claude never sets a score: the news & risk
factor is computed here from the per-topic ratings (-2..+2) after the source check has capped them (by the
number of checkable sources, then by their tier).
"""
from __future__ import annotations

FACTORS = ["growth", "quality", "balance_sheet", "valuation", "trend", "news_risk"]
LABELS = {"growth": "Growth", "quality": "Quality", "balance_sheet": "Balance sheet", "valuation": "Valuation",
          "trend": "Trend", "news_risk": "News & risk"}
SUB = {
    "growth": ["revenue_yoy_q", "revenue_yoy_ttm", "acceleration_pp"],
    "quality": ["gross_margin_pct", "operating_margin_pct", "fcf_margin_pct", "sbc_pct_revenue", "dilution_yoy_pct"],
    "valuation": ["price_to_sales", "price_to_fcf", "ev_to_ebit", "fcf_yield_vs_10y_pp",
                  "price_to_sales_percentile", "price_to_fcf_percentile", "ev_to_ebit_percentile"],
    "trend": ["vs_ma50_pct", "vs_ma200_pct", "momentum_12_1_pct", "rs_spy_pp", "rs_qqq_pp", "drawdown_52w_pct"],
}


def interp(points: list, x: float) -> float:
    """Piecewise-linear score for x; flat beyond the first and last points."""
    pts = sorted((float(a), float(b)) for a, b in points)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 != x0 else y1
    return pts[-1][1]


def _metric(value, score, weight, note=None) -> dict:
    return {"value": value, "score": None if score is None else round(score, 1), "weight": weight, "note": note}


def combine(metrics: dict, min_coverage: float, missing: str | None = None) -> dict:
    """Weighted average of the sub-metrics that have a score; missing below `min_coverage` of the weight."""
    total = sum(m["weight"] for m in metrics.values())
    have = {k: m for k, m in metrics.items() if m["score"] is not None}
    w = sum(m["weight"] for m in have.values())
    coverage = round(w / total, 3) if total else 0.0
    out = {"metrics": metrics, "coverage": coverage, "score": None, "missing": missing}
    if have and w and coverage >= min_coverage:
        out["score"] = round(sum(m["score"] * m["weight"] for m in have.values()) / w)
    elif not missing:
        out["missing"] = "no data" if not have else f"only {coverage:.0%} of its inputs had data"
    return out


def generic(name: str, values: dict, cfg: dict) -> dict:
    spec = cfg.get(name) or {}
    out = {}
    for k in SUB[name]:
        s = spec.get(k) or {}
        v = values.get(k)
        out[k] = _metric(v, None if v is None or "points" not in s else interp(s["points"], v), float(s.get("weight", 0)))
    return out


def balance_sheet(s: dict, cfg: dict) -> dict:
    spec = cfg.get("balance_sheet") or {}
    bs = s.get("balance_sheet") or {}
    net, fcf, cash, debt = bs.get("net_debt"), s.get("fcf_ttm"), bs.get("cash_and_investments"), bs.get("debt")
    nd = spec["net_debt_to_fcf"]
    if net is None or fcf is None:
        lev = _metric(None, None, nd["weight"])
    elif net <= 0 and fcf > 0:
        lev = _metric(round(net / fcf, 2), 100.0, nd["weight"], "net cash")
    elif net <= 0:
        runway = cash / -fcf if fcf < 0 and cash is not None else None
        lev = _metric(None, 100.0 if runway is None else interp(spec["runway_years"]["points"], runway), nd["weight"],
                      "net cash, negative FCF" + (f": {runway:.1f} years of cash at the current burn" if runway is not None else ""))
    elif fcf <= 0:
        lev = _metric(None, 0.0, nd["weight"], "net debt with negative FCF")
    else:
        lev = _metric(round(net / fcf, 2), interp(nd["points"], net / fcf), nd["weight"])
    cd = spec["cash_to_debt"]
    if cash is None or debt is None:
        cov = _metric(None, None, cd["weight"])
    elif debt == 0:
        cov = _metric(None, 100.0, cd["weight"], bs.get("debt_note") or "no debt")
    else:
        cov = _metric(round(cash / debt, 2), interp(cd["points"], cash / debt), cd["weight"])
    return {"net_debt_to_fcf": lev, "cash_to_debt": cov}


def valuation(v: dict, cfg: dict) -> dict:
    spec = cfg.get("valuation") or {}
    out = {}
    for k, neg in (("price_to_sales", None), ("price_to_fcf", "fcf_negative"), ("ev_to_ebit", "ebit_negative")):
        s = spec[k]
        if v.get(k) is not None:
            out[k] = _metric(v[k], interp(s["points"], v[k]), float(s["weight"]))
        elif neg and v.get(neg):
            out[k] = _metric(None, 0.0, float(s["weight"]), "negative " + ("FCF" if k == "price_to_fcf" else "EBIT"))
        else:
            out[k] = _metric(None, None, float(s["weight"]))
    s = spec["fcf_yield_vs_10y_pp"]
    x = v.get("fcf_yield_vs_10y_pp")
    out["fcf_yield_vs_10y_pp"] = _metric(x, None if x is None else interp(s["points"], x), float(s["weight"]))
    for k in ("price_to_sales", "price_to_fcf", "ev_to_ebit"):
        p = ((v.get("vs_history") or {}).get(k) or {}).get("percentile")
        out[f"{k}_percentile"] = _metric(p, None if p is None else 100.0 - p, float(spec[f"{k}_percentile"]["weight"]))
    return out


def factors(summary: dict, val: dict, trend: dict, cfg: dict) -> dict:
    """The five code-scored factors (news & risk is added by `news_risk`)."""
    mc = float((cfg.get("factors") or {}).get("min_coverage", 0.5))
    return {
        "growth": combine(generic("growth", summary, cfg), mc, None if summary.get("revenue_ttm") is not None or summary.get("revenue_q") is not None else "no revenue in XBRL"),
        "quality": combine(generic("quality", summary, cfg), mc, None if summary.get("revenue_ttm") is not None else "no revenue in XBRL"),
        "balance_sheet": combine(balance_sheet(summary, cfg), mc, None if summary.get("balance_sheet") else "no balance sheet in XBRL"),
        "valuation": combine(valuation(val, cfg), mc, val.get("missing")),
        "trend": combine(generic("trend", trend, cfg), mc, trend.get("missing")),
    }


def cap_topic(topic: dict) -> None:
    """A topic's rating can't exceed, in size, the number of its sources verified or unreachable. In place."""
    ok = sum(1 for e in topic.get("evidence") or [] if e.get("status") in ("verified", "unreachable"))
    r = int(topic.get("rating") or 0)
    if abs(r) > ok:
        topic["rating_before_cap"] = r
        topic["rating"] = (1 if r > 0 else -1) * min(abs(r), ok)
        topic["cap_note"] = (f"Capped at {topic['rating']:+d} (was {r:+d}): {ok} of {len(topic.get('evidence') or [])} "
                             "sources verified or unreachable")


WEAK_SOURCES = "capped: weak sources"


def source_quality(cfg: dict) -> dict:
    """news_risk.source_quality with its defaults."""
    q = (cfg.get("news_risk") or {}).get("source_quality") or {}
    return {"strong_tiers": list(q.get("strong_tiers") or ["primary", "major"]),
            "strong_min_verified": int(q.get("strong_min_verified", 1)),
            "cap_without_strong": int(q.get("cap_without_strong", 1)),
            "cap_other_only": int(q.get("cap_other_only", 1)),
            "cap_other_only_unverified": int(q.get("cap_other_only_unverified", 0))}


def _pm(n: int) -> str:
    return f"±{n}" if n else "0"


def _tiers(tiers: list) -> str:
    return "- or ".join(tiers) + "-tier"


def source_quality_rule(cfg: dict) -> str:
    """The source-quality cap in words, for the topic-rating prompt."""
    q = source_quality(cfg)
    tiers = _tiers(q["strong_tiers"])
    return (f"a rating above {_pm(q['cap_without_strong'])} needs at least {q['strong_min_verified']} verified {tiers} "
            f"source{'s' * (q['strong_min_verified'] != 1)}; a topic with no {tiers} source is capped at "
            f"{_pm(q['cap_other_only'])}, and at {_pm(q['cap_other_only_unverified'])} if none of its sources verify.")


def cap_weak_sources(topic: dict, cfg: dict) -> None:
    """Source quality (news_risk.source_quality), after `cap_topic`: a rating needs `strong_min_verified`
    verified sources from `strong_tiers` to exceed `cap_without_strong`; a topic with no source from those
    tiers caps at `cap_other_only`, or `cap_other_only_unverified` when none of its sources verified. The
    topic keeps the lower of this cap and `cap_topic`'s; "weak_sources_note" is set when this cap is below
    Claude's rating and not above `cap_topic`'s, so its "Capped at" value is the final rating. In place."""
    q = source_quality(cfg)
    ev = topic.get("evidence") or []
    tiers, need = q["strong_tiers"], q["strong_min_verified"]
    n_strong = sum(1 for e in ev if e.get("status") == "verified" and e.get("tier") in tiers)
    limit, why = 2, None
    if n_strong < need:
        limit = q["cap_without_strong"]
        why = (f"a rating above {_pm(limit)} needs {need} verified {_tiers(tiers)} source{'s' * (need != 1)}; "
               f"{n_strong} of its {len(ev)} source{'s' * (len(ev) != 1)} {'qualifies' if n_strong == 1 else 'qualify'}")
    if ev and not any(e.get("tier") in tiers for e in ev):
        verified = any(e.get("status") == "verified" for e in ev)
        lim = q["cap_other_only"] if verified else q["cap_other_only_unverified"]
        if lim < limit:
            limit = lim
            why = f"only other-tier sources{'' if verified else ', none verified'}"
    orig, r = int(topic.get("rating_before_cap", topic.get("rating")) or 0), int(topic.get("rating") or 0)
    if why is None or abs(orig) <= limit or abs(r) < limit:
        return
    sign = 1 if orig > 0 else -1
    topic["weak_sources_note"] = f"Capped at {f'{sign * limit:+d}' if limit else '0'} (was {orig:+d}): {why}"
    if abs(r) > limit:
        topic["rating_before_cap"] = orig
        topic["rating"] = sign * limit


def news_risk(topics: dict | None, cfg: dict, missing: str | None = None) -> dict:
    """50 + sum(weight x rating x points_per_step), clamped to 0-100."""
    nc = cfg.get("news_risk") or {}
    if not topics:
        return {"score": None, "metrics": {}, "coverage": 0.0, "missing": missing or "Claude step unavailable"}
    step = float(nc.get("points_per_step", 25))
    raw = 50.0
    metrics = {}
    for k, t in (nc.get("topics") or {}).items():
        r = int((topics.get(k) or {}).get("rating") or 0)
        raw += float(t["weight"]) * r * step
        metrics[k] = _metric(r, 50 + r * step, float(t["weight"]))
    return {"score": round(max(0.0, min(100.0, raw))), "metrics": metrics, "coverage": 1.0, "missing": None}


def composite(fs: dict, cfg: dict) -> dict:
    weights = (cfg.get("factors") or {}).get("weights") or {}
    have = {k: f["score"] for k, f in fs.items() if f.get("score") is not None and weights.get(k)}
    total = sum(weights[k] for k in have)
    if not have or not total:
        return {"score": None, "raw": None, "weights_used": {}, "available": [], "missing": list(fs)}
    raw = sum(weights[k] * v for k, v in have.items()) / total
    return {"score": int(raw + 0.5), "raw": round(raw, 2),
            "weights_used": {k: round(weights[k] / total, 4) for k in have},
            "available": list(have), "missing": [k for k in FACTORS if k not in have]}


def confidence(n_available: int, cfg: dict) -> str:
    c = cfg.get("confidence") or {}
    if n_available >= int(c.get("high_at", 6)):
        return "High"
    return "Medium" if n_available >= int(c.get("medium_at", 4)) else "Low"


def bands(cfg: dict) -> list[dict]:
    return sorted(cfg.get("ratings") or [], key=lambda b: -float(b["min"]))


def band(score: int, cfg: dict) -> str:
    for b in bands(cfg):
        if score >= float(b["min"]):
            return b["label"]
    return bands(cfg)[-1]["label"]


def order(cfg: dict) -> list[str]:
    """Rating labels from worst to best."""
    return [b["label"] for b in reversed(bands(cfg))]


def distance(score: int, cfg: dict) -> dict:
    """Points to the next band up and how far above the current band's floor."""
    bs = bands(cfg)
    i = next(i for i, b in enumerate(bs) if score >= float(b["min"]) or i == len(bs) - 1)
    up = bs[i - 1] if i > 0 else None
    return {"up": up and {"label": up["label"], "at": up["min"], "points": round(float(up["min"]) - score)},
            "down": {"label": bs[i + 1]["label"], "below": bs[i]["min"], "points": round(score - float(bs[i]["min"]) + 1)}
            if i + 1 < len(bs) else None}


def overlay(ticker: str, rating: str, ctx: dict | None, cfg: dict) -> dict:
    """AI-exposed tickers: capped at overlay.cap_at when the capex level is RED and confirmed; AMBER or an
    unconfirmed RED adds a note only."""
    oc = cfg.get("overlay") or {}
    exposed = ticker in set(oc.get("ai_exposed") or [])
    out = {"ai_exposed": exposed, "applies": False, "capped": False, "rating_before": rating, "note": None,
           "capex": ctx}
    if not exposed:
        return out
    if not ctx or not ctx.get("level"):
        out["note"] = "AI-exposed, but the capex dashboard has no data yet: no overlay"
        return out
    lvl, cap = ctx["level"], oc.get("cap_at", "Hold")
    stale = (f" (capex data {ctx['age_days']} days old)" if ctx.get("age_days") is not None
             and ctx["age_days"] > int(oc.get("stale_after_days", 14)) else "")
    num = lambda v: "?" if v is None else f"{v:g}"  # noqa: E731
    where = f"capex early warning {lvl} {num(ctx.get('total'))}/{num(ctx.get('max'))}"
    ranks = order(cfg)
    if lvl == "RED" and ctx.get("red_confirmed"):
        out["applies"] = True
        if rating in ranks and cap in ranks and ranks.index(rating) > ranks.index(cap):
            out.update(capped=True, rating=cap)
            out["note"] = f"Capped at {cap} (was {rating}): AI-exposed and the {where} is confirmed{stale}"
        else:
            out["note"] = f"AI-exposed; {where} (confirmed) caps ratings at {cap}; this rating is already at or below it{stale}"
    elif lvl in ("RED", "AMBER"):
        out["applies"] = True
        out["note"] = (f"AI-exposed; {where}" + (" (unconfirmed)" if lvl == "RED" else "")
                       + f": warning signs for AI capex, rating not capped{stale}")
    else:
        out["note"] = f"AI-exposed; {where}: no overlay{stale}"
    return out
