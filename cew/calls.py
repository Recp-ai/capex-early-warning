"""Hyperscaler earnings press releases (8-K Item 2.02, Exhibit 99.1) on earnings event runs.

The latest Item 2.02 8-K comes from data.sec.gov/submissions; its Exhibit 99.1 from the filing index.
The assessment model lists capex guidance (amount + exact sentence). Code checks each sentence is in
the release verbatim, parses the amounts, and counts the cautious phrases in filing_text.call_phrases
(counts are regex counts, not model output). Each release is stored as data/calls/<TICKER>/<quarter>.json
(calendar quarter of the period end) and compared with the previous quarter's file; the comparison
becomes evidence for the hyperscaler filings and supplier signals categories.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import analyst, edgar_text, models, scoring, util

SCHEMA = analyst._obj({
    "capex_guidance": {"type": "array", "items": analyst._obj({
        "period": {"type": "string"},
        "amount_text": {"type": "string"},
        "stated_change": {"type": "string", "enum": ["raised", "lowered", "reiterated", "not stated"]},
        "sentence": {"type": "string"},
    })},
    "summary": {"type": "string"},
})

PROMPT = """Company: {name} ({ticker}). Earnings press release (SEC 8-K Exhibit 99.1, filed {filed}) for the
period ended {period}.

From the excerpts below, list every statement of capital-expenditure guidance: expected future capital
expenditures, capital investments or purchases of property and equipment (including finance leases if
the company includes them). For each:
- period: the period it covers, written as FY2027 (fiscal year), CY2026 (calendar year), 2026Q4 (a
  calendar quarter) or "next quarter"
- amount_text: the amount or range exactly as written, e.g. "$70-72 billion"
- stated_change: raised, lowered or reiterated if the release says so, else "not stated"
- sentence: the full sentence, copied exactly
Only forward-looking guidance; skip figures for periods already reported. Many releases give no capex
guidance: then return an empty list.
summary: one sentence on what the release says about capex, capacity or AI infrastructure demand ("" if nothing).

Excerpts:
<<<
{text}
>>>"""

KEYWORDS = re.compile(r"(?i)capital expenditure|capex|capital investment|capital spending|property and equipment"
                      r"|infrastructure|data ?cent|outlook|guidance|expect|anticipate|forecast|capacity|demand")


def calls_dir(ticker: str):
    return util.DATA / "calls" / ticker


def _sentences(text: str) -> list[str]:
    return [s.strip() for line in text.split("\n") for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", line)
            if s.strip()]


def count_phrases(text: str, phrases: dict[str, str]) -> dict:
    out = {}
    sents = _sentences(text)
    for label, pattern in phrases.items():
        rx = re.compile(pattern, re.I)
        out[label] = {"count": sum(1 for _ in rx.finditer(text)),
                      "sentences": [s[:400] for s in sents if rx.search(s)][:3]}
    return out


def excerpt(text: str, phrases: dict[str, str], limit: int = 40000) -> str:
    """Prose lines about capex, outlook or the tracked phrases; numeric table rows dropped."""
    rxs = [re.compile(p, re.I) for p in phrases.values()]
    out, n = [], 0
    for line in text.split("\n"):
        if not (KEYWORDS.search(line) or any(r.search(line) for r in rxs)):
            continue
        if sum(c.isalpha() for c in line) < 0.5 * len(line):
            continue
        if n + len(line) > limit:
            break
        out.append(line)
        n += len(line) + 1
    return "\n".join(out)


def _period_key(p: str, fy_end_month: int | None = None) -> str:
    """'FY 2026' -> 'FY2026'; 'CY2026' -> 'FY2026' when the fiscal year is the calendar year."""
    k = re.sub(r"\s+", "", (p or "").upper())
    m = re.fullmatch(r"(FY|CY)?(\d{4}|\d{2})", k)
    if not m:
        return k
    kind, year = m.group(1) or "FY", m.group(2) if len(m.group(2)) == 4 else "20" + m.group(2)
    return f"{'FY' if kind == 'CY' and fy_end_month == 12 else kind}{year}"


def _guidance(rec: dict, fy_end_month: int | None) -> list[dict]:
    """Verified guidance items with comparable period keys."""
    fy = rec.get("fy_end_month") or fy_end_month
    return [{**g, "period": _period_key(g["period"], fy)} for g in rec.get("capex_guidance", []) if g["verified"]]


def verify_guidance(raw: list[dict], source: str) -> list[dict]:
    out = []
    for g in raw:
        amt, sent = (g.get("amount_text") or "").strip(), (g.get("sentence") or "").strip()
        rng = edgar_text.parse_usd_range(amt)
        problem = None
        if not edgar_text.found_in(sent, source):
            problem = "sentence not found in the release"
        elif not edgar_text.found_in(amt, sent):
            problem = "amount not found in its sentence"
        elif rng is None:
            problem = "amount could not be parsed"
        out.append({"period": _period_key(g.get("period")), "amount_text": amt,
                    "low_usd": None if problem else rng[0], "high_usd": None if problem else rng[1],
                    "stated_change": g.get("stated_change", "not stated"), "sentence": sent,
                    "verified": problem is None, "problem": problem})
    return out


def process(ticker: str, meta: dict, filing: dict, sess, model: str, usage: dict, cfg: dict,
            quarter_months: set[int] | None = None, fy_end_month: int | None = None) -> dict:
    phrases = (cfg.get("filing_text") or {}).get("call_phrases") or {}
    url = edgar_text.exhibit_99_1(sess, meta["cik"], filing["accessionNumber"])
    if not url:
        raise RuntimeError(f"no Exhibit 99.1 in 8-K {filing['accessionNumber']}")
    text = util.html_to_text(util.get_text(sess, url))
    pe = edgar_text.period_end(text) or edgar_text.period_before(date.fromisoformat(filing["filingDate"]),
                                                                  quarter_months or set())
    ex = excerpt(text, phrases)
    raw = analyst.extract(model, PROMPT.format(name=meta["name"], ticker=ticker, filed=filing["filingDate"],
                                               period=pe.isoformat(), text=ex), SCHEMA, usage)
    return {"ticker": ticker, "name": meta["name"], "accession": filing["accessionNumber"],
            "filing_date": filing["filingDate"], "period_end": pe.isoformat(),
            "quarter": scoring._cal_quarter(pe.isoformat()), "url": url,
            "capex_guidance": verify_guidance(raw.get("capex_guidance") or [], ex), "fy_end_month": fy_end_month,
            "phrases": count_phrases(text, phrases), "summary": raw.get("summary", ""),
            "model": model, "extracted_at": util.now_iso()}


def stored(ticker: str) -> list[dict]:
    d = calls_dir(ticker)
    recs = [util.read_json(p) for p in sorted(d.glob("*.json"))] if d.exists() else []
    return sorted((r for r in recs if r), key=lambda r: r["quarter"])


def compare(cur: dict, prev: dict | None, fy_end_month: int | None = None) -> dict:
    """Guidance by period and phrase counts versus the previous quarter's release."""
    if not prev:
        return {"previous_quarter": None, "guidance": [], "phrases": {}, "dropped_guidance_periods": []}
    before = {g["period"]: g for g in _guidance(prev, fy_end_month)}
    guidance = []
    for g in _guidance(cur, fy_end_month):
        p = before.get(g["period"])
        item = {"period": g["period"], "now": g["amount_text"], "before": p and p["amount_text"],
                "midpoint_change_usd": None, "midpoint_change_pct": None}
        if p:
            mid, pmid = (g["low_usd"] + g["high_usd"]) / 2, (p["low_usd"] + p["high_usd"]) / 2
            item["midpoint_change_usd"] = mid - pmid
            pc = util.pct_change(mid, pmid)
            item["midpoint_change_pct"] = None if pc is None else round(pc, 1)
        guidance.append(item)
    now_periods = {g["period"] for g in _guidance(cur, fy_end_month)}
    phrases = {k: {"now": v["count"], "before": (prev.get("phrases", {}).get(k) or {}).get("count", 0)}
               for k, v in cur.get("phrases", {}).items()}
    return {"previous_quarter": prev["quarter"], "guidance": guidance, "phrases": phrases,
            "dropped_guidance_periods": sorted(set(before) - now_periods)}


def evidence(cur: dict, comp: dict, fy_end_month: int | None = None) -> list[dict]:
    """Evidence items built by code from the verified release text (status set here, not re-fetched)."""
    name, q = cur["name"], cur["quarter"]
    base = {"url": cur["url"], "status": "verified", "source": "edgar", "event_date": cur.get("filing_date"),
            "check": "Read from the SEC filing; quoted sentences matched verbatim"}
    out = []
    by_period = {g["period"]: g for g in comp.get("guidance", [])}
    for g in _guidance(cur, fy_end_month):
        c = by_period.get(g["period"]) or {}
        claim = f"{name} capex guidance for {g['period']}: {g['amount_text']}"
        if c.get("before"):
            claim += f" (previous quarter's release: {c['before']}; midpoint {c['midpoint_change_usd'] / 1e9:+.1f}B)"
        elif comp.get("previous_quarter"):
            claim += " (not given in the previous quarter's release)"
        if g["stated_change"] != "not stated":
            claim += f", {g['stated_change']}"
        out.append({**base, "claim": claim + f'. Release: "{g["sentence"][:300]}"'})
    if not any(g["verified"] for g in cur.get("capex_guidance", [])):
        claim = f"{name} earnings release ({q}) gives no capex guidance"
        if comp.get("dropped_guidance_periods"):
            claim += f"; the previous quarter's release gave guidance for {', '.join(comp['dropped_guidance_periods'])}"
        out.append({**base, "claim": claim})
    counts = cur.get("phrases", {})
    hits = [f"{k} {v['count']}" + (f" (prev. {comp['phrases'][k]['before']})" if comp.get("phrases") else "")
            for k, v in counts.items() if v["count"] or (comp.get("phrases", {}).get(k) or {}).get("before")]
    out.append({**base, "claim": f"{name} earnings release ({q}) cautious-phrase counts: "
                + ("; ".join(hits) if hits else "none of " + ", ".join(counts) + " appear")})
    return out


def run(cfg: dict, sess, tickers: list[str], model: str, llm_on: bool, offline: bool, usage_by_step: dict,
        subs_cache: dict | None = None, since: date | None = None) -> dict:
    """Read the latest earnings release of each hyperscaler in `tickers`. `since` (an event date) skips
    releases filed more than 3 days before it, i.e. not yet on EDGAR."""
    companies, ev, errors = {}, [], {}
    for t in tickers:
        meta = cfg["hyperscalers"].get(t)
        if not meta:
            continue
        try:
            cur, fy_end = None, None
            if not offline:
                filings = edgar_text.recent_filings(edgar_text.submissions(sess, meta["cik"], subs_cache))
                eightks, months = edgar_text.earnings_8ks(filings, 2), edgar_text.quarter_end_months(filings)
                fy_end = edgar_text.fiscal_year_end_month(filings)
                if not eightks:
                    raise RuntimeError("no 8-K with Item 2.02 in the submissions index")
                if since and eightks[0]["filingDate"] < (since - timedelta(days=3)).isoformat():
                    raise RuntimeError(f"earnings release not on EDGAR yet (latest Item 2.02 8-K filed "
                                       f"{eightks[0]['filingDate']})")
                if not llm_on:
                    raise RuntimeError("press release not read (Claude step off)")
                have = {r["accession"]: r for r in stored(t)}
                usage = usage_by_step.setdefault("earnings", models.new_usage(model))
                cur = have.get(eightks[0]["accessionNumber"])
                if cur is None:
                    cur = process(t, meta, eightks[0], sess, model, usage, cfg, months, fy_end)
                    util.write_json(calls_dir(t) / f"{cur['quarter']}.json", cur)
                if len(eightks) > 1 and not any(r["quarter"] < cur["quarter"] for r in have.values()):
                    prev = process(t, meta, eightks[1], sess, model, usage, cfg, months, fy_end)  # baseline
                    if prev["quarter"] < cur["quarter"]:
                        util.write_json(calls_dir(t) / f"{prev['quarter']}.json", prev)
            recs = stored(t)
            if not recs:
                continue
            cur = cur or recs[-1]
            prev = next((r for r in reversed(recs) if r["quarter"] < cur["quarter"]), None)
            comp = compare(cur, prev, fy_end)
            companies[t] = {"name": meta["name"], "quarter": cur["quarter"], "filing_date": cur["filing_date"],
                            "url": cur["url"], "summary": cur.get("summary", ""),
                            "guidance": _guidance(cur, fy_end),
                            "phrases": {k: v["count"] for k, v in cur["phrases"].items()}, "comparison": comp}
            ev += evidence(cur, comp, fy_end)
        except Exception as e:
            errors[t] = str(e)[:300]
    return {"companies": companies, "evidence": ev, "errors": errors}
