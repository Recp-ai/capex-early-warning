"""Form 4 XML in the source check (stocks/form4.py): the archived NVDA, NTAP and ZS insider items that failed on
raw SEC XML ("219.7200", <isDirector>1</isDirector>), checked again. SEC is mocked: the fixtures are Form 4
files in SEC's ownershipDocument schema carrying those filings' figures, and each one is first shown to give the
archived failure through cew.sources (the old path), then to verify on the normalised text."""
import copy
import json
from datetime import date

import pytest

import stock_fakes as F
from cew import sources, util
from stocks import config, form4, run

CFG = config.load()
ARCHIVE = util.ROOT / "data" / "stocks" / "runs"


def form4_xml(owner, roles, transactions, footnotes=(), title=None):
    """An SEC ownershipDocument: `roles` from director / officer / ten / other; transactions (date, code, shares,
    price) with prices written as SEC does ("219.7200")."""
    flag = lambda r: "1" if r in roles else "0"  # noqa: E731
    tx = "".join(f"""<nonDerivativeTransaction><securityTitle><value>Common Stock</value></securityTitle>
<transactionDate><value>{d}</value></transactionDate><transactionCoding><transactionFormType>4</transactionFormType>
<transactionCode>{code}</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
<transactionAmounts><transactionShares><value>{n}</value></transactionShares><transactionPricePerShare><value>{p:.4f}</value>
</transactionPricePerShare><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
<ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
</nonDerivativeTransaction>""" for d, code, n, p in transactions)
    notes = "".join(f'<footnote id="F{i}">{t}</footnote>' for i, t in enumerate(footnotes, 1))
    return f"""<?xml version="1.0"?>
<ownershipDocument><schemaVersion>X0508</schemaVersion><documentType>4</documentType>
<periodOfReport>{transactions[0][0]}</periodOfReport><issuer><issuerCik>0000000001</issuerCik><issuerName>ISSUER</issuerName></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0000000002</rptOwnerCik><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>{flag("director")}</isDirector><isOfficer>{flag("officer")}</isOfficer>
<isTenPercentOwner>{flag("ten")}</isTenPercentOwner><isOther>0</isOther>{f"<officerTitle>{title}</officerTitle>" if title else ""}
</reportingOwnerRelationship></reportingOwner><nonDerivativeTable>{tx}</nonDerivativeTable>
<footnotes>{notes}</footnotes><ownerSignature><signatureName>/s/ Attorney-in-fact</signatureName>
<signatureDate>2026-09-20</signatureDate></ownerSignature></ownershipDocument>"""


PLAN = "The sales reported were effected pursuant to a Rule 10b5-1 trading plan adopted by the reporting person on {}."
TAX = "Shares sold to cover tax withholding obligations upon the vesting of restricted stock units."
FILINGS = {  # the archived items' URLs (news runs of Oct 15) and the filings' content
    "NVDA": ("2026-10-15T063201Z.json", {
        "wk-form4_1790110579.xml": form4_xml("STEVENS MARK A", {"director"}, [
            ("2026-09-18", "S", 1356000, 219.72), ("2026-09-18", "S", 10000, 220.42)], ["Held by The Stevens Trust."]),
        "wk-form4_1790196985.xml": form4_xml("Teter Timothy S.", {"officer"}, [("2026-09-22", "S", 26000, 221.15)],
                                             [PLAN.format("May 22, 2026")], title="EVP, General Counsel and Secretary"),
    }),
    "NTAP": ("2026-10-15T072828Z.json", {
        "000119312526414223/ownership.xml": form4_xml("HELD GERALD", {"director"}, [("2026-10-01", "S", 5757, 212.0)]),
    }),
    "ZS": ("2026-10-15T072828Z.json", {
        "wk-form4_1789688713.xml": form4_xml("Rubin Kevin", {"officer"}, [("2026-09-16", "S", 5957, 192.76)], [TAX],
                                             title="Chief Financial Officer"),
        "wk-form4_1789688583.xml": form4_xml("Rich Michael", {"officer"}, [("2026-09-16", "S", 4108, 192.76)], [TAX],
                                             title="CRO"),
    }),
}


def archived(ticker):
    name, files = FILINGS[ticker]
    topics = json.loads((ARCHIVE / ticker / name).read_text())["news"]["topics"]
    items = [e for e in topics["insider_activity"]["evidence"] if form4.is_sec_xml(e.get("url"))
             and any(e["url"].endswith(k) for k in files)]
    routes = {e["url"]: F.FakeResponse(200, next(x for k, x in files.items() if e["url"].endswith(k))) for e in items}
    return topics, items, routes


@pytest.mark.parametrize("ticker", ["NVDA", "NTAP", "ZS"])
def test_archived_form4_items_verify_on_the_normalised_xml(ticker):
    topics, items, routes = archived(ticker)
    assert len(items) == {"NVDA": 2, "NTAP": 1, "ZS": 2}[ticker] and all(e["status"] == "unverified" for e in items)
    for e in items:  # the fixture reproduces the archived failure through cew.sources (plain text of the XML)
        old = sources.check_claim(e["claim"], util.html_to_text(routes[e["url"]].text))
        assert old == ("unverified", e["check"]), e["claim"]
    sess = F.FakeSession(routes)
    changed = form4.recheck(topics, CFG, sess)
    assert [c["now"] for c in changed] == ["verified"] * len(items)
    for e in items:
        assert e["status"] == "verified" and e["check"].endswith("· Form 4 XML normalised"), (e["claim"], e["check"])
    assert form4.recheck(topics, CFG, sess) == [] and len(sess.calls) == len(items)  # checked once, then left alone


def test_nvda_insider_rating_is_no_longer_capped_after_the_recheck():
    topics, _, routes = archived("NVDA")
    t = topics["insider_activity"]
    assert t["rating"] == 0 and t["rating_before_cap"] == -1 and t["cap_note"].endswith("0 of 2 sources verified or unreachable")
    form4.recheck(topics, CFG, F.FakeSession(routes))
    changed = run.recap(topics, "2026-10-15T06:32:01Z", date(2026, 10, 15), CFG)
    assert t["rating"] == -1 and "cap_note" not in t and not any(t.get(n) for n in ("weak_sources_note", "old_evidence_note"))
    assert {"topic": "insider_activity", "label": "Insider activity", "was": 0, "now": -1} in changed


def test_normalised_text_keeps_every_value_and_adds_what_the_tags_mean():
    xml = form4_xml("STEVENS MARK A", {"director", "ten"}, [("2026-09-18", "S", 1356000, 219.72), ("2026-09-18", "F", 50, 212.0),
                                                            ("2026-09-19", "M", 10, 1.5), ("2026-09-19", "P", 7, 10.0)],
                    [PLAN.format("May 22, 2026")])
    text = form4.ownership_text(xml)
    plain = util.html_to_text(xml)
    assert all(tok in text for tok in plain.split())  # nothing the plain text had is lost
    for word in ("219.72", "219.7200", "212\n", "1.5", "Director", "10% owner", "sale", "tax withholding", "exercise",
                 "purchase", "2026-05-22"):
        assert word in text, word
    assert "Officer" not in text.split("\n")  # flags that are 0 add nothing
    officer = form4.ownership_text(form4_xml("Rubin Kevin", {"officer"}, [("2026-09-16", "S", 1, 1.0)], title="Chief Financial Officer"))
    assert "Officer (Chief Financial Officer)" in officer
    assert form4.ownership_text("<html><body>not a form</body></html>") is None and form4.ownership_text("<<<") is None
    assert form4.trim_zeros("$219.7200 and $212.0000, 1,356,000 at 220.42 or 10.50; 2026-05-22") == \
        "$219.72 and $212, 1,356,000 at 220.42 or 10.5; 2026-05-22"
    assert form4.iso_dates("adopted on May 22, 2026 and February 30, 2026") == ["2026-05-22"]


def test_fresh_runs_read_sec_xml_raw_and_everything_else_through_cew(monkeypatch):
    url = "https://www.sec.gov/Archives/edgar/data/0001045810/000119903926000016/wk-form4_1790110579.xml"
    other = "https://www.sec.gov/Archives/edgar/data/1594805/000159480526000057/primary_doc.xml"
    topics = {"insider_activity": {"rating": -1, "evidence": [
        {"claim": "Director Mark Stevens sold 1,356,000 shares at $219.72.", "url": url},
        {"claim": "Tobias Finkelstein filed a Form 144.", "url": other},
        {"claim": "Reuters says NVIDIA sold chips.", "url": "https://www.reuters.com/x"},
        {"claim": "A missing filing.", "url": url.replace("1790110579", "404")}]}}
    sess = F.FakeSession({url: F.FakeResponse(200, FILINGS["NVDA"][1]["wk-form4_1790110579.xml"]),
                          other: F.FakeResponse(200, "<edgarSubmission><name>Tobias Finkelstein</name><form>144</form></edgarSubmission>")})
    monkeypatch.setattr(sources, "make_fetcher", lambda *a, **k: lambda u: ("Reuters: NVIDIA sold chips.", None))
    counts = form4.check_evidence(topics, CFG, sess)
    ev = topics["insider_activity"]["evidence"]
    assert [e["status"] for e in ev] == ["verified", "verified", "verified", "unreachable"]
    assert ev[0]["check"].endswith("Form 4 XML normalised") and "normalised" not in ev[1]["check"]
    assert ev[3]["check"] == "HTTP 404" and counts == {"insider_activity": {"verified": 3, "unverified": 0, "unreachable": 1}}
    assert form4.is_sec_xml(url) and not form4.is_sec_xml("http://www.sec.gov/Archives/edgar/data/1/2/x.xml")
    assert not form4.is_sec_xml("https://evil.example/Archives/edgar/data/1/2/x.xml")


def test_sec_xml_fetch_follows_redirects_only_within_sec_gov():
    url = "https://www.sec.gov/Archives/edgar/data/1/2/ownership.xml"
    away = F.FakeResponse(302, "", {"Location": "https://evil.example/x.xml"})
    assert form4._get(url, F.FakeSession({url: away}), 5) == (None, "redirected away from sec.gov")
    inside = F.FakeResponse(301, "", {"Location": "https://sec.gov/Archives/edgar/data/1/2/ownership.xml"})
    target = "https://sec.gov/Archives/edgar/data/1/2/ownership.xml"
    raw, err = form4._get(url, F.FakeSession({url: inside, target: F.FakeResponse(200, "<ownershipDocument/>")}), 5)
    assert err is None and raw == b"<ownershipDocument/>"


def test_reuse_news_rechecks_archived_form4_items_and_says_so(sandbox, monkeypatch):
    from test_stock_pipeline import latest_run, setup
    setup(monkeypatch, sandbox)
    assert run.main(["MSFT"]) == 0
    old = latest_run()
    url = "https://www.sec.gov/Archives/edgar/data/789019/000119903926000016/wk-form4_1.xml"
    old["news"]["topics"]["insider_activity"].update(rating=-1, evidence=[
        {"claim": "Director Mark Stevens sold 1,356,000 shares at $219.72.", "url": url, "status": "unverified",
         "check": "numbers 1/2, names 2/3 · missing: 219.72, Director", "tier": "primary",
         "event_date": (util.today().replace(day=1)).isoformat()}])
    from stocks import publish
    publish.archive_run(copy.deepcopy(old))
    sess = util.session()
    sess.routes[url] = F.FakeResponse(200, FILINGS["NVDA"][1]["wk-form4_1790110579.xml"])
    F.fake_claude(monkeypatch, [F.writeup_reply()])
    assert run.main(["MSFT", "--reuse-news"]) == 0
    r = latest_run()
    ia = r["news"]["topics"]["insider_activity"]
    assert ia["evidence"][0]["status"] == "verified" and ia["rating"] == -1
    assert r["news"]["rechecked"] == [{"topic": "insider_activity", "label": "Insider activity",
                                       "claim": "Director Mark Stevens sold 1,356,000 shares at $219.72.",
                                       "was": "unverified", "now": "verified"}]
    assert r["news"]["source_check"]["form4_rechecked"] == 1
    assert r["news"]["source_check"]["counts"]["insider_activity"]["verified"] == 1
    from stocks import notify
    _, body_html, _ = notify.build_email(r, None)
    assert "Form 4 filings checked again on their XML: Insider activity: unverified → verified." in body_html
    assert "Form 4 filings checked again on their XML" in (util.ROOT / "site" / "stocks.html").read_text()
