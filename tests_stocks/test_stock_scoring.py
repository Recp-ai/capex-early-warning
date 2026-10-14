"""Rule-based stock scores: sub-metric mapping, factor coverage, news & risk, composite, confidence, bands,
and the capex overlay. Pure functions; no HTTP."""
import copy

import pytest

from stocks import config, scoring

CFG = config.load()


def test_config_weights_bands_and_overlay_list():
    assert sum(CFG["factors"]["weights"].values()) == pytest.approx(1)
    assert CFG["factors"]["weights"]["news_risk"] == 0.15
    for f in ("growth", "quality", "valuation", "trend"):
        assert sum(v.get("weight", 0) for v in CFG[f].values() if isinstance(v, dict)) == pytest.approx(1), f
    assert sum(CFG["balance_sheet"][k]["weight"] for k in ("net_debt_to_fcf", "cash_to_debt")) == pytest.approx(1)
    assert sum(t["weight"] for t in CFG["news_risk"]["topics"].values()) == pytest.approx(1)
    assert [(b["min"], b["label"]) for b in scoring.bands(CFG)] == [
        (80, "Strong Buy"), (65, "Buy"), (45, "Hold"), (30, "Sell"), (0, "Strong Sell")]
    assert set(CFG["overlay"]["ai_exposed"]) == {"NVDA", "AMD", "AVGO", "MRVL", "MU", "TSM", "MSFT", "GOOGL", "AMZN", "META",
                                                 "ORCL", "CRDO", "ALAB", "ANET", "VRT", "SMCI", "DELL", "CRWV"}
    assert CFG["max_web_searches"] == 8 and CFG["news_window_days"] == 30
    assert CFG["models"] == {"research": "claude-sonnet-5-5", "scoring": "claude-opus-5-5"}


def test_interp():
    pts = [[0, 0], [10, 50], [20, 100]]
    assert [scoring.interp(pts, x) for x in (-5, 0, 5, 10, 15, 20, 99)] == [0, 0, 25, 50, 75, 100, 100]
    assert scoring.interp([[2, 100], [10, 0]], 6) == 50  # falling scale (lower is better)


def test_band_edges_and_distance():
    assert [scoring.band(s, CFG) for s in (100, 80, 79, 65, 64, 45, 44, 30, 29, 0)] == [
        "Strong Buy", "Strong Buy", "Buy", "Buy", "Hold", "Hold", "Sell", "Sell", "Strong Sell", "Strong Sell"]
    assert scoring.distance(72, CFG) == {"up": {"label": "Strong Buy", "at": 80, "points": 8},
                                         "down": {"label": "Hold", "below": 65, "points": 8}}
    assert scoring.distance(90, CFG)["up"] is None and scoring.distance(10, CFG)["down"] is None


def test_factor_needs_half_its_inputs():
    full = scoring.combine({"a": scoring._metric(1, 80, 0.5), "b": scoring._metric(1, 40, 0.5)}, 0.5)
    assert full["score"] == 60 and full["coverage"] == 1
    half = scoring.combine({"a": scoring._metric(1, 80, 0.5), "b": scoring._metric(None, None, 0.5)}, 0.5)
    assert half["score"] == 80 and half["coverage"] == 0.5
    thin = scoring.combine({"a": scoring._metric(1, 80, 0.4), "b": scoring._metric(None, None, 0.6)}, 0.5)
    assert thin["score"] is None and thin["missing"] == "only 40% of its inputs had data"
    assert scoring.combine({"a": scoring._metric(None, None, 1)}, 0.5, "no price data")["missing"] == "no price data"


def summary(**kw):
    s = {"revenue_ttm": 100e9, "revenue_yoy_q": 20, "revenue_yoy_ttm": 15, "acceleration_pp": 5, "gross_margin_pct": 60,
         "operating_margin_pct": 30, "fcf_margin_pct": 25, "sbc_pct_revenue": 5, "dilution_yoy_pct": 0, "fcf_ttm": 25e9,
         "balance_sheet": {"net_debt": 50e9, "cash_and_investments": 10e9, "debt": 60e9}}
    s.update(kw)
    return s


def test_growth_and_quality_scores_follow_the_config_points():
    fs = scoring.factors(summary(), {"missing": "no price data"}, {"missing": "no price data"}, CFG)
    # growth: 20% -> 75, 15% -> 65, +5 pp -> 75; weights .45/.30/.25
    assert fs["growth"]["score"] == round(75 * .45 + 65 * .3 + 75 * .25)
    # quality: 60 -> 75, 30 -> 85, 25 -> 85, 5 -> 80, 0 -> 75
    assert fs["quality"]["score"] == round(75 * .2 + 85 * .25 + 85 * .25 + 80 * .15 + 75 * .15)
    assert fs["valuation"]["score"] is None and fs["valuation"]["missing"] == "no price data"
    # net debt / FCF = 2 years -> 70; cash / debt = 0.167 -> 20
    assert fs["balance_sheet"]["metrics"]["net_debt_to_fcf"]["value"] == 2
    assert fs["balance_sheet"]["score"] == round(70 * .6 + 20 * .4)


@pytest.mark.parametrize("bs,fcf,score,note", [
    ({"net_debt": -10e9, "cash_and_investments": 30e9, "debt": 20e9}, 5e9, 100, "net cash"),
    ({"net_debt": -10e9, "cash_and_investments": 10e9, "debt": 0}, -5e9, 50, "2.0 years of cash"),
    ({"net_debt": 10e9, "cash_and_investments": 1e9, "debt": 11e9}, -1e9, 0, "net debt with negative FCF"),
    ({"net_debt": 40e9, "cash_and_investments": 1e9, "debt": 41e9}, 10e9, 40, None),
])
def test_balance_sheet_rules(bs, fcf, score, note):
    m = scoring.balance_sheet(summary(balance_sheet=bs, fcf_ttm=fcf), CFG)["net_debt_to_fcf"]
    assert m["score"] == score and (note is None or note in m["note"])


def test_no_debt_scores_full_cash_cover():
    m = scoring.balance_sheet(summary(balance_sheet={"net_debt": -5e9, "cash_and_investments": 5e9, "debt": 0,
                                                     "debt_note": "no debt tagged"}), CFG)["cash_to_debt"]
    assert m["score"] == 100 and m["note"] == "no debt tagged"


def test_valuation_scores_and_negative_multiples():
    v = {"price_to_sales": 3, "price_to_fcf": None, "fcf_negative": True, "ev_to_ebit": 25, "fcf_yield_vs_10y_pp": 0,
         "vs_history": {"price_to_sales": {"percentile": 20}, "price_to_fcf": {"percentile": None}, "ev_to_ebit": {"percentile": 90}}}
    m = scoring.valuation(v, CFG)
    assert (m["price_to_sales"]["score"], m["price_to_fcf"]["score"], m["ev_to_ebit"]["score"]) == (75, 0, 45)
    assert m["price_to_fcf"]["note"] == "negative FCF" and m["fcf_yield_vs_10y_pp"]["score"] == 50
    assert (m["price_to_sales_percentile"]["score"], m["price_to_fcf_percentile"]["score"], m["ev_to_ebit_percentile"]["score"]) == (80, None, 10)


def test_news_risk_from_topic_ratings():
    assert scoring.news_risk({}, CFG)["score"] is None
    assert scoring.news_risk(None, CFG, "Claude step off")["missing"] == "Claude step off"
    zero = {k: {"rating": 0} for k in CFG["news_risk"]["topics"]}
    assert scoring.news_risk(zero, CFG)["score"] == 50
    best = {k: {"rating": 2} for k in CFG["news_risk"]["topics"]}
    worst = {k: {"rating": -2} for k in CFG["news_risk"]["topics"]}
    assert scoring.news_risk(best, CFG)["score"] == 100 and scoring.news_risk(worst, CFG)["score"] == 0
    mixed = dict(zero, guidance={"rating": -2}, analyst_actions={"rating": 1})
    assert scoring.news_risk(mixed, CFG)["score"] == round(50 - 0.25 * 2 * 25 + 0.15 * 25)


def test_topic_rating_capped_by_checkable_sources():
    t = {"rating": 2, "evidence": [{"status": "verified"}, {"status": "unverified"}]}
    scoring.cap_topic(t)
    assert t["rating"] == 1 and t["rating_before_cap"] == 2 and "1 of 2 sources" in t["cap_note"]
    t = {"rating": -1, "evidence": [{"status": "unverified"}]}
    scoring.cap_topic(t)
    assert t["rating"] == 0
    t = {"rating": -2, "evidence": [{"status": "verified"}, {"status": "unreachable"}]}
    scoring.cap_topic(t)
    assert t["rating"] == -2 and "cap_note" not in t
    t = {"rating": 0, "evidence": []}
    scoring.cap_topic(t)
    assert t == {"rating": 0, "evidence": []}


def capped(rating, sources, cfg=CFG):
    """`sources`: (tier, status) pairs. Both caps, in the run's order."""
    t = {"rating": rating, "evidence": [{"tier": a, "status": b} for a, b in sources]}
    scoring.cap_topic(t)
    scoring.cap_weak_sources(t, cfg)
    return t


def test_source_quality_thresholds_in_config():
    q = CFG["news_risk"]["source_quality"]
    assert q == {"strong_tiers": ["primary", "major"], "strong_min_verified": 1, "cap_without_strong": 1,
                 "cap_other_only": 1, "cap_other_only_unverified": 0}
    assert scoring.source_quality(CFG) == q and scoring.source_quality({}) == q


@pytest.mark.parametrize("rating, sources, out", [
    (2, [("major", "verified")] * 2, 2),
    (-2, [("primary", "verified"), ("other", "unreachable")], -2),
    (2, [("primary", "unreachable"), ("other", "verified")], 1),     # +-2 needs a *verified* primary/major source
    (-2, [("major", "unverified"), ("other", "verified"), ("other", "verified")], -1),
    (-2, [("other", "verified")] * 2, -1),                            # only other-tier: +-1
    (1, [("other", "verified")], 1),
    (-1, [("other", "unreachable")], 0),                              # only other-tier, none verified: 0
    (2, [("other", "unreachable")] * 2, 0),
    (1, [("major", "unreachable")], 1),                               # a primary/major source, even unverified, isn't "only other"
    (0, [("other", "unverified")], 0),
])
def test_weak_sources_cap(rating, sources, out):
    assert capped(rating, sources)["rating"] == out


def test_weak_sources_note_only_when_the_quality_rule_caps():
    t = capped(2, [("primary", "unreachable"), ("other", "verified")])
    assert t["rating_before_cap"] == 2 and "cap_note" not in t
    assert t["weak_sources_note"] == ("Capped at +1 (was +2): a rating above ±1 needs 1 verified primary- or "
                                      "major-tier source; 0 of its 2 sources qualify")
    t = capped(-1, [("other", "unreachable")])
    assert t["weak_sources_note"] == "Capped at 0 (was -1): only other-tier sources, none verified"
    # both rules cap: each says so, the lower cap wins, rating_before_cap stays Claude's rating
    t = capped(2, [("other", "verified"), ("other", "unverified")])
    assert t["rating"] == 1 and t["rating_before_cap"] == 2 and "1 of 2 sources" in t["cap_note"]
    assert t["weak_sources_note"].startswith("Capped at +1 (was +2)")
    t = capped(2, [("other", "unreachable"), ("other", "unverified")])
    assert t["rating"] == 0 and t["rating_before_cap"] == 2 and t["cap_note"].startswith("Capped at +1")
    assert t["weak_sources_note"].startswith("Capped at 0 (was +2)")
    t = capped(-2, [("other", "unverified")])   # both cap at 0
    assert t["rating"] == 0 and t["weak_sources_note"] == "Capped at 0 (was -2): only other-tier sources, none verified"
    # the count rule alone, or the count rule lower: no weak-sources flag
    for t in (capped(2, [("major", "verified"), ("other", "unverified")]), capped(1, []), capped(2, []),
              capped(2, [("major", "verified")]), capped(-2, [("major", "unverified")])):
        assert "weak_sources_note" not in t and abs(t["rating"]) < 2
    t = {"rating": 0, "evidence": []}
    scoring.cap_weak_sources(t, CFG)
    assert t == {"rating": 0, "evidence": []}


def test_weak_sources_thresholds_come_from_config():
    cfg = copy.deepcopy(CFG)
    cfg["news_risk"]["source_quality"].update(strong_tiers=["primary"], strong_min_verified=2, cap_other_only_unverified=1)
    assert capped(2, [("primary", "verified"), ("major", "verified")], cfg)["rating"] == 1
    assert capped(2, [("primary", "verified")] * 2, cfg)["rating"] == 2
    assert capped(-1, [("other", "unreachable")], cfg)["rating"] == -1
    t = capped(2, [("major", "verified")] * 2, cfg)   # major isn't a strong tier here
    assert t["rating"] == 1 and "needs 2 verified primary-tier sources; 0 of its 2 sources qualify" in t["weak_sources_note"]
    assert scoring.source_quality_rule(cfg) == ("a rating above ±1 needs at least 2 verified primary-tier sources; a topic "
                                                "with no primary-tier source is capped at ±1, and at ±1 if none of its "
                                                "sources verify.")
    assert scoring.source_quality_rule(CFG).endswith("is capped at ±1, and at 0 if none of its sources verify.")


def test_composite_renormalises_weights_and_sets_confidence():
    fs = {k: {"score": 80} for k in scoring.FACTORS}
    fs["valuation"] = {"score": 20}
    c = scoring.composite(fs, CFG)
    assert c["raw"] == pytest.approx(80 * .8 + 20 * .2) and c["score"] == 68
    fs["trend"] = {"score": None}
    fs["valuation"] = {"score": None}
    c = scoring.composite(fs, CFG)
    assert c["score"] == 80 and c["missing"] == ["valuation", "trend"]
    assert c["weights_used"]["growth"] == pytest.approx(0.2 / 0.65, abs=1e-4)
    assert scoring.composite({k: {"score": None} for k in scoring.FACTORS}, CFG)["score"] is None
    assert [scoring.confidence(n, CFG) for n in (6, 5, 4, 3, 1)] == ["High", "Medium", "Medium", "Low", "Low"]
    assert scoring.composite({**{k: {"score": 64} for k in scoring.FACTORS}, "growth": {"score": 66.5}}, CFG)["score"] == 65


CTX = {"total": 10.0, "max": 18.0, "age_days": 1}


@pytest.mark.parametrize("ticker,rating,ctx,out", [
    ("NVDA", "Strong Buy", {**CTX, "level": "RED", "red_confirmed": True}, ("Hold", True, True, "Capped at Hold (was Strong Buy)")),
    ("NVDA", "Sell", {**CTX, "level": "RED", "red_confirmed": True}, ("Sell", True, False, "already at or below it")),
    ("NVDA", "Buy", {**CTX, "level": "RED", "red_confirmed": False}, ("Buy", True, False, "(unconfirmed): warning signs")),
    ("MSFT", "Buy", {**CTX, "level": "AMBER", "red_confirmed": False}, ("Buy", True, False, "AMBER 10/18: warning signs")),
    ("MSFT", "Buy", {**CTX, "level": "GREEN", "red_confirmed": False}, ("Buy", False, False, "no overlay")),
    ("AAPL", "Buy", {**CTX, "level": "RED", "red_confirmed": True}, ("Buy", False, False, None)),
    ("MSFT", "Buy", None, ("Buy", False, False, "no data yet")),
])
def test_overlay(ticker, rating, ctx, out):
    ov = scoring.overlay(ticker, rating, ctx, CFG)
    final, applies, capped, note = out
    assert (ov.get("rating", rating), ov["applies"], ov["capped"]) == (final, applies, capped)
    assert (note is None and ov["note"] is None) or note in ov["note"]


def test_overlay_mentions_stale_capex_data():
    ov = scoring.overlay("MSFT", "Buy", {**CTX, "level": "AMBER", "age_days": 30}, CFG)
    assert "(capex data 30 days old)" in ov["note"]
