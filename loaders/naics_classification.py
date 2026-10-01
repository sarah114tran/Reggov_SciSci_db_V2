"""
loaders/naics_classification.py
Backfills NAICS/commenter-classification data onto public_comments and
comment_authors from two upstream files:

  - processed_authorship_with_naics.json: one row per author extracted from
    comment PDF text, enriched with a `naics` block (resolved_name, category,
    naics_code, naics_title, confidence). doc_id is "{comment_id}_{N}".
    Rows with a non-null `name` are real per-author records and back-fill
    comment_authors. Rows with `name: null` mean an org was found in the
    comment text but no individual author was identified — these back-fill
    public_comments as a fallback classification source.

  - unique_orgs_with_naics.json: org-level, deduplicated across both
    extraction sources. Each org has a `refs` list; refs tagged
    "source": "comment_metadata" come from PublicComment.organization_name
    (comment-level, regulations.gov metadata) and are the primary
    classification source for public_comments — they take precedence over
    the processed_authorship_with_naics.json fallback above.

Rows with no NAICS match (naics is null/missing) are still written, with
naics_code/naics_title/confidence/commenter_type/organization_root_name set
to NULL — callers should not skip them.

Both loaders overwrite existing organization_root_name/commenter_type values
(e.g. ones populated earlier from raw regulations.gov metadata).
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import PublicComment, CommentAuthor
from utils import make_id

BATCH = 500

# The naics-classifier skill's "individual/unaffiliated" category and this
# project's pre-existing "individual" tag (from raw regulations.gov metadata)
# mean the same thing — collapse to one value so commenter_type doesn't fork
# into two spellings of "no organization".
_CATEGORY_ALIASES = {
    "individual/unaffiliated": "individual",
}


def _apply_naics(target, naics: dict | None) -> None:
    naics = naics or {}
    category = naics.get("category")
    target.organization_root_name = naics.get("resolved_name")
    target.commenter_type         = _CATEGORY_ALIASES.get(category, category)
    target.naics_code             = naics.get("naics_code")
    target.naics_title            = naics.get("naics_title")
    target.confidence             = naics.get("confidence")


def load_comment_author_naics(
    processed_authorship_with_naics_path: str | Path,
    session: Session,
) -> None:
    with open(processed_authorship_with_naics_path, encoding="utf-8") as f:
        records = json.load(f)

    updated       = 0
    authors_not_found = 0
    skipped_no_author  = 0

    for record in records:
        if not record.get("name"):
            skipped_no_author += 1
            continue

        doc_id       = record.get("doc_id", "")
        author_order = record.get("author_order")
        author_id    = make_id("comment_author", doc_id, str(author_order))

        author = session.get(CommentAuthor, author_id)
        if not author:
            authors_not_found += 1
            continue

        author.author_job_title = record.get("job_title")
        _apply_naics(author, record.get("naics"))
        updated += 1

        if updated % BATCH == 0:
            session.commit()

    session.commit()
    print(
        f"[naics_classification] comment_authors updated={updated}  "
        f"authors_not_found={authors_not_found}  skipped_no_author={skipped_no_author}"
    )


def load_public_comment_naics(
    unique_orgs_with_naics_path: str | Path,
    processed_authorship_with_naics_path: str | Path,
    session: Session,
    comment_key_map: dict[str, str],
) -> None:
    with open(unique_orgs_with_naics_path, encoding="utf-8") as f:
        orgs = json.load(f)

    # Primary source: comment_metadata refs (one org per comment_id — verified
    # no comment_id has more than one such ref in this dataset).
    metadata_naics: dict[str, dict] = {}
    for org in orgs:
        naics = org.get("naics")
        for ref in org.get("refs", []):
            if ref.get("source") == "comment_metadata" and ref.get("comment_id"):
                metadata_naics[ref["comment_id"]] = naics

    # Fallback source: comment_text extractions with no author identified.
    with open(processed_authorship_with_naics_path, encoding="utf-8") as f:
        author_records = json.load(f)

    fallback_naics: dict[str, dict] = {}
    for record in author_records:
        if record.get("name"):
            continue
        comment_id = record.get("doc_id", "").rsplit("_", 1)[0]
        if comment_id:
            fallback_naics[comment_id] = record.get("naics")

    updated   = 0
    not_found = 0

    all_comment_ids = set(metadata_naics) | set(fallback_naics)
    for comment_id in all_comment_ids:
        naics = metadata_naics[comment_id] if comment_id in metadata_naics else fallback_naics[comment_id]

        resolved_id = comment_key_map.get(comment_id, comment_id)
        comment = session.get(PublicComment, resolved_id)
        if not comment:
            not_found += 1
            continue

        _apply_naics(comment, naics)
        updated += 1

        if updated % BATCH == 0:
            session.commit()

    session.commit()
    print(
        f"[naics_classification] public_comments updated={updated}  not_found={not_found}  "
        f"(comment_metadata source: {len(metadata_naics)}, comment_text fallback: {len(fallback_naics)})"
    )


def propagate_first_author_org(
    session: Session,
    max_authors_before_flag: int = 5,
    review_output_path: str | Path | None = None,
) -> list[dict]:
    """
    Third-tier PublicComment classification source, lowest precedence: for
    comments with no org classification yet (naics_code IS NULL) but at least
    one named CommentAuthor, propagate the first author's (min author_order)
    org classification up to PublicComment.

    Comments with more than `max_authors_before_flag` authors are NOT
    auto-propagated — a single first-listed author is a weak proxy for the
    org behind a large multi-signatory letter — they're returned (and
    optionally written to `review_output_path`) for manual review instead.
    """
    unclassified = (
        session.query(PublicComment)
        .filter(PublicComment.naics_code.is_(None))
        .all()
    )
    unclassified_ids = {c.comment_id for c in unclassified}
    if not unclassified_ids:
        print("[naics_classification] propagate_first_author_org: nothing to do")
        return []

    authors = (
        session.query(CommentAuthor)
        .filter(CommentAuthor.comment_id.in_(unclassified_ids))
        .all()
    )
    by_comment: dict[str, list[CommentAuthor]] = {}
    for a in authors:
        by_comment.setdefault(a.comment_id, []).append(a)

    comment_by_id = {c.comment_id: c for c in unclassified}

    propagated = 0
    no_author_naics = 0
    flagged: list[dict] = []

    for comment_id, group in by_comment.items():
        if len(group) > max_authors_before_flag:
            flagged.append({
                "comment_id": comment_id,
                "author_count": len(group),
                "authors": [
                    {
                        "author_name": a.author_name,
                        "author_order": a.author_order,
                        "organization_root_name": a.organization_root_name,
                        "naics_code": a.naics_code,
                    }
                    for a in sorted(group, key=lambda x: (x.author_order is None, x.author_order))
                ],
            })
            continue

        first_author = min(
            group, key=lambda a: (a.author_order is None, a.author_order)
        )
        if first_author.naics_code is None:
            no_author_naics += 1
            continue

        comment = comment_by_id[comment_id]
        comment.organization_root_name = first_author.organization_root_name
        comment.commenter_type         = first_author.commenter_type
        comment.naics_code             = first_author.naics_code
        comment.naics_title            = first_author.naics_title
        comment.confidence             = first_author.confidence
        propagated += 1

    session.commit()
    print(
        f"[naics_classification] propagate_first_author_org: propagated={propagated}  "
        f"flagged_for_review(>{max_authors_before_flag} authors)={len(flagged)}  "
        f"first_author_had_no_naics={no_author_naics}"
    )

    if review_output_path and flagged:
        with open(review_output_path, "w", encoding="utf-8") as f:
            json.dump(flagged, f, indent=2)
        print(f"[naics_classification] wrote {len(flagged)} flagged comments to {review_output_path}")

    return flagged
