"""
loaders/citation_resolved_works.py
Loads the merged retreive_doi_output.json (LLM-parsed fields + validated
OpenAlex data) into citation_resolved_works, upserting the academic_works
and work_authors rows each resolved work points at.

Expected row shape (one entry per (citation_id, sub_index)):
{
  "citation_id":   str,
  "sub_index":     int,             # position when one citation_reference
                                     # splits into multiple works
  "parsed_author": [str, ...] | str | null,   # from citation_parsing_llm.py
  "parsed_title":  str | null,                # from citation_parsing_llm.py
  "parsed_year":   int | null,                # from citation_parsing_llm.py
  "match_source":  str | null,      # embedded_doi / arxiv_id / crossref_query / llm_parsed
  "confidence":    float | null,
  "status":        str | null,      # matched / tentative / low_confidence / unmatched
  "openalex_id":   str | null,      # e.g. "https://openalex.org/W123..."
  "doi":           str | null,
  "title":         str | null,      # resolved work's title (may differ from parsed_title)
  "authors":       [str, ...] | null,
  "year":          int | null,
  "cited_by_count": int | null,
}

Rows are upserted (not skip-on-exists), since a resolved work can be written
in two passes: an initial LLM-parse pass, then an OpenAlex-validation pass
that fills in match_source/confidence/status/openalex_id on the same row.
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import CitationResolvedWork, AcademicWork, WorkAuthor
from utils import make_id


def _join_authors(authors) -> str | None:
    if not authors:
        return None
    if isinstance(authors, str):
        return authors
    return "; ".join(a for a in authors if a)


def _upsert_academic_work(session: Session, row: dict) -> str | None:
    work_id = row.get("openalex_id")
    if not work_id:
        return None

    work = session.get(AcademicWork, work_id)
    is_new = work is None
    if is_new:
        work = AcademicWork(work_id=work_id)
        session.add(work)

    work.doi              = row.get("doi") or work.doi
    work.title             = row.get("title") or work.title
    work.publication_year  = row.get("year") or work.publication_year
    work.citation_count    = row.get("cited_by_count") if row.get("cited_by_count") is not None else work.citation_count

    # Only seed authors the first time we see this work — it can be cited by
    # many rows, and we don't want duplicate WorkAuthor rows on every citation.
    if is_new:
        for order, name in enumerate(row.get("authors") or []):
            session.add(WorkAuthor(
                work_author_id = make_id("work_author", work_id, str(order)),
                work_id        = work_id,
                author_name    = name,
                author_order   = order,
            ))

    return work_id


def load_citation_resolved_works(
    json_path: str | Path,
    session: Session,
) -> None:
    json_path = Path(json_path)
    with open(json_path, encoding="utf-8") as f:
        rows = json.load(f)

    inserted = 0
    updated  = 0
    skipped  = 0

    for row in rows:
        citation_id = row.get("citation_id")
        if not citation_id:
            skipped += 1
            continue
        sub_index = row.get("sub_index") or 0

        work_id = _upsert_academic_work(session, row)

        resolved_id = make_id("resolved_work", citation_id, str(sub_index))
        resolved = session.get(CitationResolvedWork, resolved_id)
        is_new = resolved is None
        if is_new:
            resolved = CitationResolvedWork(
                resolved_id=resolved_id,
                citation_id=citation_id,
                sub_index=sub_index,
            )
            session.add(resolved)

        resolved.parsed_author    = _join_authors(row.get("parsed_author")) or resolved.parsed_author
        resolved.parsed_title     = row.get("parsed_title") or resolved.parsed_title
        resolved.parsed_year      = row.get("parsed_year") if row.get("parsed_year") is not None else resolved.parsed_year
        resolved.match_source     = row.get("match_source") or resolved.match_source
        resolved.confidence       = row.get("confidence") if row.get("confidence") is not None else resolved.confidence
        resolved.status           = row.get("status") or resolved.status
        # An unmatched row clears any work a previous (wrong) match linked.
        resolved.openalex_work_id = work_id or (None if row.get("status") == "unmatched" else resolved.openalex_work_id)

        if is_new:
            inserted += 1
        else:
            updated += 1

        if (inserted + updated) % 500 == 0:
            session.commit()

    session.commit()
    print(
        f"[citation_resolved_works] inserted={inserted}  updated={updated}  skipped={skipped}"
    )
