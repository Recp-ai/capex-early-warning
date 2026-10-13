"""Follow-ups: the previous run's "watch next" items, searched for and given a status in this run.

The list and its run counts are kept by code. An item is followed up in the run after it was listed
and, while unresolved, in the runs after that, up to follow_ups.max_runs runs in all; then it drops off.
Claude only gives each item a status, a one-sentence note and a URL from its own research. The URL is
checked like evidence: item["source"] = {"url", "claim": note} gets the tier and verification status.
"""
from __future__ import annotations

import difflib
import re

STATUSES = ("resolved", "improved", "worsened", "still_open", "no_news")


def _key(text) -> str:
    return re.sub(r"\W+", " ", str(text or "").lower()).strip()


def pending(prev: dict | None, cfg: dict) -> list[dict]:
    """Follow-ups for this run from the previous assessed run: its watch_next items (first follow-up)
    and its follow-ups that are unresolved and under max_runs. Newest first, at most max_items."""
    if not prev:
        return []
    fc = cfg.get("follow_ups") or {}
    max_runs, max_items = int(fc.get("max_runs", 3)), int(fc.get("max_items", 12))
    seen: set[str] = set()
    carried, fresh = [], []
    for f in prev.get("follow_ups") or []:
        k, runs = _key(f.get("item")), int(f.get("runs") or 1) + 1
        if not k or k in seen or f.get("status") == "resolved":
            continue
        seen.add(k)  # an item that ran out of runs is not revived by being listed again
        if runs <= max_runs:
            carried.append({"item": str(f["item"]).strip(), "runs": runs, "listed": f.get("listed")})
    a = (prev.get("claude") or {}).get("assessment") or {}
    for w in a.get("watch_next") or []:
        k = _key(w)
        if k and k not in seen:
            seen.add(k)
            fresh.append({"item": str(w).strip(), "runs": 1, "listed": prev.get("run_id")})
    return (fresh + carried)[:max_items]


def _match(item: str, candidates: list[dict]) -> int | None:
    """Index of Claude's entry for an item: the same text, else the closest text (ratio 0.6 or more)."""
    k = _key(item)
    keys = [_key(c.get("item")) for c in candidates]
    if k in keys:
        return keys.index(k)
    ratios = [difflib.SequenceMatcher(None, k, c).ratio() for c in keys]
    best = max(range(len(ratios)), key=ratios.__getitem__, default=None)
    return best if best is not None and ratios[best] >= 0.6 else None


def merge(items: list[dict], returned: list[dict] | None) -> list[dict]:
    """Attach Claude's status, note and URL to each pending item. Items it skipped are no_news; entries
    that match no pending item are ignored."""
    left = [r for r in (returned or []) if isinstance(r, dict)]
    out = []
    for p in items:
        j = _match(p["item"], left)
        r = left.pop(j) if j is not None else {}
        url = str(r.get("url") or "").strip()
        url = url if url.startswith(("https://", "http://")) else ""
        note = str(r.get("note") or "").strip() or ("" if r else "Not covered in this run's assessment.")
        out.append({**p,
                    "status": r.get("status") if r.get("status") in STATUSES else ("still_open" if r else "no_news"),
                    "note": note, "url": url, "source": {"url": url, "claim": note} if url else None})
    return out
