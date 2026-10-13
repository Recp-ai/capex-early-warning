"""Check the evidence URLs Claude cites: fetch each page once and look for the claim's key numbers
and names. Each evidence item gets status verified / unverified / unreachable.

- verified: the page loaded and contains every number in the claim and at least half of its names
- unverified: the page loaded but the check failed (or the claim has nothing checkable)
- unreachable: timeout, HTTP error, blocked address, or a non-text page (PDF, image)

A news category scoring source_check.cap_from or more with fewer than source_check.min_sources
verified-or-unreachable sources is capped at source_check.cap_to (see apply_caps).

Every evidence URL also gets a source tier (primary / major / other) from its host and the domain
lists in source_tiers (see tier). Tiers are labels only.
"""
from __future__ import annotations

import ipaddress
import math
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from urllib.parse import urljoin, urlsplit

from . import scoring, util

USER_AGENT = "capex-early-warning-source-check/2.0 (research bot; fetches each cited page once)"
MAX_BYTES = 3_000_000
OK_STATUSES = ("verified", "unreachable")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
NAME = re.compile(r"\b[A-Z][A-Za-z0-9&'.-]*[A-Za-z0-9]|\b[A-Z]{2,}\b")
NOT_NAMES = {
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to", "for", "by", "from", "with", "as",
    "per", "after", "before", "over", "under", "about", "also", "its", "it", "this", "that", "these",
    "those", "there", "their", "they", "he", "she", "we", "our", "us", "new", "report", "reports",
    "reported", "reportedly", "said", "says", "according", "while", "since", "despite", "amid", "q1",
    "q2", "q3", "q4", "h1", "h2", "fy", "yoy", "qoq", "ceo", "cfo", "inc", "corp", "ltd", "co",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}


def key_terms(claim: str) -> tuple[list[str], list[str]]:
    """Numbers (thousands separators removed) and capitalized names in a claim."""
    numbers = list(dict.fromkeys(n.replace(",", "") for n in NUMBER.findall(claim or "")))
    names = [re.sub(r"'s$", "", n).strip(".'-") for n in NAME.findall(re.sub(r"https?://\S+", " ", claim or ""))]
    names = list(dict.fromkeys(n for n in names if n and n.lower() not in NOT_NAMES and not n[0].isdigit()))
    return numbers, names


def check_claim(claim: str, page: str) -> tuple[str, str]:
    """(status, detail) for one claim against one page's text."""
    numbers, names = key_terms(claim)
    if not numbers and not names:
        return "unverified", "no numbers or names in the claim to check"
    page_numbers = {n.replace(",", "") for n in NUMBER.findall(page)}
    low = page.lower()
    missing_n = [n for n in numbers if n not in page_numbers]
    found_names = [n for n in names if re.search(r"(?<!\w)" + re.escape(n.lower()) + r"(?!\w)", low)]
    need = math.ceil(len(names) / 2)
    detail = f"numbers {len(numbers) - len(missing_n)}/{len(numbers)}, names {len(found_names)}/{len(names)}"
    missing = missing_n + [n for n in names if n not in found_names]
    if missing_n or len(found_names) < need:
        return "unverified", detail + " · missing: " + ", ".join(missing[:6])
    return "verified", detail


def public_host(host: str, resolver: Callable = socket.getaddrinfo) -> bool:
    """False for localhost and private, loopback, link-local or reserved addresses."""
    if not host or host.lower() in ("localhost", "localhost.localdomain"):
        return False
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        return literal.is_global and not literal.is_multicast
    except ValueError:
        pass  # a name: check every address it resolves to
    try:
        addrs = {info[4][0] for info in resolver(host, None)}
    except (OSError, UnicodeError):
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(addrs)


def make_fetcher(timeout: float = 10, session=None, resolver: Callable = socket.getaddrinfo) -> Callable:
    """fetch(url) -> (page_text | None, error | None). Redirects are followed by hand (max 5) so every
    hop is checked; the whole fetch is bounded by `timeout` seconds."""

    def fetch(url: str):
        sess = session or util.session(USER_AGENT)
        deadline = time.monotonic() + timeout
        for _ in range(6):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                return None, f"unsupported URL scheme {parts.scheme or '(none)'}"
            if not public_host(parts.hostname or "", resolver):
                return None, "host not public or not resolvable"
            left = deadline - time.monotonic()
            if left <= 0:
                return None, "timed out"
            try:
                r = sess.get(url, timeout=min(left, timeout), allow_redirects=False, stream=True,
                             headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"})
            except Exception as e:
                return None, f"{type(e).__name__}"
            try:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status_code >= 400:
                    return None, f"HTTP {r.status_code}"
                ctype = (r.headers.get("Content-Type") or "").lower()
                if ctype and not any(t in ctype for t in ("html", "text", "xml", "json")):
                    return None, f"not a text page ({ctype.split(';')[0]})"
                body = b""
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) >= MAX_BYTES or time.monotonic() > deadline:
                        break
                return util.html_to_text(body.decode(r.encoding or "utf-8", errors="replace")), None
            except Exception as e:
                return None, f"{type(e).__name__}"
            finally:
                r.close()
        return None, "too many redirects"

    return fetch


def tier(url: str, cfg: dict) -> str:
    """'primary', 'major' or 'other' for a URL, from its host and the source_tiers domain lists."""
    host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    tiers = cfg.get("source_tiers") or {}
    for name in ("primary", "major"):
        for d in tiers.get(name) or []:
            d = str(d).lower().strip(".")
            if d and (host == d or host.endswith("." + d)):
                return name
    return "other"


def tag_tiers(items: list[dict], cfg: dict) -> None:
    """Set "tier" on every item that has a URL (evidence items, follow-ups). In place."""
    for e in items:
        if e.get("url"):
            e["tier"] = tier(e["url"], cfg)


def check_evidence(scores: dict, cfg: dict, fetch: Callable | None = None, workers: int = 4,
                   extra: list[dict] | None = None) -> dict:
    """Annotate every not-yet-checked evidence item in place; returns counts per category.
    `extra` evidence-shaped items ({"url", "claim"}, e.g. follow-up sources) are checked in the same pass."""
    sc = cfg.get("source_check") or {}
    fetch = fetch or make_fetcher(sc.get("timeout_seconds", 10))
    items = [e for k, s in scores.items() for e in (s.get("evidence") or [])] + list(extra or [])
    items = [e for e in items if e.get("url") and e.get("status") not in ("verified", "unverified", "unreachable")]
    urls = list(dict.fromkeys(e["url"] for e in items))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pages = dict(zip(urls, pool.map(fetch, urls)))
    for e in items:
        text, err = pages[e["url"]]
        if text is None:
            e["status"], e["check"] = "unreachable", err or "could not fetch"
        else:
            e["status"], e["check"] = check_claim(e.get("claim", ""), text)
    return summary(scores)


def summary(scores: dict) -> dict:
    out = {}
    for k, s in scores.items():
        ev = s.get("evidence") or []
        if ev:
            out[k] = {st: sum(1 for e in ev if e.get("status") == st) for st in ("verified", "unverified", "unreachable")}
    return out


def apply_caps(scores: dict, cfg: dict) -> list[str]:
    """Cap news categories that score high on too few checkable sources. Returns the capped keys."""
    sc = cfg.get("source_check") or {}
    cap_from, min_ok, cap_to = sc.get("cap_from", 2), sc.get("min_sources", 2), sc.get("cap_to", 1)
    capped = []
    for k in scoring.NEWS_CATEGORIES:
        s = scores.get(k)
        if not s or s["score"] < cap_from:
            continue
        ev = s.get("evidence") or []
        ok = sum(1 for e in ev if e.get("status") in OK_STATUSES)
        if ok < min_ok:
            s["score_before_cap"] = s["score"]
            s["score"] = cap_to
            s["reasons"].append(f"Capped at {cap_to} (was {s['score_before_cap']}): only {ok} of {len(ev)} "
                                f"sources verified or unreachable; a score of {cap_from} or more needs {min_ok}.")
            capped.append(k)
    return capped
