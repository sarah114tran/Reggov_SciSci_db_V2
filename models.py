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
    domain_registered = Column(S())     # PSL registrable domain; see loaders/citation_domain_org.py
    domain_org       = Column(S())      # short org name, e.g. blog.google -> google
    classifier_used  = Column(S(64))

    legal_citation_type = Column(S(64))
    legal_citation_text = Column(Text)

    comment        = relationship("PublicComment", back_populates="citations")
    resolved_works = relationship("CitationResolvedWork", back_populates="citation")


class CitationContext(Base):
    """Sentence before/after each citation_sentence, sliced verbatim from the
    comment's markdown. Built by loaders/citation_context.py (run separately).

    prev/next_sentence are NULL when context stops at a heading, table, or
    footnote/document edge; prev/next_boundary records which.
    """
    __tablename__ = "citation_context"

    citation_id     = Column(S(36), ForeignKey("comment_citations.citation_id"), primary_key=True)
    comment_id      = Column(S(), nullable=False)
    source_path     = Column(Text)          # relative to REGGOV_MD_ROOT
    match_method    = Column(S(32))         # exact | exact_body_flow | anchor | fuzzy | unmatched | empty_sentence | no_source_file
    match_score     = Column(Float)
    n_hits          = Column(Integer)       # >1 when the sentence occurs more than once in the file
    char_start      = Column(Integer)       # raw markdown offsets of source_sentence
    char_end        = Column(Integer)
    unit_type       = Column(S(16))         # body | footnote | table | heading
    source_sentence = Column(Text)
    prev_sentence   = Column(Text)
    next_sentence   = Column(Text)
    prev_boundary   = Column(S(32))
    next_boundary   = Column(S(32))


class CitationContextV2(Base):
    """citation_context with exactly one core sentence per citation and
    core ± 1 sentence of context. Built by loaders/citation_context_v2.py
    (run separately; see its docstring for the matching changes).

    core_method says how the core was chosen:
      footnote_number – the sentence carrying this citation's footnote marker
                        (relocated = 1 when it lies outside the matched span)
      single          – the matched span is one sentence
      link            – the span sentence containing citation_link
      marker          – the only span sentence carrying any citation marker
      span_last       – fallback: last sentence of a multi-sentence span
    Text columns are raw markdown slices; *_clean columns are markup-free.
    Several citations can share a window: dedupe on window_key when totalling.
    """
    __tablename__ = "citation_context_v2"

    citation_id     = Column(S(36), ForeignKey("comment_citations.citation_id"), primary_key=True)
    comment_id      = Column(S(), nullable=False)
    source_path     = Column(Text)
    match_method    = Column(S(32))         # exact | exact_nonbody | anchor | fuzzy | unmatched | empty_sentence | no_source_file
    match_score     = Column(Float)
    n_hits          = Column(Integer)
    match_unit_type = Column(S(16))         # unit the LLM sentence matched in: body | footnote | table | heading
    unit_type       = Column(S(16))         # unit of the core sentence
    repaired        = Column(Boolean)       # page-break continuation found from the LLM sentence's tail
    span_sentence   = Column(Text)          # whole matched LLM span
    span_char_start = Column(Integer)       # raw markdown offsets of span_sentence
    span_char_end   = Column(Integer)
    n_span_sents    = Column(Integer)
    n_marker_sents  = Column(Integer)       # span sentences carrying a citation marker
    citation_number = Column(Integer)       # footnote number, when recovered
    number_source   = Column(S(16))         # llm_marker | footnote_text | matched_footnote
    relocated       = Column(Boolean)       # core moved outside the matched span to the marker sentence
    core_method     = Column(S(32))
    core_sentence   = Column(Text)
    prev_sentence   = Column(Text)
    next_sentence   = Column(Text)
    core_clean      = Column(Text)
    prev_clean      = Column(Text)
    next_clean      = Column(Text)
    prev_boundary   = Column(S(32))
    next_boundary   = Column(S(32))
    n_window_sents  = Column(Integer)       # 1 + available neighbours (max 3)
    core_char_start = Column(Integer)       # raw markdown offsets, for deduping hedge cues
    core_char_end   = Column(Integer)
    prev_char_start = Column(Integer)
    prev_char_end   = Column(Integer)
    next_char_start = Column(Integer)
    next_char_end   = Column(Integer)
    window_key      = Column(S(64))         # "<comment_id>:<start>-<end>" of the whole window


class CitationContextV3(Base):
    """citation_context_v2 with an analysis scope per citation. Built by
    loaders/citation_context_v3.py (run separately; see its docstring).

    analysis_scope:
      in_scope            LLM sentence matched body text, core is a body sentence
      recovered           LLM sentence matched a footnote/reference; the body
                          sentence carrying its marker was not captured by the LLM
      excluded_duplicate  as recovered, but an in_scope row already covers that
                          sentence (duplicate_of)
      excluded_footnote / excluded_reference / excluded_table /
      excluded_heading / excluded_bare_link
      unmatched / empty_sentence / no_source_file
    Analyse in_scope + recovered; dedupe on window_key when totalling.
    """
    __tablename__ = "citation_context_v3"

    citation_id     = Column(S(36), ForeignKey("comment_citations.citation_id"), primary_key=True)
    comment_id      = Column(S(), nullable=False)
    source_path     = Column(Text)
    analysis_scope  = Column(S(32))
    recovered       = Column(Boolean)
    duplicate_of    = Column(S(36))         # citation_id of the in_scope row covering a duplicate
    match_method    = Column(S(32))         # exact | exact_nonbody | anchor | fuzzy | unmatched | empty_sentence | no_source_file
    match_score     = Column(Float)
    n_hits          = Column(Integer)
    match_unit_type = Column(S(16))         # body | footnote | reference | table | heading
    unit_type       = Column(S(16))         # unit of the core sentence
    repaired        = Column(Boolean)
    span_sentence   = Column(Text)
    span_char_start = Column(Integer)
    span_char_end   = Column(Integer)
    n_span_sents    = Column(Integer)
    n_marker_sents  = Column(Integer)
    citation_number = Column(Integer)
    number_source   = Column(S(16))         # llm_marker | footnote_text | matched_footnote
    relocated       = Column(Boolean)
    core_method     = Column(S(32))
    core_sentence   = Column(Text)
    prev_sentence   = Column(Text)
    next_sentence   = Column(Text)
    core_clean      = Column(Text)
    prev_clean      = Column(Text)
    next_clean      = Column(Text)
    prev_boundary   = Column(S(32))
    next_boundary   = Column(S(32))
    n_window_sents  = Column(Integer)
    core_char_start = Column(Integer)
    core_char_end   = Column(Integer)
    prev_char_start = Column(Integer)
    prev_char_end   = Column(Integer)
    next_char_start = Column(Integer)
    next_char_end   = Column(Integer)
    window_key      = Column(S(64))


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
