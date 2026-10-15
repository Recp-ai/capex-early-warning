"""Valuation against the company's own history, in words: the phrases code builds for the write-up, and
the post-check of the write-up against them.

metrics.valuation ranks each multiple against its own month-end history on yields; its "percentile" is the
share of month-ends that were cheaper than now. Given that bare number, the write-up read it backwards
(NVDA's EV/EBIT at 0, the cheapest of 61 month-ends, became "has not been more expensive than now at any
of them"). So the write-up gets only words, built here: per multiple "cheaper than at 58 of the last 61
month-ends", "more expensive than at 57 of the last 61 month-ends" or "near the middle of the last 61
month-ends (...)", and an overall label, cheap / middle / expensive vs own history (thresholds in
valuation.history_label). `conflicts` then finds write-up sentences that call valuation expensive (or
cheap) against its own history while the label says the opposite; stocks/run.py regenerates the write-up
once naming them, and drops any sentence still in conflict (noted in the run details).
"""
from __future__ import annotations

import re

MULTIPLES = {"price_to_sales": "P/S", "price_to_fcf": "P/FCF", "ev_to_ebit": "EV/EBIT"}
FIGURE = {"price_to_sales": "revenue", "price_to_fcf": "FCF", "ev_to_ebit": "EBIT"}
OVERALL = {"cheap": "cheap against its own history", "middle": "near the middle of its own history",
           "expensive": "expensive against its own history"}
FIELDS = ("headline", "thesis", "key_risks", "what_would_change")


# ---------------------------------------------------------------- the phrases
def thresholds(cfg: dict | None) -> tuple[float, float]:
    h = ((cfg or {}).get("valuation") or {}).get("history_label") or {}
    return float(h.get("cheap_below", 33)), float(h.get("expensive_above", 67))


def label(share: float | None, cfg: dict | None) -> str | None:
    """cheap / middle / expensive for a share (0-100) of month-ends that were cheaper than now."""
    if share is None:
        return None
    lo, hi = thresholds(cfg)
    return "cheap" if share < lo else "expensive" if share > hi else "middle"


def phrase(h: dict, lab: str) -> str:
    """One multiple against its month-end history, from the counts (never the percentile)."""
    n = int(h["points"])
    cheaper = h.get("cheaper_points")
    cheaper = round(h["percentile"] * n / 100) if cheaper is None else int(cheaper)
    dearer = h.get("dearer_points")
    dearer = n - cheaper if dearer is None else int(dearer)
    if lab == "cheap":
        return (f"cheaper than at every one of the last {n} month-ends" if dearer == n
                else f"cheaper than at {dearer} of the last {n} month-ends")
    if lab == "expensive":
        return (f"more expensive than at every one of the last {n} month-ends" if cheaper == n
                else f"more expensive than at {cheaper} of the last {n} month-ends")
    return f"near the middle of the last {n} month-ends (cheaper than at {dearer}, more expensive than at {cheaper})"


def history_view(val: dict, cfg: dict | None) -> dict:
    """{"label": cheap|middle|expensive|None, "text", "multiples": {key: {"label", "phrase", "note"?} or
    {"label": None, "phrase": None, "missing": why}}}. The overall label is the average of the multiples'
    shares, weighted by valuation.<multiple>_percentile.weight."""
    vc = (cfg or {}).get("valuation") or {}
    min_pts = int(vc.get("min_history_points", 24))
    vs = val.get("vs_history") or {}
    by, shares, weights = {}, {}, {}
    for k in MULTIPLES:
        h = vs.get(k) or {}
        p = h.get("percentile")
        if p is None:
            why = (val["ev_note"] if k == "ev_to_ebit" and val.get("ev_note")
                   else f"{h.get('points') or 0} month-ends of history (needs {min_pts})" if h else "no history")
            by[k] = {"label": None, "phrase": None, "missing": why}
            continue
        lab = label(p, cfg)
        e = {"label": lab, "phrase": phrase(h, lab)}
        y = h.get("current_yield_pct")
        if y is not None and y <= 0:
            e["note"] = (f"{FIGURE[k]} is zero or negative now, so there is no {MULTIPLES[k]} multiple; the comparison ranks "
                         f"the {FIGURE[k]} yield ({h.get('negative_points') or 0} of the {h['points']} month-ends were zero "
                         "or negative too)")
        by[k] = e
        shares[k], weights[k] = p, float((vc.get(f"{k}_percentile") or {}).get("weight", 1))
    total = sum(weights.values())
    if not shares or not total:
        return {"label": None, "text": "no comparison with its own history", "multiples": by}
    lab = label(sum(shares[k] * weights[k] for k in shares) / total, cfg)
    text = OVERALL[lab]
    if {"cheap", "expensive"} <= {by[k]["label"] for k in shares}:
        text += " (the multiples disagree: " + ", ".join(f"{MULTIPLES[k]} {by[k]['label']}" for k in shares) + ")"
    return {"label": lab, "text": text, "multiples": by}


def brief(view: dict | None, missing: str | None = None) -> dict:
    """What the write-up is given: the label and phrases, no percentile."""
    if not view or not view.get("label"):
        return {"label": None, "overall": (view or {}).get("text") or "no comparison with its own history"
                + (f" ({missing})" if missing else "")}
    return {"label": view["label"], "overall": view["text"],
            "multiples": {MULTIPLES[k]: ({"label": v["label"], "phrase": v["phrase"], **({"note": v["note"]} if v.get("note") else {})}
                                         if v.get("phrase") else {"label": None, "not_compared": v.get("missing")})
                          for k, v in view["multiples"].items()}}


# ---------------------------------------------------------------- the post-check
_ABBREV = re.compile(r"\b(?:vs|Inc|Corp|Co|Ltd|No|approx|e\.g|i\.e|U\.S|Mr|Ms|Dr|St)\.$")


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“'(])", (text or "").strip()):
        if out and _ABBREV.search(out[-1]):
            out[-1] += " " + part
        elif part:
            out.append(part)
    return out


_HISTORY = re.compile(r"\bhistor(?:y|ic|ical|ically)\b|\bmonth[- ]?ends?\b|\b(?:five|5)[- ]year\b"
                      r"|\bits (?:own )?(?:range|average|median|norm)\b|\bthan (?:usual|normal|typical(?:ly)?)\b"
                      r"|\b(?:past|last) (?:five|5|few) years\b", re.I)
_ABSOLUTE = re.compile(r"\babsolute\b|\boutright\b", re.I)
_CLAUSE = re.compile(r";|\b(?:but|though|although|while|whereas|yet|however)\b", re.I)
_NOT_VALUATION = re.compile(r"\b\d+[- ](?:week|month|day|year)[- ](?:highs?|lows?)\b|\b(?:all-time|record|multi-year) (?:highs?|lows?)\b", re.I)
_CONTEXT = re.compile(r"valu|multiple|\bP/S\b|\bP/FCF\b|\bEV/EBIT\b|price[- ]to|times (?:sales|revenue|free cash flow|FCF|EBIT)", re.I)
_NEG = re.compile(r"(?:\bnot|\bless|n't|\bno longer|\bnever|\bhardly|\bfar from)\s+"
                  r"(?:(?:as|so|that|too|very|particularly|especially|overly)\s+)?$", re.I)
_VERB = r"(?:is|are|was|were|looks?|remains?|appears?|seems?|sits?|stays?|trades?|stands?)"
_SUBJ = r"(?:valuations?|multiples?|it|stock|shares|P/S|P/FCF|EV/EBIT)"
# strong words count in any clause about history; weak ones only next to a valuation word and not after "yield"
_TERMS = {
    "expensive": ([r"\b(?:expensive|pricey|pricier|overvalued|stretched|lofty)\b", r"(?<!-)\b(?:rich|richer|richly)\b",
                   r"\bat a premium\b|\bpremium to\b"],
                  [rf"\b{_SUBJ}\s+{_VERB}\s+(?:\w+\s+)?(?:high|elevated|demanding)\b",
                   r"\b(?:high|higher|elevated)\s+(?:against|versus|vs\.?|relative to|compared (?:with|to)|than|by)\b",
                   r"\b(?:high|higher|elevated|demanding)\s+(?:valuations?|multiples?)\b",
                   r"\b(?:top|high end|upper end) of (?:its|the) (?:own )?(?:history|range)\b"]),
    "cheap": ([r"\b(?:cheap|cheaper|cheapest|inexpensive|undervalued|bargain)\b", r"\bat a discount\b|\bdiscount to\b"],
              [rf"\b{_SUBJ}\s+{_VERB}\s+(?:\w+\s+)?(?:low|modest|depressed|compressed)\b",
               r"\b(?:low|lower|modest|depressed)\s+(?:against|versus|vs\.?|relative to|compared (?:with|to)|than|by)\b",
               r"\b(?:low|lower|modest|depressed|compressed)\s+(?:valuations?|multiples?)\b",
               r"\b(?:bottom|low end|lower end) of (?:its|the) (?:own )?(?:history|range)\b"]),
}
_RX = {d: ([re.compile(x, re.I) for x in strong], [re.compile(x, re.I) for x in weak]) for d, (strong, weak) in _TERMS.items()}
_MULT = {"price_to_sales": re.compile(r"\bP/S\b|price[- ]to[- ]sales|times (?:trailing )?(?:sales|revenue)|(?:sales|revenue) multiple", re.I),
         "price_to_fcf": re.compile(r"\bP/FCF\b|price[- ]to[- ](?:free[- ]cash[- ]flow|FCF)|times (?:trailing )?(?:free cash flow|FCF)"
                                    r"|(?:FCF|free[- ]cash[- ]flow) multiple", re.I),
         "ev_to_ebit": re.compile(r"\bEV/EBIT\b|\bEV[- ]to[- ]EBIT\b|enterprise value to EBIT|times EBIT|EBIT multiple", re.I)}
_FLIP = {"expensive": "cheap", "cheap": "expensive"}


def _claims(clause: str) -> set[str]:
    """Which way the clause calls valuation (expensive / cheap); "not cheap", "less expensive" flip."""
    found = set()
    context = bool(_CONTEXT.search(clause))
    for direction, (strong, weak) in _RX.items():
        for rx, is_weak in [(r, False) for r in strong] + [(r, True) for r in weak]:
            if is_weak and not context:
                continue
            for m in rx.finditer(clause):
                before = clause[max(0, m.start() - 40):m.start()]
                if is_weak and re.search(r"\byields?\b", before, re.I):
                    continue  # "the FCF yield is higher": a higher yield is cheaper
                found.add(_FLIP[direction] if _NEG.search(before) else direction)
    return found


def _code_phrases(view: dict) -> list[str]:
    """Each multiple's phrase and note (the middle phrase holds both directions). Not the overall text: it has
    one direction, and "not cheap against its own history" must still count."""
    out = [x for v in (view.get("multiples") or {}).values() for x in (v.get("phrase"), v.get("note")) if x]
    return sorted(out, key=len, reverse=True)


def sentence_conflict(sentence: str, view: dict) -> dict | None:
    """{"claims", "label", "about"} when a clause about the company's own history (history, month-ends, its
    usual range ...) calls valuation expensive while the code's label is cheap, or the reverse. The label is
    that of the multiples the clause (else the sentence) names, else the overall one. The code's own
    phrases are taken out first, and clauses that say "absolute" are skipped."""
    if not view or not view.get("label"):
        return None
    clean = sentence
    for p in _code_phrases(view):
        clean = re.sub(re.escape(p), " ", clean, flags=re.I)
    clean = _NOT_VALUATION.sub(" ", clean)
    named = [k for k, rx in _MULT.items() if rx.search(sentence)]
    labels = view.get("multiples") or {}
    for clause in _CLAUSE.split(clean):
        if not _HISTORY.search(clause) or _ABSOLUTE.search(clause):
            continue
        claims = _claims(clause)
        if not claims:
            continue
        about = [k for k, rx in _MULT.items() if rx.search(clause)] or named
        labs = {(labels.get(k) or {}).get("label") for k in about} - {None} or {view["label"]}
        for claim in claims:
            if _FLIP[claim] in labs and claim not in labs:
                return {"claims": claim, "label": _FLIP[claim],
                        "about": [MULTIPLES[k] for k in about if (labels.get(k) or {}).get("label")] or ["valuation"]}
    return None


def _texts(w: dict):
    for f in FIELDS:
        v = w.get(f)
        if isinstance(v, str):
            yield f, None, v
        elif isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, str):
                    yield f, i, x


def conflicts(w: dict | None, view: dict | None) -> list[dict]:
    """Every write-up sentence in conflict: [{"field", "item", "sentence", "claims", "label", "about"}]."""
    out = []
    for f, i, text in _texts(w or {}):
        for s in sentences(text):
            c = sentence_conflict(s, view or {})
            if c:
                out.append({"field": f, "item": i, "sentence": s, **c})
    return out


def drop(w: dict, found: list[dict]) -> dict:
    """The write-up without the sentences in `found`; a list item left empty is removed."""
    bad = {(c["field"], c["item"], c["sentence"]) for c in found}
    out = dict(w)
    for f in FIELDS:
        v = w.get(f)
        if isinstance(v, str):
            out[f] = " ".join(s for s in sentences(v) if (f, None, s) not in bad)
        elif isinstance(v, list):
            items = [" ".join(s for s in sentences(x) if (f, i, s) not in bad) if isinstance(x, str) else x
                     for i, x in enumerate(v)]
            out[f] = [x for x in items if x]
    return out


def _describe(c: dict, verb: str = "calls") -> str:
    return f"{verb} {' and '.join(c['about'])} {c['claims']} against its own history, but code labels it {c['label']}"


def correction(found: list[dict], view: dict) -> str:
    """The note for the one regeneration: what conflicted, and the code's labels and phrases."""
    lines = "\n".join(f'- "{c["sentence"]}" {_describe(c)}' for c in found)
    per = "\n".join(f"- {MULTIPLES[k]}: {v['label']}, \"{v['phrase']}\"" for k, v in (view.get("multiples") or {}).items() if v.get("phrase"))
    return ("## Correction needed\nA check by code found sentences in your note that contradict the valuation-vs-history "
            f"labels:\n{lines}\n\nThe code's labels: overall \"{view['text']}\"; per multiple:\n{per}\n\n"
            "Write the whole note again. Keep what was right. Describe valuation against its own history only with "
            "these phrases, word for word; never call it expensive, rich, stretched or high against its history when "
            "the label is cheap, or cheap when it is expensive. The multiples themselves and the FCF yield vs the "
            "10-year are absolute levels: call them high or low only in absolute terms.")


def summary(first: list[dict], regenerated: bool, dropped: list[dict], error: str | None = None) -> str | None:
    """The run-details line, or None when the first draft passed."""
    if not first:
        return None
    n = len(first)
    same = all(_describe(c) == _describe(first[0]) for c in first)
    text = (f"Write-up check: {n} sentence{'s' * (n != 1)} of the first draft {_describe(first[0], 'called')}"
            + ("" if same else " (or similar)") + "; ")
    if regenerated:
        text += "regenerated once"
    else:
        text += f"regenerating failed ({error})" if error else "not regenerated"
    if dropped:
        text += (f"; {len(dropped)} sentence{'s' * (len(dropped) != 1)} still in conflict "
                 f"{'were' if len(dropped) != 1 else 'was'} dropped: " + " / ".join(f'"{c["sentence"]}"' for c in dropped))
    elif regenerated:
        text += " and the new draft passed"
    return text + "."
