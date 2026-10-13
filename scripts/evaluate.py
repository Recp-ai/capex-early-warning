"""Did the warnings lead price moves? Forward returns after each logged week, by alert level.

Run any time after a few months of history:  python scripts/evaluate.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cew import util  # noqa: E402

HORIZONS = {"4w": 28, "8w": 56, "12w": 84}


def price_after(rows: list[dict], start: date, days: int) -> float | None:
    target = start + timedelta(days=days)
    best = None
    for r in rows:
        d = date.fromisoformat(r["date"])
        if d <= target:
            best = r
        else:
            break
    if best is None or date.fromisoformat(best["date"]) < target - timedelta(days=5):
        return None  # horizon not reached yet
    return float(best["value"])


def main() -> None:
    log = util.read_csv(util.DATA / "log.csv")
    if not log:
        print("No log yet.")
        return
    for sid, label in (("SP500", "S&P 500"), ("NASDAQ100", "Nasdaq-100")):
        rows = util.read_csv(util.DATA / "fred" / f"{sid}.csv")
        print(f"\n{label}: average forward return by alert level")
        by_level: dict[str, dict[str, list[float]]] = {}
        for entry in log:
            d = date.fromisoformat(entry["date"])
            p0 = price_after(rows, d, 0)
            if p0 is None:
                continue
            for h, days in HORIZONS.items():
                p1 = price_after(rows, d, days)
                if p1 is not None:
                    by_level.setdefault(entry["level"], {}).setdefault(h, []).append((p1 / p0 - 1) * 100)
        for level in ("GREEN", "AMBER", "RED"):
            cells = []
            for h in HORIZONS:
                v = by_level.get(level, {}).get(h, [])
                cells.append(f"{h}: {mean(v):+.2f}% (n={len(v)})" if v else f"{h}: n/a")
            print(f"  {level:<6} " + "   ".join(cells))
    print("\nSmall samples mean little; wait for at least a few dozen weeks before trusting a pattern.")


if __name__ == "__main__":
    main()
