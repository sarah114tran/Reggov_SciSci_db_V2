"""
loaders/edu_resources_reclassification.py
Corrects comment_citations rows that were falsely classified as
cited_source_catagory == 'educational_institution_resource' but are actually
research articles/working papers/reports, per a Claude re-review of the
"research-like link pattern" subset produced by
db/eda_output/find_research_like_educational_citations.py.

Source is citations/citation_parsing/final_output/edu_resources_classified.json
(59 rows). Only the 39 rows carrying a metadata_doi are in scope for this
pass — those DOIs were looked up against OpenAlex by
citations/citation_bib_data/open_alex_edu_reclassification.py, and every one
of the 39 returned a confirmed match. Per the existing convention for
OpenAlex-confirmed matches elsewhere in this table (see
load_openalex_matched_reclassification in citation_reclassification.py),
a confirmed match is reclassified to 'research_article'.

The other 20 rows in edu_resources_classified.json (no DOI: mostly
nonprofit_or_ngo_report / other_or_unresolved) are left untouched here —
out of scope for this pass.
"""
import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import CommentCitation

EDU_CLASSIFIED_PATH = (
    Path(__file__).parent.parent / "data" / "citation_reclassification" / "edu_resources_classified.json"
)
CLASSIFIER_TAG = "claude_edu_resources_openalex_confirmed"


def load_edu_resources_reclassification(
    session: Session, json_path: str | Path = EDU_CLASSIFIED_PATH
) -> None:
    with open(json_path, encoding="utf-8") as f:
        rows = json.load(f)

    rows_with_doi = [r for r in rows if r.get("metadata_doi")]

    updated = 0
    skipped_no_doi = 0
    not_educational = 0
    citations_not_found = 0

    for row in rows:
        if not row.get("metadata_doi"):
            skipped_no_doi += 1
            continue

        citation = session.get(CommentCitation, row.get("citation_id"))
        if citation is None:
            citations_not_found += 1
            continue

        if citation.cited_source_catagory != "educational_institution_resource":
            not_educational += 1
            continue

        citation.cited_source_catagory = "research_article"
        citation.classifier_used = CLASSIFIER_TAG
        updated += 1

        if updated % 500 == 0:
            session.commit()

    session.commit()
    print(
        f"[edu_resources_reclassification] updated={updated}/{len(rows_with_doi)}  "
        f"skipped_no_doi={skipped_no_doi}  not_educational={not_educational}  "
        f"citations_not_found={citations_not_found}"
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
    load_edu_resources_reclassification(session)
    session.close()
