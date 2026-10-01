"""
loaders/comment_authors.py
Loads per-author rows into comment_authors from processed_authorship.json.

processed_authorship.json structure:
[
  {
    "doc_id":              "{comment_id}_N",
    "author_order":        float,
    "name":                str | null,
    "organization":        str | null,
    "parent_organization": str | null,
    "job_title":           str | null,
    "affiliation":         str | null
  },
  ...
]
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import CommentAuthor, PublicComment
from utils import make_id


def load_comment_authors(
    processed_authorship_path: str | Path,
    session: Session,
    comment_key_map: dict[str, str],
) -> None:
    with open(processed_authorship_path, encoding="utf-8") as f:
        records = json.load(f)

    inserted           = 0
    skipped             = 0
    comments_not_found = 0

    for record in records:
        doc_id        = record.get("doc_id", "")
        author_order  = record.get("author_order")
        comment_id    = doc_id.rsplit("_", 1)[0]
        resolved_comment_id = comment_key_map.get(comment_id, comment_id)

        if not session.get(PublicComment, resolved_comment_id):
            comments_not_found += 1
            continue

        author_id = make_id("comment_author", doc_id, str(author_order))
        if session.get(CommentAuthor, author_id):
            skipped += 1
            continue

        session.add(CommentAuthor(
            author_id                    = author_id,
            comment_id                   = resolved_comment_id,
            author_name                  = record.get("name"),
            author_order                 = int(author_order) if author_order is not None else None,
            author_parent_affiliation    = record.get("parent_organization"),
            author_secondary_affiliation = record.get("organization"),
        ))
        inserted += 1

        if inserted % 500 == 0:
            session.commit()

    session.commit()
    print(
        f"[comment_authors] inserted={inserted}  skipped_existing={skipped}  "
        f"comments_not_found={comments_not_found}"
    )
