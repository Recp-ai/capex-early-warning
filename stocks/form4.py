"""SEC Form 4 (and 3 / 5) XML for the stock module's source check.

cew.sources reads every cited page as plain text, which for a raw ownership XML file (sec.gov
.../ownership.xml, wk-form4_*.xml) leaves prices as "219.7200" and roles as a bare "1"
(<isDirector>1</isDirector>), so claims such as "Director Mark Stevens sold ... at $219.72" fail. Here such a
file is fetched raw from sec.gov (with the run's SEC session) and turned into text that keeps every value
and adds what the tags meant: numbers with trailing decimal zeros trimmed (the claim's numbers too), role
flags as words (Director, Officer + officerTitle, 10% owner), transaction codes S / P / M / F as sale /
purchase / exercise / tax withholding, and written dates in footnotes also as YYYY-MM-DD. The claim is then
checked with cew.sources.check_claim, as for any page. Any other page goes through cew.sources unchanged.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urljoin, urlsplit

from cew import sources, util

SEC_XML = re.compile(r"^https://(?:www\.)?sec\.gov/Archives/edgar/data/[^?#]+\.xml$", re.I)
SEC_HOSTS = ("www.sec.gov", "sec.gov")
ROLES = (("isDirector", "Director"), ("isOfficer", "Officer"), ("isTenPercentOwner", "10% owner"), ("isOther", "Other"))
CODES = {"S": "sale", "P": "purchase", "M": "exercise", "F": "tax withholding"}
TRAILING_ZEROS = re.compile(r"\b(\d+)\.(\d*[1-9])?0+\b")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
WRITTEN_DATE = re.compile(r"\b(" + "|".join(m.capitalize() for m in MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})\b")
NORMALISED = "Form 4 XML normalised"


def is_sec_xml(url: str | None) -> bool:
    return bool(SEC_XML.match(url or ""))


def trim_zeros(text: str) -> str:
    """Numbers without trailing decimal zeros: "219.7200" -> "219.72", "212.0000" -> "212"."""
    return TRAILING_ZEROS.sub(lambda m: m.group(1) + (f".{m.group(2)}" if m.group(2) else ""), text or "")


def iso_dates(text: str) -> list[str]:
    """Written dates ("May 22, 2026") as YYYY-MM-DD."""
    out = []
    for m in WRITTEN_DATE.finditer(text or ""):
        try:
            out.append(date(int(m.group(3)), MONTHS.index(m.group(1).lower()) + 1, int(m.group(2))).isoformat())
        except ValueError:
            continue
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _flag(el) -> bool:
    return el is not None and (el.text or "").strip().lower() in ("1", "true")


def ownership_text(raw: bytes | str) -> str | None:
    """The normalised text of an ownership document (Form 3 / 4 / 5 XML), or None for anything else."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    if _local(root.tag) != "ownershipDocument":
        return None
    values = [t for el in root.iter() for t in ((el.text or "").strip(), (el.tail or "").strip()) if t]
    words = []
    for rel in (el for el in root.iter() if _local(el.tag) == "reportingOwnerRelationship"):
        kids = {_local(k.tag): k for k in rel}
        for tag, word in ROLES:
            if _flag(kids.get(tag)):
                title = kids.get("officerTitle" if tag == "isOfficer" else "otherText")
                words.append(word + (f" ({title.text.strip()})" if title is not None and (title.text or "").strip() else ""))
    for el in root.iter():
        if _local(el.tag) == "transactionCode" and (el.text or "").strip().upper() in CODES:
            words.append(CODES[el.text.strip().upper()])
    plain = "\n".join(values)
    return "\n".join([plain, trim_zeros(plain), *words, *iso_dates(plain)])


def _get(url: str, sess, timeout: float) -> tuple[bytes | None, str | None]:
    """The raw file, following redirects only within sec.gov (https)."""
    for _ in range(4):
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in SEC_HOSTS:
            return None, "redirected away from sec.gov"
        try:
            r = sess.get(url, timeout=timeout, allow_redirects=False, stream=True)
        except Exception as e:
            return None, type(e).__name__
        try:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                if len(body) >= sources.MAX_BYTES:
                    break
            return body, None
        except Exception as e:
            return None, type(e).__name__
        finally:
            r.close()
    return None, "too many redirects"


def check_item(e: dict, sess, timeout: float = 10, recheck: bool = False) -> bool:
    """Check one evidence item citing a sec.gov XML file. An ownership document is checked on its normalised
    text with the claim's numbers trimmed alike; any other XML as cew.sources would read it. On a recheck
    (archived items) only an ownership document that loaded changes the status. Returns whether it did."""
    raw, err = _get(e["url"], sess, timeout)
    text = ownership_text(raw) if raw is not None else None
    if recheck and text is None:
        return False
    if raw is None:
        e["status"], e["check"] = "unreachable", err or "could not fetch"
    elif text is None:
        e["status"], e["check"] = sources.check_claim(e.get("claim", ""), util.html_to_text(raw.decode("utf-8", "replace")))
    else:
        status, detail = sources.check_claim(trim_zeros(e.get("claim", "")), text)
        e["status"], e["check"] = status, f"{detail} · {NORMALISED}"
    return True


def check_evidence(topics: dict, cfg: dict, sess=None) -> dict:
    """cew.sources.check_evidence for the topics, with sec.gov XML items read here first (with `sess`, the
    run's SEC session; without one they go through cew.sources like any page). Returns the counts."""
    if sess is not None:
        timeout = float((cfg.get("source_check") or {}).get("timeout_seconds", 10))
        for t in topics.values():
            for e in t.get("evidence") or []:
                if is_sec_xml(e.get("url")) and e.get("status") not in ("verified", "unverified", "unreachable"):
                    check_item(e, sess, timeout)
    return sources.check_evidence(topics, cfg)


def recheck(topics: dict, cfg: dict, sess) -> list[dict]:
    """--reuse-news: archived sec.gov XML items left unverified, checked again on their normalised text (the
    other source checks stay as archived). In place. Returns the ones whose status changed:
    [{"topic", "label", "claim", "was", "now"}]."""
    timeout = float((cfg.get("source_check") or {}).get("timeout_seconds", 10))
    changed = []
    for k, t in topics.items():
        for e in t.get("evidence") or []:
            if is_sec_xml(e.get("url")) and e.get("status") == "unverified" and NORMALISED not in (e.get("check") or ""):
                was = e["status"]
                if check_item(e, sess, timeout, recheck=True) and e["status"] != was:
                    changed.append({"topic": k, "label": t.get("label") or k, "claim": e.get("claim"), "was": was,
                                    "now": e["status"]})
    return changed
