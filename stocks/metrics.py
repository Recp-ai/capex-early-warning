"""Price-based metrics (valuation and trend), the 10-year Treasury yield, and the capex dashboard context.

Market cap = latest close x shares. Shares: the cover-page count when it is within 0.8-1.25x of the latest
diluted weighted-average count, else the diluted count. Valuation history: at each month-end close of the
last `valuation.history_years`, the multiples use only the TTM figures, balance sheet and share count
already filed on that date, in today's share units (stock splits come from the price data's split
factors; share counts more than `valuation.share_outlier_x` off their neighbours are dropped). The
percentile versus history is computed on yields (revenue / FCF / EBIT over market cap or EV), so periods
with near-zero or negative FCF or EBIT rank as the most expensive instead of producing huge multiples;
the page still shows the multiples. The write-up gets that comparison only in words (stocks/wording.py).
Trend: the chart, moving averages and drawdown use split-adjusted closes (what a price chart shows);
momentum and relative strength use total-return closes (dividends included).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import median

from cew import util

from . import fundamentals as fund
from . import wording

SHARE_BAND = (0.8, 1.25)


def _r(v, n=2):
    return None if v is None else round(v, n)


# ---------------------------------------------------------------- prices
def split_adjusted(rows: list[dict]) -> list[tuple[str, float]]:
    """(date, close in today's share units): each close divided by the splits after its date."""
    out, factor = [], 1.0
    for r in reversed(rows):
        out.append((r["date"], r["close"] / factor))
        sf = r.get("split_factor") or 1.0
        if sf and sf != 1.0:
            factor *= sf
    return out[::-1]


def splits_after(rows: list[dict], day: str) -> float:
    f = 1.0
    for r in rows:
        if r["date"] > day and (r.get("split_factor") or 1.0) != 1.0:
            f *= r["split_factor"]
    return f


def current_units(value: float, day: str, rows: list[dict], reference: float | None) -> float:
    """A share count from `day` in today's units. Later filings restate counts for splits, earlier ones
    don't, so take whichever of value / value x (splits since) is closer to the current count."""
    f = splits_after(rows, day)
    if f == 1.0:
        return value
    if not reference:  # nothing to compare with: assume the count predates the split
        return value * f
    return min((value, value * f), key=lambda v: abs(math.log(v / reference)))


def on_or_before(rows: list[tuple[str, float]], day: str) -> float | None:
    best = None
    for d, v in rows:
        if d <= day:
            best = v
        else:
            break
    return best


# ---------------------------------------------------------------- valuation
def share_count(s: dict, rows: list[dict]) -> dict | None:
    dil, dil_end = s.get("diluted_shares"), s.get("diluted_shares_end")
    out_sh = s.get("shares_outstanding") or None
    if dil is None and not out_sh:
        return None
    dil_now = current_units(dil, dil_end, rows, None) if dil is not None else None
    if out_sh:
        cover = current_units(out_sh["val"], out_sh["end"], rows, dil_now)
        if dil_now is None or SHARE_BAND[0] <= cover / dil_now <= SHARE_BAND[1]:
            return {"shares": cover, "source": "cover page shares outstanding", "date": out_sh["end"]}
    return {"shares": dil_now, "source": "diluted weighted-average shares", "date": dil_end}


MULTIPLES = {"price_to_sales": "sales_yield_pct", "price_to_fcf": "fcf_yield_pct", "ev_to_ebit": "ebit_yield_pct"}


def multiples(mcap: float, revenue, fcf, ebit, debt, cash) -> dict:
    """Multiples (None when the denominator is zero or negative) and the yields behind them (revenue / FCF /
    EBIT over market cap or EV, in %, negative when the figure is; None when EV is unknown or not positive)."""
    ev = None if debt is None or cash is None else mcap + debt - cash
    return {"market_cap": mcap, "enterprise_value": ev,
            "price_to_sales": mcap / revenue if revenue and revenue > 0 else None,
            "price_to_fcf": mcap / fcf if fcf and fcf > 0 else None,
            "ev_to_ebit": ev / ebit if ev is not None and ev > 0 and ebit and ebit > 0 else None,
            "sales_yield_pct": revenue / mcap * 100 if revenue is not None and mcap else None,
            "fcf_yield_pct": fcf / mcap * 100 if fcf is not None and mcap else None,
            "ebit_yield_pct": ebit / ev * 100 if ebit is not None and ev is not None and ev > 0 else None,
            "fcf_negative": fcf is not None and fcf <= 0, "ebit_negative": ebit is not None and ebit <= 0}


def history(f: dict, rows: list[dict], years: int, share_ref: float | None, cfg: dict | None = None,
            fcf_basis: str = "reported") -> list[dict]:
    """Month-end multiples and yields over the last `years`, from filings already public on each date.
    Share counts without outliers (fundamentals.clean_shares); FCF on the same basis as the current figure
    (reported, ex excess prepayments, or normalised), so now and history compare alike."""
    if not rows:
        return []
    cfg = cfg or {}
    adj = split_adjusted(rows)
    start = (date.fromisoformat(adj[-1][0]) - timedelta(days=round(365.25 * years))).isoformat()
    month_end: dict[str, tuple[str, float]] = {}
    for d, c in adj:
        if d >= start:
            month_end[d[:7]] = (d, c)
    flows = f.get("flows") or {}
    ttm = {m: fund.ttm_points(flows.get(m) or {}) for m in ("revenue", "operating_cash_flow", "capex")}
    ttm["operating_income"] = fund.ttm_points(fund.flow_series(f, "operating_income"))
    excess = fund.excess_points(f) if fcf_basis == "ex-prepayments" else {}
    normalised = fund.normalised_points(f, cfg) if fcf_basis == "normalised" else {}
    bs_dates = fund.balance_sheet_dates(f)
    clean = fund.shares_cleaned(f, float((cfg.get("valuation") or {}).get("share_outlier_x", fund.SHARE_OUTLIER_X)))
    shares = {**clean["diluted_a"], **clean["diluted_q"]}
    out = []
    for d, close in month_end.values():
        rev = fund.asof(ttm["revenue"], d)
        sp = fund.asof(shares, d)
        bs = fund.asof({k: {"filed": v} for k, v in bs_dates.items()}, d)
        if not rev or not sp:
            continue
        end = rev[0]
        ocf, capex = (ttm[m].get(end, {}).get("val") for m in ("operating_cash_flow", "capex"))
        ebit = ttm["operating_income"].get(end, {}).get("val")
        fcf = None if ocf is None or capex is None else ocf - capex
        x = excess.get(end)
        if fcf is not None and x and x["val"] > 0 and (x.get("filed") or "") <= d:
            fcf -= x["val"]  # FCF minus the excess prepayments
        if fcf_basis == "normalised":
            fcf = (normalised.get(end) or {}).get("val")
        b = fund.balance_sheet_at(f, bs[0], cfg) if bs else {}
        mcap = close * current_units(sp[1]["val"], sp[0], rows, share_ref)
        m = multiples(mcap, rev[1]["val"], fcf, ebit, b.get("debt"), b.get("cash_and_investments"))
        out.append({"date": d, **{k: _r(m[k], 3) for k in (*MULTIPLES, *MULTIPLES.values())}})
    return out


def percentile(cur: float, hist: list[float]) -> float | None:
    if cur is None or not hist:
        return None
    below = sum(1 for h in hist if h < cur)
    equal = sum(1 for h in hist if h == cur)
    return round(100 * (below + 0.5 * equal) / len(hist), 1)


def cheaper_share(cur_yield: float | None, hist_yields: list[float]) -> float | None:
    """The share of month-ends that were cheaper than now (a higher yield), ties counted half: the same as a
    multiple's percentile when every figure is positive, but near-zero and negative FCF / EBIT periods rank
    as the most expensive instead of exploding or dropping out."""
    if cur_yield is None or not hist_yields:
        return None
    higher = sum(1 for h in hist_yields if h > cur_yield)
    equal = sum(1 for h in hist_yields if h == cur_yield)
    return round(100 * (higher + 0.5 * equal) / len(hist_yields), 1)


def _implied(y: float | None) -> float | None:
    """A yield (%) as a multiple; None for zero or negative."""
    return None if y is None or y <= 0 else _r(100 / y, 3)


def valuation(s: dict, f: dict, price_rows: list[dict] | None, dgs10: dict | None, cfg: dict) -> dict:
    """Current multiples, FCF yield vs the 10-year, and each multiple vs the company's own history."""
    vc = cfg.get("valuation") or {}
    if not price_rows:
        return {"missing": "no price data"}
    if s.get("foreign"):
        return {"missing": "foreign filer (20-F/40-F): price per share and reported share count / currency not reconciled"}
    if s.get("currency") != "USD":
        return {"missing": f"financials reported in {s.get('currency') or 'an unknown currency'}, prices in USD"}
    if s.get("revenue_ttm") is None:
        return {"missing": "no TTM revenue"}
    sc = share_count(s, price_rows)
    if not sc or not sc["shares"]:
        return {"missing": "no share count in XBRL"}
    last = price_rows[-1]
    bs = s.get("balance_sheet") or {}
    mcap = last["close"] * sc["shares"]
    # FCF for scoring: ex excess prepayments, or normalised after a spike (fundamentals.summarize's fcf_basis)
    fcf_rep = s.get("fcf_ttm")
    basis = s.get("fcf_basis") or "reported"
    if s.get("fcf_scored_ttm") is None:
        basis = "reported"
    fcf = fcf_rep if basis == "reported" else s["fcf_scored_ttm"]
    m = multiples(mcap, s.get("revenue_ttm"), fcf, s.get("operating_income_ttm"), bs.get("debt"),
                  bs.get("cash_and_investments"))
    out = {"price": last["close"], "price_date": last["date"], "shares": sc, **{k: _r(v, 3) if isinstance(v, float) else v for k, v in m.items()}}
    out["fcf_basis"] = basis
    if basis != "reported":
        rep = multiples(mcap, None, fcf_rep, None, None, None)
        out["price_to_fcf_reported"], out["fcf_yield_reported_pct"] = _r(rep["price_to_fcf"], 3), _r(rep["fcf_yield_pct"], 3)
    if bs.get("unknown"):
        out["ev_note"] = "EV unknown: " + "; ".join(bs["unknown"])
    elif out["enterprise_value"] is not None and out["enterprise_value"] <= 0:
        out["ev_note"] = "EV is zero or negative (net cash above the market cap): EV/EBIT left out"
    out["ust10y_pct"] = (dgs10 or {}).get("value")
    out["ust10y_date"] = (dgs10 or {}).get("date")
    out["fcf_yield_vs_10y_pp"] = (_r(out["fcf_yield_pct"] - out["ust10y_pct"], 3)
                                  if out["fcf_yield_pct"] is not None and out["ust10y_pct"] is not None else None)
    hist = history(f, price_rows, int(vc.get("history_years", 5)), sc["shares"], cfg, basis)
    out["history"] = hist
    out["share_outliers"] = s.get("share_outliers") or []
    out["vs_history"] = {}
    for k, y in MULTIPLES.items():
        ys = [h[y] for h in hist if h.get(y) is not None]
        enough = len(ys) >= int(vc.get("min_history_points", 24)) and out[y] is not None
        out["vs_history"][k] = {"current": out[k], "current_yield_pct": out[y], "points": len(ys),
                                "median": _implied(median(ys)) if ys else None,
                                "low": _implied(max(ys)) if ys else None, "high": _implied(min(ys)) if ys else None,
                                "negative_points": sum(1 for v in ys if v <= 0),
                                "median_yield_pct": _r(median(ys), 3) if ys else None,
                                "percentile": cheaper_share(out[y], ys) if enough else None,
                                # month-ends cheaper than now (a higher yield) and dearer (a lower one)
                                "cheaper_points": sum(1 for v in ys if v > out[y]) if enough else None,
                                "dearer_points": sum(1 for v in ys if v < out[y]) if enough else None,
                                "min_points": int(vc.get("min_history_points", 24)),
                                "from": hist[0]["date"] if hist else None}
    out["vs_own_history"] = wording.history_view(out, cfg)
    return out


# ---------------------------------------------------------------- trend
def _ret(series: list[float], back: int, skip: int = 0) -> float | None:
    if len(series) <= back:
        return None
    a, b = series[-1 - back], series[-1 - skip]
    return None if not a else (b / a - 1) * 100


def _ret_dated(rows: list[dict], start_day: str) -> float | None:
    start = on_or_before([(r["date"], r["adj_close"]) for r in rows], start_day)
    return None if not start or not rows else (rows[-1]["adj_close"] / start - 1) * 100


def trend(rows: list[dict] | None, benchmarks: dict, cfg: dict) -> dict:
    if not rows:
        return {"missing": "no price data"}
    tc = cfg.get("trend") or {}
    rs_days = int(tc.get("rs_days", 126))
    adj = [c for _, c in split_adjusted(rows)]
    tr = [r["adj_close"] for r in rows]
    last = adj[-1]
    ma = lambda n: sum(adj[-n:]) / n if len(adj) >= n else None  # noqa: E731
    ma50, ma200 = ma(50), ma(200)
    out = {"close": rows[-1]["close"], "date": rows[-1]["date"], "ma50": _r(ma50), "ma200": _r(ma200),
           "vs_ma50_pct": _r((last / ma50 - 1) * 100) if ma50 else None,
           "vs_ma200_pct": _r((last / ma200 - 1) * 100) if ma200 else None,
           "momentum_12_1_pct": _r(_ret(tr, 252, 21)),
           "high_52w": _r(max(adj[-252:])), "drawdown_52w_pct": _r((last / max(adj[-252:]) - 1) * 100),
           "return_rs_pct": _r(_ret(tr, rs_days)), "rs_days": rs_days}
    start_day = rows[-1 - rs_days]["date"] if len(rows) > rs_days else None
    for b in ("SPY", "QQQ"):
        brows = benchmarks.get(b) or []
        key = f"rs_{b.lower()}_pp"
        bret = _ret_dated(brows, start_day) if start_day and brows else None
        out[f"{b.lower()}_return_pct"] = _r(bret)
        out[key] = _r(out["return_rs_pct"] - bret) if out["return_rs_pct"] is not None and bret is not None else None
    if len(rows) < 252:
        out["note"] = f"only {len(rows)} trading days of prices"
    return out


def chart(rows: list[dict] | None) -> list[list]:
    """[[date, close, ma50, ma200], ...] in split-adjusted terms, for the page's price chart."""
    if not rows:
        return []
    adj = split_adjusted(rows)
    out, s50, s200 = [], 0.0, 0.0
    vals = [c for _, c in adj]
    for i, (d, c) in enumerate(adj):
        s50 += c - (vals[i - 50] if i >= 50 else 0)
        s200 += c - (vals[i - 200] if i >= 200 else 0)
        out.append([d, _r(c), _r(s50 / 50) if i >= 49 else None, _r(s200 / 200) if i >= 199 else None])
    return out


# ---------------------------------------------------------------- context files written by the capex pipeline
def ust10y(path=None) -> dict | None:
    rows = util.read_csv(path or util.DATA / "fred" / "DGS10.csv")
    for r in reversed(rows):
        v = util.to_float(r.get("value"))
        if v is not None:
            return {"value": v, "date": r["date"]}
    return None


def capex_context(path=None, today: date | None = None) -> dict | None:
    """Level, composite and confirmation from the capex dashboard's site/data/latest.json."""
    L = util.read_json(path or util.SITE_DATA / "latest.json")
    if not L or not L.get("composite"):
        return None
    c = L["composite"]
    day = str(L.get("run_id") or L.get("date") or "")[:10]
    try:
        age = ((today or util.today()) - date.fromisoformat(day)).days
    except ValueError:
        age = None
    return {"level": c.get("level"), "total": c.get("total"), "max": c.get("max"),
            "red_confirmed": bool(c.get("red_confirmed")), "run_id": L.get("run_id") or L.get("date"),
            "age_days": age}
