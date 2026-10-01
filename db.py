"""
db.py
Engine creation and session factory. SQLite only — the local dockets.db
file next to this module.
"""

from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from models import Base

_SQLITE_URL = f"sqlite:///{Path(__file__).parent / 'dockets.db'}"

engine  = create_engine(_SQLITE_URL, echo=False)
Session = sessionmaker(bind=engine)


_FR_DOCKET_NEW_COLS = [
    ("additional_rins",    "VARCHAR(255)"),
    ("html_url",           "VARCHAR(512)"),
    ("full_text_xml_url",  "VARCHAR(512)"),
    ("comment_start_date", "VARCHAR(64)"),
    ("comment_end_date",   "VARCHAR(64)"),
    ("regs_document_id",   "VARCHAR(255)"),
    ("regs_docket_id",     "VARCHAR(255)"),
    ("comments_count",     "INTEGER"),
    ("comments_url",       "VARCHAR(512)"),
    ("api_url",            "VARCHAR(512)"),
    ("object_id",          "VARCHAR(255)"),
    ("agency_name",        "VARCHAR(255)"),
    ("agency_raw_name",    "VARCHAR(255)"),
    ("agency_id_number",   "INTEGER"),
    ("citation",           "INTEGER"),
]

_PUBLIC_COMMENTS_NEW_COLS = [
    ("non_profit_status", "VARCHAR(255)"),
]

_COMMENT_CITATIONS_NEW_COLS = [
    ("citation_footnote_text",             "TEXT"),
    ("status_code",                        "INTEGER"),
    ("reason_phrase",                      "VARCHAR(64)"),
    ("http_version",                       "VARCHAR(16)"),
    ("resolved_url",                       "VARCHAR(512)"),
    ("is_success",                         "BOOLEAN"),
    ("content_type",                       "VARCHAR(128)"),
    ("domain",                             "VARCHAR(255)"),
    ("domain_type",                        "VARCHAR(64)"),
    ("classification",                     "VARCHAR(64)"),
    ("unmatched_citation_reference_text",  "TEXT"),
    ("uncleaned_domain",                   "VARCHAR(255)"),
    ("classifier_used",                    "VARCHAR(64)"),
]


def _existing_columns(conn, table: str) -> set:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def _migrate(conn):
    """Add any missing columns to existing tables (safe to run repeatedly)."""
    fr_cols = _existing_columns(conn, "agency_docket")
    for col_name, col_type in _FR_DOCKET_NEW_COLS:
        if col_name not in fr_cols:
            conn.execute(text(f"ALTER TABLE agency_docket ADD COLUMN {col_name} {col_type}"))

    comment_cols = _existing_columns(conn, "public_comments")
    for col_name, col_type in _PUBLIC_COMMENTS_NEW_COLS:
        if col_name not in comment_cols:
            conn.execute(text(f"ALTER TABLE public_comments ADD COLUMN {col_name} {col_type}"))

    citation_cols = _existing_columns(conn, "comment_citations")
    for col_name, col_type in _COMMENT_CITATIONS_NEW_COLS:
        if col_name not in citation_cols:
            conn.execute(text(f"ALTER TABLE comment_citations ADD COLUMN {col_name} {col_type}"))


def init_db():
    """Create all tables if they don't already exist, then run migrations."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _migrate(conn)
    print(f"Database initialised at {engine.url}")


def reset_db():
    """Drop all tables and recreate from current models. ALL DATA WILL BE LOST."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    print(f"Database reset at {engine.url}")
