"""
loaders/individual_reclassification.py
Second-pass LLM reclassification of public_comments still labeled
commenter_type='individual' after the additional_org_classification pass
(see loaders/additional_org_classification.py). Source is
classified_all.json, keyed by commenter_name rather than comment_id.

Only rows currently classified 'individual' are touched, and only when
their commenter_name matches a record in this file. The 'individual'
label is left in place for a matched row only if this file's own
classification confirms it (category == "individual/unaffiliated",
aliased to "individual" — same alias used in naics_classification.py);
every other matched row is overwritten with the new
category/naics_code/naics_title/confidence/organization_root_name.
Unmatched 'individual' rows are left untouched (no new information).
"""
import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import PublicComment

BATCH = 500

_CATEGORY_ALIASES = {
    "individual/unaffiliated": "individual",
}


def load_individual_reclassification(json_path: str | Path, session: Session) -> None:
    with open(json_path, encoding="utf-8") as f:
        records = json.load(f)

    by_name = {r["name"]: r for r in records}

    candidates = (
        session.query(PublicComment)
        .filter(PublicComment.commenter_type == "individual")
        .filter(PublicComment.commenter_name.in_(by_name.keys()))
        .all()
    )

    updated = 0
    for comment in candidates:
        record = by_name[comment.commenter_name]
        comment.organization_root_name = record.get("resolved_name")
        comment.commenter_type         = _CATEGORY_ALIASES.get(record.get("category"), record.get("category"))
        comment.naics_code             = record.get("naics_code")
        comment.naics_title            = record.get("naics_title")
        comment.confidence             = record.get("confidence")
        updated += 1

        if updated % BATCH == 0:
            session.commit()

    session.commit()
    print(
        f"[individual_reclassification] public_comments updated={updated}  "
        f"(matched {len(by_name)} names from {json_path})"
    )
