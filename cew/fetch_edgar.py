"""SEC EDGAR XBRL: hyperscaler capex, cash flow, new lease assets, debt issuance.

Cash-flow items are reported year-to-date in 10-Qs, so quarterly values are
derived by differencing YTD figures that share the same fiscal-year start.
"""
from __future__ import annotations

from datetime import date

from . import util

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# For each metric, candidate us-gaap tags in order of preference.
METRIC_TAGS = {
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
    ],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "new_operating_lease_assets": ["RightOfUseAssetObtainedInExchangeForOperatingLeaseLiability"],
    "new_finance_lease_assets": ["RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability"],
    "debt_issued": [
        "ProceedsFromIssuanceOfLongTermDebt",
        "ProceedsFromIssuanceOfSeniorLongTermDebt",
        "ProceedsFromIssuanceOfDebt",
        "ProceedsFromIssuanceOfUnsecuredDebt",
        "ProceedsFromDebtNetOfIssuanceCosts",
    ],
}


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def quarterly_from_entries(entries: list[dict]) -> dict[str, float]:
    """Map quarter-end date -> 3-month value from a list of XBRL duration facts."""
    # Keep only 10-Q / 10-K style duration facts; dedupe (start, end) keeping latest filing.
    dedup: dict[tuple[str, str], dict] = {}
    for e in entries:
        if "start" not in e or "end" not in e or e.get("val") is None:
            continue
        if not str(e.get("form", "")).startswith(("10-Q", "10-K")):
            continue
        key = (e["start"], e["end"])
        if key not in dedup or e.get("filed", "") > dedup[key].get("filed", ""):
            dedup[key] = e
    facts = list(dedup.values())

    quarters: dict[str, float] = {}
    for e in facts:
        if 80 <= _days(e["start"], e["end"]) <= 100:
            quarters[e["end"]] = float(e["val"])

    by_start: dict[str, list[dict]] = {}
    for e in facts:
        if _days(e["start"], e["end"]) <= 370:
            by_start.setdefault(e["start"], []).append(e)
    for group in by_start.values():
        group.sort(key=lambda x: x["end"])
        for prev, cur in zip(group, group[1:]):
            gap = _days(prev["end"], cur["end"])
            if 80 <= gap <= 100 and cur["end"] not in quarters:
                quarters[cur["end"]] = float(cur["val"]) - float(prev["val"])
    return dict(sorted(quarters.items()))


def pick_metric(facts: dict, tags: list[str]) -> tuple[str | None, dict[str, float]]:
    """Choose the candidate tag whose quarterly series is most recent."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    best_tag, best_q = None, {}
    for tag in tags:
        units = gaap.get(tag, {}).get("units", {})
        q = quarterly_from_entries(units.get("USD", []))
        if not q:
            continue
        if not best_q or max(q) > max(best_q):
            best_tag, best_q = tag, q
    return best_tag, best_q


def company_metrics(facts: dict) -> dict:
    out = {"entity": facts.get("entityName"), "metrics": {}, "tags": {}}
    for metric, tags in METRIC_TAGS.items():
        tag, q = pick_metric(facts, tags)
        out["metrics"][metric] = q
        out["tags"][metric] = tag
    return out


def last_n(q: dict[str, float], n: int) -> list[tuple[str, float]]:
    return list(q.items())[-n:]


def _r(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(v, n)


def summarize_company(m: dict) -> dict:
    """Latest quarter, QoQ and YoY for each metric, plus free cash flow."""
    s: dict = {}
    for metric, q in m["metrics"].items():
        items = list(q.items())
        if not items:
            s[metric] = None
            continue
        end, val = items[-1]
        prev = items[-2][1] if len(items) >= 2 else None
        yoy = None
        for e2, v2 in items:
            if 350 <= _days(e2, end) <= 380:
                yoy = v2
        s[metric] = {
            "quarter_end": end,
            "value": val,
            "qoq_pct": _r(util.pct_change(val, prev)),
            "yoy_pct": _r(util.pct_change(val, yoy)),
            "history": last_n(q, 12),
        }
    cap, ocf = s.get("capex"), s.get("operating_cash_flow")
    if cap and ocf and cap["quarter_end"] == ocf["quarter_end"]:
        s["free_cash_flow"] = {"quarter_end": cap["quarter_end"], "value": ocf["value"] - cap["value"]}
    else:
        s["free_cash_flow"] = None
    return s


def run(cfg: dict, user_agent: str, offline: bool = False) -> dict:
    out_dir = util.DATA / "edgar"
    sess = util.session(user_agent)
    companies, errors = {}, {}
    for ticker, meta in cfg["hyperscalers"].items():
        path = out_dir / f"{ticker}.json"
        try:
            if offline:
                m = util.read_json(path)
                if m is None:
                    raise RuntimeError("offline and no cached data")
            else:
                facts = util.get_json(sess, FACTS_URL.format(cik=meta["cik"]), timeout=120)
                m = company_metrics(facts)
                util.write_json(path, m)
            companies[ticker] = {"name": meta["name"], "tags": m["tags"], **summarize_company(m)}
        except Exception as e:
            errors[ticker] = str(e)
    return {"companies": companies, "errors": errors}
