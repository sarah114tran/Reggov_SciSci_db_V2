"""
loaders/citation_classification_backfill.py
Backfills comment_citations.cited_source_catagory from
claude_classified_citations_unverified.json — a re-classification pass over
citations the original link/text classifiers left as "unclassified" (or only
coarsely tagged "research_article").

citation_id in the source file is generated with the same
make_id("gemini_citation"/"gemini_unmatched", custom_id, idx) scheme as
db/loaders/gemini_citations.py, so it already lines up with
comment_citations.citation_id — no re-keying needed.

"academic_article" predictions are skipped: those 115 rows are still
pending a separate OpenAlex verification pass and should be loaded
(and classified) from that pass's output once it's ready, not from here.

predicted_type is this file's own 15-value taxonomy (government_report,
website_or_blog, nonprofit_or_ngo_report, academic_book_or_chapter,
conference_paper, working_paper_or_preprint, dataset,
dissertation_or_thesis, legal_or_regulatory_document,
industry_or_corporate_report, bare_author_year_fragment,
non_citation_footnote, other_or_unresolved, news_article,
academic_article) -- not the same controlled vocabulary as
cited_source_catagory elsewhere in this table (website, research_article,
government_resource, unclassified, news_article, legal_citation,
educational_institution_resource, social_media). Only "news_article"
happens to match both. Writing any other predicted_type straight into
cited_source_catagory mixes two incompatible taxonomies in the same
column, so -- same as academic_article -- every predicted_type other than
a genuine vocabulary match is held back here as unverified, pending a
real mapping/verification pass, rather than adopted as-is.

JSON structure:
[
  {
    "citation_id":    str,
    "sub_index":      int,
    "citation":       str,
    "predicted_type": str,   # e.g. "government_report", "news_article", ...
    "is_academic":    bool,
    "confidence":     str | null,
    "evidence":       str,
    ...
  },
  ...
]
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import CommentCitation

# cited_source_catagory's real controlled vocabulary (see models.py /
# citation_classifier.py). Anything else in predicted_type is this file's
# own, incompatible taxonomy and is held back, not adopted.
ALLOWED_TYPES = {
    "website", "research_article", "government_resource", "unclassified",
    "news_article", "legal_citation", "educational_institution_resource",
    "social_media",
}


def load_citation_classification_backfill(
    json_path: str | Path,
    session: Session,
) -> None:
    json_path = Path(json_path)
    with open(json_path, encoding="utf-8") as f:
        rows = json.load(f)

    updated          = 0
    skipped_pending  = 0
    citations_not_found = 0

    for row in rows:
        predicted_type = row.get("predicted_type")
        if predicted_type not in ALLOWED_TYPES:
            skipped_pending += 1
            continue

        citation = session.get(CommentCitation, row.get("citation_id"))
        if citation is None:
            citations_not_found += 1
            continue

        citation.cited_source_catagory = predicted_type
        citation.classifier_used = "claude_unverified"
        updated += 1

        if updated % 500 == 0:
            session.commit()

    session.commit()
    print(
        f"[citation_classification_backfill] updated={updated}  "
        f"skipped_pending={skipped_pending}  citations_not_found={citations_not_found}"
    )
