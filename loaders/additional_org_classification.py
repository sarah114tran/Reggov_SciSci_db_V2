"""
loaders/additional_org_classification.py
Backfills public_comments for the batch of comments that were previously
mislabeled commenter_type='individual' but are actually a company/
non-profit/trade group (identified via audit_commenter_type.py in
db/eda_output/), now hand-classified in additional_classified_orgs_final.csv.

Overwrites organization_root_name/commenter_type/naics_code/naics_title/
confidence — same fields and precedence as loaders/naics_classification.py,
this is just a supplemental correction pass keyed on comment_id.
"""
import csv
from pathlib import Path
from sqlalchemy.orm import Session
from models import PublicComment

BATCH = 500


def load_additional_org_classification(csv_path: str | Path, session: Session) -> None:
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    updated   = 0
    not_found = 0

    for row in rows:
        comment = session.get(PublicComment, row["comment_id"])
        if not comment:
            not_found += 1
            continue

        comment.organization_root_name = row.get("resolved_name") or None
        comment.commenter_type         = row.get("category") or None
        comment.naics_code             = row.get("naics_code") or None
        comment.naics_title            = row.get("naics_title") or None
        comment.confidence             = int(row["confidence"]) if row.get("confidence") else None
        updated += 1

        if updated % BATCH == 0:
            session.commit()

    session.commit()
    print(
        f"[additional_org_classification] public_comments updated={updated}  "
        f"not_found={not_found}"
    )
