"""Valuation versus own history in words (stocks/wording.py): the code-built phrases and label, and the
write-up post-check, on the sentences the Oct 15 pages got wrong. Pure functions."""
import copy

import pytest

from stocks import config, wording

CFG = config.load()


def hist(points, cheaper, dearer, current_yield=1.0, negative=0):
    """One multiple's vs_history entry: `cheaper` month-ends were cheaper than now, `dearer` dearer."""
    return {"points": points, "cheaper_points": cheaper, "dearer_points": dearer, "current_yield_pct": current_yield,
            "percentile": round(100 * (cheaper + 0.5 * (points - cheaper - dearer)) / points, 1), "negative_points": negative}


def nc(points, negative, reason, current_yield=1.0):
    """A vs_history entry metrics.valuation marked not comparable (FCF / EBIT negative now or too often)."""
    return {"points": points, "cheaper_points": None, "dearer_points": None, "current_yield_pct": current_yield,
            "percentile": None, "negative_points": negative, "not_comparable": reason}


def view(ps, pfcf, evebit, cfg=CFG, **val):
    return wording.history_view({"vs_history": {"price_to_sales": ps, "price_to_fcf": pfcf, "ev_to_ebit": evebit}, **val}, cfg)


NVDA = view(hist(61, 10, 51), hist(41, 2, 39), hist(61, 0, 61))          # 16th, 5th, 0th percentile
GTLB = view(hist(55, 11, 44), nc(55, 29, "negative in 29 of 55 month-ends"),
            nc(55, 55, "negative now and in 55 of 55 month-ends", -1.17))
SHOP = view(hist(61, 26, 35), hist(61, 15, 46), hist(61, 9, 52, negative=9))  # 43rd, 25th, 15th (9 of 61 is under 20%)
PLTR = view(hist(61, 51, 10), hist(61, 40, 21, negative=1), nc(57, 21, "negative in 21 of 57 month-ends"))
MIXED = view(hist(61, 51, 10), hist(61, 40, 21), hist(57, 4, 53))         # 83rd, 65th, 8th: the multiples disagree
ORCL = view(hist(61, 17, 44), nc(61, 17, "negative now and in 17 of 61 month-ends", -10.96), hist(61, 3, 58))


def test_thresholds_live_in_config():
    assert CFG["valuation"]["history_label"] == {"cheap_below": 33, "expensive_above": 67}
    assert [wording.label(p, CFG) for p in (0, 32.9, 33, 67, 67.1, 100, None)] == [
        "cheap", "cheap", "middle", "middle", "expensive", "expensive", None]
    cfg = copy.deepcopy(CFG)
    cfg["valuation"]["history_label"] = {"cheap_below": 10, "expensive_above": 90}
    assert wording.label(16.4, cfg) == "middle"


def test_phrases_come_from_the_counts_never_the_percentile():
    m = NVDA["multiples"]
    assert m["price_to_sales"] == {"label": "cheap", "phrase": "cheaper than at 51 of the last 61 month-ends"}
    assert m["price_to_fcf"]["phrase"] == "cheaper than at 39 of the last 41 month-ends"
    assert m["ev_to_ebit"]["phrase"] == "cheaper than at every one of the last 61 month-ends"
    assert NVDA["label"] == "cheap" and NVDA["text"] == "cheap against its own history"
    assert PLTR["multiples"]["price_to_sales"] == {"label": "expensive", "phrase": "more expensive than at 51 of the last 61 month-ends"}
    assert PLTR["multiples"]["price_to_fcf"]["phrase"] == ("near the middle of the last 61 month-ends "
                                                           "(cheaper than at 21, more expensive than at 40)")
    # weighted average of the three (0.10 / 0.15 / 0.15): 83 / 65 / 8 is the middle, with the disagreement named
    assert MIXED["label"] == "middle"
    assert MIXED["text"] == "near the middle of its own history (the multiples disagree: P/S expensive, P/FCF middle, EV/EBIT cheap)"
    assert SHOP["label"] == "cheap" and SHOP["multiples"]["price_to_sales"]["label"] == "middle"


def test_not_comparable_multiples_get_their_phrase_and_stay_out_of_the_label():
    # ORCL's P/FCF (negative now): it made the label "middle"; P/S and EV/EBIT alone say cheap
    assert ORCL["multiples"]["price_to_fcf"] == {"label": None, "phrase": "not comparable (negative now and in 17 of 61 month-ends)",
                                                 "not_comparable": "negative now and in 17 of 61 month-ends"}
    assert ORCL["label"] == "cheap" and ORCL["text"] == "cheap against its own history (P/FCF not comparable)"
    # PLTR's EV/EBIT (negative in 21 of 57) was the "cheap" pulling it to the middle
    assert PLTR["label"] == "expensive" and PLTR["text"] == "expensive against its own history (EV/EBIT not comparable)"
    assert GTLB["label"] == "cheap" and GTLB["text"] == "cheap against its own history (P/FCF and EV/EBIT not comparable)"
    # the disagreement and the not-comparable ones in one parenthesis
    v = view(hist(61, 57, 4), hist(61, 2, 59), nc(61, 30, "negative in 30 of 61 month-ends"))
    assert v["text"] == "near the middle of its own history (the multiples disagree: P/S expensive, P/FCF cheap; EV/EBIT not comparable)"
    # the write-up gets the phrase; no percentile
    b = wording.brief(ORCL)
    assert b["multiples"]["P/FCF"] == {"label": "not comparable", "phrase": "not comparable (negative now and in 17 of 61 month-ends)"}
    assert b["multiples"]["EV/EBIT"]["label"] == "cheap" and "percentile" not in str(b)
    # nothing comparable: no label, but the phrases still reach the write-up
    v = view({"points": 12, "percentile": None}, nc(61, 36, "negative in 36 of 61 month-ends"), nc(25, 14, "negative in 14 of 25 month-ends"))
    assert v["label"] is None and v["text"] == "no comparison with its own history (P/FCF and EV/EBIT not comparable)"
    b = wording.brief(v)
    assert b["label"] is None and b["overall"] == v["text"]
    assert b["multiples"] == {"P/S": {"label": None, "not_compared": "12 month-ends of history (needs 24)"},
                              "P/FCF": {"label": "not comparable", "phrase": "not comparable (negative in 36 of 61 month-ends)"},
                              "EV/EBIT": {"label": "not comparable", "phrase": "not comparable (negative in 14 of 25 month-ends)"}}


def test_missing_history_and_the_write_up_brief():
    v = view(hist(61, 10, 51), {"points": 12, "percentile": None}, {"points": 61, "percentile": None},
             ev_note="EV unknown: debt tag stopped reporting")
    assert v["multiples"]["price_to_fcf"] == {"label": None, "phrase": None, "missing": "12 month-ends of history (needs 24)"}
    assert v["multiples"]["ev_to_ebit"]["missing"] == "EV unknown: debt tag stopped reporting"
    assert v["label"] == "cheap"  # from the one multiple that has a history
    b = wording.brief(v)
    assert b == {"label": "cheap", "overall": "cheap against its own history",
                 "multiples": {"P/S": {"label": "cheap", "phrase": "cheaper than at 51 of the last 61 month-ends"},
                               "P/FCF": {"label": None, "not_compared": "12 month-ends of history (needs 24)"},
                               "EV/EBIT": {"label": None, "not_compared": "EV unknown: debt tag stopped reporting"}}}
    assert "percentile" not in str(b)
    none = view({}, {}, {})
    assert none["label"] is None and wording.brief(none, None) == {"label": None, "overall": "no comparison with its own history"}
    assert wording.brief(None, "no price data")["overall"] == "no comparison with its own history (no price data)"


# The Oct 15 write-ups: each sentence, the view it was written against, and what the check must say.
WRONG = [
    (NVDA, "Valuation holds the score back at 53, because the stock trades at 41.993 times free cash flow, and on EV to EBIT "
           "it has not been more expensive than now at any of the last 61 month-ends.", ["P/FCF", "EV/EBIT"]),
    (NVDA, "Valuation is high against the stock's own history, with a free cash flow yield 2.829 points below the 10-year "
           "Treasury yield.", ["valuation"]),
    (GTLB, "Valuation (46) is a drag, at 8.499 times sales and 41.972 times free cash flow, more expensive than in about 80% "
           "of the past 55 month-ends.", ["P/S"]),                     # P/FCF not comparable: P/S's label decides
    (GTLB, "Valuation is high against its own history, and the free-cash-flow yield trails the 10-year Treasury by 2.827 points.",
     ["valuation"]),
    (SHOP, "Valuation is stretched: EV/EBIT of 118.735 is cheaper than now in only 15.6% of the past 61 month-ends.", ["EV/EBIT"]),
    (NVDA, "Valuation is not cheap against its own history.", ["valuation"]),
    (NVDA, "Shares look rich versus their five-year history.", ["valuation"]),
]


@pytest.mark.parametrize("v, sentence, about", WRONG)
def test_check_flags_valuation_called_expensive_against_history_when_the_label_is_cheap(v, sentence, about):
    assert wording.sentence_conflict(sentence, v) == {"claims": "expensive", "label": "cheap", "about": about}


RIGHT = [
    (NVDA, "On EV/EBIT the stock is cheaper than at every one of the last 61 month-ends."),          # the code's phrase
    (NVDA, "NVIDIA rates Buy at 77, with exceptional growth, quality and balance sheet offset by a rich valuation."),  # no history
    (NVDA, "At 42 times FCF the stock is expensive in absolute terms, though cheap against its own history."),
    (NVDA, "Valuation is rich in absolute terms against its history of faster growth."),               # says absolute
    (NVDA, "The shares sit 12% below their 52-week high, and P/S is less expensive than usual."),     # "less expensive": cheap
    (SHOP, "P/S is near the middle of the last 61 month-ends (cheaper than at 35, more expensive than at 26)."),
    (PLTR, "Valuation leaves little room for error: price-to-sales of 83.859 is richer than in 82.8% of the past 61 month-ends."),
    (ORCL, "Valuation scores 35: price-to-sales of 5.5 and EV/EBIT of 20.949 look modest against the stock's own five-year history, "
           "but negative free cash flow leaves no cash-flow support."),
    (PLTR, "The FCF yield is higher than at most month-ends in its history."),                        # a higher yield is cheaper
    (ORCL, "P/FCF is not comparable (negative now and in 17 of 61 month-ends), so it is left out."),  # the code's phrase
    (GTLB, "P/FCF and EV/EBIT are not comparable with their own history, and the stock is cheap against its own history on P/S."),
]


@pytest.mark.parametrize("v, sentence", RIGHT)
def test_check_leaves_correct_or_absolute_sentences_alone(v, sentence):
    assert wording.sentence_conflict(sentence, v) is None


def test_check_flags_the_reverse_too():
    exp = view(hist(61, 57, 4), hist(61, 55, 6), hist(61, 58, 3))
    assert exp["label"] == "expensive"
    assert wording.sentence_conflict("The stock looks cheap against its own history.", exp)["claims"] == "cheap"
    assert wording.sentence_conflict("P/S is more expensive than at 57 of the last 61 month-ends.", exp) is None
    assert wording.sentence_conflict("Valuation is expensive against its own history.", MIXED) is None  # middle: not checked


# A multiple that is not comparable can't be cheap or expensive against its history at all.
NOT_COMPARABLE = [
    (ORCL, "P/FCF is more expensive than at 60 of the last 61 month-ends (FCF is negative now).", "expensive", ["P/FCF"]),
    (GTLB, "On EV/EBIT the stock is cheaper than at every one of the last 55 month-ends.", "cheap", ["EV/EBIT"]),
    (PLTR, "EV/EBIT looks cheap against its own history.", "cheap", ["EV/EBIT"]),
    (GTLB, "Both P/FCF and EV/EBIT look cheap against their history.", "cheap", ["P/FCF", "EV/EBIT"]),
]


@pytest.mark.parametrize("v, sentence, claims, about", NOT_COMPARABLE)
def test_check_flags_a_not_comparable_multiple_called_cheap_or_expensive_against_history(v, sentence, claims, about):
    assert wording.sentence_conflict(sentence, v) == {"claims": claims, "label": "not comparable", "about": about}


def test_not_comparable_correction_and_note():
    found = wording.conflicts({"thesis": NOT_COMPARABLE[0][1]}, ORCL)
    assert [c["label"] for c in found] == ["not comparable"]
    fix = wording.correction(found, ORCL)
    assert '- P/FCF: not comparable, "not comparable (negative now and in 17 of 61 month-ends)"' in fix
    assert "never call a multiple that is not comparable cheap or expensive against its history" in fix
    assert wording.summary(found, True, []) == ("Write-up check: 1 sentence of the first draft called P/FCF expensive against "
                                                "its own history, but code labels it not comparable; regenerated once and the "
                                                "new draft passed.")
    # with no label at all, the check still runs for the not-comparable multiples
    v = view({}, nc(61, 36, "negative in 36 of 61 month-ends"), {})
    assert v["label"] is None and wording.sentence_conflict("P/FCF is cheap against its history.", v)["label"] == "not comparable"
    assert wording.sentence_conflict("Valuation is cheap against its history.", v) is None


def test_conflicts_drop_and_the_run_details_note():
    w = {"headline": "NVIDIA looks expensive against its own history",
         "thesis": f"Growth is exceptional. {WRONG[0][1]} Trend is middling.",
         "key_risks": [WRONG[1][1], "Export controls."], "what_would_change": ["A 3-point gain would reach Strong Buy."]}
    found = wording.conflicts(w, NVDA)
    assert [(c["field"], c["item"]) for c in found] == [("headline", None), ("thesis", None), ("key_risks", 0)]
    out = wording.drop(w, found)
    assert out == {"headline": "", "thesis": "Growth is exceptional. Trend is middling.", "key_risks": ["Export controls."],
                   "what_would_change": ["A 3-point gain would reach Strong Buy."]}
    assert wording.conflicts(out, NVDA) == [] and wording.conflicts(w, None) == []
    fix = wording.correction(found, NVDA)
    assert WRONG[0][1] in fix and "code labels it cheap" in fix and '"cheaper than at 51 of the last 61 month-ends"' in fix
    assert wording.summary([], False, []) is None
    assert wording.summary(found, True, []) == (
        "Write-up check: 3 sentences of the first draft called valuation expensive against its own history, but code "
        "labels it cheap (or similar); regenerated once and the new draft passed.")
    assert wording.summary(found[:1], True, []).startswith("Write-up check: 1 sentence of the first draft called valuation "
                                                           "expensive against its own history, but code labels it cheap; ")
    note = wording.summary(found[:1], True, found[1:2])
    assert note.endswith(f'regenerated once; 1 sentence still in conflict was dropped: "{WRONG[0][1]}".')
    assert "regenerating failed (APIError: x)" in wording.summary(found[:1], False, found[:1], "APIError: x")


def test_sentence_split_keeps_abbreviations_and_decimals():
    assert wording.sentences("Revenue rose 21.3% vs. last year. It lags U.S. peers. Fine") == [
        "Revenue rose 21.3% vs. last year.", "It lags U.S. peers.", "Fine"]
