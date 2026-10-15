"""A company's own domains count as primary for its own stock only (stocks/domains.py)."""
import pytest

from cew import sources
from stocks import config, domains

CFG = config.load()
NAMES = {"ZS": "Zscaler, Inc.", "SHOP": "SHOPIFY INC.", "MSGS": "Madison Square Garden Sports Corp.",
         "GOOGL": "Alphabet Inc.", "GTLB": "Gitlab Inc.", "INOD": "INNODATA INC", "RBRK": "Rubrik, Inc.",
         "NTAP": "NetApp, Inc.", "META": "Meta Platforms, Inc.", "TIC": "TIC Solutions, Inc."}


def tier(ticker, url, subs=None, cfg=CFG):
    return sources.tier(url, domains.tiers(cfg, ticker, NAMES.get(ticker, ticker), subs or {}, [url]))


@pytest.mark.parametrize("ticker, url", [
    ("ZS", "https://www.zscaler.com/press/zscaler-reports-results"),        # tagged "other" before
    ("SHOP", "https://www.shopify.com/news/shopify-editions"),
    ("MSGS", "https://investor.msgsports.com/news-releases/x"),
    ("GTLB", "https://about.gitlab.com/press/releases/x"),
    ("INOD", "https://investor.innodata.com/news/x"),
    ("RBRK", "https://www.rubrik.com/company/newsroom/x"),
])
def test_a_companys_own_site_is_primary_for_its_own_stock(ticker, url):
    assert tier(ticker, url) == "primary"
    assert tier("ORCL", url) == "other"  # never for another company's stock


def test_the_config_map_covers_the_rated_tickers_and_never_lists_a_domain_twice():
    m = CFG["company_domains"]["tickers"]
    assert set(m) == {"GOOGL", "GTLB", "INOD", "META", "MSFT", "MSGS", "NTAP", "NVDA", "ORCL", "PLTR", "RBRK", "SHOP", "TIC", "ZS"}
    owners = {}
    for t, ds in m.items():
        for d in ds:
            assert d not in owners, f"{d} listed for {owners.get(d)} and {t}"
            owners[d] = t
    assert set(CFG["company_domains"]["exclude"]) >= {"shop.com", "alphabet.com", "madison.com"}


def test_sec_website_fields_come_first_and_any_subdomain_counts():
    subs = {"website": "https://www.acme-widgets.com", "investorWebsite": "investors.acmewidgets.io"}
    assert domains.company_domains("ACME", subs, CFG) == ["acme-widgets.com", "investors.acmewidgets.io"]
    assert tier("ACME", "https://news.acme-widgets.com/x", subs) == "primary"
    assert tier("ACME", "https://investors.acmewidgets.io/q3", subs) == "primary"
    assert domains.company_domains("ZS", {}, CFG) == ["zscaler.com"]


@pytest.mark.parametrize("ticker, name, url, primary", [
    ("NEWCO", "Acmeware Corp.", "https://ir.acmeware.com/x", True),           # first word of the name
    ("NEWCO", "Acmeware Corp.", "https://acmeware.co.uk/x", True),            # any country suffix
    ("ACMW", "Acmeware Corp.", "https://www.acmw.com/x", True),               # the ticker
    ("ACMW", "Acmeware Corp.", "https://acmewarenews.com/x", False),          # a longer label isn't a match
    ("ACMW", "Acmeware Corp.", "https://acmeware.example.com/x", False),      # a subdomain of someone else's site
    ("SHOP", "SHOPIFY INC.", "https://www.shop.com/deals", False),            # the ticker's .com is someone else's
    ("GOOGL", "Alphabet Inc.", "https://www.alphabet.com/en-gb", False),      # BMW's leasing arm, not Alphabet's
    ("MSGS", "Madison Square Garden Sports Corp.", "https://madison.com/news", False),  # a Wisconsin news site
    ("ZS", "Zscaler, Inc.", "https://zs.com/x", False),                       # two-letter tickers don't match
    ("TIC", "TIC Solutions, Inc.", "https://www.tic.com/x", True),            # a three-letter ticker does
    ("FGI", "First Group Inc.", "https://www.first.com/x", False),            # generic first words don't
    ("NTAP", "NetApp, Inc.", "https://www.netapp.com/x", True),               # punctuation dropped from the name
])
def test_strict_match_on_the_ticker_or_the_first_word_of_the_name(ticker, name, url, primary):
    cfg = {**CFG, "company_domains": {**CFG["company_domains"], "tickers": {}}}  # the strict match alone
    got = sources.tier(url, domains.tiers(cfg, ticker, name, {}, [url]))
    assert (got == "primary") is primary, (ticker, url, got)


def test_registered_domain():
    assert domains.registered("investor.msgsports.com") == ("msgsports", "msgsports.com")
    assert domains.registered("news.example.co.uk") == ("example", "example.co.uk")
    assert domains.registered("localhost") is None and domains.registered(None) is None
    assert domains.first_word("NetApp, Inc.") == "netapp" and domains.first_word("") is None


def test_research_notes_list_the_companys_own_site_as_primary():
    tc = domains.with_matches(domains.tiers(CFG, "ZS", NAMES["ZS"], {}),
                              domains.urls_in("See https://www.zscaler.com/press/a and https://benzinga.com/b"), "ZS", NAMES["ZS"])
    by = sources.hosts_by_tier("See https://www.zscaler.com/press/a and https://benzinga.com/b", tc)
    assert by["primary"] == ["zscaler.com"] and by["other"] == ["benzinga.com"]
