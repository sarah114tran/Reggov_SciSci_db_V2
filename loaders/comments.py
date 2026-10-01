"""
loaders/comments.py
Loads public_comments from a JSON file.

Expected JSON structure (list of comment objects):
[
  {
    "comment_id": "...",
    "documentId": "...",
    "agencyId": "...",
    "agency_name": "...",
    "fr_number": "...",
    "source": "...",
    "submitted_date": "...",
    "title": "...",
    "pageCount": 1,
    "attachment_count": 0,
    "comment_link": [...],
    "commenter_type": "..."
  },
  ...
]
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import PublicComment
from utils import make_id


def _map(raw: dict) -> dict:
    """Map raw JSON keys → model field names."""
    links = raw.get("comment_link") or []
    return {
        "reg_document_id":   raw.get("documentId"),
        "agency_id":         raw.get("agencyId"),
        "agency_name":       raw.get("agency_name"),
        "document_number":   raw.get("document_number"),
        "source":            raw.get("source"),
        "submitted_date":    raw.get("submitted_date"),
        "title":             raw.get("title"),
        "page_count":        raw.get("pageCount"),
        "attachment_count":  raw.get("attachment_count"),
        "comment_link":      json.dumps(links),
        "commenter_type":    raw.get("commenter_type"),
        "commenter_name":    raw.get("commenter_name"),
        "organization_name": raw.get("organization_name"),
        "comment_text":           raw.get("comment_text"),
        "duplicate":              raw.get("duplicate"),
        "citation_type":          raw.get("citation_type"),
        "inclusion":              raw.get("inclusion"),
        "organization_root_name": raw.get("organization_root_name"),
        "non_profit_status":      raw.get("non_profit_status"),
    }


def load_comments(
    json_path: str | Path,
    session: Session,
    docket_key_map: dict[str, str],
    default_docket_id: str | None = None,
    supplemental_fields: dict[str, dict] | None = None,
) -> dict[str, str]:
    """
    Load comments from a JSON file.

    Args:
        json_path:           Path to the comments JSON file.
        session:             Active SQLAlchemy session.
        docket_key_map:      {docketId / regs_document_id → fr_docket.docket_id}
                             from load_fr_dockets().
        default_docket_id:   If every comment in this file belongs to one docket,
                             pass its docket_id here and skip per-row lookup.
        supplemental_fields: {comment_id → {field: value, ...}} for fields not
                             present in the primary file (e.g. organization_root_name).

    Returns:
        {comment_id → comment_id} for use by child loaders.
    """
    json_path = Path(json_path)
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "comments" in data:
        data = data["comments"]
    elif isinstance(data, dict):
        data = [data]

    AUTHORSHIP_FIELDS = [
        "commenter_name", "commenter_type", "organization_name",
        "organization_root_name", "non_profit_status",
    ]

    natural_key_to_comment_id: dict[str, str] = {}
    inserted = 0
    updated  = 0
    skipped  = 0
    BATCH    = 500

    for idx, raw in enumerate(data):
        mapped = _map(raw)

        if supplemental_fields:
            cid = raw.get("comment_id")
            if cid and cid in supplemental_fields:
                for k, v in supplemental_fields[cid].items():
                    if mapped.get(k) is None:
                        mapped[k] = v

        # Resolve docket_id
        if default_docket_id:
            docket_id = default_docket_id
        else:
            lookup_key = raw.get("docketId") or raw.get("document_number") or ""
            docket_id  = docket_key_map.get(lookup_key)
            if not docket_id:
                print(f"  [comments] WARNING: no docket found for key '{lookup_key}' — row {idx} skipped")
                continue

        comment_id  = raw.get("comment_id") or make_id("comment", docket_id, str(idx))
        natural_key = comment_id

        existing = session.get(PublicComment, comment_id)
        if existing:
            for field in AUTHORSHIP_FIELDS:
                setattr(existing, field, mapped.get(field))
            natural_key_to_comment_id[natural_key] = comment_id
            updated += 1
            if updated % BATCH == 0:
                session.commit()
            continue

        session.add(PublicComment(
            comment_id=comment_id,
            regs_docket_id=docket_id,
            **mapped,
        ))
        natural_key_to_comment_id[natural_key] = comment_id
        inserted += 1

        if inserted % BATCH == 0:
            session.commit()
            print(f"  [comments] committed {inserted} rows...")

    session.commit()
    print(f"[comments] inserted={inserted}  updated={updated}  skipped={skipped}")
    return natural_key_to_comment_id
