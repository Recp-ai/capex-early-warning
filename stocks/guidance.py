"""Latest earnings 8-K (Item 2.02) Exhibit 99.1: guidance sentences and cautious-phrase counts.

The scoring model lists guidance statements; code keeps one only if its sentence is in the release
verbatim and its amount is in that sentence (cew.edgar_text.found_in), as cew/calls.py does. Phrase
counts are regex counts (config guidance.cautious_phrases) with the forward-looking-statements disclaimer
left out, also taken from the previous release for comparison (no Claude call for that one). Each release
is cached as data/stocks/calls/<TICKER>/<accession>.json, so a re-run reads the same 8-K only once.
Guidance is shown on the page and given to Claude as context; it is not a score input.

The period a release covers (`release_period`): cew.edgar_text.period_end takes the release's first
"quarter ended ..." date, which can be a prior period quoted first (Rubrik's Aug 27 release gave Jan 31). It
counts only when it is 20-120 days before the filing and companyfacts has no later quarter end before the
filing; else the latest companyfacts quarter end before the filing; else the fiscal calendar's.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from cew import analyst, calls, edgar_text, models, util

from . import config

METRICS = ["revenue", "eps", "gross_margin", "operating_margin", "operating_income", "capex", "free_cash_flow", "other"]
CHANGES = ["raised", "lowered", "reiterated", "initiated", "withdrawn", "not stated"]
SCHEMA = analyst._obj({
    "guidance": {"type": "array", "items": analyst._obj({
        "metric": {"type": "string", "enum": METRICS},
        "period": {"type": "string"},
        "amount_text": {"type": "string"},
        "stated_change": {"type": "string", "enum": CHANGES},
        "sentence": {"type": "string"},
    })},
    "summary": {"type": "string"},
})

PROMPT = """Company: {name} ({ticker}). Earnings press release (SEC 8-K Exhibit 99.1, filed {filed}) for the
period ended {period}.

From the excerpts below, list every forward-looking guidance statement: the company's outlook or
expectations for revenue, earnings per share, margins, operating income, capital expenditures, free cash
flow or other financial results in a future period. For each:
- metric: revenue, eps, gross_margin, operating_margin, operating_income, capex, free_cash_flow or other
- period: the period it covers, e.g. "Q4 FY2026", "FY2027", "next quarter"
- amount_text: the figure or range exactly as written (e.g. "$70-72 billion", "between 45% and 46%"), or ""
  if the sentence gives none
- stated_change: raised, lowered, reiterated, initiated or withdrawn if the release says so, else "not stated"
- sentence: the full sentence, copied exactly
Skip results for periods already reported and the forward-looking-statements disclaimer. Many releases
give no guidance: then return an empty list.
summary: one sentence on the outlook the release gives ("" if none).

Excerpts:
<<<
{text}
>>>"""

KEYWORDS = re.compile(r"(?i)outlook|guidance|expect|anticipate|forecast|project|range of|between|target"
                      r"|full[- ]year|next quarter|fiscal (?:year )?20\d\d")
BOILERPLATE = re.compile(r"(?i)forward-looking statements?|private securities litigation reform act")
SAFE_HARBOR = re.compile(r"(?i)^\W*(?:cautionary (?:note|statements?) (?:regarding|about|concerning) )?"
                         r"(?:safe harbor(?: statement)?|forward[- ]looking (?:statements?|information))\W*$")
VERSION = 2  # of the phrase counting; cached releases from an older version are recounted (guidance kept)
PERIOD_DAYS = (20, 120)  # a release's own period end counts only this many days before the filing date
SLACK_DAYS = 7           # 52/53-week fiscal quarters end a few days off the month end (NVIDIA's Jul 26)


def release_period(found: date | None, filed: date, quarter_ends: list[str] | None, months: set[int]) -> tuple[date, str]:
    """The period end a release covers, and where it came from: "release" (`found`, the date read from its
    text) when it is PERIOD_DAYS before the filing and no companyfacts quarter end falls more than SLACK_DAYS
    after it and before the filing (a prior period quoted first: Zscaler's May 26 release gave Jan 31,
    115 days back); else "companyfacts", the latest quarter end before the filing, unless companyfacts
    doesn't have that quarter yet (its 10-Q not filed: more than 120 days back, or before the fiscal
    calendar's last quarter end); else "fiscal calendar" (cew.edgar_text.period_before)."""
    qs = sorted(q for q in (date.fromisoformat(x) for x in quarter_ends or []) if q < filed)
    slack = timedelta(days=SLACK_DAYS)
    if found and PERIOD_DAYS[0] <= (filed - found).days <= PERIOD_DAYS[1] and not any(q > found + slack for q in qs):
        return found, "release"
    calendar = edgar_text.period_before(filed, months)
    if qs and (filed - qs[-1]).days <= PERIOD_DAYS[1] and qs[-1] >= calendar - slack:
        return qs[-1], "companyfacts"
    return calendar, "fiscal calendar"


def checked_period(rec: dict | None, quarter_ends: list[str] | None, months: set[int]) -> dict | None:
    """A cached release with its period end put through `release_period` (records from before the check may
    hold a prior period's date); saved when it changes. In place."""
    if rec is None:
        return None
    pe, src = release_period(date.fromisoformat(rec["period_end"]), date.fromisoformat(rec["filing_date"]),
                             quarter_ends, months)
    if pe.isoformat() != rec["period_end"]:
        rec["period_rejected"] = rec["period_end"]
        rec.update(period_end=pe.isoformat(), period_source=src)
        _save(rec)
    rec.setdefault("period_source", src)
    return rec


def excerpt(text: str, phrases: dict[str, str], limit: int = 40000) -> str:
    """Prose lines about the outlook or the tracked phrases; numeric table rows and disclaimers dropped."""
    rxs = [re.compile(p, re.I) for p in phrases.values()]
    out, n = [], 0
    for line in text.split("\n"):
        if not (KEYWORDS.search(line) or any(r.search(line) for r in rxs)) or BOILERPLATE.search(line):
            continue
        if sum(c.isalpha() for c in line) < 0.5 * len(line):
            continue
        if n + len(line) > limit:
            break
        out.append(line)
        n += len(line) + 1
    return "\n".join(out)


def _heading(s: str) -> bool:
    """A short title line ("About Microsoft"), not a sentence, list item or the disclaimer itself."""
    return 0 < len(s) <= 60 and s[0].isupper() and not s.endswith((".", ";", ",", ":")) and "forward-looking" not in s.lower()


def without_boilerplate(text: str) -> str:
    """The release without its forward-looking-statements disclaimer: lines naming it, and the whole section
    under a "Forward-Looking Statements" / "Safe Harbor" heading up to the next heading. Its list of risks
    ("uncertainties", "macroeconomic") would otherwise inflate every phrase count."""
    out, skip = [], False
    for line in text.split("\n"):
        s = line.strip()
        if SAFE_HARBOR.match(s):
            skip = True
            continue
        if skip and _heading(s):
            skip = False
        if not skip and not BOILERPLATE.search(line):
            out.append(line)
    return "\n".join(out)


def verify(raw: list[dict], source: str) -> list[dict]:
    out = []
    for g in raw:
        sent, amt = (g.get("sentence") or "").strip(), (g.get("amount_text") or "").strip()
        problem = None
        if not edgar_text.found_in(sent, source):
            problem = "sentence not found in the release"
        elif amt and not edgar_text.found_in(amt, sent):
            problem = "amount not found in its sentence"
        out.append({"metric": g.get("metric") if g.get("metric") in METRICS else "other",
                    "period": (g.get("period") or "").strip()[:40], "amount_text": amt,
                    "stated_change": g.get("stated_change") if g.get("stated_change") in CHANGES else "not stated",
                    "sentence": sent, "verified": problem is None, "problem": problem})
    return out


def calls_dir(ticker: str):
    return config.data_dir() / "calls" / ticker


def _release_text(sess, cik: str, filing: dict) -> tuple[str, str]:
    url = edgar_text.exhibit_99_1(sess, cik, filing["accessionNumber"])
    if not url:
        raise RuntimeError(f"no Exhibit 99.1 in 8-K {filing['accessionNumber']}")
    return url, util.html_to_text(util.get_text(sess, url))


def process(sess, cik: str, ticker: str, name: str, filing: dict, model: str | None, usage: dict | None,
            cfg: dict, months: set[int] | None = None, quarter_ends: list[str] | None = None) -> dict:
    """Read one release. model=None counts phrases only (no Claude call; guidance stays None)."""
    phrases = (cfg.get("guidance") or {}).get("cautious_phrases") or {}
    url, text = _release_text(sess, cik, filing)
    found = edgar_text.period_end(text)
    pe, src = release_period(found, date.fromisoformat(filing["filingDate"]), quarter_ends, months or set())
    rec = {"ticker": ticker, "accession": filing["accessionNumber"], "filing_date": filing["filingDate"],
           "period_end": pe.isoformat(), "period_source": src,
           **({"period_rejected": found.isoformat()} if found and found != pe else {}),
           "url": url, "phrases": calls.count_phrases(without_boilerplate(text), phrases),
           "guidance": None, "summary": "", "model": None, "extracted_at": util.now_iso(), "version": VERSION}
    if model:
        ex = excerpt(without_boilerplate(text), phrases)
        before = models.snapshot(usage) if usage is not None else None
        raw = analyst.extract(model, PROMPT.format(name=name, ticker=ticker, filed=filing["filingDate"],
                                                   period=pe.isoformat(), text=ex), SCHEMA, usage)
        if usage is not None:
            models.record_request(usage, before)  # one request: tiered prices are per request
        rec.update(guidance=verify(raw.get("guidance") or [], ex), summary=raw.get("summary", ""), model=model)
    return rec


def _cached(ticker: str, accession: str, sess=None, cfg: dict | None = None) -> dict | None:
    """A cached release; one from an older phrase counter is recounted from the release text (no Claude call)."""
    rec = util.read_json(calls_dir(ticker) / f"{accession}.json")
    if rec is None or rec.get("version") == VERSION or sess is None:
        return rec
    try:
        text = util.html_to_text(util.get_text(sess, rec["url"]))
    except Exception:
        return rec  # keep the old counts rather than none
    rec.update(phrases=calls.count_phrases(without_boilerplate(text), (cfg.get("guidance") or {}).get("cautious_phrases") or {}),
               version=VERSION)
    _save(rec)
    return rec


def _save(rec: dict) -> None:
    util.write_json(calls_dir(rec["ticker"]) / f"{rec['accession']}.json", rec)


def summarize(cur: dict, prev: dict | None, note: str | None = None) -> dict:
    prev_phrases = (prev or {}).get("phrases") or {}
    phrases = {k: {"count": v["count"], "previous": (prev_phrases.get(k) or {}).get("count") if prev else None,
                   "sentences": v.get("sentences", [])} for k, v in (cur.get("phrases") or {}).items()}
    g = cur.get("guidance")
    return {"accession": cur["accession"], "filing_date": cur["filing_date"], "period_end": cur["period_end"],
            "period_source": cur.get("period_source"), "period_rejected": cur.get("period_rejected"),
            "url": cur["url"], "summary": cur.get("summary", ""), "read": g is not None,
            "guidance": [x for x in (g or []) if x["verified"]],
            "dropped": sum(1 for x in (g or []) if not x["verified"]),
            "phrases": phrases, "previous": prev and {"accession": prev["accession"], "filing_date": prev["filing_date"],
                                                      "period_end": prev["period_end"], "url": prev["url"]},
            "note": note}


def _previous(cur: dict, recs) -> dict | None:
    """The release the phrase counts compare with: the first of `recs` (newest first; None for one that
    couldn't be read) covering an earlier period than `cur` (a second release for the same quarter is skipped)."""
    for r in recs:
        if r is not None and r["period_end"] < cur["period_end"]:
            return r
    return None


def run(sess, cik: str, ticker: str, name: str, subs: dict | None, model: str, llm_on: bool, offline: bool,
        usage_by_step: dict, cfg: dict, quarter_ends: list[str] | None = None) -> dict:
    """The latest earnings release (guidance + phrase counts vs the previous one), or {"error": ...}.
    `quarter_ends`: companyfacts quarter ends (`release_period`)."""
    try:
        if offline:
            recs = sorted((util.read_json(p) for p in calls_dir(ticker).glob("*.json")), key=lambda r: r["filing_date"]) \
                if calls_dir(ticker).exists() else []
            if not recs:
                return {"error": "offline and no cached earnings release"}
            recs = [checked_period(r, quarter_ends, set()) for r in recs]
            return summarize(recs[-1], _previous(recs[-1], recs[-2::-1]))
        filings = edgar_text.recent_filings(subs or {})
        eightks = edgar_text.earnings_8ks(filings, 3)
        if not eightks:
            return {"error": "no 8-K with Item 2.02 (earnings release) on EDGAR; foreign filers report on 6-K"}
        months = edgar_text.quarter_end_months(filings)
        cur = checked_period(_cached(ticker, eightks[0]["accessionNumber"], sess, cfg), quarter_ends, months)
        note = None
        if cur is None or (cur.get("guidance") is None and llm_on):
            usage = usage_by_step.setdefault("guidance", models.new_usage(model)) if llm_on else None
            cur = process(sess, cik, ticker, name, eightks[0], model if llm_on else None, usage, cfg, months, quarter_ends)
            _save(cur)
        if cur.get("guidance") is None:
            note = "guidance not read (Claude step off); phrase counts only"

        def earlier(filing):  # comparison only; the latest release stands without it
            rec = checked_period(_cached(ticker, filing["accessionNumber"], sess, cfg), quarter_ends, months)
            if rec is None:
                try:
                    rec = process(sess, cik, ticker, name, filing, None, None, cfg, months, quarter_ends)
                    _save(rec)
                except Exception:
                    return None
            return rec
        prev = _previous(cur, (earlier(f) for f in eightks[1:]))
        return summarize(cur, prev, note)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
