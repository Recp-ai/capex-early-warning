"""Backtest: buying SPY on the day it CLOSES BELOW a support zone (the indicator's "closed below zone" alert)
versus (a) dips of the same depth and trend that are not at a zone break, (b) any day, and (c) dips that
closed INSIDE a support zone (the zone held at the close).

Zones come from sr_backtest.py's replica of the TradingView indicator, rebuilt each day from information
available at the previous close only. A break on day t: a zone with at least `min_touch` swing reversals
whose bottom is at or below the previous close, and today's close is below that bottom.

All p-values here are two-sided: the question is whether broken-support dips do worse OR better.

Usage: python research/sr_break_backtest.py research/data/SPY.csv [--out research/results]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from scipy import stats

sys.path.insert(0, os.path.dirname(__file__))
import sr_backtest as b  # noqa: E402

HORIZONS = (5, 10, 20, 60)
RNG = np.random.default_rng(20261031)
PERIODS = {"1994-2007": (1994, 2007), "2008-2016": (2008, 2016), "2017-2026": (2017, 2026)}


def break_signals(df, swing=10, width=0.5, min_touch=2, lookback=1500, warmup=300):
    """breaks[t]: close[t] fell below the bottom of a zone that the previous close was in or above.
    held[t]: close[t] inside a support zone (the earlier test's signal). touches[t]: the broken zone's touches."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    atr = b.atr_wilder(h, l, c)
    pb, pp, _ = b.find_pivots(h, l, swing)
    n = len(c)
    breaks, held = np.zeros(n, bool), np.zeros(n, bool)
    touches = np.zeros(n, int)
    for t in range(warmup, n):
        a = atr[t - 1]
        zs = b.zones_at(t - 1, pb, pp, swing, lookback, width * a, 0.1 * a, min_touch)
        prev = c[t - 1]
        for bot, top, k in zs:
            if prev >= bot and c[t] < bot:
                breaks[t] = True
                touches[t] = max(touches[t], k)
            if top < prev and bot <= c[t] <= top:
                held[t] = True
    return breaks, held, touches


def matched(ret, sig, cell, eligible, exclude, n_iter=10000):
    """Signal days vs random days from the same dip-depth x trend cell (excluding `exclude` days).
    Two-sided permutation p-values; a t-test on each signal's return minus its cell's control mean;
    a bootstrap 95% interval for the difference in means."""
    idx = np.flatnonzero(sig & eligible & ~np.isnan(ret))
    pools = {k: np.flatnonzero(eligible & ~exclude & (cell == k) & ~np.isnan(ret)) for k in set(cell[idx])}
    idx = np.array([i for i in idx if len(pools[cell[i]]) > 20])
    if len(idx) < 10:
        return None
    obs_mean, obs_win = ret[idx].mean(), (ret[idx] > 0).mean()
    counts = {}
    for i in idx:
        counts[cell[i]] = counts.get(cell[i], 0) + 1
    dm, dw = np.empty(n_iter), np.empty(n_iter)
    for it in range(n_iter):
        picks = np.concatenate([RNG.choice(pools[k], size=m, replace=True) for k, m in counts.items()])
        dm[it], dw[it] = ret[picks].mean(), (ret[picks] > 0).mean()
    two = lambda d, o: float(min(1.0, 2 * min((d >= o).mean(), (d <= o).mean())))  # noqa: E731
    cell_mean = {k: ret[p].mean() for k, p in pools.items()}
    diffs = np.array([ret[i] - cell_mean[cell[i]] for i in idx])
    t = stats.ttest_1samp(diffs, 0.0)
    boot = np.array([RNG.choice(diffs, len(diffs)).mean() for _ in range(5000)])
    return {"n": int(len(idx)), "signal_mean_pct": 100 * float(obs_mean), "signal_win": float(obs_win),
            "control_mean_pct": 100 * float(dm.mean()), "control_win": float(dw.mean()),
            "diff_pct": 100 * float(diffs.mean()), "diff_ci95_pct": [100 * float(np.percentile(boot, 2.5)), 100 * float(np.percentile(boot, 97.5))],
            "p_mean_perm": two(dm, obs_mean), "p_win_perm": two(dw, obs_win), "t_stat": float(t.statistic), "p_t": float(t.pvalue)}


def vs_any_day(ret, sig, eligible, n_iter=10000):
    """Circular-shift test: the same signal pattern moved to random start points (keeps clustering)."""
    e = np.flatnonzero(eligible & ~np.isnan(ret))
    s, r = sig[e], ret[e]
    obs = r[s].mean()
    m = np.array([r[np.roll(s, RNG.integers(1, len(e)))].mean() for _ in range(n_iter)])
    return {"signal_mean_pct": 100 * float(obs), "signal_win": float((r[s] > 0).mean()),
            "all_days_mean_pct": 100 * float(r.mean()), "all_days_win": float((r > 0).mean()),
            "p_mean_shift": float(min(1.0, 2 * min((m >= obs).mean(), (m <= obs).mean())))}


def break_vs_held(ret, brk, held):
    """Broken-zone episodes vs held-zone episodes directly: Welch t-test, Mann-Whitney, Fisher (win rates)."""
    a, c = ret[brk & ~np.isnan(ret)], ret[held & ~np.isnan(ret)]
    if len(a) < 10 or len(c) < 10:
        return None
    wa, wc = int((a > 0).sum()), int((c > 0).sum())
    return {"n_break": int(len(a)), "n_held": int(len(c)),
            "break_mean_pct": 100 * float(a.mean()), "held_mean_pct": 100 * float(c.mean()),
            "break_win": float(wa / len(a)), "held_win": float(wc / len(c)),
            "p_welch_t": float(stats.ttest_ind(a, c, equal_var=False).pvalue),
            "p_mann_whitney": float(stats.mannwhitneyu(a, c).pvalue),
            "p_fisher_win": float(stats.fisher_exact([[wa, len(a) - wa], [wc, len(c) - wc]]).pvalue)}


def analyse(df, swing=10, width=0.5, min_touch=2, cooldown=10, n_iter=10000, full=True):
    c = df["close"].values
    breaks, held, touches = break_signals(df, swing, width, min_touch)
    cell, dd = b.strata(c, 300)
    elig = cell >= 0
    brk = b.episodes(breaks, cooldown) & elig
    hld = b.episodes(held, cooldown) & elig
    exclude = breaks | held  # controls are dips that are neither at a break nor inside a zone
    out = {"params": {"swing": swing, "width_atr": width, "min_touches": min_touch, "cooldown": cooldown},
           "break_days": int(breaks[elig].sum()), "break_episodes": int(brk.sum()), "held_episodes": int(hld.sum()),
           "median_dip_20d_at_break_pct": float(np.nanmedian(dd[brk]) * 100), "horizons": {}}
    for H in HORIZONS:
        r = b.fwd_returns(c, H)
        out["horizons"][H] = {"vs_matched_dips": matched(r, brk, cell, elig, exclude, n_iter)}
        if full:
            out["horizons"][H]["vs_any_day"] = vs_any_day(r, brk, elig, n_iter)
            out["horizons"][H]["vs_held_zone"] = break_vs_held(r, brk, hld)
    if full:
        r20 = b.fwd_returns(c, 20)
        years = df["date"].dt.year.values
        out["periods_20d"] = {name: matched(r20, brk & (years >= a) & (years <= z), cell, elig & (years >= a) & (years <= z), exclude, 4000)
                              for name, (a, z) in PERIODS.items()}
        strong = brk & (touches >= 4)
        out["strong_zones_4plus_touches"] = {H: matched(b.fwd_returns(c, H), strong, cell, elig, exclude, 4000) for H in HORIZONS}
        # Non-overlapping check: episodes at least 20 days apart, so 20-day returns never overlap.
        sparse = b.episodes(breaks, 20) & elig
        out["non_overlapping_20d"] = matched(r20, sparse, cell, elig, exclude, 4000)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default="research/results")
    a = ap.parse_args()
    df = b.load(a.csv)
    os.makedirs(a.out, exist_ok=True)
    res = analyse(df)
    res["data"] = {"first": str(df["date"].iloc[0].date()), "last": str(df["date"].iloc[-1].date()), "bars": len(df)}
    sens = []
    for swing in (5, 10, 20):
        for width in (0.25, 0.5, 1.0):
            for mt in (2, 3):
                s = analyse(df, swing, width, mt, n_iter=1000, full=False)
                h20 = s["horizons"][20]["vs_matched_dips"] or {}
                sens.append({"swing": swing, "width": width, "min_touches": mt, "episodes": s["break_episodes"],
                             **{k: h20.get(k) for k in ("signal_mean_pct", "control_mean_pct", "diff_pct", "signal_win", "control_win", "p_mean_perm", "p_t")}})
                print(json.dumps(sens[-1]), flush=True)
    res["sensitivity_20d"] = sens
    with open(os.path.join(a.out, "spy_sr_break_backtest.json"), "w") as f:
        json.dump(res, f, indent=2)
    print(json.dumps({k: v for k, v in res.items() if k != "sensitivity_20d"}, indent=1))
