# Citation dataset: what changed

Oct 16, 2026 · @Nic Weber

## In short

`merged_tiers_openalex_matched.json` keeps all 4,429 records in their original order and with their original keys. It adds author affiliations, and it corrects records that pointed at the wrong paper. 
| --- | --- | --- |
| Records | 4,429 | 4,429 |
| Records linked to a cited paper | 4,148 | 4,331 |
| Records whose paper has at least one affiliated organization | not recorded | 4,280 |
| Records pointing at a different paper than before |  | 262 |
| Distinct cited papers | 2,953 | 2,897 |
| Author slots on cited papers with an institution |  | 22,331 of 23,154 (96.4%) |

"Linked" counts records with `has_openalex_match` true in the original, and records with a paper in `enrichment.work_id` in the new file.

## New fields

Two top-level fields are new. `enrichment` is on every record. `original_match` is on the 2,005 records where a value was replaced.

**`enrichment`**

| Field | Meaning |
| --- | --- |
| `work_id` | The paper the citation points at after checking: an OpenAlex W id, or `X-...` for 12 papers OpenAlex does not hold. Null when no paper could be identified. |
| `openalex_id` | Same as `work_id` when it is an OpenAlex paper, otherwise null. |
| `match_status` | `original_match` (your match, kept as is; 4,018), `confirmed_original` (re-checked and kept; 138), `rematched` (moved to a different paper; 155), `newly_matched` (had none, now has one; 20), or one of `unresolved` (62), `wrong_match_not_in_openalex` (21), `unmatched` (11) and `citation_inconsistent` (4), which have no paper. |
| `match_method` | How the match was made or checked. |
| `work_title` | Title of that paper, corrected where OpenAlex has it wrong. |
| `n_authors`, `n_authors_with_affiliation` | Author count on the paper, and how many have an institution. |
| `min_confidence` | Lowest confidence among the paper's affiliations: high (3,331 records), medium (802), low (150). |
| `organizations` | One entry per organization on the paper: `organization`, `ror_id`, `org_type`, `country`, and `author_share`, the fraction of the paper's affiliated authors at it. Shares sum to 1 per record. Labs roll up to their parent (Google DeepMind to Alphabet). |
| `unmatched_source` | For the 98 records with no paper: `source_type` (report, law, news, web page and so on), `issuing_org`, `org_type`, `country`, `confidence`, `note`. |
| `exclude_review` | For the 110 records you flagged `legal_fragment`: `valid_citation` (83) or `repeat_in_same_doc` (27). |
| `enriched_on`, `release` | 2026-10-09, release v2. |

**`original_match`** holds only the fields that were replaced on that record, with their old values: the old match on 262 records, the old title and DOI on 34, and the old `institutions` list on 1,847.

## Existing fields that changed

Values changed in four ways; no existing field was renamed or removed. Your file mixes two record shapes: 2,523 flat records (`openalex_id`, `title`, `authors`...) and 1,906 records with a nested `openalex` object.

| Change | Records | Fields affected |
| --- | --- | --- |
| Moved to the correct paper (the original match was a different work) | 150 | Flat: `openalex_id`, `doi`, `arxiv_id`, `title`, `authors`, `year`, `venue`, `publication_type`, `cited_by_count`, `referenced_works_count`, `open_access`, `oa_url`, `concepts`, `status`, `match_source` (set to `affiliation_enrichment_rematch`), `confidence` (set to null). Nested: `openalex` replaced by the correct OpenAlex work, `match_reason` set to `affiliation_enrichment_rematch`. |
| Moved to a paper not in OpenAlex | 24 | Flat: same fields, filled from the paper's own page, `openalex_id` null, `match_source` = `affiliation_enrichment_paper_page`. Nested: `openalex` set to null. The paper is named in `enrichment`. |
| Wrong match removed, no correct paper exists | 88 | `openalex_id` / `openalex` set to null, `has_openalex_match` false. Mostly reports, news and web pages matched to unrelated papers (a Medium post by Hardt had been matched to a big-data survey). |
| Corrected title and DOI (OpenAlex holds the right authors under a wrong title or DOI) | 34 | Flat `title`, `doi`. Examples: LoRA, InstructGPT, Llama 2, GPT-4, Constitutional AI. |
| `institutions` rewritten (flat records only) | 1,847 | Now the cleaned, deduplicated institution names of the paper's authors (ROR display names where one exists). Records with a non-empty list rose from 1,346 to 2,496. |

Values re-fetched from OpenAlex for moved records (citation counts, concepts) are as of October 2026, while the rest of the file keeps your original pull's values.

## What did not change

- Record count, order, `citation_id`, `sub_index`, `row_index`, and all citation text fields (`citation_reference`, `citation_sentence`, `citation_footnote`, `citation_link`, `parsed_*`, `extra`).
- `exclude_reason`: your 110 `legal_fragment` flags stand; the review sits in `enrichment.exclude_review`.
- The nested `openalex` object on records whose paper did not change. Its affiliation data is the OpenAlex snapshot you pulled; the checked affiliations are in `enrichment.organizations`.
- Formatting: 2-space indented JSON array, as before.

One thing worth knowing from your data: 28 `citation_id` + `sub_index` pairs appear twice (56 records). Both copies of each pair get the same enrichment.

## Accuracy and caveats

Two blind audits checked 392 random author affiliations against the papers themselves. Of those that could be verified, OpenAlex-sourced affiliations were 84 to 90% correct and the web-checked ones 97%. The weakest sources (ORCID profiles, OpenAlex raw strings matched to organizations, author profiles) were then re-checked paper by paper and replaced.

- 823 author slots have no institution, mostly because the paper prints none: BLOOM (372 authors), BIG-bench contributors, independent researchers, and CMS authors listed only under the CERN cooperation-agreement block.
- 35 affiliations still rest on an author's profile rather than the paper; 169 are confidence low. Filter on `enrichment.min_confidence` for strict work.
- Lab and company reports often print one shared affiliation for all authors; those are confidence medium.
- Five papers with huge author lists (two CMS papers, BIG-bench, Foundation Models, an anomaly-detection paper) dominate author counts. Use `author_share`, which sums to 1 per record, to count by organization.
