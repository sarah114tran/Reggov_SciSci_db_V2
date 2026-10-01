"""
validate.py
Post-load sanity checks.
Run this after main.py to verify referential integrity and row counts.
"""

from sqlalchemy import text
from db import Session

CHECKS = [
    ("federal_docket rows",              "SELECT COUNT(*) FROM federal_docket"),
    ("docket_agencies rows",             "SELECT COUNT(*) FROM docket_agencies"),
    ("docket_urls rows",                 "SELECT COUNT(*) FROM docket_urls"),
    ("docket_documents rows",            "SELECT COUNT(*) FROM docket_documents"),
    ("public_comments rows",             "SELECT COUNT(*) FROM public_comments"),
    ("comment_paragraphs rows",          "SELECT COUNT(*) FROM comment_paragraphs"),
    ("comment_authors rows",             "SELECT COUNT(*) FROM comment_authors"),
    ("comment_footnotes rows",           "SELECT COUNT(*) FROM comment_footnotes"),
    ("comment_references rows",          "SELECT COUNT(*) FROM comment_references"),
    ("comment_citations rows",           "SELECT COUNT(*) FROM comment_citations"),
    ("citation_metadata rows",           "SELECT COUNT(*) FROM citation_metadata"),
    ("citation_metadata_academic rows",  "SELECT COUNT(*) FROM citation_metadata_academic"),

    # Orphan FK checks — these should all return 0
    ("orphaned comments (no docket)",
     "SELECT COUNT(*) FROM public_comments c "
     "LEFT JOIN federal_docket d ON c.docket_id = d.docket_id "
     "WHERE d.docket_id IS NULL"),

    ("orphaned docket_agencies (no docket)",
     "SELECT COUNT(*) FROM docket_agencies a "
     "LEFT JOIN federal_docket d ON a.docket_id = d.docket_id "
     "WHERE d.docket_id IS NULL"),

    ("orphaned docket_urls (no docket)",
     "SELECT COUNT(*) FROM docket_urls u "
     "LEFT JOIN federal_docket d ON u.docket_id = d.docket_id "
     "WHERE d.docket_id IS NULL"),

    ("orphaned docket_documents (no docket)",
     "SELECT COUNT(*) FROM docket_documents dd "
     "LEFT JOIN federal_docket d ON dd.docket_id = d.docket_id "
     "WHERE d.docket_id IS NULL"),

    ("orphaned paragraphs (no comment)",
     "SELECT COUNT(*) FROM comment_paragraphs p "
     "LEFT JOIN public_comments c ON p.comment_id = c.comment_id "
     "WHERE c.comment_id IS NULL"),

    ("orphaned citations (no comment)",
     "SELECT COUNT(*) FROM comment_citations ci "
     "LEFT JOIN public_comments c ON ci.comment_id = c.comment_id "
     "WHERE c.comment_id IS NULL"),

    ("orphaned citation_metadata (no citation)",
     "SELECT COUNT(*) FROM citation_metadata m "
     "LEFT JOIN comment_citations ci ON m.citation_id = ci.citation_id "
     "WHERE ci.citation_id IS NULL"),

    ("orphaned citation_metadata_academic (no citation)",
     "SELECT COUNT(*) FROM citation_metadata_academic a "
     "LEFT JOIN comment_citations ci ON a.citation_id = ci.citation_id "
     "WHERE ci.citation_id IS NULL"),
]

ORPHAN_CHECKS = {
    "orphaned comments (no docket)",
    "orphaned docket_agencies (no docket)",
    "orphaned docket_urls (no docket)",
    "orphaned docket_documents (no docket)",
    "orphaned paragraphs (no comment)",
    "orphaned citations (no comment)",
    "orphaned citation_metadata (no citation)",
    "orphaned citation_metadata_academic (no citation)",
}


def run_validation():
    session = Session()
    print("\n── Validation Report ──────────────────────────────────────────")
    all_ok = True
    for label, sql in CHECKS:
        result = session.execute(text(sql)).scalar()
        is_orphan_check = label in ORPHAN_CHECKS
        status = ""
        if is_orphan_check and result != 0:
            status = " ← ⚠️  PROBLEM"
            all_ok = False
        print(f"  {label:<45} {result:>8}{status}")

    print("───────────────────────────────────────────────────────────────")
    if all_ok:
        print("  ✅  All checks passed.")
    else:
        print("  ❌  Some checks failed — review orphan counts above.")
    print()
    session.close()


if __name__ == "__main__":
    run_validation()
