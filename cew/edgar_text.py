"""SEC EDGAR filing text: the submissions index, filing documents, and checks on extracted quotes.

Claude extracts sentences and amounts from filing text (cew.leases, cew.calls). Nothing it returns is
used unless the sentence is found verbatim in the filing and the amount is found in that sentence;
the dollar value itself is then parsed here, by code.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime

from . import util

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{acc_dashed}-index.htm"
FIELDS = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument", "items")


def submissions(sess, cik: str, cache: dict | None = None) -> dict:
    cik = str(cik).zfill(10)
    if cache is not None and cik in cache:
        return cache[cik]
    data = util.get_json(sess, SUBMISSIONS_URL.format(cik=cik), timeout=60)
    if cache is not None:
        cache[cik] = data
    return data


def recent_filings(subs: dict) -> list[dict]:
    """Rows of filings.recent, newest first."""
    r = (subs.get("filings") or {}).get("recent") or {}
    n = len(r.get("accessionNumber") or [])
    rows = [{k: ((r.get(k) or [None] * n)[i] if i < len(r.get(k) or []) else None) for k in FIELDS}
            for i in range(n)]
    return sorted(rows, key=lambda x: x.get("filingDate") or "", reverse=True)


def earnings_8ks(filings: list[dict], n: int = 2) -> list[dict]:
    """Latest 8-Ks reporting Item 2.02 (Results of Operations and Financial Condition)."""
    out = [f for f in filings if f.get("form") == "8-K"
           and "2.02" in [x.strip() for x in str(f.get("items") or "").split(",")]]
    return out[:n]


def periodic_reports(filings: list[dict], n: int = 2) -> list[dict]:
    """Latest original 10-Q / 10-K filings (amendments skipped)."""
    return [f for f in filings if f.get("form") in ("10-Q", "10-K") and f.get("primaryDocument")][:n]


def doc_url(cik: str, accession: str, doc: str) -> str:
    return ARCHIVE_URL.format(cik=int(cik), acc=accession.replace("-", ""), doc=doc)


def exhibit_99_1(sess, cik: str, accession: str) -> str | None:
    """URL of a filing's Exhibit 99.1 (the earnings press release), from the filing index page."""
    page = util.get_text(sess, INDEX_URL.format(cik=int(cik), acc=accession.replace("-", ""),
                                                 acc_dashed=accession))
    rows = re.split(r"(?i)<tr\b", page)
    for want in (r"EX-99\.1", r"EX-99(?:\.\d+)?"):
        for row in rows:
            if re.search(r"(?i)>\s*" + want + r"\s*<", row):
                m = re.search(r'(?i)href="([^"]+)"', row)
                if m:
                    href = re.sub(r"^/ix\?doc=", "", m.group(1))
                    return href if href.startswith("http") else "https://www.sec.gov" + href
    return None


def snippets(text: str, phrase: str, window: int = 1500) -> list[str]:
    """Lines containing the phrase (case-insensitive); very long lines cut to a window around it."""
    out, low = [], phrase.lower()
    for line in text.split("\n"):
        i = line.lower().find(low)
        if i < 0:
            continue
        if len(line) > 2 * window:
            line = line[max(0, i - window): i + len(phrase) + window]
        if line not in out:
            out.append(line)
    return out


def _norm(s: str) -> str:
    s = (s or "").translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                           "–": "-", "—": "-", "−": "-", "\xa0": " "}))
    return re.sub(r"\s+", "", s).lower()


def found_in(quote: str, source: str) -> bool:
    """Whether `quote` appears verbatim in `source` (ignoring whitespace, case and quote/dash styles)."""
    q = _norm(quote)
    return len(q) >= 3 and q in _norm(source)


_SCALE = {"trillion": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6,
          "thousand": 1e3, "k": 1e3}
_AMOUNT = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(?:-|to|and|–|—)\s*\$?\s*(\d[\d,]*(?:\.\d+)?))?"
                     r"\s*(trillion|billion|million|thousand|bn|mm|b|m|k)?\b", re.I)


def parse_usd_range(text: str) -> tuple[float, float] | None:
    """'$12.3 billion' -> (12.3e9, 12.3e9); '$70-72 billion' / '$70 billion to $72 billion' -> (70e9, 72e9).
    Amounts need a scale word (billion, million, ...); bare dollar figures return None."""
    ms = list(_AMOUNT.finditer(text or ""))[:2]
    if not ms:
        return None
    scales = [_SCALE.get((m.group(3) or "").lower()) for m in ms]
    nums: list[float] = []
    for m, sc in zip(ms, scales):
        sc = sc or next((s for s in scales if s), None)  # "$70 billion to $72" shares the scale
        if sc is None:
            return None
        nums += [float(g.replace(",", "")) * sc for g in (m.group(1), m.group(2)) if g]
    return min(nums), max(nums)


def parse_usd(text: str) -> float | None:
    r = parse_usd_range(text)
    return None if r is None or r[0] != r[1] else r[0]


_PERIOD = re.compile(r"(?:quarter|three months|fiscal year|year)\s+end(?:ed|ing)\s+([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})")


def period_end(text: str) -> date | None:
    """First 'quarter ended September 30, 2026' style date in a press release."""
    for m in _PERIOD.finditer(text or ""):
        raw = m.group(1).replace(".", "")
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def quarter_end_months(filings: list[dict]) -> set[int]:
    """Fiscal quarter-end months, from the report dates of a company's 10-Qs and 10-Ks."""
    return {int(f["reportDate"][5:7]) for f in filings
            if f.get("form") in ("10-Q", "10-K") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.get("reportDate") or "")}


def period_before(d: date, months: set[int]) -> date:
    """Last fiscal quarter end strictly before d (e.g. Oracle's Nov 30 for a Dec 10 release)."""
    if not months:
        return quarter_before(d)
    y, m = d.year, d.month
    for _ in range(12):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        if m in months:
            return date(y, m, calendar.monthrange(y, m)[1])
    return quarter_before(d)


def quarter_before(d: date) -> date:
    """Last calendar-quarter end strictly before d."""
    q_month = ((d.month - 1) // 3) * 3  # 0, 3, 6, 9
    if q_month == 0:
        return date(d.year - 1, 12, 31)
    return date(d.year, q_month, 30 if q_month in (6, 9) else 31)
