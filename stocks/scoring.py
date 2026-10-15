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


MISSING = "missing"


def generic(name: str, values: dict, cfg: dict, notes: dict | None = None) -> dict:
    """Each sub-metric's value and score; a missing value is noted as missing (with why, from `notes`)."""
    spec = cfg.get(name) or {}
    out = {}
    for k in SUB[name]:
        s = spec.get(k) or {}
        v = values.get(k)
        note = (notes or {}).get(k)
        if v is None and not (note or "").startswith(MISSING):
            note = MISSING + (f": {note}" if note else "")
        out[k] = _metric(v, None if v is None or "points" not in s else interp(s["points"], v), float(s.get("weight", 0)), note)
    return out


def quality_values(s: dict) -> tuple[dict, dict]:
    """Quality inputs and notes: the FCF margin is ex-prepayments when that applies (the reported one in the
    note); a margin no tag covers is noted as missing with the reason."""
    notes = {k: f"{MISSING}: {why}" for k, why in (s.get("missing_inputs") or {}).items()}
    values = dict(s)
    if (s.get("prepayments") or {}).get("applies") and s.get("fcf_ex_prepayments_margin_pct") is not None:
        values["fcf_margin_pct"] = s["fcf_ex_prepayments_margin_pct"]
        rep = s.get("fcf_margin_pct")
        notes["fcf_margin_pct"] = ("FCF ex-prepayments" + ("" if rep is None else f"; reported FCF margin {rep:.1f}%"))
    return values, notes


def balance_sheet(s: dict, cfg: dict) -> dict:
    spec = cfg.get("balance_sheet") or {}
    bs = s.get("balance_sheet") or {}
    net, fcf, cash, debt = bs.get("net_debt"), s.get("fcf_ttm"), bs.get("cash_and_investments"), bs.get("debt")
    nd = spec["net_debt_to_fcf"]
    if bs.get("unknown"):
        why = f"{MISSING}: " + "; ".join(bs["unknown"])
        return {"net_debt_to_fcf": _metric(None, None, nd["weight"], why),
                "cash_to_debt": _metric(None, None, spec["cash_to_debt"]["weight"], why)}
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
    ex_pp = v.get("fcf_basis") == "ex-prepayments"
    for k, neg in (("price_to_sales", None), ("price_to_fcf", "fcf_negative"), ("ev_to_ebit", "ebit_negative")):
        s = spec[k]
        note = None
        if k == "price_to_fcf" and ex_pp:
            rep = v.get("price_to_fcf_reported")
            note = "FCF ex-prepayments" + ("; reported FCF negative" if rep is None else f"; reported P/FCF {rep:.1f}x")
        if v.get(k) is not None:
            out[k] = _metric(v[k], interp(s["points"], v[k]), float(s["weight"]), note)
        elif neg and v.get(neg):
            out[k] = _metric(None, 0.0, float(s["weight"]), "negative " + ("FCF" if k == "price_to_fcf" else "EBIT")
                             + (" ex-prepayments" if note else ""))
        else:
            out[k] = _metric(None, None, float(s["weight"]), f"{MISSING}: {v['ev_note']}" if k == "ev_to_ebit" and v.get("ev_note") else MISSING)
    s = spec["fcf_yield_vs_10y_pp"]
    x = v.get("fcf_yield_vs_10y_pp")
    out["fcf_yield_vs_10y_pp"] = _metric(x, None if x is None else interp(s["points"], x), float(s["weight"]),
                                         ("FCF ex-prepayments" if ex_pp else None) if x is not None else MISSING)
    min_pts = int(spec.get("min_history_points", 24))
    for k in ("price_to_sales", "price_to_fcf", "ev_to_ebit"):
        h = (v.get("vs_history") or {}).get(k) or {}
        p = h.get("percentile")
        if p is not None:
            note = f"cheaper than now in {p:g}% of {h.get('points')} month-ends (ranked on yields)"
        elif k == "ev_to_ebit" and v.get("ev_note"):
            note = f"{MISSING}: {v['ev_note']}"
        elif h and h.get("points", 0) < min_pts:
            note = f"{MISSING}: {h.get('points', 0)} month-ends of history (needs {min_pts})"
        else:
            note = MISSING
        out[f"{k}_percentile"] = _metric(p, None if p is None else 100.0 - p, float(spec[f"{k}_percentile"]["weight"]), note)
    return out


def factors(summary: dict, val: dict, trend: dict, cfg: dict) -> dict:
    """The five code-scored factors (news & risk is added by `news_risk`)."""
    mc = float((cfg.get("factors") or {}).get("min_coverage", 0.5))
    bs = summary.get("balance_sheet") or {}
    qv, qn = quality_values(summary)
    return {
        "growth": combine(generic("growth", summary, cfg), mc, None if summary.get("revenue_ttm") is not None or summary.get("revenue_q") is not None else "no revenue in XBRL"),
        "quality": combine(generic("quality", qv, cfg, qn), mc, None if summary.get("revenue_ttm") is not None else "no revenue in XBRL"),
        "balance_sheet": combine(balance_sheet(summary, cfg), mc,
                                 "; ".join(bs["unknown"]) if bs.get("unknown") else None if bs else "no balance sheet in XBRL"),
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


CONFIDENCE_POINTS = {"missing_factor": 1, "missing_sub_metric": 1, "unknown_cash_debt": 1, "fcf_ex_prepayments": 1,
                     "short_valuation_history": 1, "near_band_edge": 1, "weak_sources": 1}
SUB_LABELS = {
    "revenue_yoy_q": "revenue YoY (quarter)", "revenue_yoy_ttm": "revenue YoY (TTM)", "acceleration_pp": "growth acceleration",
    "gross_margin_pct": "gross margin", "operating_margin_pct": "operating margin", "fcf_margin_pct": "FCF margin",
    "sbc_pct_revenue": "stock comp. / revenue", "dilution_yoy_pct": "diluted shares YoY",
    "net_debt_to_fcf": "net debt / FCF", "cash_to_debt": "cash / debt", "price_to_sales": "P/S", "price_to_fcf": "P/FCF",
    "ev_to_ebit": "EV/EBIT", "fcf_yield_vs_10y_pp": "FCF yield vs 10y", "price_to_sales_percentile": "P/S vs history",
    "price_to_fcf_percentile": "P/FCF vs history", "ev_to_ebit_percentile": "EV/EBIT vs history",
    "vs_ma50_pct": "price vs 50-day", "vs_ma200_pct": "price vs 200-day", "momentum_12_1_pct": "momentum 12-1",
    "rs_spy_pp": "relative strength vs SPY", "rs_qqq_pp": "relative strength vs QQQ", "drawdown_52w_pct": "from 52-week high",
}


def _conf_cfg(cfg: dict) -> dict:
    c = cfg.get("confidence") or {}
    return {"medium_at": float(c.get("medium_at", 1)), "low_at": float(c.get("low_at", 3)),
            "band_edge_points": float(c.get("band_edge_points", 2)),
            "points": {**CONFIDENCE_POINTS, **{k: float(v) for k, v in (c.get("points") or {}).items()}}}


def confidence_level(points: float, cfg: dict) -> str:
    c = _conf_cfg(cfg)
    return "Low" if points >= c["low_at"] else "Medium" if points >= c["medium_at"] else "High"


def confidence(n_available: int, cfg: dict) -> str:
    """The level from the factor count alone (each factor without data costs `points.missing_factor`)."""
    return confidence_level((len(FACTORS) - n_available) * _conf_cfg(cfg)["points"]["missing_factor"], cfg)


def confidence_detail(fs: dict, score: int | None, summary: dict, val: dict, topics: dict | None, cfg: dict) -> dict:
    """Data quality: starts at High; each issue adds its points (confidence.points) and the total sets the
    level (confidence.medium_at / low_at). Issues: factors with no data (each), missing inputs of scored
    factors, cash or debt unknown, FCF ex-prepayments, valuation history shorter than
    valuation.min_history_points, a composite within confidence.band_edge_points of a band edge, and news
    topics capped for weak sources. Returns {level, points, reasons: [{issue, points, text}], factors_with_data}."""
    c = _conf_cfg(cfg)
    pts = c["points"]
    val = val or {}
    reasons: list[dict] = []

    def add(issue, text, n=1):
        reasons.append({"issue": issue, "points": pts[issue] * n, "text": text})

    have = [k for k in FACTORS if (fs.get(k) or {}).get("score") is not None]
    gone = [k for k in FACTORS if k not in have]
    if gone:
        add("missing_factor", f"{len(gone)} of {len(FACTORS)} factors had no data: {', '.join(LABELS[k] for k in gone)}", len(gone))
    bs = (summary or {}).get("balance_sheet") or {}
    min_pts = int((cfg.get("valuation") or {}).get("min_history_points", 24))
    vh = (val or {}).get("vs_history") or {}
    short = [k for k in ("price_to_sales", "price_to_fcf", "ev_to_ebit")
             if k in vh and (vh[k].get("points") or 0) < min_pts and not (k == "ev_to_ebit" and val.get("ev_note"))]
    subs = []
    for k in have:
        for mk, m in ((fs[k].get("metrics") or {}).items()):
            if m.get("score") is not None or k == "news_risk":
                continue
            if mk.endswith("_percentile") and mk[:-len("_percentile")] in short:
                continue  # counted as short history
            if mk.startswith("ev_to_ebit") and val.get("ev_note") and bs.get("unknown"):
                continue  # counted as unknown cash / debt
            subs.append(SUB_LABELS.get(mk, mk))
    if subs:
        add("missing_sub_metric", "missing inputs: " + ", ".join(subs))
    if bs.get("unknown"):
        add("unknown_cash_debt", "; ".join(bs["unknown"]))
    pp = (summary or {}).get("prepayments") or {}
    if pp.get("applies"):
        share = f"{pp['share_of_ocf']:.0%} of operating cash flow" if pp.get("share_of_ocf") is not None else "operating cash flow not positive"
        add("fcf_ex_prepayments", f"FCF scored ex-prepayments: customer prepayments rose {_money(pp['increase'])} "
                                  f"over the year ({share}; threshold {pp['max_share_of_ocf']:.0%})")
    if short and (fs.get("valuation") or {}).get("score") is not None:
        add("short_valuation_history", "valuation history: " + ", ".join(
            f"{SUB_LABELS[k]} {vh[k].get('points') or 0} month-ends" for k in short) + f" (needs {min_pts})")
    if score is not None:
        edges = [b for b in bands(cfg) if float(b["min"]) > 0]
        near = min(edges, key=lambda b: abs(score - float(b["min"])), default=None)
        if near is not None and abs(score - float(near["min"])) <= c["band_edge_points"]:
            add("near_band_edge", f"score {score} is within {c['band_edge_points']:g} points of the {near['label']} "
                                  f"edge ({near['min']:g})")
    weak = [t.get("label") or k for k, t in (topics or {}).items() if t.get("weak_sources_note")]
    if weak:
        add("weak_sources", "news topics capped for weak sources: " + ", ".join(weak))
    total = sum(r["points"] for r in reasons)
    return {"level": confidence_level(total, cfg), "points": total, "reasons": reasons, "factors_with_data": len(have)}


def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    s = f"{a / 1e9:.2f}B" if a >= 1e9 else f"{a / 1e6:.1f}M" if a >= 1e6 else f"{a:,.0f}"
    return ("-" if v < 0 else "") + "$" + s


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
