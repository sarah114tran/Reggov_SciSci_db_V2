"""
loaders/rfi_self_reference_cleanup.py
Removes comment_citations rows that are self-references to the RFI/docket
notice being commented on, rather than citations to external evidence —
e.g. a link back to the comment's own federalregister.gov/regulations.gov
notice, or a bare "88 Fed. Reg. 22,433"-style citation to that notice with
no link at all.

These rows exist in final_citation_data.json (so a clean rebuild of
comment_citations from that file includes them) but were intentionally
excluded from earlier versions of this database. This pass reproduces
that exclusion.

Source is data/rfi_self_reference_citation_ids.csv — a fixed list of
specific citation_ids reviewed and confirmed as self-references (not a
general rule). A general category/link-based rule was tried and rejected:
it also matched thousands of legitimate rows already present in the
reference database, so only this reviewed, explicit list is deleted.
"""
import csv
from pathlib import Path
from sqlalchemy.orm import Session
from models import CommentCitation

CITATION_IDS_PATH = Path(__file__).parent.parent / "data" / "rfi_self_reference_citation_ids.csv"


def load_rfi_self_reference_cleanup(session: Session, csv_path: str | Path = CITATION_IDS_PATH) -> None:
    csv_path = Path(csv_path)
    with open(csv_path, encoding="utf-8") as f:
        citation_ids = [row["citation_id"] for row in csv.DictReader(f)]

    deleted = (
        session.query(CommentCitation)
        .filter(CommentCitation.citation_id.in_(citation_ids))
        .delete(synchronize_session=False)
    )
    session.commit()
    print(f"[rfi_self_reference_cleanup] deleted={deleted}  (of {len(citation_ids)} listed)")
