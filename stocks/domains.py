"""A company's own web domains count as primary-tier sources for its own stock (stock module only; the capex
pipeline's source_tiers are unchanged).

For one ticker: the hosts of SEC submissions "website" / "investorWebsite" (empty for many companies), plus
config company_domains.tickers (curated), plus a strict match on the sites actually cited: a site whose
registered domain's second-level label equals the ticker (3+ characters) or the first word of the company
name (4+ letters, not a generic word), unless company_domains.exclude lists it (shop.com is not Shopify's,
alphabet.com not Alphabet's). The domains go on the primary list of a per-ticker copy of the config, which
cew.sources.tier reads as for any domain (subdomains included). Never another company's domains: nothing is
added for any ticker but the one being rated.
"""
from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

from cew import sources

# second-level labels under a two-letter country code that are part of the suffix (example.co.uk)
SUFFIX_LABELS = {"co", "com", "net", "org", "gov", "ac", "edu", "ne", "or", "gob"}
GENERIC = {"the", "first", "american", "united", "general", "national", "international", "global", "new",
           "great", "best", "big", "home", "group", "holdings", "company", "corp", "inc", "trust", "bank",
           "capital", "energy", "health", "data", "digital", "technologies", "systems", "solutions"}
MIN_TICKER, MIN_WORD = 3, 4


def host_of(url: str | None) -> str | None:
    u = (url or "").strip()
    if not u:
        return None
    return (urlsplit(u if "://" in u else "https://" + u).hostname or "").lower().removeprefix("www.").rstrip(".") or None


def registered(host: str | None) -> tuple[str, str] | None:
    """(second-level label, registered domain): "investor.msgsports.com" -> ("msgsports", "msgsports.com"),
    "news.example.co.uk" -> ("example", "example.co.uk")."""
    labels = [x for x in (host or "").lower().strip(".").split(".") if x]
    if len(labels) < 2:
        return None
    n = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SUFFIX_LABELS else 2
    if len(labels) < n:
        return None
    return labels[-n], ".".join(labels[-n:])


def _settings(cfg: dict) -> dict:
    return cfg.get("company_domains") or {}


def first_word(name: str | None) -> str | None:
    """The company name's first word, lower case, letters and digits only ("NetApp, Inc." -> "netapp")."""
    w = re.sub(r"[^a-z0-9]", "", ((name or "").split() or [""])[0].lower())
    return w or None


def company_domains(ticker: str, subs: dict | None, cfg: dict) -> list[str]:
    """The ticker's own domains: SEC's website / investorWebsite hosts and config company_domains.tickers."""
    out = [h for h in (host_of((subs or {}).get(k)) for k in ("website", "investorWebsite")) if h]
    out += [str(d).lower().strip(".") for d in ((_settings(cfg).get("tickers") or {}).get(ticker) or [])]
    return list(dict.fromkeys(d for d in out if d))


def own_domain(host: str | None, ticker: str, name: str | None, cfg: dict) -> str | None:
    """The registered domain of `host` when it strictly matches this company (second-level label equal to the
    ticker, or to the first word of its name), else None."""
    reg = registered(host)
    if not reg:
        return None
    label, domain = reg
    if domain in {str(d).lower() for d in (_settings(cfg).get("exclude") or [])}:
        return None
    word = first_word(name)
    tick = re.sub(r"[^a-z0-9]", "", ticker.lower())
    if (len(tick) >= MIN_TICKER and label == tick) or \
            (word and len(word) >= MIN_WORD and word not in GENERIC and not word.isdigit() and label == word):
        return domain
    return None


def tiers(cfg: dict, ticker: str, name: str | None, subs: dict | None, urls=()) -> dict:
    """A copy of cfg whose source_tiers.primary also lists this ticker's own domains: `company_domains` and
    the strict matches (`own_domain`) among `urls`."""
    t = copy.deepcopy(cfg)
    primary = t.setdefault("source_tiers", {}).setdefault("primary", [])
    for d in company_domains(ticker, subs, cfg):
        if d not in primary:
            primary.append(d)
    return with_matches(t, urls, ticker, name)


def with_matches(tcfg: dict, urls, ticker: str, name: str | None) -> dict:
    """`tcfg` with the strict matches among `urls` added to its primary list. In place; returns it."""
    primary = tcfg.setdefault("source_tiers", {}).setdefault("primary", [])
    for u in urls or ():
        d = own_domain(host_of(u), ticker, name, tcfg)
        if d and d not in primary:
            primary.append(d)
    return tcfg


def urls_in(text: str | None) -> list[str]:
    return sources.URL.findall(text or "")
