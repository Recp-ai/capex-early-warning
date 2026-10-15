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


IN, OLD = {"event_date": "2026-10-10", "background": False}, {"event_date": "2026-09-01", "background": True}  # run on Oct 15


def test_topic_rating_capped_by_checkable_sources():
    t = {"rating": 2, "evidence": [{"status": "verified", **IN}, {"status": "unverified", **IN}]}
    scoring.cap_topic(t, CFG)
    assert t["rating"] == 1 and t["rating_before_cap"] == 2
    assert t["cap_note"] == "Capped at +1 (was +2): 1 of 2 sources verified or unreachable and dated within the last 30 days"
    t = {"rating": -1, "evidence": [{"status": "unverified", **IN}]}
    scoring.cap_topic(t, CFG)
    assert t["rating"] == 0 and t["cap_note"] == "Capped at 0 (was -1): 0 of 1 sources verified or unreachable"
    t = {"rating": -2, "evidence": [{"status": "verified", **IN}, {"status": "unreachable", **IN}]}
    scoring.cap_topic(t, CFG)
    assert t["rating"] == -2 and "cap_note" not in t
    t = {"rating": 0, "evidence": []}
    scoring.cap_topic(t, CFG)
    assert t == {"rating": 0, "evidence": []}


def test_only_in_window_sources_count_toward_the_count_and_quality_caps():
    # two verified major sources, one older: +-2 no longer holds (it did when every checked source counted)
    for other in ({"status": "verified", **OLD}, {"status": "verified", "event_date": None, "background": False}):
        t = {"rating": 2, "evidence": [{"tier": "major", "status": "verified", **IN}, {"tier": "major", **other}]}
        scoring.cap_all(t, CFG)
        assert t["rating"] == 1 and t["rating_before_cap"] == 2 and "old_evidence_note" not in t
        assert t["cap_note"] == ("Capped at +1 (was +2): 1 of 2 sources verified or unreachable and dated within the last "
                                 "30 days (1 more checked out but is older or undated)")
    t = {"rating": -2, "evidence": [{"tier": "major", "status": "verified", **IN}, {"tier": "other", "status": "unreachable", **IN}]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == -2 and not set(scoring.CAP_NOTES) & set(t)
    # the quality cap: an older verified primary / major source doesn't make +-2 possible
    t = {"rating": -2, "evidence": [{"tier": "primary", "status": "verified", **OLD},
                                    {"tier": "other", "status": "verified", **IN}, {"tier": "other", "status": "verified", **IN}]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == -1 and "cap_note" not in t and "old_evidence_note" not in t
    assert t["weak_sources_note"] == ("Capped at -1 (was -2): a rating above ±1 needs 1 verified primary- or major-tier "
                                      "source dated within the last 30 days; 0 of its 3 sources qualify")
    # none in the window: the date cap's job (0, "only older evidence"), not the count cap's
    t = {"rating": 1, "evidence": [{"tier": "major", "status": "verified", **OLD}]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == 0 and "cap_note" not in t and t["old_evidence_note"].startswith("Capped at 0 (was +1)")
    assert scoring.in_window({"status": "unreachable", **IN}) and not scoring.in_window({"status": "unverified", **IN})


def capped(rating, sources, cfg=CFG):
    """`sources`: (tier, status) pairs, all dated inside the window. Both caps, in the run's order."""
    t = {"rating": rating, "evidence": [{"tier": a, "status": b, **IN} for a, b in sources]}
    scoring.cap_topic(t, cfg)
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
                                      "major-tier source dated within the last 30 days; 0 of its 2 sources qualify")
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
    assert t["rating"] == 1 and ("needs 2 verified primary-tier sources dated within the last 30 days; 0 of its 2 sources "
                                 "qualify") in t["weak_sources_note"]
    assert scoring.source_quality_rule(cfg) == ("a rating above ±1 needs at least 2 verified primary-tier sources; a topic "
                                                "with no primary-tier source is capped at ±1, and at ±1 if none of its "
                                                "sources verify.")
    assert scoring.source_quality_rule(CFG).endswith("is capped at ±1, and at 0 if none of its sources verify.")


def dated(rating, sources):
    """`sources`: (status, event_date, background) triples, every one major-tier. All three caps."""
    t = {"rating": rating, "evidence": [{"tier": "major", "status": s, "event_date": d, "background": b} for s, d, b in sources]}
    scoring.cap_all(t, CFG)
    return t


def test_only_evidence_dated_inside_the_window_supports_a_rating():
    assert CFG["evidence_dates"]["background_after_days"] == CFG["news_window_days"] == 30
    # INOD's insider -1 on two Form 4s from Sep 4 and Sep 14 (the run on Oct 15): capped at 0
    t = dated(-1, [("verified", "2026-09-04", True), ("verified", "2026-09-14", True)])
    assert t["rating"] == 0 and t["rating_before_cap"] == -1 and "cap_note" not in t and "weak_sources_note" not in t
    assert t["old_evidence_note"] == ("Capped at 0 (was -1): no source that checked out is dated within the last 30 days; "
                                      "of the 2 that did, 2 dated more than 30 days before the run (2026-09-04, 2026-09-14)")
    assert scoring.OLD_EVIDENCE == "capped: only older evidence"
    # undated items support nothing either
    t = dated(1, [("verified", None, False), ("unreachable", "2026-09-01", True)])
    assert t["rating"] == 0 and t["old_evidence_note"].endswith(
        "of the 2 that did, 1 dated more than 30 days before the run (2026-09-01) and 1 undated")
    # one checked item inside the window is enough (verified or unreachable), even beside older ones
    for src in ([("verified", "2026-10-10", False), ("verified", "2026-09-01", True)], [("unreachable", "2026-10-10", False)]):
        t = dated(1, src)
        assert t["rating"] == 1 and "old_evidence_note" not in t
    # a recent item that didn't check out doesn't count: the older verified one alone can't hold the rating
    t = dated(-1, [("unverified", "2026-10-10", False), ("verified", "2026-09-01", True)])
    assert t["rating"] == 0 and "of the 1 that did, 1 dated more than 30 days" in t["old_evidence_note"]
    # nothing checkable at all: the count cap's job (already 0), no date note
    t = dated(1, [("unverified", "2026-10-10", False)])
    assert t["rating"] == 0 and "cap_note" in t and "old_evidence_note" not in t
    t = dated(0, [("verified", "2026-09-01", True)])
    assert t["rating"] == 0 and "old_evidence_note" not in t and "rating_before_cap" not in t


def test_date_cap_comes_after_the_other_caps_and_uncap_restores_claudes_rating():
    t = {"rating": 2, "evidence": [{"tier": "other", "status": "verified", "event_date": "2026-09-10", "background": True},
                                   {"tier": "other", "status": "verified", "event_date": "2026-09-10", "background": True}]}
    scoring.cap_all(t, CFG)  # ORCL's product +1: two Sep 10 items (weak sources first, then the dates)
    assert t["rating"] == 0 and t["rating_before_cap"] == 2
    assert t["weak_sources_note"].startswith("Capped at +1 (was +2)") and t["old_evidence_note"].startswith("Capped at 0 (was +2)")
    capped_once = copy.deepcopy(t)
    scoring.uncap(t)
    assert t["rating"] == 2 and not set(scoring.CAP_NOTES) & set(t) and "rating_before_cap" not in t
    scoring.cap_all(t, CFG)
    assert t == capped_once  # re-applying the caps gives the same result



def ev(direction, status="verified", tier="other", when=IN):
    return {"tier": tier, "status": status, "direction": direction, **when}


def test_a_rating_needs_recent_evidence_pointing_its_way():
    assert scoring.NO_DIRECTION == "capped: no recent evidence in this direction"
    assert ("direction_note", scoring.NO_DIRECTION) in scoring.CAP_TAGS and "direction_note" in scoring.CAP_NOTES
    # ORCL guidance +1: the Sep 10 guidance (positive, older) and one in-window item, Project Jupiter's delay (negative)
    sep10 = {"event_date": "2026-09-10", "background": True}
    t = {"rating": 1, "evidence": [ev("positive", tier="primary", when=sep10), ev("positive", tier="primary", when=sep10),
                                   ev("positive", "unverified", when=sep10),
                                   ev("negative", when={"event_date": "2026-10-14", "background": False})]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == 0 and t["rating_before_cap"] == 1 and set(t) & set(scoring.CAP_NOTES) == {"direction_note"}
    assert t["direction_note"] == ("Capped at 0 (was +1): no positive source that checked out is dated within the last 30 "
                                   "days; the 1 in the window is negative (2026-10-14); 3 positive ones are older, undated "
                                   "or didn't check out")
    # RBRK analyst actions +1: two upgrades that didn't verify, and Weiss reiterating a Sell (verified, negative)
    t = {"rating": 1, "evidence": [ev("positive", "unverified"), ev("positive", "unverified"),
                                   ev("negative", when={"event_date": "2026-09-21", "background": False})]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == 0 and set(t) & set(scoring.CAP_NOTES) == {"direction_note"}
    assert t["direction_note"].endswith("the 1 in the window is negative (2026-09-21); 2 positive ones are older, "
                                        "undated or didn't check out")
    # neutral supports neither sign
    t = {"rating": -1, "evidence": [ev("neutral", when={"event_date": "2026-10-02", "background": False}), ev("positive")]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == 0 and t["direction_note"] == ("Capped at 0 (was -1): no negative source that checked out is dated "
                                                        "within the last 30 days; of the 2 in the window, 1 neutral "
                                                        "(2026-10-02) and 1 positive (2026-10-10)")
    # one in-window item pointing the rating's way is enough, even beside items pointing the other way
    for rating, items in ((1, [ev("positive"), ev("negative")]), (-1, [ev("negative", "unreachable"), ev("positive", tier="major")])):
        t = {"rating": rating, "evidence": items}
        scoring.cap_all(t, CFG)
        assert t["rating"] == rating and not set(t) & set(scoring.CAP_NOTES)
    # nothing in the window at all stays the date cap's case
    t = {"rating": 1, "evidence": [ev("negative", when=OLD)]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == 0 and "old_evidence_note" in t and "direction_note" not in t
    # a recent item pointing the other way that didn't check out: nothing checked in the window, the date cap again
    t = {"rating": 1, "evidence": [ev("positive", when=OLD), ev("negative", "unverified")]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == 0 and "old_evidence_note" in t and "direction_note" not in t


def test_the_count_and_quality_caps_count_only_sources_pointing_the_ratings_way():
    t = {"rating": 2, "evidence": [ev("positive", tier="major"), ev("negative", tier="major"), ev("neutral")]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == 1 and t["cap_note"] == ("Capped at +1 (was +2): 1 of 3 sources verified or unreachable, dated "
                                                  "within the last 30 days and positive (2 more in the window are not positive)")
    assert "weak_sources_note" not in t  # the positive source is a verified major one
    # +-2 needs a verified primary / major source pointing its way: a major one pointing the other way doesn't count
    t = {"rating": -2, "evidence": [ev("positive", tier="major"), ev("negative"), ev("negative")]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == -1 and "cap_note" not in t and t["weak_sources_note"] == (
        "Capped at -1 (was -2): a rating above ±1 needs 1 verified primary- or major-tier negative source dated within "
        "the last 30 days; 0 of its 3 sources qualify")
    t = {"rating": -2, "evidence": [ev("negative", tier="primary"), ev("negative", "unreachable")]}
    scoring.cap_all(t, CFG)
    assert t["rating"] == -2 and not set(t) & set(scoring.CAP_NOTES)
    # uncap restores Claude's rating; re-applying gives the same result
    t = {"rating": 1, "evidence": [ev("negative")]}
    scoring.cap_all(t, CFG)
    capped_once = copy.deepcopy(t)
    scoring.uncap(t)
    assert t == {"rating": 1, "evidence": [ev("negative")]}
    scoring.cap_all(t, CFG)
    assert t == capped_once


def test_unlabelled_evidence_matches_and_is_listed_as_unchecked():
    # archived runs have no direction labels: they count as pointing the rating's way, so nothing changes
    old = {"rating": 1, "evidence": [{"tier": "other", "status": "verified", **IN}]}
    scoring.cap_all(old, CFG)
    assert old["rating"] == 1 and not set(old) & set(scoring.CAP_NOTES) and scoring.direction_unchecked(old)
    assert scoring.points_with({}, 1) and scoring.points_with({"direction": None}, -1) and not scoring.labelled({})
    assert not scoring.points_with({"direction": "neutral"}, 1) and not scoring.points_with({"direction": "negative"}, 2)
    # labelled, zero, or resting only on older unlabelled items (already capped at 0): nothing to check
    lab = {"rating": 1, "evidence": [ev("positive")]}
    scoring.cap_all(lab, CFG)
    assert lab["rating"] == 1 and not scoring.direction_unchecked(lab)
    assert not scoring.direction_unchecked({"rating": 0, "evidence": [{"status": "verified", **IN}]})
    assert not scoring.direction_unchecked({"rating": 1, "evidence": [{"status": "verified", **OLD}]})
    # a mix: one labelled supporting item plus an unlabelled one: still unchecked (the unlabelled one counts)
    assert scoring.direction_unchecked({"rating": -1, "evidence": [ev("negative"), {"status": "verified", **IN}]})


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


def conf(score=72, fs=None, summary=None, val=None, topics=None, cfg=CFG):
    fs = fs or {k: {"score": 70, "metrics": {"m": {"score": 50}}} for k in scoring.FACTORS}
    val = val if val is not None else {"vs_history": {k: {"points": 60} for k in ("price_to_sales", "price_to_fcf", "ev_to_ebit")}}
    return scoring.confidence_detail(fs, score, summary or {}, val, topics or {}, cfg)


def test_confidence_is_data_quality_and_steps_down_per_issue():
    c = conf()
    assert (c["level"], c["points"], c["reasons"], c["factors_with_data"]) == ("High", 0, [], 6)
    weak = {"analyst_actions": {"label": "Analyst actions", "weak_sources_note": "Capped at +1"}}
    c = conf(topics=weak)
    assert c["level"] == "Medium" and c["reasons"] == [{"issue": "weak_sources", "points": 1.0,
                                                        "text": "news topics capped for weak sources: Analyst actions"}]
    assert conf(63)["reasons"][0]["text"] == "score 63 is within 2 points of the Buy edge (65)"
    assert conf(67)["level"] == "Medium" and conf(68)["level"] == "High" and conf(46)["level"] == "Medium"
    summary = {"balance_sheet": {"unknown": ["debt unknown: LongTermDebt was $1.15B on 2025-04-30, and no debt tag covers 2026-07-31"]},
               "prepayments": {"applies": True, "increase": 65.2e6, "excess": 60.1e6, "revenue_growth_pct": 57.0,
                               "share_of_ocf": 0.62, "max_share_of_ocf": 0.25}, "fcf_basis": "ex-prepayments"}
    fs = {k: {"score": 70, "metrics": {}} for k in scoring.FACTORS}
    fs["balance_sheet"] = {"score": None}
    fs["quality"]["metrics"] = {"gross_margin_pct": {"score": None}, "operating_margin_pct": {"score": None}}
    val = {"vs_history": {"price_to_sales": {"points": 61}, "price_to_fcf": {"points": 61}, "ev_to_ebit": {"points": 10}}}
    c = conf(63, fs, summary, val, weak)
    assert c["level"] == "Low" and c["points"] == 7
    assert [r["issue"] for r in c["reasons"]] == ["missing_factor", "missing_sub_metric", "unknown_cash_debt", "fcf_ex_prepayments",
                                                  "short_valuation_history", "near_band_edge", "weak_sources"]
    texts = [r["text"] for r in c["reasons"]]
    assert texts[0] == "1 of 6 factors had no data: Balance sheet" and texts[1] == "missing inputs: gross margin, operating margin"
    assert texts[3] == ("excess prepayments: contract liabilities rose $65.2M over the year, $60.1M more than revenue growth "
                        "of 57.0% explains (62% of operating cash flow; threshold 25%)")
    assert texts[4] == "valuation history: EV/EBIT 10 month-ends (needs 24)"


def test_confidence_thresholds_and_points_come_from_config():
    assert CFG["confidence"]["medium_at"] == 1 and CFG["confidence"]["low_at"] == 3 and CFG["confidence"]["band_edge_points"] == 2
    cfg = copy.deepcopy(CFG)
    cfg["confidence"].update(medium_at=2, band_edge_points=0)
    cfg["confidence"]["points"]["weak_sources"] = 3
    assert conf(63, cfg=cfg)["level"] == "High"                       # 2 points from the edge no longer counts
    assert conf(topics={"g": {"weak_sources_note": "x"}}, cfg=cfg)["level"] == "Low"
    # a percentile missing for a short history is counted once, as the short history
    fs = {k: {"score": 70, "metrics": {}} for k in scoring.FACTORS}
    fs["valuation"]["metrics"] = {"ev_to_ebit_percentile": {"score": None}}
    c = conf(fs=fs, val={"vs_history": {"ev_to_ebit": {"points": 5}}})
    assert [r["issue"] for r in c["reasons"]] == ["short_valuation_history"]
