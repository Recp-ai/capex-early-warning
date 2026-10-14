"""Fakes for the stock tests: HTTP sessions, Claude, and synthetic SEC / Tiingo data.

The synthetic filer reports like a real one with a June fiscal year end: 10-Qs carry 3-month and
year-to-date income-statement facts but only year-to-date cash-flow facts; the 10-K carries the year;
each period is reported again as a comparative a year later (same value, later filing date).
"""
import json
import math
from datetime import date, timedelta
from types import SimpleNamespace as NS

import requests

from cew import analyst


class FakeResponse:
    def __init__(self, status=200, body="", headers=None):
        self.status_code = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.encoding = "utf-8"

    @property
    def text(self):
        return self.body.decode()

    def json(self):
        return json.loads(self.body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        pass


def as_json(obj, status=200):
    return FakeResponse(status, json.dumps(obj), {"Content-Type": "application/json"})


class FakeSession:
    """requests.Session stand-in: url -> FakeResponse (or a callable(params) -> response, or an exception)."""

    def __init__(self, routes):
        self.routes, self.calls = dict(routes), []
        self.headers = {}

    def get(self, url, params=None, timeout=None, headers=None, **kw):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        r = self.routes.get(url, FakeResponse(404, "not found"))
        if isinstance(r, Exception):
            raise r
        return r(params) if callable(r) else r


# ---------------------------------------------------------------- Claude
def _usage(inp=1000, out=200, searches=0):
    return NS(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches) if searches else None)


def reply(obj, inp=1000, out=200):
    return NS(stop_reason="end_turn", usage=_usage(inp, out), content=[NS(type="text", text=json.dumps(obj), citations=[])])


def research_reply(text, urls=(), searches=3, stop="end_turn"):
    cites = [NS(url=u) for u in urls]
    return NS(stop_reason=stop, usage=_usage(3000, 600, searches), content=[NS(type="text", text=text, citations=cites)])


class FakeMessages:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def create(self, **kw):
        self.calls.append(kw)
        if not self.responses:
            raise AssertionError("unexpected Claude call")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fake_claude(monkeypatch, responses):
    fake = FakeMessages(responses)
    monkeypatch.setattr(analyst, "_client", lambda: NS(messages=fake))
    return fake


# ---------------------------------------------------------------- synthetic SEC companyfacts
CIK = "0000789019"
LAST_END = date(2026, 6, 30)   # latest fiscal year end (10-K filed 2026-08-29)
FIRST_FY = 2019


def fiscal_quarters(fy):
    """FY ends June 30 of `fy`."""
    return [(date(fy - 1, 7, 1), date(fy - 1, 9, 30)), (date(fy - 1, 10, 1), date(fy - 1, 12, 31)),
            (date(fy, 1, 1), date(fy, 3, 31)), (date(fy, 4, 1), date(fy, 6, 30))]


def qindex(end):
    return (end.year * 12 + end.month - (2018 * 12 + 9)) // 3


def revenue(end):
    return round(50e9 * 1.035 ** qindex(end), -6)


def _fact(start, end, val, filed, form, fy=None):
    f = {"end": end.isoformat(), "val": val, "accn": f"0000789019-{filed:%y%m%d}", "fy": fy or end.year,
         "fp": "Q" if form == "10-Q" else "FY", "form": form, "filed": filed.isoformat()}
    if start is not None:
        f["start"] = start.isoformat()
    return f


def flow(value_fn, ytd_only=False, last_end=LAST_END, first_fy=FIRST_FY):
    out = []
    for fy in range(first_fy, last_end.year + 2):
        qs = fiscal_quarters(fy)
        vals = []
        for k, (s, e) in enumerate(qs, start=1):
            if e > last_end:
                break
            vals.append(value_fn(e))
            if k < 4:
                filed = e + timedelta(days=35)
                if not ytd_only or k == 1:
                    out.append(_fact(s, e, vals[-1], filed, "10-Q", fy))
                    out.append(_fact(s, e, vals[-1], filed + timedelta(days=364), "10-Q", fy + 1))  # comparative
                if k > 1:
                    out.append(_fact(qs[0][0], e, sum(vals), filed, "10-Q", fy))
            else:
                filed = e + timedelta(days=60)
                out.append(_fact(qs[0][0], e, sum(vals), filed, "10-K", fy))
                out.append(_fact(qs[0][0], e, sum(vals), filed + timedelta(days=365), "10-K", fy + 1))
    return out


def instant(value_fn, last_end=LAST_END, first_fy=FIRST_FY):
    out = []
    for fy in range(first_fy, last_end.year + 2):
        for k, (_, e) in enumerate(fiscal_quarters(fy), start=1):
            if e <= last_end:
                out.append(_fact(None, e, value_fn(e), e + timedelta(days=35 if k < 4 else 60), "10-Q" if k < 4 else "10-K", fy))
    return out


def diluted(value_fn, last_end=LAST_END, first_fy=FIRST_FY):
    """3-month averages in 10-Qs, the year's average in the 10-K (no Q4 3-month figure)."""
    out = []
    for fy in range(first_fy, last_end.year + 2):
        qs = fiscal_quarters(fy)
        for k, (s, e) in enumerate(qs, start=1):
            if e > last_end:
                break
            if k < 4:
                out.append(_fact(s, e, value_fn(e), e + timedelta(days=35), "10-Q", fy))
            else:
                out.append(_fact(qs[0][0], e, value_fn(e), e + timedelta(days=60), "10-K", fy))
    return out


def shares(end):
    return round(7.5e9 * 0.998 ** qindex(end), -5)


def companyfacts(last_end=LAST_END, **over):
    usd = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": flow(revenue, last_end=last_end),
        "GrossProfit": flow(lambda e: round(revenue(e) * 0.69, -6), last_end=last_end),
        "OperatingIncomeLoss": flow(lambda e: round(revenue(e) * 0.44, -6), last_end=last_end),
        "NetCashProvidedByUsedInOperatingActivities": flow(lambda e: round(revenue(e) * 0.45, -6), True, last_end),
        "PaymentsToAcquirePropertyPlantAndEquipment": flow(lambda e: round(revenue(e) * 0.15, -6), True, last_end),
        "ShareBasedCompensation": flow(lambda e: round(revenue(e) * 0.04, -6), True, last_end),
        "CashAndCashEquivalentsAtCarryingValue": instant(lambda e: 30e9, last_end),
        "ShortTermInvestments": instant(lambda e: 50e9, last_end),
        "LongTermDebtNoncurrent": instant(lambda e: 40e9, last_end),
        "LongTermDebtCurrent": instant(lambda e: 3e9, last_end),
    }
    usd.update(over)
    gaap = {tag: {"label": tag, "units": {"USD": rows}} for tag, rows in usd.items() if rows is not None}
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = {"units": {"shares": diluted(shares, last_end)}}
    cover = [{"end": "2026-07-24", "val": 7.43e9, "accn": "0000789019-260829", "fy": 2026, "fp": "FY",
              "form": "10-K", "filed": "2026-08-29"}]
    return {"cik": 789019, "entityName": "MICROSOFT CORP",
            "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": cover}}}, "us-gaap": gaap}}


# ---------------------------------------------------------------- synthetic prices (Tiingo JSON)
PRICE_END = date(2026, 10, 9)


def business_days(end=PRICE_END, years=5):
    d, out = end - timedelta(days=round(365.25 * years) + 10), []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def tiingo_rows(base, drift, end=PRICE_END, split=None, wiggle=0.0):
    """Tiingo daily rows. split=(date, factor): closes before it are factor x higher (unadjusted)."""
    days = business_days(end)
    out = []
    for n, d in enumerate(days):
        adj = base * math.exp(drift * n) * (1 + wiggle * math.sin(n / 9))
        raw = adj * (split[1] if split and d < split[0] else 1)
        out.append({"date": f"{d.isoformat()}T00:00:00.000Z", "close": round(raw, 4), "adjClose": round(adj, 4),
                    "splitFactor": split[1] if split and d == split[0] else 1.0})
    return out


def tiingo_url(t):
    return f"https://api.tiingo.com/tiingo/daily/{t.lower()}/prices"


def price_routes(ticker="MSFT", split=None):
    return {tiingo_url(ticker): as_json(tiingo_rows(300, 0.0004, split=split, wiggle=0.02)),
            tiingo_url("SPY"): as_json(tiingo_rows(400, 0.0003)),
            tiingo_url("QQQ"): as_json(tiingo_rows(350, 0.00035))}


# ---------------------------------------------------------------- SEC routes
def submissions(filings=None):
    rows = filings or [
        {"accessionNumber": "0000950170-26-000101", "filingDate": "2026-07-29", "reportDate": "2026-07-29",
         "form": "8-K", "primaryDocument": "x8k.htm", "items": "2.02,9.01"},
        {"accessionNumber": "0000950170-26-000100", "filingDate": "2026-08-29", "reportDate": "2026-06-30",
         "form": "10-K", "primaryDocument": "msft-10k.htm", "items": ""},
        {"accessionNumber": "0000950170-26-000050", "filingDate": "2026-04-29", "reportDate": "2026-04-29",
         "form": "8-K", "primaryDocument": "y8k.htm", "items": "2.02,9.01"},
        {"accessionNumber": "0000950170-26-000040", "filingDate": "2026-05-05", "reportDate": "2026-03-31",
         "form": "10-Q", "primaryDocument": "msft-10q.htm", "items": ""},
    ]
    keys = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument", "items")
    return {"cik": "789019", "name": "MICROSOFT CORP", "sicDescription": "Services-Prepackaged Software",
            "website": "https://www.microsoft.com", "filings": {"recent": {k: [r[k] for r in rows] for k in keys}}}


def index_page(href):
    return FakeResponse(200, f'<table><tr><td>1</td><td><a href="{href}">ex991.htm</a></td><td>EX-99.1</td></tr></table>')


RELEASE = """<html><body><p>Microsoft Cloud Strength Fuels Fourth Quarter Results</p>
<p>REDMOND, Wash. — July 29, 2026 — Microsoft Corp. today announced the following results for the quarter ended June 30, 2026.</p>
<p>Revenue was $120.0 billion and increased 15%.</p>
<p>We expect revenue for the first quarter of fiscal year 2027 to be between $122 billion and $124 billion.</p>
<p>Some customers are pacing deployments amid macroeconomic uncertainty, and we see near-term headwinds in devices.</p>
<p>This release contains forward-looking statements that we expect to be uncertain.</p>
</body></html>"""
PREV_RELEASE = """<html><body><p>Results for the quarter ended March 31, 2026.</p>
<p>We remain capacity constrained. No headwinds noted.</p></body></html>"""
GUIDE_SENTENCE = "We expect revenue for the first quarter of fiscal year 2027 to be between $122 billion and $124 billion."


def sec_routes(facts=None, subs=None):
    base = "https://www.sec.gov/Archives/edgar/data/789019"
    return {
        "https://www.sec.gov/files/company_tickers.json": as_json({
            "0": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
            "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"}}),
        "https://data.sec.gov/submissions/CIK0000789019.json": as_json(subs or submissions()),
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json": as_json(facts or companyfacts()),
        f"{base}/000095017026000101/0000950170-26-000101-index.htm": index_page("/Archives/edgar/data/789019/000095017026000101/ex991.htm"),
        f"{base}/000095017026000101/ex991.htm": FakeResponse(200, RELEASE),
        f"{base}/000095017026000050/0000950170-26-000050-index.htm": index_page("/Archives/edgar/data/789019/000095017026000050/ex991.htm"),
        f"{base}/000095017026000050/ex991.htm": FakeResponse(200, PREV_RELEASE),
    }


def guidance_reply(sentence=GUIDE_SENTENCE, amount="between $122 billion and $124 billion"):
    return reply({"guidance": [{"metric": "revenue", "period": "Q1 FY2027", "amount_text": amount,
                                "stated_change": "not stated", "sentence": sentence}],
                  "summary": "Guides Q1 FY2027 revenue to $122-124 billion."})


def topics_reply(ratings=None, evidence=None):
    ratings = ratings or {}
    out = {}
    for k in ("guidance", "analyst_actions", "litigation_regulation", "customer_concentration",
              "insider_activity", "product_competitive"):
        out[k] = {"rating": ratings.get(k, 0), "summary": f"{k} summary", "evidence": (evidence or {}).get(k, [])}
    return reply({"topics": out})


def writeup_reply():
    return reply({"headline": "High-quality compounder at a full price", "thesis": "Strong margins and growth.",
                  "key_risks": ["AI capex digestion", "Antitrust"], "what_would_change": ["Valuation falling to its 5-year median"]})
