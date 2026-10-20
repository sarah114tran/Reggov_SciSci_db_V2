"""
rfi_self_reference_filter.py

Flags citation_reference / citation_footnote (and unmatched_bibliography)
text that most likely refers back to the RFI a comment was itself filed
under, rather than to some genuine external legal or government source.
Commenters routinely open with "This paper provides public comment in
response to the ... request for information ..." or footnote the Federal
Register notice for the docket itself -- these get swept into
legal_citation / government_resource / unclassified by citation_classifier.py
because they share surface features (FR cites, .gov links, legal citation
syntax) with genuine external citations.

Docket identity (title, FR citation, document number, publication date,
abstract) is pulled from agency_docket in dockets.db per comment_id, rather
than hardcoded per-docket strings, so this works for every docket loaded into
the DB -- not just the AI Action Plan RFI. "Distinctive" title words are also
derived from the DB: a word only counts toward a title match if it is rare
across all loaded docket titles (e.g. "artificial"/"intelligence" show up in
27 of 40 titles in this DB and are not distinctive of any single docket;
"action"/"plan" are).

Some dockets are also commonly self-cited by a NIST special-publication
number (e.g. "NIST AI 800-1") rather than by document number or FR citation.
Where that number appears in the docket's own DB abstract (NIST's RFIs
routinely name the resulting publication, e.g. "NIST AI 800-1, Managing
Misuse Risk for Dual-Use Foundation Models" in doc 2024-17614's abstract) it
is picked up automatically. One number predates its own abstract text --
"NIST AI 100-1" / "AI RMF 1.0" was assigned when the AI Risk Management
Framework was finalized in Jan 2023, after the 2021 RFI (doc 2021-16176) that
produced it had already closed, so it can't be scraped from that docket's own
text and is listed in _NIST_ID_OVERRIDES instead. Without per-docket scoping,
a bare "NIST AI 100-1" keyword would be unusable: it's cited as a well-known
*external* reference by 15+ unrelated dockets in this DB (OMB, NTIA, FDA,
2025 AI Action Plan comments, etc.), so it only counts as a self-reference
signal for the one docket it actually belongs to.

.venv/bin/python3 - <<'EOF'
from citation_processing.rfi_self_reference_filter import annotate_file

annotate_file(
    input_path="/Users/saraht45/Reggov_SciSci_db/citations/citation_data/final_citation_data.json",
    output_path="/Users/saraht45/Reggov_SciSci_db/citations/citation_data/final_citation_data_rfi_flagged.json",
    db_path="/Users/saraht45/Reggov_SciSci_db/db/dockets.db",
)
EOF
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Docket metadata
# ---------------------------------------------------------------------------

# Generic regulatory boilerplate words -- not useful for telling one docket's
# title apart from another's even before we look at cross-docket frequency.
_BOILERPLATE = {
    "the", "a", "an", "of", "on", "in", "to", "for", "and", "or", "with", "by",
    "its", "request", "information", "rfi", "notice", "comment", "comments",
    "draft", "document", "related", "regarding", "public", "federal",
    "register", "update", "report",
}

# A title word counts as "distinctive" for a docket only if it appears in at
# most this fraction of all loaded docket titles.
_MAX_TITLE_WORD_DOC_FREQ = 0.2


def _title_words(title: str, toc_doc: str) -> set[str]:
    words = re.findall(r"[a-z']+", f"{title} {toc_doc}".lower())
    return {w for w in words if len(w) >= 4 and w not in _BOILERPLATE}


_NIST_ID_RE = re.compile(r"NIST[.\s]?AI[.\s]?(\d{3})-(\d+)", re.IGNORECASE)


def _extract_nist_ids(text: str) -> set[tuple[int, int]]:
    return {(int(m.group(1)), int(m.group(2))) for m in _NIST_ID_RE.finditer(text)}


# Publication numbers assigned after the RFI that produced them had already
# closed, so they never appear in that docket's own DB text -- see module
# docstring. Keyed by document_number; extend as similar cases turn up.
_NIST_ID_OVERRIDES: dict[str, set[tuple[int, int]]] = {
    "2021-16176": {(100, 1)},  # AI Risk Management Framework / "AI RMF 1.0"
}


@dataclass
class DocketMeta:
    docket_id: str
    title: str
    toc_doc: str
    document_number: str
    fr_citation: Optional[str]      # e.g. "90 FR 9088"
    agency_name: str
    publication_date: Optional[str]  # "YYYY-MM-DD"
    distinctive_title_words: set[str] = field(default_factory=set)
    nist_ids: set[tuple[int, int]] = field(default_factory=set)  # e.g. {(800, 1)} for "NIST AI 800-1"


def load_dockets(db_path: str | Path) -> dict[str, DocketMeta]:
    """Load every docket's identifying metadata, keyed by docket_id, and
    compute which title words are distinctive to it relative to every other
    docket currently in the DB."""
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        "SELECT docket_id, title, toc_doc, document_number, citation, "
        "       agency_name, publication_date, abstract "
        "FROM agency_docket"
    )
    rows = cur.fetchall()
    con.close()

    dockets = {
        docket_id: DocketMeta(
            docket_id=docket_id,
            title=title or "",
            toc_doc=toc_doc or "",
            document_number=document_number or "",
            fr_citation=fr_citation,
            agency_name=agency_name or "",
            publication_date=publication_date,
            nist_ids=(
                _extract_nist_ids(f"{title or ''} {toc_doc or ''} {abstract or ''}")
                | _NIST_ID_OVERRIDES.get(document_number or "", set())
            ),
        )
        for docket_id, title, toc_doc, document_number, fr_citation,
        agency_name, publication_date, abstract in rows
    }

    word_doc_count: dict[str, int] = {}
    per_docket_words: dict[str, set[str]] = {}
    for docket_id, meta in dockets.items():
        words = _title_words(meta.title, meta.toc_doc)
        per_docket_words[docket_id] = words
        for w in words:
            word_doc_count[w] = word_doc_count.get(w, 0) + 1

    total = max(len(dockets), 1)
    for docket_id, meta in dockets.items():
        meta.distinctive_title_words = {
            w for w in per_docket_words[docket_id]
            if word_doc_count[w] / total <= _MAX_TITLE_WORD_DOC_FREQ
        }

    return dockets


def load_comment_docket_map(db_path: str | Path) -> dict[str, str]:
    """comment_id -> docket_id, so a citation can be checked against the
    metadata of the specific docket its comment was filed under."""
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT comment_id, regs_docket_id FROM public_comments")
    mapping = dict(cur.fetchall())
    con.close()
    return mapping


# ---------------------------------------------------------------------------
# Signal detectors
# ---------------------------------------------------------------------------

_FR_CITE_RE = re.compile(
    r"\b(\d{1,3})\s*(?:FR|F\.R\.|Fed\.?\s*Reg\.?)\s*(\d{2,6})\b", re.IGNORECASE
)

# The Federal Register's own running page header, e.g. "Federal Register /
# Vol. 88, No. 244 / Thursday, December 21, 2023 / Notices, 88368". Some
# commenters paste this directly as their citation format instead of the
# "NN FR NNNNN" style. It's just an alternate way of writing the same
# (volume, page) pair -- NOT inherently self-referential, since a commenter
# can just as easily cite an unrelated FR document this way (seen in this
# data citing an external CMMC proposed rule). Extracted tuples go through
# the same exact-match check against the docket's own citation as any other
# FR cite, so it only flags when the volume+page actually matches.
_FR_HEADER_RE = re.compile(
    r"Federal Register\s*/\s*Vol\.\s*(\d{1,3}),?\s*No\.\s*\d+\s*/\s*"
    r"[A-Za-z]+,\s*[A-Za-z]+\.?\s+\d{1,2},\s*\d{4}\s*/\s*"
    r"(?:Notices|Proposed Rules|Rules and Regulations|Presidential Documents)\s*,?\s*(\d{2,6})",
    re.IGNORECASE,
)


def _parse_fr_citation(s: str) -> Optional[tuple[int, int]]:
    if not s:
        return None
    m = _FR_CITE_RE.search(s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _extract_fr_citations(text: str) -> set[tuple[int, int]]:
    return {
        (int(m.group(1)), int(m.group(2)))
        for pattern in (_FR_CITE_RE, _FR_HEADER_RE)
        for m in pattern.finditer(text)
    }


def _fr_citation_match(text: str, docket: DocketMeta) -> bool:
    docket_cite = _parse_fr_citation(docket.fr_citation or "")
    if not docket_cite:
        return False
    return docket_cite in _extract_fr_citations(text)


def _nist_id_match(text: str, docket: DocketMeta) -> bool:
    if not docket.nist_ids:
        return False
    return bool(_extract_nist_ids(text) & docket.nist_ids)


def _document_number_match(text: str, docket: DocketMeta) -> bool:
    if not docket.document_number:
        return False
    return bool(re.search(re.escape(docket.document_number), text))


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]


def _date_variants(date_str: Optional[str]) -> set[str]:
    if not date_str:
        return set()
    try:
        year, month, day = (int(p) for p in date_str.split("-"))
        month_name = _MONTH_NAMES[month - 1]
    except (ValueError, AttributeError, IndexError):
        return set()
    month_abbr = month_name[:3]
    return {
        f"{month_name} {day}, {year}",
        f"{month_abbr}. {day}, {year}",
        f"{month_abbr} {day}, {year}",
        f"{month}/{day}/{year}",
        f"{month:02d}/{day:02d}/{year}",
        f"{day} {month_name} {year}",
    }


def _date_match(text: str, docket: DocketMeta) -> bool:
    text_lower = text.lower()
    return any(v.lower() in text_lower for v in _date_variants(docket.publication_date))


_RFI_KEYWORD_RE = re.compile(r"\brequest for information\b|\bRFI\b", re.IGNORECASE)


def _rfi_keyword_present(text: str) -> bool:
    return bool(_RFI_KEYWORD_RE.search(text))


def _title_overlap(text: str, docket: DocketMeta) -> tuple[float, int]:
    """Returns (overlap_ratio, hit_count) over the docket's distinctive title words."""
    distinctive = docket.distinctive_title_words
    if not distinctive:
        return 0.0, 0
    text_words = set(re.findall(r"[a-z']+", text.lower()))
    hits = distinctive & text_words
    return len(hits) / len(distinctive), len(hits)


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _url_document_number_match(text: str, docket: DocketMeta) -> bool:
    if not docket.document_number:
        return False
    return any(docket.document_number in m.group(0) for m in _URL_RE.finditer(text))


# ---------------------------------------------------------------------------
# Combined verdict
# ---------------------------------------------------------------------------

_MIN_TITLE_OVERLAP_RATIO = 0.6
_MIN_TITLE_OVERLAP_HITS = 2


def classify_rfi_self_reference(strong_text: str, weak_text: str, docket: DocketMeta) -> tuple[bool, str]:
    """Returns (is_self_reference, reason).

    `strong_text` is checked for exact, unambiguous signals (the docket's own
    document number / FR citation / a URL to it), and `weak_text` for the
    fallback combined signal (distinctive title words + an RFI keyword or the
    docket's own publication date). Both are restricted to the citation's own
    text (reference/footnote/link) -- excluding the surrounding
    citation_sentence -- because surrounding prose almost always contains
    generic phrases like "in response to this RFI Document No. 2025-02305"
    regardless of what the citation next to it actually points to (e.g. a
    footnote citing an unrelated Executive Order, sitting next to a sentence
    explaining the comment responds to that RFI). strong_text used to include
    citation_sentence on the theory that its signals were precise enough not
    to need the restriction; in practice the substring search in
    _document_number_match matched the sentence far more often than the
    citation itself (82% of its flags in one audit), so it's excluded here
    the same way it already was for weak_text."""
    if not strong_text.strip() and not weak_text.strip():
        return False, "empty"

    if _document_number_match(strong_text, docket):
        return True, "document_number"
    if _url_document_number_match(strong_text, docket):
        return True, "url_document_number"
    if _fr_citation_match(strong_text, docket):
        return True, "fr_citation"
    if _nist_id_match(strong_text, docket):
        return True, "nist_publication_id"

    ratio, hits = _title_overlap(weak_text, docket)
    has_rfi_kw = _rfi_keyword_present(weak_text)
    has_date = _date_match(weak_text, docket)

    if (
        ratio >= _MIN_TITLE_OVERLAP_RATIO
        and hits >= _MIN_TITLE_OVERLAP_HITS
        and (has_rfi_kw or has_date)
    ):
        reason = f"title_overlap:{ratio:.2f}({hits})"
        if has_rfi_kw:
            reason += ",rfi_keyword"
        if has_date:
            reason += ",date"
        return True, reason

    return False, "no_match"


# ---------------------------------------------------------------------------
# File-level annotation
# ---------------------------------------------------------------------------

def _check_entry(
    entry: dict,
    docket: DocketMeta,
    strong_fields: tuple[str, ...],
    weak_fields: tuple[str, ...],
) -> dict:
    strong_text = " ".join(str(entry[f]) for f in strong_fields if entry.get(f))
    weak_text = " ".join(str(entry[f]) for f in weak_fields if entry.get(f))
    is_match, reason = classify_rfi_self_reference(strong_text, weak_text, docket)
    entry["rfi_self_reference"] = is_match
    entry["rfi_match_reason"] = reason
    return entry


def annotate_file(
    input_path: str | Path,
    output_path: str | Path,
    db_path: str | Path,
) -> None:
    """Reads a final_citation_data.json-shaped file (custom_id -> {citations,
    unmatched_bibliography}), flags each citation/bibliography entry that is
    most likely a self-reference to its own docket's RFI, and writes an
    annotated copy. Does not touch the DB or the classification/legal_citation
    fields already on each entry -- only adds rfi_self_reference/
    rfi_match_reason so the existing pipeline output can be filtered
    downstream."""
    dockets = load_dockets(db_path)
    comment_docket_map = load_comment_docket_map(db_path)

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    total = 0
    flagged = 0
    unknown_docket = 0

    for custom_id, record in data.items():
        comment_id = custom_id.rsplit("_", 1)[0]
        docket_id = comment_docket_map.get(comment_id)
        docket = dockets.get(docket_id) if docket_id else None

        if docket is None:
            unknown_docket += 1
            for entry in record.get("citations", []):
                total += 1
                entry["rfi_self_reference"] = False
                entry["rfi_match_reason"] = "docket_unknown"
            for entry in record.get("unmatched_bibliography", []):
                total += 1
                entry["rfi_self_reference"] = False
                entry["rfi_match_reason"] = "docket_unknown"
            continue

        for entry in record.get("citations", []):
            total += 1
            _check_entry(
                entry, docket,
                strong_fields=("citation_reference", "citation_footnote", "citation_link"),
                weak_fields=("citation_reference", "citation_footnote", "citation_link"),
            )
            flagged += entry["rfi_self_reference"]

        for entry in record.get("unmatched_bibliography", []):
            total += 1
            _check_entry(
                entry, docket,
                strong_fields=("text", "citation_link"),
                weak_fields=("text", "citation_link"),
            )
            flagged += entry["rfi_self_reference"]

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"[rfi_self_reference_filter] total_entries={total}  flagged={flagged}  "
          f"({flagged / total:.1%})  unknown_docket_comments={unknown_docket}")
    print(f"Wrote annotated data to {output_path}")


if __name__ == "__main__":
    annotate_file(
        input_path="/Users/saraht45/Reggov_SciSci_db/citations/citation_data/final_citation_data.json",
        output_path="/Users/saraht45/Reggov_SciSci_db/citations/citation_data/final_citation_data_rfi_flagged.json",
        db_path="/Users/saraht45/Reggov_SciSci_db/db/dockets.db",
    )
