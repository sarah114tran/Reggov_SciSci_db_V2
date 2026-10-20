"""
loaders/research_domain_reclassification.py
Reclassifies comment_citations rows from publisher/journal domains to
cited_source_catagory == 'research_article', regardless of their current
label.

The linkClassifier that produced final_citation_data.json labels any domain
in the news-reliability CSV (main_2022-11-08.csv) as 'news_article'. That
CSV rates any content publisher, not just news outlets, so journals like
nature.com and pnas.org (and every ieee.org subsite) came out as news.

In scope: every host under RESEARCH_DOMAINS (matched on domain_registered,
so subdomains like ieeexplore.ieee.org / standards.ieee.org are included),
except EXCLUDED_HOSTS — spectrum.ieee.org is IEEE Spectrum, a magazine,
and keeps its existing label.

Runs after load_citation_domain_org, which fills domain_registered.
"""
from collections import Counter
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from models import CommentCitation

RESEARCH_DOMAINS = {"ieee.org", "nature.com", "pnas.org"}
EXCLUDED_HOSTS = {"spectrum.ieee.org"}
CLASSIFIER_TAG = "research_domain_override"


def _host(url: str | None) -> str:
    if not url:
        return ""
    netloc = urlparse(url.strip()).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def load_research_domain_reclassification(session: Session) -> None:
    rows = (
        session.query(CommentCitation)
        .filter(CommentCitation.domain_registered.in_(RESEARCH_DOMAINS))
        .all()
    )

    updated = 0
    already_research = 0
    excluded = 0
    by_prev_label: Counter = Counter()
    by_domain: Counter = Counter()

    for citation in rows:
        if _host(citation.citation_link) in EXCLUDED_HOSTS:
            excluded += 1
            continue

        if citation.cited_source_catagory == "research_article":
            already_research += 1
            continue

        by_prev_label[citation.cited_source_catagory] += 1
        by_domain[citation.domain_registered] += 1
        citation.cited_source_catagory = "research_article"
        citation.classifier_used = CLASSIFIER_TAG
        updated += 1

        if updated % 500 == 0:
            session.commit()

    session.commit()
    print(
        f"[research_domain_reclassification] updated={updated}/{len(rows)}  "
        f"already_research={already_research}  excluded_hosts={excluded}"
    )
    print(f"  by previous label: {dict(by_prev_label)}")
    print(f"  by domain:         {dict(by_domain)}")


if __name__ == "__main__":
    import db as _db

    session = _db.Session()
    load_research_domain_reclassification(session)
    session.close()
