"""
loaders/citation_reclassification.py
Backfills comment_citations.cited_source_catagory (and citation_link/domain,
where the source provides them) from the high-confidence Claude
classification CSVs in citations/citation_bib_data/output/ — a human-reviewed
second pass over the original claude_classified_citations_unverified.json
predictions (see citation_classification_backfill.py), including the
academic rows that pass explicitly skipped pending OpenAlex verification.

citation_id in these CSVs is generated with the same
make_id("gemini_citation"/"gemini_unmatched", custom_id, idx) scheme as
db/loaders/gemini_citations.py, so it already lines up with
comment_citations.citation_id — no re-keying needed.

Each CSV's `metadata` column holds a Python-dict repr (not JSON — parsed
with ast.literal_eval), e.g.:
  {'doi': ..., 'title': ..., 'authors': [...], 'publication_date': ...,
   'url': ..., 'domain': ...}

Sources -> target cited_source_catagory (matching the category strings
already in use elsewhere in this table):
  claude_acedemic_high_confidence.csv           -> research_article
  claude_gov_high_confidence.csv                -> government_resource  (+ url/domain)
  claude_website_high_confidence.csv            -> website              (+ url/domain)
  claude_non_profit_high_confidence.csv         -> website              (+ url/domain)
  claude_industry_report_high_confidence.csv    -> website              (+ url/domain)
  claude_legal_high_confidence.csv              -> legal_citation

All fields are overwritten unconditionally — these are human-reviewed
high-confidence corrections, not a fallback pass that should defer to
whatever classification already happens to be sitting on the row.

load_openalex_matched_reclassification() below runs a second, independent
pass: every citation_id in merged_tiers_openalex_matched.json with a
confirmed OpenAlex match also gets set to research_article. That file mixes
two row shapes from different matching tiers (see
citations/citation_parsing/build_resolved_works_input.py for the
authoritative version of this logic):
  - "flat" rows (embedded_doi/arxiv/crossref-query tier): carry an "extra"
    field; all such rows in this file are confirmed matches.
  - "nested" rows (openalex-title-search tier): carry the raw OpenAlex work
    under "openalex", and confirm via match_reason
    (matched_exact/matched_fuzzy/matched_prefix) rather than a "status"
    field — a match_reason of "no_title" or "no_match_confirmed:..." means
    no confident match was actually found and the row is skipped.

load_news_reclassification() below is a third, independent pass over
claude_news_high_confidence_verified.csv — a human-verification pass over
the news_article predictions (rows verification downgraded are marked
classification="unclassified" there and are skipped; only classification==
"news_article" rows are applied here). Its url/domain resolution already
picks the right source per row via `resolved_url_source`:
  - "metadata": url/domain come from the `metadata` column, same
    ast.literal_eval'd dict shape as the other CSVs.
  - "corrected_url_and_domain": a human correction overriding metadata's
    (wrong or missing) url/domain, preferred whenever present. It's not
    valid Python/JSON (bare unquoted values, e.g.
    "[{'url': https://.../, 'domain': example.com}]"), so it's parsed with
    a regex instead of ast.literal_eval.

load_unclassified_cleanup() below is a fourth, independent pass — a manual-
review correction over rows still sitting at cited_source_catagory ==
"unclassified" (run after the passes above, since those already resolve the
rows they cover to something else). Two fixes, found by manually reviewing
the unclassified set:
  1. A citation_link was already extracted for the row (or one can be found
     in its footnote/reference text) but it was never classified as a
     website. This corpus's OCR frequently mangles the "https" scheme —
     observed variants include htps/hfps/hwps/hmps/hlps/ttps (a dropped or
     misread second "t") — so a plain "https?://" regex misses most of
     these; MALFORMED_URL_PATTERN also matches the corrupted schemes, and
     the scheme is normalized back to "https" when building citation_link.
  2. A U.S.C./C.F.R./"§" citation (e.g. "C.F.R. pt. 30, App. B.",
     "U.S.C. § 1702(a)(1).") sitting in citation_footnote_text or
     citation_reference_text/unmatched_citation_reference_text was never
     classified as legal_citation. Deliberately checked only against those
     three fields — not citation_sentence, which is the surrounding prose
     of the comment and produces false positives (e.g. "U.S. IaaS
     providers" is not a legal citation).
Rows matching neither check are left unclassified.
"""

import ast
import json
import re
from pathlib import Path
from urllib.parse import urlparse
import pandas as pd
from sqlalchemy.orm import Session
from models import CommentCitation

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "citation_reclassification"
MERGED_TIERS_PATH = OUTPUT_DIR / "merged_tiers_openalex_matched.json"
NEWS_VERIFIED_CSV = OUTPUT_DIR / "claude_news_high_confidence_verified.csv"

# (csv filename, target cited_source_catagory, propagate url/domain from metadata)
SOURCES = [
    ("claude_acedemic_high_confidence.csv",       "research_article",    False),
    ("claude_gov_high_confidence.csv",             "government_resource", True),
    ("claude_website_high_confidence.csv",         "website",              True),
    ("claude_non_profit_high_confidence.csv",      "website",              True),
    ("claude_industry_report_high_confidence.csv", "website",              True),
    ("claude_legal_high_confidence.csv",           "legal_citation",       False),
]

CLASSIFIER_TAG = "claude_high_confidence"


def _parse_metadata(metadata_str) -> dict:
    if pd.isna(metadata_str):
        return {}
    try:
        return ast.literal_eval(metadata_str)
    except (ValueError, SyntaxError):
        return {}


def load_citation_reclassification(session: Session, output_dir: Path = OUTPUT_DIR) -> None:
    updated = 0
    citations_not_found = 0

    for filename, category, propagate_link in SOURCES:
        csv_path = output_dir / filename
        df = pd.read_csv(csv_path)
        file_updated = 0

        for _, row in df.iterrows():
            citation = session.get(CommentCitation, str(row["citation_id"]))
            if citation is None:
                citations_not_found += 1
                continue

            citation.cited_source_catagory = category
            citation.classifier_used = CLASSIFIER_TAG

            if propagate_link:
                metadata = _parse_metadata(row.get("metadata"))
                url = metadata.get("url")
                domain = metadata.get("domain")
                if url:
                    citation.citation_link = url
                if domain:
                    citation.domain = domain

            updated += 1
            file_updated += 1
            if updated % 500 == 0:
                session.commit()

        print(f"  [citation_reclassification] {filename}: {file_updated}/{len(df)} rows -> {category}")

    session.commit()
    print(
        f"[citation_reclassification] updated={updated}  citations_not_found={citations_not_found}"
    )


def _is_confirmed_openalex_match(row: dict) -> bool:
    if row.get("extra"):
        return True
    match_reason = row.get("match_reason") or ""
    return not (match_reason.startswith("no_match_confirmed") or match_reason == "no_title")


def load_openalex_matched_reclassification(
    session: Session, merged_tiers_path: Path = MERGED_TIERS_PATH
) -> None:
    with open(merged_tiers_path, encoding="utf-8") as f:
        rows = json.load(f)

    updated = 0
    skipped_unconfirmed = 0
    citations_not_found = 0

    for row in rows:
        if not _is_confirmed_openalex_match(row):
            skipped_unconfirmed += 1
            continue

        citation = session.get(CommentCitation, row.get("citation_id"))
        if citation is None:
            citations_not_found += 1
            continue

        citation.cited_source_catagory = "research_article"
        citation.classifier_used = "openalex_confirmed_match"
        updated += 1

        if updated % 500 == 0:
            session.commit()

    session.commit()
    print(
        f"[openalex_matched_reclassification] updated={updated}  "
        f"skipped_unconfirmed={skipped_unconfirmed}  citations_not_found={citations_not_found}"
    )


CORRECTED_URL_DOMAIN_PATTERN = re.compile(r"'url':\s*(.+?),\s*'domain':\s*([^\]}]+)")


def _resolve_news_url_domain(row: pd.Series) -> tuple[str | None, str | None]:
    source = row.get("resolved_url_source")

    if source == "corrected_url_and_domain":
        match = CORRECTED_URL_DOMAIN_PATTERN.search(row.get("corrected_url_and_domain") or "")
        if not match:
            return None, None
        return match.group(1).strip(), match.group(2).strip().rstrip("}")

    if source == "metadata":
        metadata = _parse_metadata(row.get("metadata"))
        return metadata.get("url"), metadata.get("domain")

    return None, None


def load_news_reclassification(session: Session, csv_path: Path = NEWS_VERIFIED_CSV) -> None:
    df = pd.read_csv(csv_path)
    news_df = df[df["classification"] == "news_article"]

    updated = 0
    citations_not_found = 0

    for _, row in news_df.iterrows():
        citation = session.get(CommentCitation, str(row["citation_id"]))
        if citation is None:
            citations_not_found += 1
            continue

        citation.cited_source_catagory = "news_article"
        citation.classifier_used = CLASSIFIER_TAG

        url, domain = _resolve_news_url_domain(row)
        if url:
            citation.citation_link = url
        if domain:
            citation.domain = domain

        updated += 1
        if updated % 500 == 0:
            session.commit()

    session.commit()
    print(
        f"[news_reclassification] {csv_path.name}: updated={updated}/{len(news_df)}  "
        f"citations_not_found={citations_not_found}"
    )


CFR_USC_PATTERN = re.compile(r"\bU\.?\s?S\.?\s?C\.?\b|\bC\.?\s?F\.?\s?R\.?\b|§", re.IGNORECASE)
MALFORMED_URL_PATTERN = re.compile(
    r"\b(?:https?|h[tfwml]t?ps?|ttps?)://[^\s\)\]\}\"'>,]+", re.IGNORECASE
)
URL_SCHEME_PATTERN = re.compile(r"^[a-zA-Z]+(?=://)")

# Matches the domains already in use under the existing social_media category
# elsewhere in this table, plus common platforms/shorteners not yet present.
SOCIAL_MEDIA_DOMAINS = {
    "twitter.com", "x.com", "t.co", "facebook.com", "linkedin.com", "lnkd.in",
    "youtube.com", "youtu.be", "blog.youtube", "reddit.com", "instagram.com",
    "tiktok.com", "threads.net", "bsky.app", "mastodon.social",
}


def _extract_and_normalize_url(text: str) -> tuple[str | None, str | None]:
    match = MALFORMED_URL_PATTERN.search(text or "")
    if not match:
        return None, None
    # \x11 (DC1) stands in for a lost "ti" ligature from PDF text extraction
    # elsewhere in this corpus (see open_alex_api.py:_clean_for_search) —
    # same artifact shows up inside extracted URLs (e.g. "pos\x11ndustria.com"
    # -> "postindustria.com").
    raw = match.group(0).replace("\x11", "ti")
    normalized = URL_SCHEME_PATTERN.sub("https", raw, count=1).rstrip(".,;:")
    domain = urlparse(normalized).netloc.lower().removeprefix("www.") or None
    return normalized, domain


def _domain_from_link(citation_link: str) -> str | None:
    return urlparse(citation_link).netloc.lower().removeprefix("www.") or None


def load_unclassified_cleanup(session: Session) -> None:
    citations = (
        session.query(CommentCitation)
        .filter(CommentCitation.cited_source_catagory == "unclassified")
        .all()
    )

    website_via_link = 0
    website_via_extracted_url = 0
    social_media = 0
    legal = 0

    def _classify_link(citation: CommentCitation, domain: str | None, classifier_tag: str) -> str:
        if (domain or "").lower() in SOCIAL_MEDIA_DOMAINS:
            citation.cited_source_catagory = "social_media"
            citation.classifier_used = "manual_review_social_domain"
            return "social_media"
        citation.cited_source_catagory = "website"
        citation.classifier_used = classifier_tag
        return "website"

    for citation in citations:
        reference_text = " ".join(
            t for t in (
                citation.citation_footnote_text,
                citation.citation_reference_text,
                citation.unmatched_citation_reference_text,
            ) if t
        )

        if citation.citation_link:
            if not citation.domain:
                citation.domain = _domain_from_link(citation.citation_link)
            outcome = _classify_link(citation, citation.domain, "manual_review_link_present")
            if outcome == "social_media":
                social_media += 1
            else:
                website_via_link += 1
            continue

        url, domain = _extract_and_normalize_url(reference_text)
        if url:
            citation.citation_link = url
            citation.domain = domain
            outcome = _classify_link(citation, domain, "manual_review_ocr_url_extracted")
            if outcome == "social_media":
                social_media += 1
            else:
                website_via_extracted_url += 1
            continue

        if CFR_USC_PATTERN.search(reference_text):
            citation.cited_source_catagory = "legal_citation"
            citation.classifier_used = "manual_review_cfr_usc_pattern"
            legal += 1

    session.commit()
    print(
        f"[unclassified_cleanup] website_via_link={website_via_link}  "
        f"website_via_extracted_url={website_via_extracted_url}  social_media={social_media}  "
        f"legal={legal}  reviewed={len(citations)}"
    )


if __name__ == "__main__":
    import argparse
    import db as _db

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", choices=["mysql", "sqlite"], default="sqlite",
                         help="Target database (default: sqlite)")
    args = parser.parse_args()
    _db.configure(args.db)

    session = _db.Session()
    load_citation_reclassification(session)
    load_openalex_matched_reclassification(session)
    load_news_reclassification(session)
    load_unclassified_cleanup(session)
    session.close()
