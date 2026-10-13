"""Leases signed but not yet commenced, from the text of each hyperscaler's latest 10-Q / 10-K.

For every company in `hyperscalers`: find its newest periodic reports in the EDGAR submissions index,
download the primary document, keep the paragraphs containing filing_text.lease_phrases ("not yet
commenced", and Oracle's "additional lease commitments"), and have Claude (the assessment model) list
each dollar amount with its source sentence. An amount counts only if its sentence is in those
paragraphs verbatim and the amount is in that sentence; the dollar value is then parsed by code
(cew.edgar_text; bare table figures use the "(in millions)" heading found by code). Claude is called
once per new filing; results accumulate in data/edgar/<TICKER>_leases.json.
"""
from __future__ import annotations

import re

from . import analyst, edgar_text, models, scoring, util

VERSION = 2  # bump to re-read filings that gave no total under an older extractor
KINDS = ["operating", "finance", "total", "other", "after_period_end"]
SCHEMA = analyst._obj({
    "amounts": {"type": "array", "items": analyst._obj({
        "amount_text": {"type": "string"},
        "kind": {"type": "string", "enum": KINDS},
        "sentence": {"type": "string"},
        "commencement": {"type": "string"},
    })},
    "note": {"type": "string"},
})

PROMPT = """Company: {name} ({ticker}). Filing: {form} for the period ended {period}.

Below are the paragraphs of this filing that contain {phrases}. List every dollar amount of leases the
company has signed that have not yet commenced (future lease payments not yet on the balance sheet).
For each amount:
- amount_text: the amount exactly as written, e.g. "$12.3 billion"; for a table row, the figure in its
  total column (usually the last), e.g. "137,214"
- kind: operating, finance, total (one combined figure for all such leases as of the period end), other,
  or after_period_end (leases entered into after the period end, e.g. in a subsequent-events note)
- sentence: the full sentence (or table row) containing the amount, copied exactly
- commencement: when the leases commence and their terms, as stated ("" if not stated)
List each distinct amount once. Skip leases that have already commenced, and do not add figures up
yourself. A line ending in "[table amounts in millions]" is a table row; that note was added to tell you
the units. Return an empty list if there are none. note: anything unusual, else "".

Paragraphs:
<<<
{paras}
>>>"""


def path_for(ticker: str):
    return util.DATA / "edgar" / f"{ticker}_leases.json"


def verify_amounts(raw: list[dict], paragraphs: list[str]) -> list[dict]:
    """Keep Claude's amounts, marking each verified only if sentence and amount check out. A bare
    table figure is scaled by the units hint snippets() added to its paragraph."""
    out, seen = [], set()
    for a in raw:
        amt, sent = (a.get("amount_text") or "").strip(), (a.get("sentence") or "").strip()
        para = next((p for p in paragraphs if edgar_text.found_in(sent, p)), None)
        value = edgar_text.parse_usd(amt)
        whole = True
        if value is None and para is not None:
            value = edgar_text.table_amount(amt, para)
            whole = re.search(r"(?<![\d.,])" + re.escape(amt) + r"(?![\d]|[.,]\d)", sent) is not None
        problem = None
        if para is None:
            problem = "sentence not found in the filing"
        elif not edgar_text.found_in(amt, sent) or not whole:
            problem = "amount not found in its sentence"
        elif value is None:
            problem = "amount could not be parsed"
        elif value in seen:
            continue  # the same commitment quoted twice (e.g. leases note and MD&A)
        if problem is None:
            seen.add(value)
        out.append({"amount_text": amt, "amount_usd": None if problem else value, "kind": a.get("kind", "other"),
                    "sentence": sent, "commencement": a.get("commencement", ""),
                    "verified": problem is None, "problem": problem})
    return out


def total(amounts: list[dict]) -> float | None:
    """Combined figure if the filing states one, else the sum of the separate amounts."""
    ok = [a for a in amounts if a["verified"] and a["kind"] != "after_period_end"]
    if not ok:
        return None
    totals = [a["amount_usd"] for a in ok if a["kind"] == "total"]
    return sum(totals) if totals else sum(a["amount_usd"] for a in ok)


def process(ticker: str, meta: dict, filing: dict, sess, model: str, usage: dict, cfg: dict) -> dict:
    phrases = (cfg.get("filing_text") or {}).get("lease_phrases") or ["not yet commenced"]
    quoted = " or ".join(f'"{p}"' for p in phrases)
    url = edgar_text.doc_url(meta["cik"], filing["accessionNumber"], filing["primaryDocument"])
    period = filing.get("reportDate") or filing["filingDate"]
    paras = edgar_text.snippets(util.html_to_text(util.get_text(sess, url, timeout=180)), phrases)
    rec = {"accession": filing["accessionNumber"], "form": filing["form"], "filing_date": filing["filingDate"],
           "period_end": period, "quarter": scoring._cal_quarter(period), "url": url, "paragraphs": paras,
           "model": model, "extracted_at": util.now_iso(), "version": VERSION}
    if not paras:
        return {**rec, "amounts": [], "total_usd": None, "note": f"no paragraph contains {quoted}"}
    kept, n = [], 0
    for p in paras:  # cap the prompt at ~30k characters, whole paragraphs only
        if n + len(p) > 30000:
            break
        kept.append(p)
        n += len(p) + 2
    raw = analyst.extract(model, PROMPT.format(name=meta["name"], ticker=ticker, form=filing["form"], period=period,
                                               phrases=quoted, paras="\n\n".join(kept)), SCHEMA, usage)
    amounts = verify_amounts(raw.get("amounts") or [], kept)
    return {**rec, "amounts": amounts, "total_usd": total(amounts), "note": raw.get("note", "")}


def run(cfg: dict, sess, model: str, llm_on: bool, offline: bool, usage_by_step: dict,
        subs_cache: dict | None = None, n_filings: int = 2) -> dict:
    """Update the lease files and summarize them. The two newest reports are read the first time so a
    quarter-over-quarter comparison is available straight away."""
    companies, by_quarter, errors = {}, {}, {}
    for ticker, meta in cfg["hyperscalers"].items():
        path = path_for(ticker)
        store = util.read_json(path) or {"ticker": ticker, "name": meta["name"], "filings": []}
        if not offline:
            try:
                subs = edgar_text.submissions(sess, meta["cik"], subs_cache)
                have = {r["accession"] for r in store["filings"]  # retry errors, and empty reads by an older version
                        if not r.get("error") and (r.get("total_usd") is not None or r.get("version") == VERSION)}
                new = [f for f in edgar_text.periodic_reports(edgar_text.recent_filings(subs), n_filings)
                       if f["accessionNumber"] not in have]
                if new and not llm_on:
                    errors[ticker] = f"{len(new)} new filing(s) not read (Claude step off)"
                for f in new if llm_on else []:
                    usage = usage_by_step.setdefault("leases", models.new_usage(model))
                    try:
                        rec = process(ticker, meta, f, sess, model, usage, cfg)
                    except Exception as e:
                        rec = {"accession": f["accessionNumber"], "form": f["form"], "filing_date": f["filingDate"],
                               "period_end": f.get("reportDate"), "error": str(e)[:300]}
                        errors[ticker] = f"{f['form']} {f['filingDate']}: {str(e)[:200]}"
                    store["filings"] = [r for r in store["filings"] if r["accession"] != rec["accession"]] + [rec]
                if new and llm_on:
                    store["filings"].sort(key=lambda r: r.get("period_end") or "")
                    store["updated_at"] = util.now_iso()
                    util.write_json(path, store)
            except Exception as e:
                errors[ticker] = str(e)[:300]
        recs = sorted((r for r in store["filings"] if r.get("total_usd") is not None),
                      key=lambda r: r["period_end"])
        for r in recs:
            by_quarter.setdefault(r["quarter"], {})[ticker] = r["total_usd"]
        if recs:
            cur, prev = recs[-1], (recs[-2] if len(recs) > 1 else None)
            companies[ticker] = {
                "name": meta["name"], "quarter": cur["quarter"], "period_end": cur["period_end"],
                "form": cur["form"], "url": cur["url"], "total_usd": cur["total_usd"],
                "previous_quarter": prev and prev["quarter"], "previous_total_usd": prev and prev["total_usd"],
                "qoq_pct": None if prev is None else _r(util.pct_change(cur["total_usd"], prev["total_usd"])),
                "amounts": [{k: a[k] for k in ("amount_text", "kind", "sentence")} for a in cur["amounts"]
                            if a["verified"]],
            }
    return {"companies": companies, "by_quarter": dict(sorted(by_quarter.items())), "errors": errors}


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)

