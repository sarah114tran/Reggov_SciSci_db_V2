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
from loaders.no_reference_rerun_citations import (
    load_no_reference_rerun_citations, load_no_reference_rerun_research,
)
from loaders.citation_resolved_works import load_citation_resolved_works
from loaders.citation_reclassification import (
    load_citation_reclassification,
    load_openalex_matched_reclassification,
    load_news_reclassification,
    load_unclassified_cleanup,
)
from loaders.rfi_self_reference_cleanup import load_rfi_self_reference_cleanup
from loaders.edu_resources_reclassification import load_edu_resources_reclassification
from loaders.citation_domain_org import load_citation_domain_org
from loaders.research_domain_reclassification import load_research_domain_reclassification
from loaders.comment_authorship import load_comment_authorship
from loaders.comment_authors import load_comment_authors
from loaders.naics_classification import (
    load_comment_author_naics,
    load_public_comment_naics,
    propagate_first_author_org,
)
from loaders.additional_org_classification import load_additional_org_classification
from loaders.individual_reclassification import load_individual_reclassification
from loaders.long_comment_authors import load_long_comment_authors
from models import CommentCitation

# ── Configure your file paths here ────────────────────────────────────────────
DATA_DIR = Path("data")

DATA_FILES = {
    "fr_reg_metadata":              DATA_DIR / "docket_metadata.json",
    "comments":                     DATA_DIR / "normalized_comment_metadata.json",
    "gemini_citations":             DATA_DIR / "citation_data" / "extracted_citations" / "final_citation_data.json",
    "citation_resolved_works":      DATA_DIR / "citation_data" / "citation_resolved_works_normalized.json",
    "authorship":                   DATA_DIR / "comment_authorship.json",
    "processed_authorship":         DATA_DIR / "processed_authorship.json",
    "processed_authorship_naics":   DATA_DIR / "processed_authorship_with_naics.json",
    "unique_orgs_naics":            DATA_DIR / "unique_orgs_with_naics.json",
    "additional_org_classification": DATA_DIR / "additional_classified_orgs_final.csv",
    "individual_reclassification":  DATA_DIR / "classified_all.json",
    "long_comment_authorship":      Path("long_comments") / "long_comment_authorship.json",
    "long_comment_naics":           Path("long_comments") / "affiliation_naics_classified.json",
    "long_comment_citations":       Path("long_comments") / "long_comment_citations.json",
    "no_reference_rerun_citations": Path("long_comments") / "no_reference_rerun_citations.json",
    "no_reference_rerun_resolved_works": Path("long_comments") / "no_reference_rerun_research_resolved_works.json",
    "long_comment_resolved_works":  Path("long_comments") / "long_comment_research_resolved_works.json",
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

    # ── 3a-1. Citations from long comments, which final_citation_data.json
    #          never covered. Exported from the main repo by
    #          long_comments/export_long_comment_citations_v2.py; already
    #          classified by citation_classifier.py.
    if _exists("long_comment_citations"):
        load_gemini_citations(
            DATA_FILES["long_comment_citations"], session, comment_key_map,
            id_prefix="long_comment",
        )

    # ── 3a-1b. Re-extraction of 20 comments whose citations mostly lacked a
    #           link/footnote/reference (Reggov_SciSci_db/no_reference_rerun/).
    #           Replaces those comments' rows from 3 with the new extraction.
    if _exists("no_reference_rerun_citations"):
        load_no_reference_rerun_citations(
            session, DATA_FILES["no_reference_rerun_citations"], comment_key_map,
        )

    # ── 3a-2. Human-reviewed high-confidence reclassification (academic/gov/
    #          website/legal). claude_classified_citations_unverified.json's
    #          predicted_type is a different, unverified taxonomy from
    #          cited_source_catagory's real vocabulary (and its own
    #          confidence field is often low/null) -- it is not loaded here
    #          at all; this pass is the only source of truth for these.
    print("\n── Applying high-confidence citation reclassification ───────────")
    load_citation_reclassification(session)
    load_openalex_matched_reclassification(session)
    load_news_reclassification(session)
    load_unclassified_cleanup(session)

    # ── 3a-2b. Remove citations that are just self-references to the RFI/
    #           docket notice itself (not external evidence).
    print("\n── Removing RFI self-reference citations ─────────────────────────")
    load_rfi_self_reference_cleanup(session)

    # ── 3a-3. Correction pass: research-like 'educational_institution_resource'
    #          rows confirmed as real papers via OpenAlex DOI lookup.
    print("\n── Applying edu_resources reclassification ──────────────────────")
    load_edu_resources_reclassification(session)

    # ── 3a-4. Normalized domain_registered / domain_org, after every pass
    #          that rewrites citation_link/domain.
    print("\n── Normalizing citation domains ──────────────────────────────────")
    load_citation_domain_org(session)

    # ── 3a-5. Publisher/journal domains (ieee.org except Spectrum, nature.com,
    #          pnas.org) the news-domain CSV mislabels -> research_article.
    #          Needs domain_registered from 3a-4.
    print("\n── Applying research-domain reclassification ─────────────────────")
    load_research_domain_reclassification(session)

    # ── 3b. Resolved OpenAlex works for citations with a confirmed match ───
    print("\n── Loading citation resolved works ─────────────────────────────")
    if _exists("citation_resolved_works"):
        load_citation_resolved_works(DATA_FILES["citation_resolved_works"], session)

    # ── 3b-1. Resolved works for long-comment research_article citations.
    #          Built in the main repo by long_comments/research_pipeline/
    #          step4_merge_to_v2.py (Crossref >= 0.90 -> OpenAlex DOI, else
    #          Gemini parse -> OpenAlex title confirm).
    if _exists("long_comment_resolved_works"):
        load_citation_resolved_works(DATA_FILES["long_comment_resolved_works"], session)

    # ── 3b-2. Resolved works for the no_reference_rerun rows (every row sent
    #          through long_comments/research_pipeline); citations with a
    #          matched OpenAlex work -> research_article.
    if _exists("no_reference_rerun_resolved_works"):
        load_no_reference_rerun_research(session, DATA_FILES["no_reference_rerun_resolved_works"])

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

    # ── 6c. Long-comment authors with classified affiliations. Runs after
    #        propagate_first_author_org so it doesn't feed comment-level NAICS.
    print("\n── Loading long-comment authors ──────────────────────────────────")
    if _exists("long_comment_authorship") and _exists("long_comment_naics"):
        load_long_comment_authors(
            DATA_FILES["long_comment_authorship"],
            DATA_FILES["long_comment_naics"],
            session,
            comment_key_map,
        )

    session.close()
    print("\n── ETL complete ────────────────────────────────────────────────")


if __name__ == "__main__":
    run()
