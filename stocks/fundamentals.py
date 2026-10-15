"""SEC XBRL companyfacts -> quarterly / TTM fundamentals, balance sheet and share counts.

Flows (revenue, cash flow, ...) are reported year-to-date in 10-Qs, so 3-month values are derived by
differencing YTD facts that share a fiscal-year start, as cew/fetch_edgar does; Q4 is the 10-K year
minus the 9-month YTD. Each point keeps the date it was first filed, so the valuation history only
uses what was public on each date. Foreign filers (20-F / 40-F, IFRS) usually report annual figures
only: TTM is then the latest fiscal year, and valuation is left out (see `summarize`).

Balance sheet: cash, short-term investments and debt come from the tag lists below. A figure is UNKNOWN
(None), never zero, when a tag of its group stopped reporting: it had a non-zero value in the last
`stopped_tag_quarters` quarters (debt: `no_debt_quarters`) but nothing in the group covers the date, or
only tags we don't read (the latest balance sheet's tag scan) carry a value. Debt is zero ("no debt") only
when no debt tag had a non-zero value in the last `no_debt_quarters` quarters.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from statistics import median

from cew import fetch_edgar, util

from . import config

FACTS_URL = fetch_edgar.FACTS_URL
PERIODIC_FORMS = ("10-Q", "10-K", "20-F", "40-F")
FOREIGN_FORMS = ("20-F", "40-F")
KEEP_YEARS = 8
# Defaults for config/stocks.yaml balance_sheet.* and valuation.share_outlier_x
STOPPED_TAG_QUARTERS, NO_DEBT_QUARTERS, SHARE_OUTLIER_X = 4, 8, 10.0
PREPAYMENT_SHARE = 0.25  # prepayments.max_share_of_ocf

FLOW_TAGS = {
    "revenue": {
        "us-gaap": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet"],
        "ifrs-full": ["Revenue", "RevenueFromContractsWithCustomers"],
    },
    "gross_profit": {"us-gaap": ["GrossProfit"], "ifrs-full": ["GrossProfit"]},
    "cost_of_revenue": {   # gross profit = revenue - cost of revenue when GrossProfit isn't tagged
        "us-gaap": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfServices", "CostOfGoodsSold",
                    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                    "CostOfServicesExcludingDepreciationDepletionAndAmortization", "DirectOperatingCosts"],
        "ifrs-full": ["CostOfSales"],
    },
    "operating_income": {"us-gaap": ["OperatingIncomeLoss"], "ifrs-full": ["ProfitLossFromOperatingActivities"]},
    # operating income = revenue - total costs and expenses, else gross profit - operating expenses
    "costs_and_expenses": {"us-gaap": ["CostsAndExpenses"], "ifrs-full": []},
    "operating_expenses": {"us-gaap": ["OperatingExpenses"], "ifrs-full": []},
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
# Short-term investments: the first tag with a value on the date (they are alternative names for the same
# line: face-statement tags first, then the investment note's current available-for-sale / held-to-maturity
# totals). Without one, cash + short-term investments reported as one line, minus cash.
INVESTMENT_TAGS = {
    "us-gaap": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent", "AvailableForSaleSecuritiesCurrent",
                "HeldToMaturitySecuritiesCurrent", "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent",
                "DebtSecuritiesHeldToMaturityExcludingAccruedInterestAfterAllowanceForCreditLossCurrent",
                "DebtSecuritiesCurrent", "OtherShortTermInvestments"],
    "ifrs-full": ["CurrentFinancialAssetsAtAmortisedCost", "OtherCurrentFinancialAssets"],
}
CASH_AND_INVESTMENT_TAGS = {"us-gaap": ["CashCashEquivalentsAndShortTermInvestments"], "ifrs-full": []}
# Debt (leases excluded). Each family is one kind of borrowing, read as its total tag on the date, else its
# non-current tag plus its current tag (first tag with a value in each list). "long" (LongTermDebt and its
# parts) is the usual total and, when present, is taken to include the other families; otherwise the
# families present are added up. DebtCurrent (all current debt) replaces the families' current parts and
# short-term borrowings. "with_leases" only counts when nothing else is tagged; DebtInstrumentCarryingAmount
# is the last resort.
DEBT_FAMILIES = {
    "us-gaap": {
        "long": {"total": ["LongTermDebt"], "noncurrent": ["LongTermDebtNoncurrent"], "current": ["LongTermDebtCurrent"]},
        "notes": {"total": ["NotesPayable"], "noncurrent": ["LongTermNotesPayable", "LongTermNotesAndLoans", "SeniorLongTermNotes"],
                  "current": ["NotesPayableCurrent"]},
        "convertible": {"total": ["ConvertibleNotesPayable", "ConvertibleDebt"],
                        "noncurrent": ["ConvertibleLongTermNotesPayable", "ConvertibleDebtNoncurrent",
                                       "ConvertibleSubordinatedDebtNoncurrent"],
                        "current": ["ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"]},
        "other": {"total": ["OtherLongTermDebt"], "noncurrent": ["OtherLongTermDebtNoncurrent"],
                  "current": ["OtherLongTermDebtCurrent"]},
        "loans": {"total": ["LoansPayable"],
                  "noncurrent": ["LongTermLoansPayable", "LongTermLineOfCredit", "SecuredLongTermDebt", "UnsecuredLongTermDebt"],
                  "current": ["LoansPayableCurrent", "LinesOfCreditCurrent", "SecuredDebtCurrent", "UnsecuredDebtCurrent"]},
        "with_leases": {"total": ["LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"],
                        "noncurrent": ["LongTermDebtAndCapitalLeaseObligations"],
                        "current": ["LongTermDebtAndCapitalLeaseObligationsCurrent"]},
    },
    "ifrs-full": {
        "long": {"total": ["Borrowings"], "noncurrent": ["NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings"],
                 "current": ["CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings", "ShorttermBorrowings"]},
    },
}
DEBT_COMBINED = {"us-gaap": ["DebtLongtermAndShorttermCombinedAmount"], "ifrs-full": []}
DEBT_ALL_CURRENT = {"us-gaap": ["DebtCurrent"], "ifrs-full": []}
DEBT_SHORT = {"us-gaap": ["ShortTermBorrowings", "CommercialPaper"], "ifrs-full": []}
DEBT_LAST_RESORT = {"us-gaap": ["DebtInstrumentCarryingAmount"], "ifrs-full": []}
DEBT_TAGS = {tax: list(dict.fromkeys([*DEBT_COMBINED[tax], *DEBT_ALL_CURRENT[tax], *DEBT_SHORT[tax], *DEBT_LAST_RESORT[tax],
                                      *(t for fam in DEBT_FAMILIES[tax].values() for ts in fam.values() for t in ts)]))
             for tax in ("us-gaap", "ifrs-full")}
# Customer prepayments: the cash-flow statement's change in contract liabilities / customer advances (added
# up across these lines), else the year-over-year change of the balance-sheet families below.
PREPAYMENT_FLOW_TAGS = {
    "us-gaap": ["IncreaseDecreaseInContractWithCustomerLiability", "IncreaseDecreaseInDeferredRevenue",
                "IncreaseDecreaseInCustomerAdvances", "IncreaseDecreaseInCustomerAdvancesAndDeposits",
                "IncreaseDecreaseInDeferredRevenueAndCustomerAdvancesAndDeposits"],
    "ifrs-full": [],
}
PREPAYMENT_FAMILIES = {  # "deferred" only counts when "contract" isn't tagged (two names for the same line)
    "us-gaap": {
        "contract": {"total": ["ContractWithCustomerLiability"], "noncurrent": ["ContractWithCustomerLiabilityNoncurrent"],
                     "current": ["ContractWithCustomerLiabilityCurrent"]},
        "deferred": {"total": ["DeferredRevenue"], "noncurrent": ["DeferredRevenueNoncurrent"], "current": ["DeferredRevenueCurrent"]},
        "advances": {"total": [], "noncurrent": ["CustomerAdvancesNoncurrent"], "current": ["CustomerAdvancesCurrent"]},
        "deposits": {"total": [], "noncurrent": ["CustomerDepositsNoncurrent"], "current": ["CustomerDepositsCurrent"]},
    },
    "ifrs-full": {},
}
DILUTED_TAGS = {"us-gaap": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
                "ifrs-full": ["AdjustedWeightedAverageShares", "WeightedAverageShares"]}
# Share-count fallback for a period with no diluted count (Alphabet tags one only from 2023-06-30, first filed
# in July 2024): net income / diluted EPS, both undimensioned, same period, from the first filing that gave
# both. Skipped when |EPS| is under MIN_EPS (rounding to the cent alone is then over 5% off).
EPS_SHARE_TAGS = {"us-gaap": ("NetIncomeLoss", "EarningsPerShareDiluted"), "ifrs-full": None}
DERIVED_SHARES = "net income / diluted EPS"
MIN_EPS = 0.10
INSTANT_TAGS = {tax: sorted({t for group in (CASH_TAGS, INVESTMENT_TAGS, CASH_AND_INVESTMENT_TAGS, DEBT_TAGS) for t in group[tax]}
                            | {t for fam in PREPAYMENT_FAMILIES[tax].values() for ts in fam.values() for t in ts})
                for tax in ("us-gaap", "ifrs-full")}
# The tag scan of the latest balance sheet (diagnostics, the page's hover, and the "only tags we don't read"
# guard): every instant tag on that date whose name matches SCAN_RE.
SCAN_RE = re.compile(r"Cash|Investments|Securities|Debt|Notes|Borrowing|Loan|LineOfCredit|CommercialPaper"
                     r"|ContractWithCustomerLiability|Advance|CustomerDeposit|DeferredRevenue|Liabilit")
DEBT_CANDIDATE_RE = re.compile(r"Debt|Notes|Loans|Borrowing|LinesOfCredit|LineOfCredit$|CommercialPaper")
DEBT_CANDIDATE_NOT = re.compile(r"Securit|Investment|Receivable|Lease|Interest|Issuance|Discount|Premium|FairValue"
                                r"|Proceeds|Repayment|Payment|Maturit|Face|Unamortized|Extinguishment|Fee|Covenant"
                                r"|Capacity|Percentage|Rate|Ratio|Increase|Decrease|Gross|Collateral|Guarantee|Conversion")
INVESTMENT_CANDIDATE_RE = re.compile(r"(Securit|Investments).*Current$|ShortTermInvestments$")
INVESTMENT_CANDIDATE_NOT = re.compile(r"Noncurrent|Restricted|Pledged|Unrealized|Gain|Loss(?!Current)|Receivable")
UNREAD_LARGER_X = 1.1  # an unread tag above this multiple of what the read tags add up to: the figure is unknown


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


def _first_filings(entries: list[dict]) -> dict:
    """(start, end) -> {accn: (filed, val)} for periodic duration facts: each filing's own value."""
    out: dict = {}
    for e in entries:
        if _periodic(e) and "start" in e and e.get("accn"):
            out.setdefault((e["start"], e["end"]), {}).setdefault(e["accn"], (e.get("filed", ""), float(e["val"])))
    return out


def eps_shares(facts: dict, tax: str, currency: str) -> tuple[dict, dict]:
    """(quarters, annual): end -> {"val", "filed", "derived"}, diluted share counts derived as net income /
    diluted EPS (EPS_SHARE_TAGS) for each 80-100 / 350-380 day period that has both, from the first filing
    that reported both (as first filed, so a count before a split is in that day's units, like the price).
    A ratio that isn't positive, or |EPS| under MIN_EPS, gives nothing."""
    tags = EPS_SHARE_TAGS.get(tax)
    if not tags:
        return {}, {}
    ni = _first_filings(_units(facts, tax, tags[0]).get(currency, []))
    eps = _first_filings(_units(facts, tax, tags[1]).get(f"{currency}/shares", []))
    q, a = {}, {}
    for key in ni.keys() & eps.keys():
        both = ni[key].keys() & eps[key].keys()
        if not both:
            continue
        accn = min(both, key=lambda x: (ni[key][x][0], x))
        (filed, n), e = ni[key][accn], eps[key][accn][1]
        if abs(e) < MIN_EPS or n / e <= 0:
            continue
        d = _days(*key)
        point = {"val": float(round(n / e)), "filed": filed, "derived": DERIVED_SHARES}
        if 80 <= d <= 100:
            q[key[1]] = point
        elif 350 <= d <= 380:
            a[key[1]] = point
    return dict(sorted(q.items())), dict(sorted(a.items()))


def fill_shares(shares: dict, derived_q: dict, derived_a: dict) -> dict:
    """The diluted counts with the derived ones (eps_shares) added only where a period has no diluted count.
    In place; returns `shares`."""
    for kind, pts in (("diluted_q", derived_q), ("diluted_a", derived_a)):
        have = shares.get(kind) or {}
        shares[kind] = dict(sorted({**pts, **have}.items()))
    return shares


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
    out["prepayment_flows"] = {}
    for tag in PREPAYMENT_FLOW_TAGS[tax]:
        q, a = flow_points(_units(facts, tax, tag).get(currency, []))
        if q or a:
            out["prepayment_flows"][tag] = {"quarters": _keep(q, cutoff), "annual": _keep(a, cutoff)}
    for tag in DILUTED_TAGS[tax]:
        q, a = share_points(_units(facts, tax, tag).get("shares", []))
        if q or a:
            out["shares"] = {"tag": tag, "diluted_q": _keep(q, cutoff), "diluted_a": _keep(a, cutoff)}
            break
    dq, da = eps_shares(facts, tax, currency)
    if dq or da:
        fill_shares(out["shares"], _keep(dq, cutoff), _keep(da, cutoff))
    out["shares"]["outstanding"] = cover_shares(facts)
    out["scan"] = tag_scan(facts, tax, currency, latest)
    return out


FLOW_SCAN_RE = re.compile(r"Cost|Expense|OperatingIncome|GrossProfit|IncreaseDecreaseIn")


def tag_scan(facts: dict, tax: str, currency: str, latest_revenue_end: str | None) -> dict:
    """Which tags the company uses: every instant tag on the latest balance-sheet date matching SCAN_RE, and
    every duration tag ending on the latest revenue period matching FLOW_SCAN_RE (shortest period's value).
    Diagnostics for the page's hover and the run log; the balance-sheet guard reads `instants`."""
    cash_ends = [e["end"] for t in CASH_TAGS[tax] for e in _units(facts, tax, t).get(currency, [])
                 if _periodic(e) and "start" not in e]
    bs_date = max(cash_ends, default=None)
    inst, dur = {}, {}
    for tag, body in sorted(_tax(facts, tax).items()):
        rows = (body.get("units") or {}).get(currency) or []
        if bs_date and SCAN_RE.search(tag):
            p = dedup(rows, duration=False).get(bs_date)
            if p:
                inst[tag] = p["val"]
        if latest_revenue_end and FLOW_SCAN_RE.search(tag):
            facts_d = [(k, f) for k, f in dedup(rows, duration=True).items() if k[1] == latest_revenue_end]
            if facts_d:
                (start, _), f = min(facts_d, key=lambda x: _days(x[0][0], x[0][1]))
                dur[tag] = {"val": f["val"], "start": start}
    return {"balance_sheet_date": bs_date, "instants": inst, "flow_end": latest_revenue_end, "flows": dur}


def fetch(sess, cik: str) -> dict:
    return util.get_json(sess, FACTS_URL.format(cik=str(cik).zfill(10)), timeout=120)


def cache_path(ticker: str):
    return config.data_dir() / "fundamentals" / f"{ticker}.json"


def quarter_ends(f: dict) -> list[str]:
    """Every fiscal quarter end in the companyfacts record's quarterly flows, oldest first (exact dates, so
    52/53-week quarters ending Jul 26 stay Jul 26)."""
    return sorted({end for m in (f.get("flows") or {}).values() for end in (m.get("quarters") or {})})


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


def flow_series(f: dict, metric: str) -> dict:
    """A flow's quarters and annual figures; gross profit and operating income fall back, period by period,
    to revenue - cost of revenue and to revenue - costs and expenses (else gross profit - operating
    expenses) when the company doesn't tag them. Each derived point says how in "source"."""
    flows = f.get("flows") or {}
    base = flows.get(metric) or {}
    if metric not in ("gross_profit", "operating_income"):
        return base
    rev = flows.get("revenue") or {}
    tagged = metric if base.get("tag") else None
    out = {"tag": base.get("tag"), "quarters": {}, "annual": {}}
    for kind in ("quarters", "annual"):
        pts = {e: dict(p, source=base.get("tag")) for e, p in (base.get(kind) or {}).items()} if tagged else {}
        if metric == "gross_profit":
            fallbacks = [("cost_of_revenue", rev)]
        else:
            fallbacks = [("costs_and_expenses", rev), ("operating_expenses", flow_series(f, "gross_profit"))]
        for other, minuend in fallbacks:
            sub = flows.get(other) or {}
            for end, m in (minuend.get(kind) or {}).items():
                c = (sub.get(kind) or {}).get(end)
                if end in pts or c is None:
                    continue
                left = "revenue" if minuend is rev else "gross profit"
                pts[end] = {"val": m["val"] - c["val"], "filed": max(m["filed"], c["filed"]), "derived": True,
                            "source": f"{left} - {sub.get('tag')}"}
        out[kind] = dict(sorted(pts.items()))
    return out


def value_at(f: dict, metric: str, end: str) -> float | None:
    p = ttm_points(flow_series(f, metric)).get(end)
    return None if p is None else p["val"]


def ttm_sources(f: dict, metric: str, end: str) -> str | None:
    """How a TTM figure was built: the tag, or e.g. "revenue - CostOfServices" (several joined with "; ")."""
    s = flow_series(f, metric)
    q = s.get("quarters") or {}
    ends = [e for e in q if e <= end][-4:]
    if end in (s.get("annual") or {}) and (len(ends) < 4 or ends[-1] != end):
        return (s["annual"][end].get("source") or s.get("tag"))
    return "; ".join(dict.fromkeys(q[e].get("source") or s.get("tag") or "?" for e in ends)) or None


def _at(f: dict, tag: str, end: str) -> float | None:
    p = ((f.get("instants") or {}).get(tag) or {}).get(end)
    return None if p is None else p["val"]


def _first(f: dict, tags: list[str], end: str) -> tuple[str, float] | None:
    for t in tags:
        v = _at(f, t, end)
        if v is not None:
            return t, v
    return None


def _family(f: dict, fam: dict, end: str) -> dict | None:
    got = {k: _first(f, fam.get(k) or [], end) for k in ("total", "noncurrent", "current")}
    return got if any(got.values()) else None


def debt_at(f: dict, end: str) -> tuple[float | None, list[tuple[str, float]]]:
    """(total debt, [(tag, value), ...]) on one date from DEBT_FAMILIES; (None, []) when no debt tag has a
    value on the date."""
    tax = f.get("taxonomy") or "us-gaap"
    comb = _first(f, DEBT_COMBINED[tax], end)
    if comb:
        return comb[1], [comb]
    fams = DEBT_FAMILIES[tax]
    long_ = _family(f, fams["long"], end)
    if long_ and (long_["total"] or long_["noncurrent"]):
        kinds = [long_]  # the usual total; taken to include the other families
    else:  # no LongTermDebt total (at most its current part): add the families up
        kinds = [k for name, fam in fams.items() if name != "with_leases" if (k := _family(f, fam, end))]
    if not kinds and "with_leases" in fams and (wl := _family(f, fams["with_leases"], end)):
        kinds = [wl]
    all_cur, short = _first(f, DEBT_ALL_CURRENT[tax], end), _first(f, DEBT_SHORT[tax], end)
    if not kinds and not all_cur and not short:
        last = _first(f, DEBT_LAST_RESORT[tax], end)
        return (last[1], [last]) if last else (None, [])
    parts: list[tuple[str, float]] = []
    if all_cur and (not kinds or any(k["noncurrent"] for k in kinds)):
        parts.append(all_cur)  # every current borrowing; the families add only their non-current parts
        for k in kinds:
            if k["noncurrent"]:
                parts.append(k["noncurrent"])
            elif k["total"]:
                parts.append((k["total"][0], k["total"][1] - (k["current"][1] if k["current"] else 0.0)))
    else:
        for k in kinds:
            parts += [k["total"]] if k["total"] else [p for p in (k["noncurrent"], k["current"]) if p]
        if short:
            parts.append(short)
    return sum(v for _, v in parts), parts


def _quarters_before(end: str, quarters: int) -> str:
    return (date.fromisoformat(end) - timedelta(days=round(quarters * 91.31) + 10)).isoformat()


def stopped_tags(f: dict, tags: list[str], end: str, quarters: int) -> list[dict]:
    """Tags whose latest value in the `quarters` quarters before `end` is non-zero but which have no value on
    `end`. A last value of zero reads as repaid / sold, and so does a zero reported by another tag of the
    group after the stopped tag's last value (e.g. convertible notes at 0 the quarter after they matured)."""
    lo, out = _quarters_before(end, quarters), []
    inst = f.get("instants") or {}
    zeros = sorted(d for t in tags for d, p in (inst.get(t) or {}).items() if lo <= d <= end and p["val"] == 0)
    for t in tags:
        pts = inst.get(t) or {}
        if end in pts:
            continue
        ds = [d for d in pts if lo <= d < end]
        if ds and pts[max(ds)]["val"] and not any(z > max(ds) for z in zeros):
            out.append({"tag": t, "date": max(ds), "val": pts[max(ds)]["val"]})
    return out


def untracked(f: dict, end: str, include: re.Pattern, exclude: re.Pattern) -> dict:
    """Tags on the latest balance sheet's scan that look like the group but that we didn't read on that date
    (not in our lists, or in them but missing from a record cached before they were added)."""
    scan = f.get("scan")
    if not scan or scan.get("balance_sheet_date") != end:
        return {}
    return {t: v for t, v in (scan.get("instants") or {}).items()
            if v and _at(f, t, end) is None and include.search(t) and not exclude.search(t)}


def _unread_larger(f: dict, end: str, read: float, include: re.Pattern, exclude: re.Pattern) -> tuple[str, float] | None:
    """An unread tag on the latest balance sheet larger than the whole figure we read (a line is missing from
    our lists, e.g. Oracle's LongTermNotesAndLoans next to its NotesPayableCurrent)."""
    other = untracked(f, end, include, exclude)
    big = max(other.items(), key=lambda x: x[1], default=None)
    return big if big and big[1] > abs(read) * UNREAD_LARGER_X else None


def _money(v: float) -> str:
    a = abs(v)
    s = f"{a / 1e9:.2f}B" if a >= 1e9 else f"{a / 1e6:.1f}M" if a >= 1e6 else f"{a:,.0f}"
    return ("-" if v < 0 else "") + "$" + s


def _stopped_note(what: str, stopped: list[dict], end: str) -> str:
    x = max(stopped, key=lambda s: s["date"])
    more = f" (and {len(stopped) - 1} more)" if len(stopped) > 1 else ""
    return f"{what} unknown: {x['tag']} was {_money(x['val'])} on {x['date']}{more}, and no {what} tag covers {end}"


def balance_sheet_at(f: dict, end: str, cfg: dict | None = None) -> dict:
    """Cash, short-term investments and debt on one balance-sheet date. A figure is None (unknown) when a tag
    of its group stopped reporting, or only tags we don't read carry it (see the module docstring); then
    cash + investments, net debt (and so EV) are None too, and "unknown" says why."""
    tax = f.get("taxonomy") or "us-gaap"
    bc = (cfg or {}).get("balance_sheet") or {}
    q_stop, q_debt = int(bc.get("stopped_tag_quarters", STOPPED_TAG_QUARTERS)), int(bc.get("no_debt_quarters", NO_DEBT_QUARTERS))
    unknown = []
    cash_t = _first(f, CASH_TAGS[tax], end)
    cash = cash_t and cash_t[1]
    inv_t = _first(f, INVESTMENT_TAGS[tax], end)
    inv_parts, inv_note = [inv_t] if inv_t else [], None
    inv = inv_t[1] if inv_t else None
    if inv_t is None:
        comb = _first(f, CASH_AND_INVESTMENT_TAGS[tax], end)
        stopped = stopped_tags(f, INVESTMENT_TAGS[tax], end, q_stop)
        other = untracked(f, end, INVESTMENT_CANDIDATE_RE, INVESTMENT_CANDIDATE_NOT)
        if comb and cash is not None and comb[1] >= cash:
            inv, inv_parts = comb[1] - cash, [comb]
            inv_note = f"{comb[0]} minus cash"
        elif stopped:
            unknown.append(_stopped_note("short-term investments", stopped, end))
        elif other:
            unknown.append("short-term investments unknown: only tags this screen doesn't read carry a value on "
                           f"{end} ({', '.join(f'{t} {_money(v)}' for t, v in list(other.items())[:3])})")
        else:
            inv = 0.0
    elif bigger := _unread_larger(f, end, inv, INVESTMENT_CANDIDATE_RE, INVESTMENT_CANDIDATE_NOT):
        inv = None
        unknown.append(f"short-term investments unknown: {bigger[0]} is {_money(bigger[1])} on {end}, more than the "
                       f"{_money(inv_t[1])} read from {inv_t[0]}")
    debt, parts = debt_at(f, end)
    note = None
    if debt is None:
        stopped = stopped_tags(f, DEBT_TAGS[tax], end, q_debt)
        other = untracked(f, end, DEBT_CANDIDATE_RE, DEBT_CANDIDATE_NOT)
        if stopped:
            unknown.append(_stopped_note("debt", stopped, end))
        elif other:
            unknown.append(f"debt unknown: only tags this screen doesn't read carry a value on {end} "
                           f"({', '.join(f'{t} {_money(v)}' for t, v in list(other.items())[:3])})")
        else:
            debt = 0.0
            note = f"no debt: no debt tag with a value in the last {q_debt} quarters"
    elif bigger := _unread_larger(f, end, debt, DEBT_CANDIDATE_RE, DEBT_CANDIDATE_NOT):
        unknown.append(f"debt unknown: {bigger[0]} is {_money(bigger[1])} on {end}, more than the {_money(debt)} "
                       f"the debt tags read add up to ({' + '.join(t for t, _ in parts)})")
        debt = None
    cash_inv = None if cash is None or inv is None else cash + inv
    return {"date": end, "cash": cash, "cash_tag": cash_t and cash_t[0], "short_term_investments": inv,
            "investment_tags": [{"tag": t, "val": v} for t, v in inv_parts], "investment_note": inv_note,
            "cash_and_investments": cash_inv, "debt": debt, "debt_tags": [t for t, _ in parts],
            "debt_parts": [{"tag": t, "val": v} for t, v in parts], "debt_note": note,
            "net_debt": None if cash_inv is None or debt is None else debt - cash_inv, "unknown": unknown}


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


def clean_shares(points: dict, factor: float = SHARE_OUTLIER_X) -> tuple[dict, list[dict]]:
    """Drop share counts more than `factor` times off the median of their neighbours on BOTH sides (two
    before, two after; one side at either end), e.g. a filing that gave 29,452 for 29.5 million. A stock
    split moves every later count by the same factor, so a split's points match one side and are kept."""
    ends = sorted(points)
    vals = [points[e]["val"] for e in ends]
    kept, dropped = {}, []

    def off(v, side):
        side = [x for x in side if x > 0]
        if not side:
            return None
        m = median(side)
        return v <= 0 or max(v / m, m / v) > factor

    for i, e in enumerate(ends):
        before, after = off(vals[i], vals[max(0, i - 2):i]), off(vals[i], vals[i + 1:i + 3])
        if (before or after) and before is not False and after is not False:
            dropped.append({"end": e, "val": vals[i]})
        else:
            kept[e] = points[e]
    return kept, dropped


def shares_cleaned(f: dict, factor: float = SHARE_OUTLIER_X) -> dict:
    """{"diluted_q", "diluted_a", "dropped"}: the diluted counts without outliers (see clean_shares)."""
    sh = f.get("shares") or {}
    q, dq = clean_shares(sh.get("diluted_q") or {}, factor)
    a, da = clean_shares(sh.get("diluted_a") or {}, factor)
    return {"diluted_q": q, "diluted_a": a, "dropped": sorted(dq + da, key=lambda x: x["end"])}


def _family_level(g: dict | None) -> float | None:
    """A balance-sheet family's level: its total, else current + non-current (either alone if that's all)."""
    if not g:
        return None
    if g["total"]:
        return g["total"][1]
    parts = [p[1] for p in (g["noncurrent"], g["current"]) if p]
    return sum(parts) if parts else None


def contract_liabilities_at(f: dict, end: str) -> dict | None:
    """Customer prepayments on the balance sheet on one date: contract liabilities (else deferred revenue,
    the same line under an older name) plus customer advances and deposits. {"val", "families"} or None."""
    fams = PREPAYMENT_FAMILIES.get(f.get("taxonomy") or "us-gaap") or {}
    total, used = 0.0, []
    for name, fam in fams.items():
        if name == "deferred" and "contract" in used:
            continue
        v = _family_level(_family(f, fam, end))
        if v is not None:
            total += v
            used.append(name)
    return {"val": total, "families": used} if used else None


def prepayment_points(f: dict) -> dict:
    """end -> {"val", "filed", "source", "base", "base_date"}: the TTM increase in customer prepayments
    (contract liabilities, deferred revenue, customer advances / deposits) and their level a year earlier
    ("base", None when not tagged). The increase is the cash-flow statement's change lines (added up), else
    the balance-sheet families' change over the year, on the same basis on both dates."""
    tags = f.get("prepayment_flows") or {}
    merged: dict = {"quarters": {}, "annual": {}}
    for kind in ("quarters", "annual"):
        for tag, fl in tags.items():
            for end, p in (fl.get(kind) or {}).items():
                cur = merged[kind].setdefault(end, {"val": 0.0, "filed": "", "tags": []})
                cur["val"] += p["val"]
                cur["filed"] = max(cur["filed"], p["filed"])
                cur["tags"].append(tag)
    out = {e: {"val": p["val"], "filed": p["filed"], "source": "cash flow statement: " + " + ".join(
        sorted({t for k in ("quarters", "annual") for x in merged[k].values() for t in x["tags"]}))}
        for e, p in ttm_points(merged).items()}
    tax = f.get("taxonomy") or "us-gaap"
    fams = PREPAYMENT_FAMILIES.get(tax) or {}
    filed = balance_sheet_dates(f)
    dates = {d: d for d in filed}
    for end in dates:
        prev = year_before(dates, end)  # the balance-sheet date closest to a year earlier
        if end in out or not prev:
            continue
        total, used = 0.0, []
        for name, fam in fams.items():
            if name == "deferred" and "contract" in used:
                continue
            now_v, then_v = _family(f, fam, end), _family(f, fam, prev)
            if not now_v or not then_v:
                continue
            # the same basis on both dates: the total, else current + non-current, else the current part alone
            if now_v["total"] and then_v["total"]:
                change = now_v["total"][1] - then_v["total"][1]
            elif all(g[k] for g in (now_v, then_v) for k in ("noncurrent", "current")):
                change = sum(now_v[k][1] - then_v[k][1] for k in ("noncurrent", "current"))
            elif now_v["current"] and then_v["current"]:
                change = now_v["current"][1] - then_v["current"][1]
            else:
                continue
            total += change
            used.append(name)
        if used:
            out[end] = {"val": total, "filed": filed[end], "source": f"balance sheet: {', '.join(used)} {prev} to {end}"}
    for end, p in out.items():
        prev = year_before(dates, end)
        base = contract_liabilities_at(f, prev) if prev else None
        p["base"], p["base_date"] = (base or {}).get("val"), prev if base else None
    return dict(sorted(out.items()))


def excess_points(f: dict) -> dict:
    """end -> {"val", "filed", ...}: excess prepayments, i.e. the TTM increase minus what growing with revenue
    explains (the level a year earlier x TTM revenue growth). Only ends with a known level a year earlier."""
    rev = ttm_points((f.get("flows") or {}).get("revenue") or {})
    out = {}
    for end, p in prepayment_points(f).items():
        r, r0 = rev.get(end), year_before(rev, end)
        if p["base"] is None or not r or not r0 or not r0["val"]:
            continue
        growth = r["val"] / r0["val"] - 1
        expected = p["base"] * growth
        out[end] = {**p, "increase": p["val"], "val": p["val"] - expected, "expected": expected, "revenue_growth": growth}
    return out


def prepayments(f: dict, end: str, ocf: float | None, max_share: float = PREPAYMENT_SHARE) -> dict | None:
    """Customer prepayments over the TTM to `end`: the increase, the level a year earlier, TTM revenue growth,
    the increase that growth explains, and the excess. FCF ex-prepayments applies when the excess is above
    `max_share` of TTM operating cash flow (any positive excess when that is zero or negative); only the
    excess is subtracted, so deferred revenue growing with sales doesn't count."""
    p = prepayment_points(f).get(end)
    if p is None:
        return None
    x = excess_points(f).get(end)
    out = {"increase": p["val"], "source": p["source"], "base": p["base"], "base_date": p["base_date"],
           "revenue_growth_pct": None if x is None else round(x["revenue_growth"] * 100, 2),
           "expected": None if x is None else x["expected"], "excess": None if x is None else x["val"],
           "max_share_of_ocf": max_share, "applies": False,
           "share_of_ocf": round(x["val"] / ocf, 4) if x is not None and ocf and ocf > 0 else None}
    if x is None:
        out["note"] = "no contract-liability balance a year earlier: the excess over revenue growth can't be measured"
    else:
        out["applies"] = ocf is not None and x["val"] > 0 and (ocf <= 0 or x["val"] > max_share * ocf)
    return out


def quarterly_fcf(f: dict) -> dict:
    """end -> {"fcf", "revenue", "margin", "filed"}: each quarter's free cash flow (operating cash flow minus
    capex) and revenue, where all three are known."""
    flows = f.get("flows") or {}
    q = lambda m: (flows.get(m) or {}).get("quarters") or {}  # noqa: E731
    rq, oq, cq = q("revenue"), q("operating_cash_flow"), q("capex")
    out = {}
    for end, r in rq.items():
        if end in oq and end in cq and r["val"] > 0:
            fcf = oq[end]["val"] - cq[end]["val"]
            out[end] = {"fcf": fcf, "revenue": r["val"], "margin": fcf / r["val"],
                        "filed": max(r["filed"], oq[end]["filed"], cq[end]["filed"])}
    return out


def _spike_cfg(cfg: dict | None) -> dict:
    c = (cfg or {}).get("fcf_spike") or {}
    return {"quarter_fcf_to_revenue": float(c.get("quarter_fcf_to_revenue", 1.0)),
            "margin_jump_x": float(c.get("margin_jump_x", 2.0)), "margin_jump_min_pct": float(c.get("margin_jump_min_pct", 30)),
            "median_quarters": int(c.get("median_quarters", 8)), "min_quarters": int(c.get("min_quarters", 4))}


def normalised_margin(qf: dict, end: str, n: int, min_q: int, day: str | None = None) -> tuple[float | None, int]:
    """The median FCF margin of the last `n` quarters to `end` (filed by `day` if given), and how many."""
    ends = [e for e in qf if e <= end and (day is None or qf[e]["filed"] <= day)][-n:]
    if len(ends) < min_q:
        return None, len(ends)
    return median(qf[e]["margin"] for e in ends), len(ends)


def fcf_spike(f: dict, end: str, s: dict, cfg: dict | None = None) -> dict:
    """The FCF spike guard (no tag needed): a quarter of the last four with FCF above its revenue, or a TTM
    FCF margin more than `margin_jump_x` times the one a year earlier and above `margin_jump_min_pct`.
    Then the normalised FCF is the median quarterly FCF margin of the last `median_quarters` x TTM revenue."""
    c = _spike_cfg(cfg)
    qf = quarterly_fcf(f)
    last4 = [e for e in qf if e <= end][-4:]
    reasons = [f"FCF {_money(qf[e]['fcf'])} in the quarter to {e} on revenue of {_money(qf[e]['revenue'])}"
               for e in last4 if qf[e]["fcf"] > c["quarter_fcf_to_revenue"] * qf[e]["revenue"]]
    rev = ttm_points((f.get("flows") or {}).get("revenue") or {})
    margin, prev_margin = s.get("fcf_margin_pct"), None
    e0 = year_before({e: e for e in rev}, end)  # the TTM end closest to a year earlier
    if e0 and rev[e0]["val"]:
        o0, c0 = value_at(f, "operating_cash_flow", e0), value_at(f, "capex", e0)
        if o0 is not None and c0 is not None:
            prev_margin = round((o0 - c0) / rev[e0]["val"] * 100, 2)
    if (margin is not None and prev_margin is not None and margin > c["margin_jump_min_pct"]
            and margin > c["margin_jump_x"] * prev_margin):
        reasons.append(f"TTM FCF margin {margin:.1f}% vs {prev_margin:.1f}% a year earlier")
    med, n = normalised_margin(qf, end, c["median_quarters"], c["min_quarters"])
    out = {"triggered": bool(reasons), "reasons": reasons, "fcf_margin_pct": margin, "fcf_margin_year_earlier_pct": prev_margin,
           "median_margin_pct": None if med is None else round(med * 100, 2), "quarters": n, **c}
    if reasons and med is not None and s.get("revenue_ttm"):
        out["normalised_fcf"] = med * s["revenue_ttm"]
    elif reasons:
        out["note"] = f"only {n} quarters with FCF and revenue (needs {c['min_quarters']}): FCF not normalised"
    return out


def normalised_points(f: dict, cfg: dict | None = None) -> dict:
    """end -> normalised FCF (median quarterly FCF margin of the last quarters x TTM revenue), for history."""
    c = _spike_cfg(cfg)
    qf = quarterly_fcf(f)
    out = {}
    for end, r in ttm_points((f.get("flows") or {}).get("revenue") or {}).items():
        med, _ = normalised_margin(qf, end, c["median_quarters"], c["min_quarters"])
        if med is not None:
            out[end] = {"val": med * r["val"], "filed": r["filed"]}
    return out


def summarize(f: dict, cfg: dict | None = None) -> dict:
    """Latest fundamentals for scoring and display. Missing inputs give None, never a guess."""
    cfg = cfg or {}
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
    ocf, capex = value_at(f, "operating_cash_flow", end), value_at(f, "capex", end)
    s.update(gross_profit_ttm=gp, operating_income_ttm=value_at(f, "operating_income", end),
             operating_cash_flow_ttm=ocf, capex_ttm=capex, sbc_ttm=value_at(f, "sbc", end))
    s["gross_profit_source"] = ttm_sources(f, "gross_profit", end) if gp is not None else None
    s["operating_income_source"] = ttm_sources(f, "operating_income", end) if s["operating_income_ttm"] is not None else None
    s["fcf_ttm"] = None if ocf is None or capex is None else ocf - capex
    s["gross_margin_pct"] = _ratio(gp, s["revenue_ttm"])
    s["operating_margin_pct"] = _ratio(s["operating_income_ttm"], s["revenue_ttm"])
    s["fcf_margin_pct"] = _ratio(s["fcf_ttm"], s["revenue_ttm"])
    s["capex_intensity_pct"] = _ratio(capex, s["revenue_ttm"])
    s["sbc_pct_revenue"] = _ratio(s["sbc_ttm"], s["revenue_ttm"])
    missing = {}
    if gp is None:
        missing["gross_margin_pct"] = f"no GrossProfit or cost-of-revenue tag covers the TTM to {end}"
    if s["operating_income_ttm"] is None:
        missing["operating_margin_pct"] = (f"no OperatingIncomeLoss, CostsAndExpenses or OperatingExpenses tag covers "
                                           f"the TTM to {end}")
    if s["fcf_ttm"] is None:
        missing["fcf_margin_pct"] = f"no operating cash flow or capex for the TTM to {end}"
    s["missing_inputs"] = missing
    # Which FCF quality and valuation are scored on ("fcf_basis"), the reported one always shown too:
    # - "ex-prepayments": FCF minus the excess prepayments (the rise in contract liabilities beyond what revenue
    #   growth explains) when that excess is above prepayments.max_share_of_ocf of operating cash flow;
    # - "normalised": the FCF spike guard (fcf_spike) fired: the median quarterly FCF margin x TTM revenue;
    # - both apply: the lower of the two.
    pp = prepayments(f, end, ocf, float((cfg.get("prepayments") or {}).get("max_share_of_ocf", PREPAYMENT_SHARE)))
    s["prepayments"] = pp
    s["fcf_spike"] = fcf_spike(f, end, s, cfg) if s["fcf_ttm"] is not None else None
    s["fcf_basis"], s["fcf_scored_ttm"] = "reported", s["fcf_ttm"]
    options = []
    if pp and pp["applies"] and s["fcf_ttm"] is not None:
        s["fcf_ex_prepayments_ttm"] = s["fcf_ttm"] - pp["excess"]
        s["fcf_ex_prepayments_margin_pct"] = _ratio(s["fcf_ex_prepayments_ttm"], s["revenue_ttm"])
        options.append(("ex-prepayments", s["fcf_ex_prepayments_ttm"]))
    if (s["fcf_spike"] or {}).get("normalised_fcf") is not None:
        s["fcf_normalised_ttm"] = s["fcf_spike"]["normalised_fcf"]
        s["fcf_normalised_margin_pct"] = s["fcf_spike"]["median_margin_pct"]
        options.append(("normalised", s["fcf_normalised_ttm"]))
    if options:
        s["fcf_basis"], s["fcf_scored_ttm"] = min(options, key=lambda o: o[1])
    s["fcf_scored_margin_pct"] = _ratio(s["fcf_scored_ttm"], s["revenue_ttm"])
    bs_dates = balance_sheet_dates(f)
    if bs_dates:
        s["balance_sheet"] = balance_sheet_at(f, list(bs_dates)[-1], cfg)
    sh = f.get("shares") or {}
    clean = shares_cleaned(f, float((cfg.get("valuation") or {}).get("share_outlier_x", SHARE_OUTLIER_X)))
    dq, da = clean["diluted_q"], clean["diluted_a"]
    s["share_outliers"] = clean["dropped"]
    if dq or da:  # the newest count, quarterly or (10-K, 20-F) annual
        e0 = max([*dq, *da])
        pts = dq if e0 in dq else da
        s["diluted_shares"], s["diluted_shares_end"] = pts[e0]["val"], e0
        s["diluted_shares_derived"] = pts[e0].get("derived")
    # dilution compares reported counts only (restated for splits; a derived count is as first filed),
    # the newest with the same kind a year earlier
    rq, ra = ({k: v for k, v in x.items() if not v.get("derived")} for x in (dq, da))
    if rq or ra:
        e1 = max([*rq, *ra])
        pts = rq if e1 in rq else ra
        s["dilution_yoy_pct"] = _pct(pts[e1]["val"], (year_before(pts, e1) or {}).get("val"))
    s["shares_outstanding"] = sh.get("outstanding")
    s["quarterly"] = quarterly_table(f, shares=dq)
    return s


def quarterly_table(f: dict, n: int = 12, shares: dict | None = None) -> list[dict]:
    """Last n quarters: revenue, its YoY, operating margin, FCF and diluted shares (for display)."""
    flows = f.get("flows") or {}
    rq = (flows.get("revenue") or {}).get("quarters") or {}
    oq = flow_series(f, "operating_income").get("quarters") or {}
    shares = ((f.get("shares") or {}).get("diluted_q") or {}) if shares is None else shares
    out = []
    for end in list(rq)[-n:]:
        def q(metric):
            p = ((flows.get(metric) or {}).get("quarters") or {}).get(end)
            return None if p is None else p["val"]
        r = rq[end]["val"]
        ocf, capex, opi = q("operating_cash_flow"), q("capex"), (oq.get(end) or {}).get("val")
        sh = shares.get(end) or {}
        out.append({"end": end, "revenue": r, "revenue_yoy_pct": _pct(r, (year_before(rq, end) or {}).get("val")),
                    "operating_margin_pct": _ratio(opi, r), "fcf": None if ocf is None or capex is None else ocf - capex,
                    "diluted_shares": sh.get("val"), **({"diluted_shares_derived": sh["derived"]} if sh.get("derived") else {})})
    return out


# ---------------------------------------------------------------- python -m stocks.fundamentals tags MSFT ...
def tag_report(ticker: str, rec: dict, cfg: dict | None = None) -> str:
    """Which tags a cached record uses on its latest balance sheet, and the margin / prepayment sources."""
    s = summarize(rec, cfg)
    bs = s.get("balance_sheet") or {}
    scan = rec.get("scan") or {}
    lines = [f"{ticker}: balance sheet {bs.get('date')} (filed data as of {rec.get('fetched_at')})",
             f"  cash: {bs.get('cash_tag')} {_money(bs['cash']) if bs.get('cash') is not None else 'n/a'}",
             "  short-term investments: " + (", ".join(f"{x['tag']} {_money(x['val'])}" for x in bs.get("investment_tags") or [])
                                            or ("none (zero)" if bs.get("short_term_investments") == 0 else "unknown")),
             "  debt: " + (" + ".join(f"{x['tag']} {_money(x['val'])}" for x in bs.get("debt_parts") or [])
                          or (bs.get("debt_note") or "unknown"))]
    lines += [f"  UNKNOWN: {u}" for u in bs.get("unknown") or []]
    if scan.get("balance_sheet_date") == bs.get("date"):
        other = {**untracked(rec, bs["date"], DEBT_CANDIDATE_RE, DEBT_CANDIDATE_NOT),
                 **untracked(rec, bs["date"], INVESTMENT_CANDIDATE_RE, INVESTMENT_CANDIDATE_NOT)}
        lines.append("  other debt / investment tags on this date (not read): "
                     + (", ".join(f"{t} {_money(v)}" for t, v in other.items()) or "none"))
    else:
        lines.append("  (no tag scan in this cache; re-fetch to add one)")
    lines.append(f"  gross profit: {s.get('gross_profit_source') or 'missing'}; operating income: "
                 f"{s.get('operating_income_source') or 'missing'}")
    pp = s.get("prepayments")
    if pp:
        share = "n/a" if pp["share_of_ocf"] is None else f"{pp['share_of_ocf']:.0%}"
        lines.append(f"  prepayments: {_money(pp['increase'])} over the TTM ({pp['source']}); {share} of operating cash flow"
                     + (" -> FCF ex-prepayments" if pp["applies"] else ""))
    else:
        lines.append("  prepayments: none tagged")
    if s.get("share_outliers"):
        lines.append("  share counts dropped: " + ", ".join(f"{x['end']} {x['val']:,.0f}" for x in s["share_outliers"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Report the XBRL tags behind a cached stock's balance sheet and margins")
    p.add_argument("step", choices=["tags"])
    p.add_argument("tickers", nargs="+")
    args = p.parse_args(argv)
    cfg = config.load()
    for t in args.tickers:
        t = config.normalize_ticker(t) or t
        rec = util.read_json(cache_path(t))
        print(tag_report(t, rec, cfg) if rec else f"{t}: no cached fundamentals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
