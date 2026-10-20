"""
loaders/no_reference_rerun_citations.py
Replaces the comment_citations rows of the 20 comments re-extracted on
2026-10-19 (Reggov_SciSci_db/no_reference_rerun/) with the new extraction.

Those comments' citation sentences mostly came back without a link /
footnote / reference in final_citation_data.json (the reference list sat
far from the body text, or under no heading). The re-extraction used 3-page
chunks with the whole reference list attached to each chunk, and a bib
prompt that matches author-year / numeric / roman-numeral markers to their
entries. long_comments/no_reference_rerun_citations.json was built by
Reggov_SciSci_db/no_reference_rerun/prepare_v2.py (bibliography entries
already captured as a citation_reference dropped; links extracted from
footnote/reference text; classified with citation_classifier.py).

For every comment in the file, all existing comment_citations rows and their
dependent rows (citation_resolved_works, citation_context*) are deleted, then
the file is loaded with id_prefix "no_ref_rerun".

load_no_reference_rerun_research then loads the resolved works built for
those rows by long_comments/research_pipeline (RESEARCH_PIPELINE_TAG=
no_reference_rerun; every row was sent, not only research_article) and, like
citation_reclassification.load_openalex_matched_reclassification did for the
old rows, labels citations with a matched OpenAlex work research_article
(classifier_used "openalex_confirmed_match") unless they are government /
legal / social-media rows (see RELABEL_FROM).

    python -m loaders.no_reference_rerun_citations [--db PATH] [--research]
"""
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from loaders.citation_resolved_works import load_citation_resolved_works
from loaders.gemini_citations import load_gemini_citations
from models import CommentCitation

DEFAULT_PATH = Path(__file__).parent.parent / "long_comments" / "no_reference_rerun_citations.json"
# Labels a confirmed OpenAlex match may override. Government / legal / social
# rows are left alone: their matches were mostly similarly-titled papers or the
# statute itself (checked 2026-10-19: 8 of 10 wrong).
RELABEL_FROM = {None, "unclassified", "website", "news_article", "educational_institution_resource"}
RESOLVED_PATH = Path(__file__).parent.parent / "long_comments" / "no_reference_rerun_research_resolved_works.json"
ID_PREFIX = "no_ref_rerun"
DEPENDENT_TABLES = ("citation_resolved_works", "citation_context", "citation_context_v2", "citation_context_v3")


def comment_ids(json_path: str | Path) -> list[str]:
    with open(json_path, encoding="utf-8") as f:
        return sorted(k.rsplit("_", 1)[0] for k in json.load(f))


def load_no_reference_rerun_citations(
    session: Session,
    json_path: str | Path = DEFAULT_PATH,
    comment_key_map: dict[str, str] | None = None,
) -> None:
    ids = comment_ids(json_path)
    old = [c for (c,) in session.query(CommentCitation.citation_id)
           .filter(CommentCitation.comment_id.in_(ids)).all()]

    tables = set(session.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars())
    dep_counts = {}
    for t in DEPENDENT_TABLES:
        if t not in tables:
            continue
        n = 0
        for i in range(0, len(old), 500):
            chunk = old[i:i + 500]
            params = {f"p{j}": v for j, v in enumerate(chunk)}
            sql = f"DELETE FROM {t} WHERE citation_id IN ({','.join(':' + k for k in params)})"
            n += session.execute(text(sql), params).rowcount
        dep_counts[t] = n
    deleted = (session.query(CommentCitation)
               .filter(CommentCitation.comment_id.in_(ids))
               .delete(synchronize_session=False))
    session.commit()
    print(f"[no_ref_rerun_citations] {len(ids)} comments: deleted {deleted} old comment_citations rows, "
          f"dependents {dep_counts}")

    load_gemini_citations(json_path, session, comment_key_map or {}, id_prefix=ID_PREFIX)


def load_no_reference_rerun_research(session: Session, json_path: str | Path = RESOLVED_PATH) -> None:
    load_citation_resolved_works(json_path, session)
    with open(json_path, encoding="utf-8") as f:
        matched = {r["citation_id"] for r in json.load(f) if r.get("status") == "matched"}
    moves, kept = Counter(), Counter()
    for cid in matched:
        c = session.get(CommentCitation, cid)
        if c is None or c.cited_source_catagory == "research_article":
            continue
        if c.cited_source_catagory not in RELABEL_FROM:
            kept[c.cited_source_catagory] += 1
            continue
        moves[c.cited_source_catagory] += 1
        c.cited_source_catagory = "research_article"
        c.classifier_used = "openalex_confirmed_match"
    session.commit()
    print(f"[no_ref_rerun_research] matched citations={len(matched)}  "
          f"relabelled research_article={sum(moves.values())}  from {dict(moves)}  "
          f"left as is {dict(kept)}")


if __name__ == "__main__":
    import argparse

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(__file__).parent.parent / "dockets.db")
    ap.add_argument("--json", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--research", action="store_true",
                    help="load resolved works + OpenAlex relabel instead of the citations")
    args = ap.parse_args()
    s = sessionmaker(bind=create_engine(f"sqlite:///{args.db.resolve()}"))()
    if args.research:
        load_no_reference_rerun_research(s)
    else:
        load_no_reference_rerun_citations(s, args.json)
    s.close()
