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

SKIPPED_TYPES = {"academic_article"}


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
        if predicted_type in SKIPPED_TYPES:
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
