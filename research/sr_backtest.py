"""Backtest: does buying SPY when it closes inside a support zone (the EB S/R indicator's green zones) beat
(a) buying on any day and (b) buying a dip of the same depth that is NOT at a support zone?

Zones are rebuilt every day from information available at the previous close only (swing points confirmed
`swing` bars after they happen, ATR as of that close), exactly as the TradingView indicator does.

Usage: python research/sr_backtest.py research/data/SPY.csv [--out research/results]
"""
from __future__ import annotations

import argparse
import bisect
import json
import os

import numpy as np
import pandas as pd

HORIZONS = (5, 10, 20, 60)
RNG = np.random.default_rng(20261026)


# ───────────────────────────── data and indicator replica ─────────────────────────────
def load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    out = pd.DataFrame({"date": df["date"], "open": df["adj_open"], "high": df["adj_high"],
                        "low": df["adj_low"], "close": df["adj_close"]})
    return out.dropna().reset_index(drop=True)


def atr_wilder(h, l, c, n=14):
    prev = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - prev), np.abs(l - prev)]), axis=0)
    tr[0] = h[0] - l[0]
    atr = np.full(len(c), np.nan)
    atr[n - 1] = tr[:n].mean()
    for i in range(n, len(c)):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def find_pivots(h, l, L):
    """Swing highs/lows like ta.pivothigh/low(L, L): extreme of the 2L+1 window. Returns sorted by bar."""
    piv = []
    for j in range(L, len(h) - L):
        wh, wl = h[j - L:j + L + 1], l[j - L:j + L + 1]
        if h[j] == wh.max() and h[j] > h[j - L:j].max():
            piv.append((j, h[j], 1))
        if l[j] == wl.min() and l[j] < l[j - L:j].min():
            piv.append((j, l[j], -1))
    piv.sort()
    return np.array([p[0] for p in piv]), np.array([p[1] for p in piv]), np.array([p[2] for p in piv])


def zones_at(t, pb, pp, L, lookback, tol, pad, min_touch):
    """Zones from pivots confirmed by bar t (pivot bar + L <= t) within `lookback` bars. List of (bot, top, touches)."""
    hi = bisect.bisect_right(pb, t - L)          # pivots with bar <= t-L are confirmed
    lo = bisect.bisect_left(pb, t - lookback)
    lo = max(lo, hi - 600)
    prices = np.sort(pp[lo:hi])
    zones = []
    start = None
    top = None
    n = 0
    for p in prices:
        if start is None or p - start > tol:
            if start is not None and n >= min_touch:
                zones.append((start - pad, top + pad, n))
            start, top, n = p, p, 0
        top = max(top, p)
        n += 1
    if start is not None and n >= min_touch:
        zones.append((start - pad, top + pad, n))
    return zones


def signals(df, swing=10, width=0.5, min_touch=2, lookback=1500, warmup=300):
    """Per day t: in_zone[t] = close[t] is inside a support zone defined at close[t-1]; plus that zone's
    bottom (for the stop) and the nearest resistance bottom above close[t-1] (for the target)."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    atr = atr_wilder(h, l, c)
    pb, pp, _ = find_pivots(h, l, swing)
    n = len(c)
    in_zone = np.zeros(n, bool)
    zone_bot = np.full(n, np.nan)
    res_bot = np.full(n, np.nan)
    for t in range(warmup, n):
        a = atr[t - 1]
        zs = zones_at(t - 1, pb, pp, swing, lookback, width * a, 0.1 * a, min_touch)
        prev = c[t - 1]
        sup = [z for z in zs if z[1] < prev]
        res = [z for z in zs if z[0] > prev]
        for bot, top, _ in sup:
            if bot <= c[t] <= top:
                in_zone[t] = True
                zone_bot[t] = bot
                break
        if res:
            res_bot[t] = min(z[0] for z in res)
    return in_zone, zone_bot, res_bot, atr


# ───────────────────────────── statistics ─────────────────────────────
def fwd_returns(c, H):
    r = np.full(len(c), np.nan)
    r[:-H] = c[H:] / c[:-H] - 1
    return r


def episodes(mask, cooldown):
    """First day of each zone entry, at least `cooldown` days after the previous episode."""
    out = np.zeros_like(mask)
    last = -10**9
    for t in np.flatnonzero(mask):
        if (t == 0 or not mask[t - 1]) and t - last >= cooldown:
            out[t] = True
            last = t
    return out


def strata(c, warmup):
    """Matching cells: depth of the dip (close vs 20-day high, deciles) x trend (close vs 200-day average)."""
    s = pd.Series(c)
    dd = (s / s.rolling(20).max() - 1).values
    trend = (s > s.rolling(200).mean()).values.astype(int)
    valid = ~np.isnan(dd)
    valid[:warmup] = False
    qs = np.nanquantile(dd[valid], np.linspace(0, 1, 11))
    dec = np.clip(np.searchsorted(qs, dd, side="right") - 1, 0, 9)
    cell = dec * 2 + trend
    cell[~valid] = -1
    return cell, dd


def matched_test(ret, sig, cell, eligible, n_iter=10000):
    """Signal episodes vs random non-signal days from the same cell. Returns observed means / win rates,
    control means, and one-sided p-values (share of resamples at least as good as the signal)."""
    idx = np.flatnonzero(sig & ~np.isnan(ret))
    pools = {}
    for k in set(cell[idx]):
        pools[k] = np.flatnonzero(eligible & ~sig & (cell == k) & ~np.isnan(ret))
    idx = np.array([i for i in idx if len(pools[cell[i]]) > 20])
    if len(idx) < 10:
        return None
    obs_mean, obs_win = ret[idx].mean(), (ret[idx] > 0).mean()
    draws_mean = np.empty(n_iter)
    draws_win = np.empty(n_iter)
    by_cell = {}
    for i in idx:
        by_cell.setdefault(cell[i], 0)
        by_cell[cell[i]] += 1
    for it in range(n_iter):
        picks = np.concatenate([RNG.choice(pools[k], size=m, replace=True) for k, m in by_cell.items()])
        draws_mean[it] = ret[picks].mean()
        draws_win[it] = (ret[picks] > 0).mean()
    return {"n": int(len(idx)), "signal_mean": float(obs_mean), "signal_win": float(obs_win),
            "control_mean": float(draws_mean.mean()), "control_win": float(draws_win.mean()),
            "p_mean": float((draws_mean >= obs_mean).mean()), "p_win": float((draws_win >= obs_win).mean())}


def shift_test(ret, sig, eligible, n_iter=10000):
    """Signal episodes vs the same episode pattern shifted to random start points (keeps clustering)."""
    e = np.flatnonzero(eligible & ~np.isnan(ret))
    s = sig[e]
    r = ret[e]
    obs_mean, obs_win = r[s].mean(), (r[s] > 0).mean()
    m, w = np.empty(n_iter), np.empty(n_iter)
    for it in range(n_iter):
        k = RNG.integers(1, len(e))
        ss = np.roll(s, k)
        m[it], w[it] = r[ss].mean(), (r[ss] > 0).mean()
    return {"signal_mean": float(obs_mean), "signal_win": float(obs_win), "all_days_mean": float(r.mean()),
            "all_days_win": float((r > 0).mean()), "p_mean": float((m >= obs_mean).mean()), "p_win": float((w >= obs_win).mean())}


def trade_sim(df, sig, zone_bot, res_bot, atr, cell, eligible, max_days=20, stop_buf=0.25, n_iter=2000):
    """Buy at close on a signal episode; stop below the zone, target the next resistance (else 2 ATR), exit at
    max_days. Same-day stop and target: stop first. Control: matched dip days with the same stop/target
    distances in ATR. Returns win rate and average R (profit / initial risk)."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(c)

    def run(t, stop_atr, tgt_atr):
        a = atr[t]
        entry, stop, tgt = c[t], c[t] - stop_atr * a, c[t] + tgt_atr * a
        risk = entry - stop
        for k in range(t + 1, min(t + 1 + max_days, n)):
            if l[k] <= stop:
                return (stop - entry) / risk
            if h[k] >= tgt:
                return (tgt - entry) / risk
        k = min(t + max_days, n - 1)
        return (c[k] - entry) / risk

    idx = [t for t in np.flatnonzero(sig) if t + max_days < n]
    plans, results = [], []
    for t in idx:
        stop_atr = max((c[t] - (zone_bot[t] - stop_buf * atr[t])) / atr[t], 0.25)
        tgt_atr = (res_bot[t] - c[t]) / atr[t] if not np.isnan(res_bot[t]) and res_bot[t] > c[t] else 2.0
        plans.append((t, stop_atr, tgt_atr))
        results.append(run(t, stop_atr, tgt_atr))
    results = np.array(results)
    pools = {k: np.array([t for t in np.flatnonzero(eligible & ~sig & (cell == k)) if t + max_days < n]) for k in set(cell[idx])}
    ctrl_mean, ctrl_win = np.empty(n_iter), np.empty(n_iter)
    for it in range(n_iter):
        rr = []
        for t, sa, ta in plans:
            pool = pools[cell[t]]
            if len(pool):
                rr.append(run(int(RNG.choice(pool)), sa, ta))
        rr = np.array(rr)
        ctrl_mean[it], ctrl_win[it] = rr.mean(), (rr > 0).mean()
    return {"n": len(results), "signal_avg_R": float(results.mean()), "signal_win": float((results > 0).mean()),
            "control_avg_R": float(ctrl_mean.mean()), "control_win": float(ctrl_win.mean()),
            "p_avg_R": float((ctrl_mean >= results.mean()).mean()), "p_win": float((ctrl_win >= (results > 0).mean()).mean()),
            "median_stop_atr": float(np.median([p[1] for p in plans])), "median_target_atr": float(np.median([p[2] for p in plans]))}


# ───────────────────────────── main ─────────────────────────────
def analyse(df, swing=10, width=0.5, min_touch=2, cooldown=10, n_iter=10000, full=True):
    c = df["close"].values
    warmup = 300
    in_zone, zone_bot, res_bot, atr = signals(df, swing, width, min_touch, warmup=warmup)
    sig = episodes(in_zone, cooldown)
    cell, dd = strata(c, warmup)
    eligible = cell >= 0
    out = {"params": {"swing": swing, "width_atr": width, "min_touches": min_touch, "cooldown": cooldown},
           "days_in_zone": int(in_zone[eligible].sum()), "episodes": int(sig[eligible].sum()),
           "median_dip_20d_at_signal_pct": float(np.nanmedian(dd[sig]) * 100), "horizons": {}}
    for H in HORIZONS:
        r = fwd_returns(c, H)
        out["horizons"][H] = {"vs_matched_dips": matched_test(r, sig & eligible, cell, eligible, n_iter),
                              "vs_any_day": shift_test(r, sig & eligible, eligible, n_iter) if full else None}
    if full:
        out["trade"] = trade_sim(df, sig & eligible, zone_bot, res_bot, atr, cell, eligible)
        years = df["date"].dt.year.values
        out["periods"] = {}
        for name, (a, b) in {"1994-2007": (1994, 2007), "2008-2016": (2008, 2016), "2017-2026": (2017, 2026)}.items():
            m = (years >= a) & (years <= b)
            r = fwd_returns(c, 20)
            out["periods"][name] = matched_test(r, sig & eligible & m, cell, eligible & m, 4000)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default="research/results")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    df = load(a.csv)
    os.makedirs(a.out, exist_ok=True)
    main = analyse(df, n_iter=2000 if a.quick else 10000)
    main["data"] = {"first": str(df["date"].iloc[0].date()), "last": str(df["date"].iloc[-1].date()), "bars": len(df)}
    sens = []
    for swing in (5, 10, 20):
        for width in (0.25, 0.5, 1.0):
            for mt in (2, 3):
                res = analyse(df, swing, width, mt, n_iter=1000, full=False)
                h20 = res["horizons"][20]["vs_matched_dips"]
                sens.append({"swing": swing, "width": width, "min_touches": mt, "episodes": res["episodes"],
                             **({k: h20[k] for k in ("signal_mean", "control_mean", "signal_win", "control_win", "p_mean", "p_win")} if h20 else {})})
    main["sensitivity_20d"] = sens
    with open(os.path.join(a.out, "spy_sr_backtest.json"), "w") as f:
        json.dump(main, f, indent=2)
    print(json.dumps(main, indent=2)[:4000])
