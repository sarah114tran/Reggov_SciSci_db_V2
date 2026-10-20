import json, re, collections

URL_RE = re.compile(r'://|www\.\w')

ID_IBID_RE = re.compile(r'\b(id\.?|ibid\.?|ibid\*|see\s+(also\s+)?id\.?|supra|infra)\b', re.I)

AUTHOR_ONLY_RE = re.compile(
    r'^[A-Z][\w&.\'-]*(\s+(and|&)\s+[A-Z][\w.\'-]*|,?\s+et\s+al\.?)?'
    r'(,)?\s*\(?\d{4}\)?[.,]?\s*(n\.d\.)?[.,]?$'
)

LEGAL_FRAGMENT_RE = re.compile(
    r'\bU\.?S\.?C\.?\b|\bC\.?F\.?R\.?\b|\bFed\.?\s*Reg(ister)?\b|§|'
    r'\bSec(tion)?\.?\s*\d|\bpg\.?\s*\d|\bp\.\s*\d|\bat\s+\d|\bF\.\s*(2d|3d)\b|'
    r'\bRFI\b|\bISO\b|\bMPEP\b|\bPub\.?\s*L\.?\b|\bF\.R\.\b|\bFR\s*\d|'
    r'\b(S\.|H\.R\.)\s*\d+\b|\bCong\.'
)

GARBAGE_RE = re.compile(r'!\[|&lt;sup|_page_\d+_Picture|^\s*\d+\.\s*$')


def classify_reference(ref: str, has_match: bool) -> str | None:
    """Returns the exclusion bucket name if `ref` should be filtered out,
    or None if it survives (i.e. still worth reviewing for a web search).

    "url" only counts as an exclusion reason when the record has NOT already
    been resolved to an OpenAlex record -- a bare URL that already matched
    doesn't need to be excluded from anything, it's just done."""
    ref = (ref or "").strip()
    if not ref or GARBAGE_RE.search(ref):
        return "garbage"
    if URL_RE.search(ref) and not has_match:
        return "url"
    if ID_IBID_RE.search(ref):
        return "id_ibid"
    if AUTHOR_ONLY_RE.match(ref):
        return "author_only"
    if LEGAL_FRAGMENT_RE.search(ref):
        return "legal_fragment"
    return None



def has_openalex_match(entry: dict) -> bool:
    """tier_2/tier_3 nest the match under "openalex"; tier_1 flattens it
    onto the record directly as "openalex_id" -- check both conventions."""
    return bool(entry.get("openalex")) or bool(entry.get("openalex_id"))


file_names = [
    "/Users/saraht45/Reggov_SciSci_db/citations/citation_parsing/citation_data/tier_1_oa_enriched.json",
    "/Users/saraht45/Reggov_SciSci_db/citations/citation_parsing/citation_data/tier_2_oa_enriched.json",
    "/Users/saraht45/Reggov_SciSci_db/citations/citation_parsing/citation_data/tier_3_oa_enriched.json",
]

# retreive_doi_output records that never got binned into a tier (Crossref
# low_confidence/tentative/unmatched) -- carries a different, noisier schema
# (confidence, match_source, doi, etc.), so only the four fields relevant to
# classification/review are kept before merging it in alongside the tiers.
MISSING_FROM_TIERS_PATH = "/Users/saraht45/Reggov_SciSci_db/citations/citation_parsing/final_output/retreive_doi_output_missing_from_tiers.json"
MISSING_FROM_TIERS_FIELDS = ["citation_reference", "citation_id", "citation_sentence", "citation_footnote"]

OPENALEX_MATCHED_PATH = "/Users/saraht45/Reggov_SciSci_db/citations/citation_parsing/final_output/merged_tiers_openalex_matched.json"
CANDIDATES_PATH       = "/Users/saraht45/Reggov_SciSci_db/citations/citation_parsing/final_output/merged_tiers_candidates.json"
EXCLUDED_PATH         = "/Users/saraht45/Reggov_SciSci_db/citations/citation_parsing/final_output/merged_tiers_excluded.json"

merged_data = []
for file_name in file_names:
    with open(file_name, "r") as infile:
        merged_data.extend(json.load(infile))

with open(MISSING_FROM_TIERS_PATH, "r") as infile:
    missing_from_tiers = json.load(infile)
merged_data.extend(
    {field: e.get(field) for field in MISSING_FROM_TIERS_FIELDS}
    for e in missing_from_tiers
)

for e in merged_data:
    e["has_openalex_match"] = has_openalex_match(e)
    e["exclude_reason"] = classify_reference(e.get("citation_reference"), e["has_openalex_match"])

openalex_matched = [e for e in merged_data if e["has_openalex_match"]]
candidates       = [e for e in merged_data if e["exclude_reason"] is None and not e["has_openalex_match"]]
excluded         = [e for e in merged_data if e["exclude_reason"] is not None]

with open(OPENALEX_MATCHED_PATH, "w") as f:
    json.dump(openalex_matched, f, indent=2)
with open(CANDIDATES_PATH, "w") as f:
    json.dump(candidates, f, indent=2)
with open(EXCLUDED_PATH, "w") as f:
    json.dump(excluded, f, indent=2)

# ---------------------------------------------------------------- EDA -----
total = len(merged_data)
print("="*70)
print("EDA: merged_tiers_oa (tier_1 + tier_2 + tier_3 + missing_from_tiers)")
print("="*70)
print(f"total records:                 {total}")
print(f"  openalex_matched.json:       {len(openalex_matched):5d}  ({len(openalex_matched)/total:.1%})")
print(f"  candidates.json (no filter): {len(candidates):5d}  ({len(candidates)/total:.1%})")
print(f"  excluded.json (filtered):    {len(excluded):5d}  ({len(excluded)/total:.1%})")

print()
print("-- breakdown of excluded.json by reason --")
reason_counts = collections.Counter(e["exclude_reason"] for e in excluded)
for reason, count in reason_counts.most_common():
    print(f"  {count:5d}  ({count/total:5.1%} of total, {count/len(excluded):5.1%} of excluded)  {reason}")

print()
print("-- openalex match rate, by bucket --")
def rate(group):
    n = len(group)
    matched = sum(1 for e in group if e["has_openalex_match"])
    return n, matched, (matched/n if n else 0)

n, m, r = rate(candidates)
print(f"  candidates:      {m:5d}/{n:5d}  ({r:.1%}) already have an OpenAlex match")
for reason in reason_counts:
    bucket = [e for e in excluded if e["exclude_reason"] == reason]
    n, m, r = rate(bucket)
    print(f"  excluded/{reason:14s} {m:5d}/{n:5d}  ({r:.1%}) already have an OpenAlex match")

print()
print(f"-- breakdown of the {len(missing_from_tiers)} missing_from_tiers records just merged in --")
missing_ids = {e["citation_id"] for e in missing_from_tiers}
missing_classified = [e for e in merged_data if e["citation_id"] in missing_ids]
missing_reason_counts = collections.Counter(e["exclude_reason"] for e in missing_classified)
for reason, count in missing_reason_counts.most_common():
    print(f"  {count:5d}  ({count/len(missing_classified):5.1%})  {reason if reason else 'candidate (no filter match)'}")
