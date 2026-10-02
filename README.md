# Reggov SciSci DB

A self-contained ETL pipeline that builds a local SQLite database of federal
regulatory dockets, public comments, their citations, and author/organization
classifications. This repo contains everything needed to build the database
from scratch: the processed input data files and the scripts that load them.

It's a trimmed export from a larger working repo — only the final-stage
loaders and their direct inputs are included here, not the upstream scraping
and classification pipelines that originally produced those inputs.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Build the database

```bash
python main.py
```

This creates `dockets.db` (SQLite) in the repo root. The script is
idempotent — all row IDs are deterministic UUIDs derived from natural keys
(see `utils.py`), so re-running it won't create duplicates.

## Validate

```bash
python validate.py
```

> **Note:** `validate.py` currently checks table names (`federal_docket`,
> `comment_paragraphs`, `citation_metadata`, ...) that predate the schema in
> `models.py` (which uses `agency_docket`, `public_comments`,
> `comment_citations`, etc.). It needs to be updated to match the current
> schema before it will run correctly.

## Pipeline

`main.py` orchestrates the load in this order:

| Step | Loader | Reads |
|---|---|---|
| FR dockets | `loaders/docket.py` | `data/docket_metadata.json` |
| Comments | `loaders/comments.py` | `data/normalized_comment_metadata.json` (+ `data/filtered_comment_metadata.json` for organization_root_name) |
| Citations | `loaders/gemini_citations.py` | `data/citation_data/extracted_citations/final_citation_data.json` |
| Citation reclassification (high-confidence) | `loaders/citation_reclassification.py` | `data/citation_reclassification/*.csv`, `merged_tiers_openalex_matched.json` |
| RFI self-reference cleanup | `loaders/rfi_self_reference_cleanup.py` | `data/rfi_self_reference_citation_ids.csv` |
| Educational-resource reclassification | `loaders/edu_resources_reclassification.py` | `data/citation_reclassification/edu_resources_classified.json` |
| Resolved OpenAlex works | `loaders/citation_resolved_works.py` | `data/citation_data/citation_resolved_works_normalized.json` |
| Comment authorship backfill | `loaders/comment_authorship.py` | `data/comment_authorship.json` |
| Per-author rows | `loaders/comment_authors.py` | `data/processed_authorship.json` |
| NAICS / commenter classification | `loaders/naics_classification.py` | `data/processed_authorship_with_naics.json`, `data/unique_orgs_with_naics.json` |
| Org classification corrections | `loaders/additional_org_classification.py` | `data/additional_classified_orgs_final.csv` |
| Individual reclassification (2nd pass) | `loaders/individual_reclassification.py` | `data/classified_all.json` |


## Schema

See `models.py` for the full SQLAlchemy schema: `Docket`, `PublicComment`,
`CommentAuthor`, `CommentCitation`, `CitationResolvedWork`, `AcademicWork`,
`WorkAuthor`.
