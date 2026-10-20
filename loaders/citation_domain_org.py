"""
loaders/citation_domain_org.py
Fills comment_citations.domain_registered and domain_org — normalized forms
of the cited site's domain, alongside the existing (inconsistently cleaned)
`domain` column, which is left untouched.

  domain_registered – the registrable domain per the Public Suffix List
                      (tldextract's bundled snapshot, so builds are
                      reproducible offline): cloud.google.com -> google.com,
                      bidenwhitehouse.archives.gov -> archives.gov,
                      ox.ac.uk -> ox.ac.uk, www.gov.uk -> gov.uk.
  domain_org        – a short organization name to group by:
                      cloud.google.com, blog.google, googleblog.com -> google.
                      Resolved in order:
                        1. HOST_ALIASES   (host-suffix overrides)
                        2. DOMAIN_ALIASES (registrable-domain overrides)
                        3. brand TLD      (deepmind.google -> google)
                        4. LABEL_ALIASES  (same org across TLDs: google.fr)
                        5. the registrable domain's label (openai.com -> openai),
                           unless that label is shared by different
                           registrable domains in the table (nsf.gov vs
                           nsf.org, state.gov vs state.ny.us) — then the full
                           registrable domain is kept so they stay distinct.

The host comes from citation_link, falling back to uncleaned_domain and then
domain when the link is truncated or malformed (e.g. "https://papers.ssrn").
Rows with no parseable host get NULL in both columns.

Run after every pass that rewrites citation_link/domain.
"""
from collections import defaultdict
from urllib.parse import urlparse

import tldextract
from sqlalchemy.orm import Session

from models import CommentCitation

# suffix_list_urls=() -> use the PSL snapshot bundled with tldextract, never fetch.
_extract = tldextract.TLDExtract(suffix_list_urls=())

# Brand top-level domains owned by one organization: <anything>.google -> google.
BRAND_TLDS = {
    "google", "youtube", "microsoft", "apple", "amazon", "aws", "ibm",
    "intel", "nvidia", "cern", "kpmg", "bbc", "sony", "deloitte", "pwc",
}

# Host suffix -> org, checked before anything else.
HOST_ALIASES = {
    "whitehouse.archives.gov": "whitehouse",   # trump/obama/bidenwhitehouse.archives.gov
    "clintonwhitehouse4.archives.gov": "whitehouse",
    "clintonwhitehouse5.archives.gov": "whitehouse",
}

# Registrable domain -> org, for sibling domains of one organization.
DOMAIN_ALIASES = {
    "googleblog.com": "google",
    "googleapis.com": "google",
    "googleusercontent.com": "google",
    "googlecloudpresscorner.com": "google",
    "withgoogle.com": "google",
    "deepmind.com": "google",
    "youtu.be": "youtube",
    "fb.com": "meta",
    "amazonaws.com": "amazon",
    "aboutamazon.com": "amazon",
    "awsstatic.com": "amazon",
    "cdn-apple.com": "apple",
    "hf.co": "huggingface",
    "springeropen.com": "springer",
    "sciencedirectassets.com": "sciencedirect",
}

# Labels known to be one organization across TLDs (google.com / google.fr,
# github.com / github.io). Exempts them from the collision fallback in step 5.
LABEL_ALIASES = {
    "google", "github", "amazon", "mozilla", "oecd", "bbc", "wired",
    "politico", "caidp", "intel", "artificialintelligenceact", "truepic",
    "urldefense", "webflow", "setda", "queerinai", "goodjudgment",
    "controlai", "readthedocs", "libertas", "fairlearn", "aiworld", "wandb",
    "useplus",
}


def _host(value: str | None) -> str | None:
    if not value:
        return None
    if "://" not in value:
        value = "//" + value
    try:
        host = urlparse(value.strip()).hostname
    except ValueError:
        return None
    return host.removeprefix("www.") if host else None


def _registered(host: str | None) -> tuple[str, str, str] | None:
    """(host, registrable domain, label) or None if the host has no known suffix."""
    if not host:
        return None
    r = _extract(host)
    if not r.suffix:
        return None
    if not r.domain:
        # host is itself a public suffix (e.g. gov.uk after stripping www.)
        return host, r.suffix, r.suffix
    return host, f"{r.domain}.{r.suffix}", r.domain


def _org_before_collisions(host: str, registered: str, label: str) -> tuple[str, bool]:
    """(org, is_final). is_final=False means step 5 may still replace it."""
    for suffix, org in HOST_ALIASES.items():
        if host == suffix or host.endswith("." + suffix):
            return org, True
    if registered in DOMAIN_ALIASES:
        return DOMAIN_ALIASES[registered], True
    tld = registered.rsplit(".", 1)[-1]
    if tld in BRAND_TLDS:
        return tld, True
    if label in LABEL_ALIASES:
        return label, True
    return label, False


def load_citation_domain_org(session: Session) -> None:
    citations = session.query(CommentCitation).all()

    parsed: dict[str, tuple[str, str, str]] = {}
    for c in citations:
        for source in (c.citation_link, c.uncleaned_domain, c.domain):
            hit = _registered(_host(source))
            if hit:
                parsed[c.citation_id] = hit
                break

    # Labels claimed by more than one registrable domain (after aliasing).
    label_domains: dict[str, set[str]] = defaultdict(set)
    provisional: dict[str, tuple[str, bool]] = {}
    for cid, (host, registered, label) in parsed.items():
        org, final = _org_before_collisions(host, registered, label)
        provisional[cid] = (org, final)
        if not final:
            label_domains[org].add(registered)

    filled = collided = 0
    for c in citations:
        hit = parsed.get(c.citation_id)
        if not hit:
            c.domain_registered = None
            c.domain_org = None
            continue
        _, registered, _ = hit
        org, final = provisional[c.citation_id]
        if not final and len(label_domains[org]) > 1:
            org = registered
            collided += 1
        c.domain_registered = registered
        c.domain_org = org
        filled += 1

    session.commit()
    print(
        f"[citation_domain_org] filled={filled}  "
        f"kept_full_domain_on_label_collision={collided}  "
        f"no_host={len(citations) - filled}"
    )
