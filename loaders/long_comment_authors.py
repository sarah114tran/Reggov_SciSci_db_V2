"""
loaders/long_comment_authors.py
Adds comment_authors rows, with affiliation + NAICS classification, for
authors extracted from long comments (which the main processed_authorship
pipeline never covered). Two source files, both in long_comments/:

  - long_comment_authorship.json: one row per extracted author
    (doc_id, name, affiliation, contact_info). doc_id is always
    "{comment_id}_1" even when a comment has several authors, so
    author_order is the author's position among that comment's rows
    (0-based, file order) rather than the doc_id suffix.

  - affiliation_naics_classified.json: naics-classifier output keyed by
    the raw affiliation string (`name` == authorship `affiliation`).

Only authors whose affiliation was classified to a known organization are
loaded — rows with no affiliation, or whose classification category is
"unknown/unverified", are skipped. The raw affiliation string goes in
author_secondary_affiliation; the resolved org/NAICS fields are written via
naics_classification._apply_naics (same alias handling as the main pass).

public_comments is not touched.
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import CommentAuthor, PublicComment
from loaders.naics_classification import _apply_naics
from utils import make_id

_SKIP_CATEGORIES = {"unknown/unverified"}


def load_long_comment_authors(
    authorship_path: str | Path,
    classified_path: str | Path,
    session: Session,
    comment_key_map: dict[str, str],
) -> None:
    with open(authorship_path, encoding="utf-8") as f:
        records = json.load(f)
    with open(classified_path, encoding="utf-8") as f:
        classified = {e["name"]: e for e in json.load(f)}

    inserted           = 0
    updated            = 0
    skipped_no_aff     = 0
    skipped_unknown    = 0
    unclassified       = 0
    comments_not_found = 0

    order_by_comment: dict[str, int] = {}

    for record in records:
        doc_id     = record.get("doc_id", "")
        comment_id = doc_id.rsplit("_", 1)[0]
        author_order = order_by_comment.get(comment_id, 0)
        order_by_comment[comment_id] = author_order + 1

        affiliation = record.get("affiliation")
        if not affiliation:
            skipped_no_aff += 1
            continue
        naics = classified.get(affiliation)
        if naics is None:
            unclassified += 1
            continue
        if naics.get("category") in _SKIP_CATEGORIES:
            skipped_unknown += 1
            continue

        resolved_comment_id = comment_key_map.get(comment_id, comment_id)
        if not session.get(PublicComment, resolved_comment_id):
            comments_not_found += 1
            continue

        author_id = make_id("long_comment_author", doc_id, str(author_order))
        author = session.get(CommentAuthor, author_id)
        if author:
            updated += 1
        else:
            author = CommentAuthor(author_id=author_id, comment_id=resolved_comment_id)
            session.add(author)
            inserted += 1

        author.author_name                  = record.get("name")
        author.author_order                 = author_order
        author.author_secondary_affiliation = affiliation
        _apply_naics(author, naics)

    session.commit()
    print(
        f"[long_comment_authors] inserted={inserted}  updated={updated}  "
        f"skipped_no_affiliation={skipped_no_aff}  skipped_unknown={skipped_unknown}  "
        f"unclassified_affiliation={unclassified}  comments_not_found={comments_not_found}"
    )
