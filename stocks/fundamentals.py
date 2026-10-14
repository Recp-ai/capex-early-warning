"""SEC XBRL companyfacts -> quarterly / TTM fundamentals, balance sheet and share counts.

Flows (revenue, cash flow, ...) are reported year-to-date in 10-Qs, so 3-month values are derived by
differencing YTD facts that share a fiscal-year start, as cew/fetch_edgar does; Q4 is the 10-K year
minus the 9-month YTD. Each point keeps the date it was first filed, so the valuation history only
uses what was public on each date. Foreign filers (20-F / 40-F, IFRS) usually report annual figures
only: TTM is then the latest fiscal year, and valuation is left out (see `summarize`).
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from cew import fetch_edgar, util

from . import config

FACTS_URL = fetch_edgar.FACTS_URL
PERIODIC_FORMS = ("10-Q", "10-K", "20-F", "40-F")
FOREIGN_FORMS = ("20-F", "40-F")
KEEP_YEARS = 8

FLOW_TAGS = {
    "revenue": {
        "us-gaap": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet"],
        "ifrs-full": ["Revenue", "RevenueFromContractsWithCustomers"],
    },
    "gross_profit": {"us-gaap": ["GrossProfit"], "ifrs-full": ["GrossProfit"]},
    "cost_of_revenue": {
        "us-gaap": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
        "ifrs-full": ["CostOfSales"],
    },
    "operating_income": {"us-gaap": ["OperatingIncomeLoss"], "ifrs-full": ["ProfitLossFromOperatingActivities"]},
    "operating_cash_flow": {
        "us-gaap": ["NetCashProvidedByUsedInOperatingActivities",
                    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
        "ifrs-full": ["CashFlowsFromUsedInOperatingActivities"],
    },
    "capex": {
        "us-gaap": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                    "PaymentsForCapitalImprovements"],
        "ifrs-full": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                      "PurchaseOfPropertyPlantAndEquipment"],
    },
    "sbc": {
        "us-gaap": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
        "ifrs-full": ["AdjustmentsForSharebasedPayments", "ExpenseFromSharebasedPaymentTransactions"],
    },
}
CASH_TAGS = {
    "us-gaap": ["CashAndCashEquivalentsAtCarryingValue",
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "ifrs-full": ["CashAndCashEquivalents"],
}
INVESTMENT_TAGS = {
    "us-gaap": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "ifrs-full": ["CurrentFinancialAssetsAtAmortisedCost", "OtherCurrentFinancialAssets"],
}
# Debt: the first group whose first tag has a value on the balance-sheet date is summed (leases excluded),
# plus the first short-term group present.
DEBT_GROUPS = {
    "us-gaap": {
        "long": [["LongTermDebt"], ["LongTermDebtNoncurrent", "LongTermDebtCurrent"],
                 ["LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
                 ["SeniorLongTermNotes"], ["ConvertibleNotesPayable"], ["ConvertibleDebtNoncurrent", "ConvertibleDebtCurrent"]],
        "short": [["ShortTermBorrowings"], ["CommercialPaper"]],
    },
    "ifrs-full": {
        "long": [["Borrowings"], ["NoncurrentPortionOfNoncurrentBorrowings", "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings"],
                 ["LongtermBorrowings", "ShorttermBorrowings"]],
        "short": [],
    },
}
DILUTED_TAGS = {"us-gaap": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
                "ifrs-full": ["AdjustedWeightedAverageShares", "WeightedAverageShares"]}
INSTANT_TAGS = {tax: sorted({t for group in (CASH_TAGS, INVESTMENT_TAGS) for t in group[tax]}
                            | {t for gs in DEBT_GROUPS[tax].values() for g in gs for t in g})
                for tax in ("us-gaap", "ifrs-full")}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _periodic(e: dict) -> bool:
    return e.get("val") is not None and "end" in e and str(e.get("form", "")).startswith(PERIODIC_FORMS)


def _keep(points: dict, cutoff: str) -> dict:
    return dict(sorted((k, v) for k, v in points.items() if k >= cutoff))


def dedup(entries: list[dict], duration: bool) -> dict:
    """(start, end) -> fact for duration facts, end -> fact for instants. Value from the latest filing
    (restatements win), "filed" = the first filing that reported the period."""
    out: dict = {}
    for e in entries:
        if not _periodic(e) or (duration and "start" not in e) or (not duration and "start" in e):
            continue
        key = (e["start"], e["end"]) if duration else e["end"]
        cur = out.get(key)
        filed = e.get("filed", "")
        if cur is None:
            out[key] = {"val": float(e["val"]), "filed": filed, "last": filed, "form": e.get("form", "")}
            continue
        if filed > cur["last"]:
            cur.update(val=float(e["val"]), last=filed, form=e.get("form", ""))
        cur["filed"] = min(cur["filed"], filed) if cur["filed"] else filed
    return out


def flow_points(entries: list[dict]) -> tuple[dict, dict]:
    """(quarters, annual): end -> {"val", "filed", "derived"}. Quarters are 80-100 day facts, else the
    difference of consecutive YTD facts with the same start (Q2 = 6M - 3M, Q4 = FY - 9M)."""
    facts = dedup(entries, duration=True)
    quarters: dict = {}
    annual: dict = {}
    for (start, end), f in facts.items():
        d = _days(start, end)
        if 80 <= d <= 100:
            quarters[end] = {"val": f["val"], "filed": f["filed"], "derived": False}
        elif 350 <= d <= 380:
            annual[end] = {"val": f["val"], "filed": f["filed"]}
    by_start: dict[str, list] = {}
    for (start, end), f in facts.items():
        if _days(start, end) <= 380:
            by_start.setdefault(start, []).append((end, f))
    for group in by_start.values():
        group.sort()
        for (pend, prev), (end, cur) in zip(group, group[1:]):
            if 80 <= _days(pend, end) <= 100 and end not in quarters:
                quarters[end] = {"val": cur["val"] - prev["val"], "filed": max(cur["filed"], prev["filed"]),
                                 "derived": True}
    return dict(sorted(quarters.items())), dict(sorted(annual.items()))


def share_points(entries: list[dict]) -> tuple[dict, dict]:
    """Weighted-average share counts are averages, not flows: quarterly and annual facts as reported,
    never differenced."""
    facts = dedup(entries, duration=True)
    q, a = {}, {}
    for (start, end), f in facts.items():
        d = _days(start, end)
        if 80 <= d <= 100:
            q[end] = {"val": f["val"], "filed": f["filed"]}
        elif 350 <= d <= 380:
            a[end] = {"val": f["val"], "filed": f["filed"]}
    return dict(sorted(q.items())), dict(sorted(a.items()))


def _tax(facts: dict, name: str) -> dict:
    return (facts.get("facts") or {}).get(name) or {}


def _units(facts: dict, tax: str, tag: str) -> dict:
    return (_tax(facts, tax).get(tag) or {}).get("units") or {}


def pick_taxonomy_and_currency(facts: dict) -> tuple[str | None, str | None]:
    """us-gaap if it has revenue, else ifrs-full; the currency is USD if reported, else the unit with
    the most revenue facts."""
    for tax in ("us-gaap", "ifrs-full"):
        counts: dict[str, int] = {}
        for tag in FLOW_TAGS["revenue"][tax]:
            for unit, rows in _units(facts, tax, tag).items():
                if re.fullmatch(r"[A-Z]{3}", unit):
                    counts[unit] = counts.get(unit, 0) + len(rows)
        if counts:
            return tax, "USD" if "USD" in counts else max(counts, key=counts.get)
    return None, None


def merged_flow(facts: dict, tax: str, currency: str, tags: list[str]) -> dict:
    """The tag with the most recent data leads; older periods it lacks are filled from the other tags."""
    per_tag = []
    for tag in tags:
        q, a = flow_points(_units(facts, tax, tag).get(currency, []))
        if q or a:
            per_tag.append((tag, q, a))
    if not per_tag:
        return {"tag": None, "quarters": {}, "annual": {}}
    lead = max(per_tag, key=lambda x: (max([*x[1], *x[2]]), -tags.index(x[0])))
    quarters, annual = dict(lead[1]), dict(lead[2])
    for tag, q, a in per_tag:
        for k, v in q.items():
            quarters.setdefault(k, v)
        for k, v in a.items():
            annual.setdefault(k, v)
    return {"tag": lead[0], "quarters": dict(sorted(quarters.items())), "annual": dict(sorted(annual.items()))}


def is_foreign(facts: dict, tax: str, currency: str) -> bool:
    """Annual reports on 20-F / 40-F (or no 10-K / 10-Q revenue facts at all)."""
    forms = [str(e.get("form", "")) for tag in FLOW_TAGS["revenue"][tax]
             for e in _units(facts, tax, tag).get(currency, [])]
    if any(f.startswith(("10-K", "10-Q")) for f in forms):
        latest = max((e for tag in FLOW_TAGS["revenue"][tax] for e in _units(facts, tax, tag).get(currency, [])
                      if _periodic(e)), key=lambda e: e.get("filed", ""), default={})
        return str(latest.get("form", "")).startswith(FOREIGN_FORMS)
    return True


def cover_shares(facts: dict) -> dict | None:
    """dei:EntityCommonStockSharesOutstanding from the latest filing's cover page; several classes
    (e.g. Alphabet's A, B and C) on the same date are added up."""
    rows = [e for e in (_units(facts, "dei", "EntityCommonStockSharesOutstanding").get("shares") or [])
            if e.get("val") is not None and e.get("end") and e.get("accn")]
    if not rows:
        return None
    latest = max(rows, key=lambda e: (e.get("filed", ""), e["end"]))
    same = [e for e in rows if e["accn"] == latest["accn"] and e["end"] == latest["end"]]
    vals = list(dict.fromkeys((e.get("frame"), float(e["val"])) for e in same))  # drop exact repeats
    return {"val": sum(v for _, v in vals), "end": latest["end"], "filed": latest.get("filed", ""),
            "classes": len(vals)}


def extract(facts: dict, cik: str) -> dict:
    """The cached fundamentals record (data/stocks/fundamentals/<TICKER>.json): the last KEEP_YEARS years
    before the latest revenue figure."""
    tax, currency = pick_taxonomy_and_currency(facts)
    out = {"cik": cik, "entity": facts.get("entityName"), "taxonomy": tax, "currency": currency,
           "foreign": None, "flows": {}, "instants": {}, "shares": {}, "fetched_at": util.now_iso()}
    if not tax:
        return out
    out["foreign"] = is_foreign(facts, tax, currency)
    flows = {metric: merged_flow(facts, tax, currency, tags[tax]) for metric, tags in FLOW_TAGS.items()}
    latest = max([*flows["revenue"]["quarters"], *flows["revenue"]["annual"]], default=None)
    cutoff = (date.fromisoformat(latest) - timedelta(days=365 * KEEP_YEARS)).isoformat() if latest else ""
    for metric, m in flows.items():
        out["flows"][metric] = {"tag": m["tag"], "quarters": _keep(m["quarters"], cutoff), "annual": _keep(m["annual"], cutoff)}
    for tag in INSTANT_TAGS[tax]:
        pts = {end: {"val": f["val"], "filed": f["filed"]}
               for end, f in dedup(_units(facts, tax, tag).get(currency, []), duration=False).items()}
        if pts:
            out["instants"][tag] = _keep(pts, cutoff)
    for tag in DILUTED_TAGS[tax]:
        q, a = share_points(_units(facts, tax, tag).get("shares", []))
        if q or a:
            out["shares"] = {"tag": tag, "diluted_q": _keep(q, cutoff), "diluted_a": _keep(a, cutoff)}
            break
    out["shares"]["outstanding"] = cover_shares(facts)
    return out


def fetch(sess, cik: str) -> dict:
    return util.get_json(sess, FACTS_URL.format(cik=str(cik).zfill(10)), timeout=120)


def cache_path(ticker: str):
    return config.data_dir() / "fundamentals" / f"{ticker}.json"


def run(sess, ticker: str, cik: str, offline: bool = False) -> dict:
    """Fetch and cache, or read the cache when offline. Returns the record, or one with "error"."""
    path = cache_path(ticker)
    try:
        if offline:
            rec = util.read_json(path)
            if rec is None:
                raise RuntimeError("offline and no cached fundamentals")
            return rec
        rec = extract(fetch(sess, cik), cik)
        util.write_json(path, rec)
        return rec
    except Exception as e:
        return {"cik": cik, "error": f"{type(e).__name__}: {str(e)[:300]}", "flows": {}, "instants": {}, "shares": {}}


# ---------------------------------------------------------------- series helpers
def ttm_points(flow: dict) -> dict:
    """end -> {"val", "filed"}: four consecutive quarters added up, or the fiscal year as reported."""
    q = flow.get("quarters") or {}
    ends = list(q)
    out = {}
    for i in range(3, len(ends)):
        window = ends[i - 3:i + 1]
        if all(80 <= _days(a, b) <= 100 for a, b in zip(window, window[1:])):
            out[ends[i]] = {"val": sum(q[e]["val"] for e in window), "filed": max(q[e]["filed"] for e in window)}
    for end, f in (flow.get("annual") or {}).items():
        out.setdefault(end, {"val": f["val"], "filed": f["filed"]})
    return dict(sorted(out.items()))


def year_before(points: dict, end: str) -> dict | None:
    """The point 350-380 days before `end` (closest to a year)."""
    best = None
    for e, v in points.items():
        d = _days(e, end)
        if 350 <= d <= 380 and (best is None or abs(d - 365) < abs(_days(best[0], end) - 365)):
            best = (e, v)
    return best and best[1]


def asof(points: dict, day: str) -> tuple[str, dict] | None:
    """The latest (end, point) already filed on `day`."""
    best = None
    for e, v in points.items():
        if (v.get("filed") or "") <= day and (best is None or e > best[0]):
            best = (e, v)
    return best


def value_at(f: dict, metric: str, end: str) -> float | None:
    p = ttm_points((f.get("flows") or {}).get(metric) or {}).get(end)
    return None if p is None else p["val"]


def _at(f: dict, tag: str, end: str) -> float | None:
    p = ((f.get("instants") or {}).get(tag) or {}).get(end)
    return None if p is None else p["val"]


def balance_sheet_at(f: dict, end: str) -> dict:
    """Cash, short-term investments and debt on one balance-sheet date."""
    tax = f.get("taxonomy") or "us-gaap"
    cash = next((v for t in CASH_TAGS[tax] if (v := _at(f, t, end)) is not None), None)
    inv = next((v for t in INVESTMENT_TAGS[tax] if (v := _at(f, t, end)) is not None), 0.0)
    groups = DEBT_GROUPS[tax]
    long_g = next((g for g in groups["long"] if _at(f, g[0], end) is not None), None)
    short_g = next((g for g in groups["short"] if _at(f, g[0], end) is not None), None)
    debt = sum(_at(f, t, end) or 0.0 for g in (long_g, short_g) if g for t in g)
    note = None if (long_g or short_g) else "no debt tagged in XBRL on this date (counted as zero)"
    return {"date": end, "cash": cash, "short_term_investments": inv,
            "cash_and_investments": None if cash is None else cash + inv, "debt": debt,
            "debt_tags": [t for g in (long_g, short_g) if g for t in g], "debt_note": note,
            "net_debt": None if cash is None else debt - cash - inv}


def balance_sheet_dates(f: dict) -> dict:
    """end -> first filed, for dates that have a cash figure."""
    tax = f.get("taxonomy") or "us-gaap"
    out: dict = {}
    for t in CASH_TAGS[tax]:
        for end, p in ((f.get("instants") or {}).get(t) or {}).items():
            out[end] = min(out.get(end, p["filed"]), p["filed"])
    return dict(sorted(out.items()))


def _pct(new, old):
    v = util.pct_change(new, old)
    return None if v is None else round(v, 2)


def _ratio(a, b, pct=True):
    if a is None or b in (None, 0):
        return None
    return round(a / b * (100 if pct else 1), 2)


def summarize(f: dict) -> dict:
    """Latest fundamentals for scoring and display. Missing inputs give None, never a guess."""
    flows = f.get("flows") or {}
    rev = flows.get("revenue") or {}
    rq = rev.get("quarters") or {}
    rttm = ttm_points(rev)
    s: dict = {"currency": f.get("currency"), "taxonomy": f.get("taxonomy"), "foreign": f.get("foreign"),
               "annual_only": not rq, "revenue_tag": rev.get("tag")}
    if rq:
        ends = list(rq)
        e0 = ends[-1]
        s["latest_quarter_end"], s["revenue_q"] = e0, rq[e0]["val"]
        prev_year = year_before(rq, e0)
        s["revenue_yoy_q"] = _pct(rq[e0]["val"], prev_year and prev_year["val"])
        prev_q = ends[-2] if len(ends) > 1 and 80 <= _days(ends[-2], e0) <= 100 else None
        if prev_q:
            py = year_before(rq, prev_q)
            s["revenue_yoy_q_prev"] = _pct(rq[prev_q]["val"], py and py["val"])
        acc = (s["revenue_yoy_q"] - s["revenue_yoy_q_prev"]) if s.get("revenue_yoy_q") is not None and s.get("revenue_yoy_q_prev") is not None else None
        s["acceleration_pp"] = None if acc is None else round(acc, 2)
    elif rev.get("annual"):  # annual filers: last fiscal year's growth and its change
        ann = rev["annual"]
        ends = list(ann)
        yoy = [(e, _pct(ann[e]["val"], (year_before(ann, e) or {}).get("val"))) for e in ends]
        s["revenue_yoy_q"] = None
        if len(yoy) >= 2 and yoy[-1][1] is not None and yoy[-2][1] is not None:
            s["acceleration_pp"] = round(yoy[-1][1] - yoy[-2][1], 2)
    if not rttm:
        return s
    end = list(rttm)[-1]
    s["ttm_end"], s["revenue_ttm"] = end, rttm[end]["val"]
    s["revenue_yoy_ttm"] = _pct(rttm[end]["val"], (year_before(rttm, end) or {}).get("val"))
    gp = value_at(f, "gross_profit", end)
    if gp is None and value_at(f, "cost_of_revenue", end) is not None:
        gp = s["revenue_ttm"] - value_at(f, "cost_of_revenue", end)
    ocf, capex = value_at(f, "operating_cash_flow", end), value_at(f, "capex", end)
    s.update(gross_profit_ttm=gp, operating_income_ttm=value_at(f, "operating_income", end),
             operating_cash_flow_ttm=ocf, capex_ttm=capex, sbc_ttm=value_at(f, "sbc", end))
    s["fcf_ttm"] = None if ocf is None or capex is None else ocf - capex
    s["gross_margin_pct"] = _ratio(gp, s["revenue_ttm"])
    s["operating_margin_pct"] = _ratio(s["operating_income_ttm"], s["revenue_ttm"])
    s["fcf_margin_pct"] = _ratio(s["fcf_ttm"], s["revenue_ttm"])
    s["capex_intensity_pct"] = _ratio(capex, s["revenue_ttm"])
    s["sbc_pct_revenue"] = _ratio(s["sbc_ttm"], s["revenue_ttm"])
    bs_dates = balance_sheet_dates(f)
    if bs_dates:
        s["balance_sheet"] = balance_sheet_at(f, list(bs_dates)[-1])
    sh = f.get("shares") or {}
    dq, da = sh.get("diluted_q") or {}, sh.get("diluted_a") or {}
    if dq or da:  # the newest count, quarterly or (10-K, 20-F) annual, compared with the same kind
        e0 = max([*dq, *da])
        pts = dq if e0 in dq else da
        s["diluted_shares"], s["diluted_shares_end"] = pts[e0]["val"], e0
        s["dilution_yoy_pct"] = _pct(pts[e0]["val"], (year_before(pts, e0) or {}).get("val"))
    s["shares_outstanding"] = sh.get("outstanding")
    s["quarterly"] = quarterly_table(f)
    return s


def quarterly_table(f: dict, n: int = 12) -> list[dict]:
    """Last n quarters: revenue, its YoY, operating margin, FCF and diluted shares (for display)."""
    flows = f.get("flows") or {}
    rq = (flows.get("revenue") or {}).get("quarters") or {}
    out = []
    for end in list(rq)[-n:]:
        def q(metric):
            p = ((flows.get(metric) or {}).get("quarters") or {}).get(end)
            return None if p is None else p["val"]
        r = rq[end]["val"]
        ocf, capex, opi = q("operating_cash_flow"), q("capex"), q("operating_income")
        sh = (((f.get("shares") or {}).get("diluted_q") or {}).get(end) or {}).get("val")
        out.append({"end": end, "revenue": r, "revenue_yoy_pct": _pct(r, (year_before(rq, end) or {}).get("val")),
                    "operating_margin_pct": _ratio(opi, r), "fcf": None if ocf is None or capex is None else ocf - capex,
                    "diluted_shares": sh})
    return out
