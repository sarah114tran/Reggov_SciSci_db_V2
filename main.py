"""
main.py
Orchestrates the full ETL pipeline.

Edit the DATA_FILES dict at the top to point at your actual JSON files.
Any file path set to None will be skipped.
"""

from pathlib import Path
import db as _db
from db import init_db
from loaders.docket import load_fr_dockets
from loaders.comments import load_comments
from loaders.gemini_citations import load_gemini_citations
from loaders.citation_resolved_works import load_citation_resolved_works
from loaders.citation_classification_backfill import load_citation_classification_backfill
from loaders.citation_reclassification import (
    load_citation_reclassification,
    load_openalex_matched_reclassification,
    load_news_reclassification,
    load_unclassified_cleanup,
)
from loaders.edu_resources_reclassification import load_edu_resources_reclassification
from loaders.comment_authorship import load_comment_authorship
from loaders.comment_authors import load_comment_authors
from loaders.naics_classification import (
    load_comment_author_naics,
    load_public_comment_naics,
    propagate_first_author_org,
)
from loaders.additional_org_classification import load_additional_org_classification
from loaders.individual_reclassification import load_individual_reclassification
from models import CommentCitation

# ── Configure your file paths here ────────────────────────────────────────────
DATA_DIR = Path("data")

DATA_FILES = {
    "fr_reg_metadata":              DATA_DIR / "docket_metadata.json",
    "comments":                     DATA_DIR / "normalized_comment_metadata.json",
    "gemini_citations":             DATA_DIR / "citation_data" / "extracted_citations" / "final_citation_data.json",
    "citation_classification_backfill": DATA_DIR / "citation_data" / "claude_classified_citations_unverified.json",
    "citation_resolved_works":      DATA_DIR / "citation_data" / "citation_resolved_works_normalized.json",
    "authorship":                   DATA_DIR / "comment_authorship.json",
    "processed_authorship":         DATA_DIR / "processed_authorship.json",
    "processed_authorship_naics":   DATA_DIR / "processed_authorship_with_naics.json",
    "unique_orgs_naics":            DATA_DIR / "unique_orgs_with_naics.json",
    "additional_org_classification": DATA_DIR / "additional_classified_orgs_final.csv",
    "individual_reclassification":  DATA_DIR / "classified_all.json",
}

# If ALL comments in a single file belong to one docket, set this to the
# document_id / fr_number of that docket so the loader can resolve the FK
# without a per-row lookup. Leave as None if comments span multiple dockets.
DEFAULT_DOCKET_NATURAL_KEY: str | None = None


def _exists(key: str) -> bool:
    path = DATA_FILES[key]
    if path is None or not path.exists():
        print(f"  [main] Skipping '{key}' — file not found: {path}")
        return False
    return True


def run():
    print("── Initialising database ───────────────────────────────────────")
    init_db()

    session = _db.Session()

    # ── 1. FR Dockets ──────────────────────────────────────────────────────
    print("\n── Loading FR dockets ──────────────────────────────────────────")
    docket_key_map: dict[str, str] = {}
    if _exists("fr_reg_metadata"):
        docket_key_map = load_fr_dockets(DATA_FILES["fr_reg_metadata"], session)

    # Resolve default_docket_id if a natural key was provided
    default_docket_id = (
        docket_key_map.get(DEFAULT_DOCKET_NATURAL_KEY)
        if DEFAULT_DOCKET_NATURAL_KEY else None
    )

    # ── 3. Comments ────────────────────────────────────────────────────────
    print("\n── Loading comments ────────────────────────────────────────────")
    comment_key_map: dict[str, str] = {}
    if _exists("comments"):
        # Build supplemental lookup from filtered_comment_metadata.json for
        # organization_root_name, which is absent from normalized_comment_metadata.json
        import json
        supplemental_fields: dict[str, dict] = {}
        filtered_path = DATA_DIR / "filtered_comment_metadata.json"
        if filtered_path.exists():
            with open(filtered_path, encoding="utf-8") as f:
                filtered = json.load(f)
            if isinstance(filtered, dict):
                filtered = filtered.get("comments", [])
            for row in filtered:
                cid = row.get("comment_id")
                org_root = row.get("organization_root_name")
                if cid and org_root is not None:
                    supplemental_fields[cid] = {"organization_root_name": org_root}

        comment_key_map = load_comments(
            DATA_FILES["comments"],
            session,
            docket_key_map=docket_key_map,
            default_docket_id=default_docket_id,
            supplemental_fields=supplemental_fields or None,
        )

    # ── 3. Extracted citations with link metadata ──────────────────────────
    print("\n── Loading citations ───────────────────────────────────────────")
    if _exists("gemini_citations"):
        # final_citation_data.json is the single source of truth for this table;
        # clear prior loads (e.g. from the old Gemini-sourced file) before reloading.
        deleted = session.query(CommentCitation).delete()
        session.commit()
        print(f"  [main] Cleared {deleted} existing comment_citations rows")
        load_gemini_citations(DATA_FILES["gemini_citations"], session, comment_key_map)

    # ── 3a. Re-classification backfill for citations left "unclassified" ───
    print("\n── Backfilling citation classifications ─────────────────────────")
    if _exists("citation_classification_backfill"):
        load_citation_classification_backfill(
            DATA_FILES["citation_classification_backfill"], session
        )

    # ── 3a-2. Human-reviewed high-confidence reclassification (academic/gov/
    #          website/legal) — overrides the unverified backfill above,
    #          including the academic_article rows it explicitly skipped.
    print("\n── Applying high-confidence citation reclassification ───────────")
    load_citation_reclassification(session)
    load_openalex_matched_reclassification(session)
    load_news_reclassification(session)
    load_unclassified_cleanup(session)

    # ── 3a-3. Correction pass: research-like 'educational_institution_resource'
    #          rows confirmed as real papers via OpenAlex DOI lookup.
    print("\n── Applying edu_resources reclassification ──────────────────────")
    load_edu_resources_reclassification(session)

    # ── 3b. Resolved OpenAlex works for citations with a confirmed match ───
    print("\n── Loading citation resolved works ─────────────────────────────")
    if _exists("citation_resolved_works"):
        load_citation_resolved_works(DATA_FILES["citation_resolved_works"], session)

    # ── 4. Comment authorship backfill ────────────────────────────────────
    print("\n── Backfilling comment authorship ──────────────────────────────")
    if _exists("authorship"):
        load_comment_authorship(DATA_FILES["authorship"], session, comment_key_map)

    # ── 5. Per-author rows (name, order, affiliations) ─────────────────────
    print("\n── Loading comment authors ─────────────────────────────────────")
    if _exists("processed_authorship"):
        load_comment_authors(
            DATA_FILES["processed_authorship"],
            session,
            comment_key_map,
        )

    # ── 6. NAICS / commenter classification ─────────────────────────────────
    print("\n── Loading NAICS classification ────────────────────────────────")
    if _exists("processed_authorship_naics"):
        load_comment_author_naics(
            DATA_FILES["processed_authorship_naics"],
            session,
        )
    if _exists("unique_orgs_naics") and _exists("processed_authorship_naics"):
        load_public_comment_naics(
            DATA_FILES["unique_orgs_naics"],
            DATA_FILES["processed_authorship_naics"],
            session,
            comment_key_map,
        )
        propagate_first_author_org(
            session,
            max_authors_before_flag=5,
            review_output_path=DATA_DIR / "first_author_propagation_review.json",
        )

    # ── 6a. Hand-classified correction pass for 'individual'-mislabeled orgs ─
    print("\n── Applying additional org classification corrections ───────────")
    if _exists("additional_org_classification"):
        load_additional_org_classification(
            DATA_FILES["additional_org_classification"], session
        )

    # ── 6b. Second-pass LLM reclassification of remaining 'individual' rows ─
    print("\n── Applying individual reclassification ──────────────────────────")
    if _exists("individual_reclassification"):
        load_individual_reclassification(
            DATA_FILES["individual_reclassification"], session
        )

    session.close()
    print("\n── ETL complete ────────────────────────────────────────────────")


if __name__ == "__main__":
    run()
