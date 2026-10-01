"""
models.py
SQLAlchemy ORM definitions for the federal docket comment database.
UUID primary keys (S(36)) are generated at insert time; PublicComment uses
the regulations.gov comment ID as its natural string primary key.
"""

from sqlalchemy import (
    Column, String, Integer, Text, DateTime,
    ForeignKey, Boolean, Float
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

S   = lambda n=255: String(n)   # noqa: E731  shorthand for varchar(n)


class Docket(Base):
    __tablename__ = "agency_docket"

    docket_id            = Column(S(36), primary_key=True)
    regs_document_id        = Column(S())
    regs_docket_id          = Column(S())
    document_number      = Column(S(),   unique=True)
    regulation_id_number = Column(S())
    additional_rins         = Column(S())

    title                   = Column(Text)
    toc_doc                 = Column(Text)
    doc_type                = Column(S())
    action                  = Column(S())
    abstract                = Column(Text)

    publication_date        = Column(S(64))
    effective_on            = Column(S(64))
    comment_start_date      = Column(S(64))
    comment_end_date        = Column(S(64))
    significant             = Column(Boolean)
    citation                  = Column(Integer)

    html_url                = Column(S(512))
    full_text_xml_url       = Column(S(512))

    comments_count          = Column(Integer)
    comments_url            = Column(S(512))

    api_url                 = Column(S(512))
    object_id               = Column(S())

    agency_name             = Column(S())
    agency_raw_name         = Column(S())
    agency_id_number            = Column(Integer)

    public_comments = relationship("PublicComment", back_populates="docket")


class PublicComment(Base):
    __tablename__ = "public_comments"

    comment_id             = Column(S(),    primary_key=True)
    regs_docket_id         = Column(S(36),  ForeignKey("agency_docket.docket_id"), nullable=False)
    reg_document_id        = Column(S())
    document_number        = Column(S())
    agency_id              = Column(S(64))
    agency_name            = Column(S())
    source                 = Column(S())
    submitted_date         = Column(S(64))
    title                  = Column(Text)
    page_count             = Column(Integer)
    attachment_count       = Column(Integer)

    comment_link           = Column(Text)
    gcs_pdf_uri            = Column(S(512)) # originally called "gcs_url"
    gcs_md_uri             = Column(S(512))
    gcs_public_url         = Column(S(512))
    filename               = Column(S())
    uploaded_at            = Column(DateTime(timezone=True))
    
    commenter_name         = Column(Text)
    organization_name      = Column(Text)
    organization_root_name = Column(S())
    commenter_type         = Column(S())
    naics_code             = Column(Text)
    naics_title            = Column(Text)
    confidence             = Column(Integer)

    comment_text           = Column(Text)
    citation_type          = Column(S())
    duplicate              = Column(Boolean)
    inclusion              = Column(Boolean)
    processed              = Column(Boolean)
    non_profit_status      = Column(S())

    docket    = relationship("Docket",          back_populates="public_comments")
    citations = relationship("CommentCitation", back_populates="comment")
    authors   = relationship("CommentAuthor",   back_populates="comment")

class CommentAuthor(Base):
    __tablename__ = "comment_authors"

    author_id                    = Column(S(36), primary_key=True)
    comment_id                   = Column(S(),   ForeignKey("public_comments.comment_id"), nullable=False)
    author_name                  = Column(S())
    author_order                 = Column(Integer)
    organization_root_name       = Column(S())
    author_parent_affiliation    = Column(S())
    author_secondary_affiliation = Column(S())
    author_job_title             = Column(S())
    commenter_type               = Column(S())
    naics_code                   = Column(Text)
    naics_title                  = Column(Text)
    confidence                   = Column(Integer)
   
    comment = relationship("PublicComment", back_populates="authors")


class CommentCitation(Base):
    __tablename__ = "comment_citations"

    citation_id              = Column(S(36), primary_key=True)
    comment_id               = Column(S(),   ForeignKey("public_comments.comment_id"), nullable=False)
    citation_sentence        = Column(Text)
    citation_link            = Column(Text)
    citation_footnote_text   = Column(Text)
    citation_reference_text  = Column(Text)
    unmatched_citation_reference_text = Column(Text)

    cited_source_catagory  = Column(S(64))
    status_code     = Column(Integer)
    reason_phrase   = Column(S(64))
    http_version    = Column(S(16))
    resolved_url    = Column(Text)
    is_success      = Column(Boolean)

    domain          = Column(S())
    uncleaned_domain = Column(S())
    classifier_used  = Column(S(64))

    legal_citation_type = Column(S(64))
    legal_citation_text = Column(Text)

    comment        = relationship("PublicComment", back_populates="citations")
    resolved_works = relationship("CitationResolvedWork", back_populates="citation")


class CitationResolvedWork(Base):
    """One parsed/resolved publication extracted from a citation instance.

    A single citation_reference_text can split into multiple works (e.g. a
    string like "Smith 2020; Jones 2019"), so this is keyed by
    (citation_id, sub_index) rather than assuming a 1:1 relationship between
    a CommentCitation row and a resolved work.
    """
    __tablename__ = "citation_resolved_works"

    resolved_id = Column(S(36), primary_key=True)  # make_id("resolved_work", citation_id, sub_index)
    citation_id = Column(S(36), ForeignKey("comment_citations.citation_id"), nullable=False)
    sub_index   = Column(Integer, nullable=False, default=0)

    parsed_author = Column(S())
    parsed_title  = Column(Text)
    parsed_year   = Column(Integer)

    match_source = Column(S(64))  # embedded_doi / arxiv_id / crossref_query / llm_parsed
    confidence   = Column(Float)
    status       = Column(S(32))  # matched / tentative / low_confidence / unmatched

    # Resolved OpenAlex match, if any (a work can be cited by many comments).
    openalex_work_id = Column(S(), ForeignKey("academic_works.work_id"))

    citation      = relationship("CommentCitation", back_populates="resolved_works")
    academic_work = relationship("AcademicWork",    back_populates="citation_matches")


class AcademicWork(Base):
    """A distinct paper as resolved from OpenAlex. Title/DOI/citation_count live
    here rather than on CitationResolvedWork so a paper cited by many comments
    is stored once instead of duplicated (and drifting) per citation."""
    __tablename__ = "academic_works"

    work_id          = Column(S(),  primary_key=True)  # OpenAlex work ID
    doi              = Column(S())
    title            = Column(Text)
    publication_year = Column(Integer)
    citation_count   = Column(Integer)

    authors          = relationship("WorkAuthor",          back_populates="work")
    citation_matches = relationship("CitationResolvedWork", back_populates="academic_work")


class WorkAuthor(Base):
    __tablename__ = "work_authors"

    work_author_id     = Column(S(36), primary_key=True)
    work_id            = Column(S(),   ForeignKey("academic_works.work_id"), nullable=False)
    author_name        = Column(S())
    author_order       = Column(Integer)
    author_affiliation = Column(S())

    work = relationship("AcademicWork", back_populates="authors")
