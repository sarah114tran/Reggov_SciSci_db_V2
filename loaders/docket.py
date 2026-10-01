"""
loaders/docket.py
Loads Docket metadata from fr_reg_metadata.json.

Each entry is expected to have 'fr_data' and optionally 'reg_data' keys.
All fields are flattened into a single Docket row — no child tables.

    # From file:
    load_fr_dockets("data/fr_reg_metadata.json", session)

    # From pre-loaded list:
    load_fr_dockets(entries, session)
"""

import json
from pathlib import Path
from sqlalchemy.orm import Session
from models import Docket
from utils import make_id


def _serialize(value):
    """Convert lists/dicts to a JSON string; pass scalars through unchanged."""
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


def _map_fr_docket(entry: dict) -> dict:
    """
    Map a full fr_reg_metadata entry → Docket field dict.
    Pulls from both fr_data and reg_data, preferring fr_data where both provide a field.
    """
    raw      = entry.get("fr_data", entry)
    reg_data = entry.get("reg_data") or {}
    reg_inner = reg_data.get("data", reg_data)
    reg_attrs = (reg_inner.get("attributes") or {}) if isinstance(reg_inner, dict) else {}
    reg_links = (reg_inner.get("links") or {}) if isinstance(reg_inner, dict) else {}
    reg_info  = raw.get("regulations_dot_gov_info") or {}

    # Agency: comma-join all names if multiple agencies
    agencies       = raw.get("agencies") or []
    agency_names   = [a.get("name") for a in agencies if a.get("name")]
    agency_raws    = [a.get("raw_name") for a in agencies if a.get("raw_name")]
    first_agency   = agencies[0] if agencies else {}

    # Comment dates and submit URL from dockets[].documents[]
    comment_start_date = None
    comment_end_date   = None
    comment_url        = None
    for docket_block in raw.get("dockets") or []:
        for doc in docket_block.get("documents") or []:
            comment_start_date = comment_start_date or doc.get("comment_start_date")
            comment_end_date   = comment_end_date   or doc.get("comment_end_date")
            comment_url        = comment_url        or doc.get("comment_url")

    # Fallback to reg_data if fr_data dockets were empty
    comment_start_date = comment_start_date or reg_attrs.get("commentStartDate")
    comment_end_date   = comment_end_date   or reg_attrs.get("commentEndDate")

    return {
        "document_number":      raw.get("document_number"),
        "regulation_id_number": reg_info.get("regulation_id_number") or raw.get("regulation_id_number"),
        "additional_rins":         _serialize(reg_attrs.get("additionalRins")),

        "title":                   raw.get("title"),
        "toc_doc":                 raw.get("toc_doc"),
        "doc_type":                raw.get("type"),
        "action":                  raw.get("action"),
        "abstract":                raw.get("abstract"),

        "publication_date":        raw.get("publication_date"),
        "effective_on":            raw.get("effective_on"),
        "comment_start_date":      comment_start_date,
        "comment_end_date":        comment_end_date,
        "significant":             raw.get("significant"),
        "citation":                raw.get("citation"),

        "html_url":                raw.get("html_url"),
        "full_text_xml_url":       raw.get("full_text_xml_url"),

        "regs_document_id":        reg_info.get("document_id") or (reg_inner.get("id") if isinstance(reg_inner, dict) else None),
        "regs_docket_id":          reg_info.get("docket_id")   or reg_attrs.get("docketId"),
        "comments_count":          reg_info.get("comments_count"),
        "comments_url":            reg_info.get("comments_url"),

        "api_url":                 reg_links.get("self"),
        "object_id":               reg_attrs.get("objectId"),

        "agency_name":             ", ".join(agency_names) if agency_names else None,
        "agency_raw_name":         ", ".join(agency_raws)  if agency_raws  else None,
        "agency_id_number":        first_agency.get("id"),
    }


def load_fr_dockets(
    source: "str | Path | list",
    session: Session,
    update: bool = True,
) -> dict[str, str]:
    """
    Load Docket records from fr_reg_metadata.json (or a pre-loaded list).

    Returns:
        {document_number / regs_docket_id → docket_id (UUID)}
        Pass this to load_comments() for FK resolution.
    """
    if isinstance(source, (str, Path)):
        with open(Path(source), encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = source

    if isinstance(data, dict):
        data = [data]

    key_map: dict[str, str] = {}
    inserted = updated = skipped = 0

    for entry in data:
        raw        = entry.get("fr_data", entry)
        doc_number = raw.get("document_number", "")
        docket_id  = make_id("docket", doc_number)

        # Build regs.gov ID index regardless of insert/skip
        reg_info    = raw.get("regulations_dot_gov_info") or {}
        regs_doc_id = reg_info.get("document_id") or ""
        regs_dkt_id = reg_info.get("docket_id") or ""

        # Also index from reg_data (covers cases where reg_info fields are None)
        reg_data  = entry.get("reg_data") or {}
        reg_inner = reg_data.get("data", reg_data) if isinstance(reg_data, dict) else {}
        reg_attrs = reg_inner.get("attributes") or {} if isinstance(reg_inner, dict) else {}
        if not regs_doc_id:
            regs_doc_id = (reg_inner.get("id") or "") if isinstance(reg_inner, dict) else ""
        if not regs_dkt_id:
            regs_dkt_id = reg_attrs.get("docketId") or ""

        for docket_block in raw.get("dockets") or []:
            bid = docket_block.get("id")
            if bid:
                key_map[bid] = docket_id

        key_map[doc_number] = docket_id
        if regs_doc_id:
            key_map[regs_doc_id] = docket_id
        if regs_dkt_id:
            key_map[regs_dkt_id] = docket_id

        if session.get(Docket, docket_id):
            if update:
                session.merge(Docket(docket_id=docket_id, **_map_fr_docket(entry)))
                updated += 1
            else:
                skipped += 1
            continue

        session.add(Docket(docket_id=docket_id, **_map_fr_docket(entry)))
        inserted += 1

    session.commit()
    print(f"[fr_dockets] inserted={inserted}  updated={updated}  skipped={skipped}")
    return key_map
