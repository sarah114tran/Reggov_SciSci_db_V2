"""
loaders/comment_authorship.py
Backfills commenter_name and organization_name on existing public_comments rows
from comment_authorship.json. Only NULL fields are updated — existing values
are never overwritten.

JSON structure:
[
  {
    "doc_id":       "{comment_id}_N",
    "name":         str | null,
    "affiliation":  str | null,
    "contact_info": str | null,
    "source_file":  str
  },
  ...
]
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import PublicComment


def load_comment_authorship(
    json_path: str | Path,
    session: Session,
    comment_key_map: dict[str, str],
) -> None:
    json_path = Path(json_path)
    with open(json_path, encoding="utf-8") as f:
        records = json.load(f)

    updated = 0
    skipped = 0
    not_found = 0

    for record in records:
        doc_id     = record.get("doc_id", "")
        comment_id = doc_id.rsplit("_", 1)[0]
        name       = record.get("name") or None
        affiliation = record.get("affiliation") or None

        if not name and not affiliation:
            skipped += 1
            continue

        resolved_id = comment_key_map.get(comment_id, comment_id)
        comment = session.get(PublicComment, resolved_id)
        if not comment:
            not_found += 1
            continue

        if comment.commenter_name is None and comment.organization_name is None:
            comment.commenter_name = name
            comment.organization_name = affiliation
            updated += 1

    session.commit()
    print(
        f"[comment_authorship] updated={updated}  skipped_no_name={skipped}  "
        f"not_found={not_found}"
    )
