"""
loaders/gemini_citations.py
Loads citations from final_citation_data.json into comment_citations.

JSON structure:
{
  "{comment_id}_1": {
    "custom_id": "{comment_id}_1",
    "citations": [
      {
        "citation_sentence":  str | null,
        "citation_link":      str | null,
        "citation_footnote":  str | null,
        "citation_reference": str | null,
        "legal_citations":    [{"source": str, "text": str, "citation_type": str}, ...],
        "domain":             str | null,
        "uncleaned_domain":   str | null,
        "classification":     str | null,
        "classifier_used":    str | null,
      },
      ...
    ],
    "unmatched_bibliography": [
      {
        "text":           str,
        "citation_link":  str | null,
        "classification": str | null,
        "legal_citations": [...],
        "domain":         str | null,
        "uncleaned_domain": str | null,
      },
      ...
    ]
  },
  ...
}
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import CommentCitation, PublicComment
from utils import make_id


def load_gemini_citations(
    json_path: str | Path,
    session: Session,
    comment_key_map: dict[str, str],
    id_prefix: str = "gemini",
) -> None:
    """id_prefix namespaces citation_ids per source file (default keeps the
    original "gemini_citation"/"gemini_unmatched" ids)."""
    json_path = Path(json_path)
    with open(json_path, encoding="utf-8") as f:
        records = json.load(f)

    inserted          = 0
    skipped           = 0
    comments_not_found = 0

    if isinstance(records, dict):
        records = records.values()

    for record in records:
        custom_id  = record.get("custom_id", "")
        comment_id = custom_id.rsplit("_", 1)[0]

        resolved_comment_id = comment_key_map.get(comment_id, comment_id)

        if not session.get(PublicComment, resolved_comment_id):
            print(f"  [gemini_citations] WARNING: comment '{comment_id}' not found — skipping")
            comments_not_found += 1
            continue

        for idx, raw in enumerate(record.get("citations", [])):
            citation_id = make_id(f"{id_prefix}_citation", custom_id, str(idx))

            if session.get(CommentCitation, citation_id):
                skipped += 1
                continue

            legal = raw.get("legal_citations") or []
            legal_type = legal[0].get("citation_type") if legal else None
            legal_text = legal[0].get("text")         if legal else None

            session.add(CommentCitation(
                citation_id             = citation_id,
                comment_id              = resolved_comment_id,
                citation_sentence       = raw.get("citation_sentence"),
                citation_link           = raw.get("citation_link"),
                citation_footnote_text  = raw.get("citation_footnote"),
                citation_reference_text = raw.get("citation_reference"),
                cited_source_catagory   = raw.get("classification"),
                domain                  = raw.get("domain"),
                uncleaned_domain        = raw.get("uncleaned_domain"),
                classifier_used         = raw.get("classifier_used"),
                legal_citation_type     = legal_type,
                legal_citation_text     = legal_text,
            ))
            inserted += 1

        for idx, raw in enumerate(record.get("unmatched_bibliography", [])):
            citation_id = make_id(f"{id_prefix}_unmatched", custom_id, str(idx))

            if session.get(CommentCitation, citation_id):
                skipped += 1
                continue

            legal = raw.get("legal_citations") or []
            legal_type = legal[0].get("citation_type") if legal else None
            legal_text = legal[0].get("text")         if legal else None

            session.add(CommentCitation(
                citation_id                      = citation_id,
                comment_id                       = resolved_comment_id,
                unmatched_citation_reference_text = raw.get("text"),
                citation_link                    = raw.get("citation_link"),
                cited_source_catagory            = raw.get("classification"),
                domain                           = raw.get("domain"),
                uncleaned_domain                 = raw.get("uncleaned_domain"),
                legal_citation_type              = legal_type,
                legal_citation_text              = legal_text,
            ))
            inserted += 1

        if inserted % 500 == 0 and inserted > 0:
            session.commit()

    session.commit()
    print(
        f"[{id_prefix}_citations] inserted={inserted}  skipped={skipped}  "
        f"comments_not_found={comments_not_found}"
    )
