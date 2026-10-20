"""
cleaning_scripts/apply_citation_filters.py
Runs the citation-cleaning filters over every comment_citations row in
dockets.db. A dry run writes a review CSV of everything flagged; --apply
then deletes the flagged rows (plus their dependent citation_resolved_works /
citation_context* rows) after backing up the DB and writing a JSON copy of
every deleted row.

Rows with a citation_link are never flagged by any filter -- a link is still
a usable pointer to the source.

Text fields checked: citation_reference_text, citation_footnote_text,
unmatched_citation_reference_text (citation_sentence is prose, never the
citation itself) -- except for rows where all three are empty, which are
checked on citation_sentence instead. Apart from rfi_self_reference, a row is only flagged when
ALL of its non-empty text fields match the same filter, so a real reference
paired with an "Id." footnote survives.

Pass 1 (applied 2026-10-18), first match wins:
  rfi_self_reference  loaders/rfi_self_reference_filter.py -- the per-docket
                      signal classifier (own document number, FR citation,
                      NIST ID, URL to the notice, title words + RFI keyword).
  id_ibid_only        just "Id."/"Ibid."/"i.b." plus at most a pinpoint or an
                      explanatory parenthetical.
  author_only         "Smith 2020", "Smith et al., 2021".

Pass 2 adds, after rfi_self_reference / id_ibid_only (which now also accepts
"See e.g., id." and "arts." pinpoints):
  author_year         names/orgs + year, optional pinpoint:
                      "Yeung (2021, p. 20)", "Lawrence, Cui, and Ho 2023",
                      "State Policy Network, 2021", "Sidney Black et al. [2022."
  short_form_pinpoint short name + "at <page>": "Kapoor and Bommasani at 9",
                      "Draft Memorandum at 11", "Appendix B at 9."
  internal_xref       "Sec. 8.b.iv.", "Pg. 6", "See footnotes 18 and 44."
  roman_see           "ix See iv."
  bare_marker         "[2]", "29", "B", "See"
  bracket_keys        "[LAS+16,RBW+19 ]"
  year_only           "(2015)."
  fr_fragment         FR cite with no volume: "Fed. Reg. 22,440."
  name_only           "OpenAI", "Hickok-Hu". Laws (legal_citation rows or
                      LAW_ACRONYMS) are NOT deleted; they're listed in the
                      review file as name_only_kept_law with keep=y.

Pass 4 = pass-3 rules rerun with the citation_sentence fallback (the 938
sentence-only, no-category rows). No-category rows stay separate from
'unclassified'.

Usage (from the repo root):
  ./venv/bin/python cleaning_scripts/apply_citation_filters.py --pass 2
      dry run; writes cleaning_audit/pass2_review_<date>.csv
  (mark keep=y on any row in the review file that should stay)
  ./venv/bin/python cleaning_scripts/apply_citation_filters.py --pass 2 \\
      --apply --review-file cleaning_audit/pass2_review_<date>.csv
"""
import argparse
import collections
import csv
import json
import random
import re
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from loaders.rfi_self_reference_filter import (  # noqa: E402
    _parse_fr_citation, classify_rfi_self_reference, load_comment_docket_map, load_dockets,
)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import reclassify_citations as reclassify  # noqa: E402

DB_PATH   = ROOT / "dockets.db"
AUDIT_DIR = ROOT / "cleaning_audit"
TEXT_FIELDS = ("citation_reference_text", "citation_footnote_text", "unmatched_citation_reference_text")
UNCLASSIFIED_SAMPLE_SIZE = 1000
DEPENDENT_TABLES = ("citation_resolved_works", "citation_context", "citation_context_v2", "citation_context_v3")
KEEP_VALUES = {"y", "yes", "keep", "1", "true", "x"}
KEPT_LAW = "name_only_kept_law"

# ── id / ibid ────────────────────────────────────────────────────────────────
ID_LEAD_RE = re.compile(
    r'^\s*(&lt;sup>|<sup>)?\s*((\d+|[ivxlc]+)\.?\s+)?[*_]*'
    r'((see(\s+also|\s+generally)?|e\.\s*g\.)\s*,?\s+)?'
    r'(id|ibid(em)?|i\.\s*b)\b\.?[*_]*', re.I)
QUOTE_RE = re.compile(r'["“][^"”]*["”]')
SUBSECTION_RE = re.compile(r'\(\s*[A-Za-z0-9]{1,5}\s*\)')
PAREN_RE = re.compile(r'\([^()]*\)')
SOURCE_PAREN_RE = re.compile(r'\b(quoting|citing|cited in|available at)\b|https?://|www\.', re.I)
PINPOINT_WORDS = {
    "at", "p", "pp", "pg", "pgs", "page", "pages", "section", "sections", "sec",
    "para", "paras", "fig", "figure", "art", "arts", "article", "articles", "tbl",
    "table", "n", "nn", "note", "notes", "line", "lines", "and", "respectively",
    "see", "also", "id", "ibid", "part", "ch", "chapter", "slide", "app",
    "appendix", "cl", "intro", "commitment", "amendment", "recital", "principle",
    "question",
}
SEE_EG_RE = re.compile(r'^(\s*)see\s*,?\s*e\.\s*g\.\s*,?\s*', re.I)


def is_id_ibid_only(text: str, see_eg: bool = False) -> bool:
    if see_eg:
        text = SEE_EG_RE.sub(r'\1See ', text)
    m = ID_LEAD_RE.match(text)
    if not m:
        return False
    rest = SUBSECTION_RE.sub(" ", QUOTE_RE.sub(" ", text[m.end():]))
    parens = []
    while PAREN_RE.search(rest):
        parens += PAREN_RE.findall(rest)
        rest = PAREN_RE.sub(" ", rest)
    if any(SOURCE_PAREN_RE.search(p) for p in parens):
        return False
    for word in re.findall(r'[A-Za-z]{2,}', rest):
        if word.lower() not in PINPOINT_WORDS and not re.fullmatch(r'[ivxlc]+', word, re.I):
            return False
    return True


# ── pass 1 author fragment ───────────────────────────────────────────────────
AUTHOR_ONLY_RE = re.compile(
    r'^[A-Z][\w&.\'-]*(\s+(and|&)\s+[A-Z][\w.\'-]*|,?\s+et\s+al\.?)?'
    r'(,)?\s*\(?\d{4}\)?[.,]?\s*(n\.d\.)?[.,]?$'
)

# ── pass 2 fragments ─────────────────────────────────────────────────────────
# One capitalized word (Unicode letters: "Ćirković"), or an initial ("V.").
_W = r"[A-ZÀ-ɏ][A-Za-zÀ-ɏ&.'’-]*"
_NAME = rf"{_W}(?:\s+{_W}){{0,3}}"
_AUTHORS = (rf"{_NAME}(?:\s*,\s*{_NAME}){{0,5}}(?:\s*,?\s+(?:and|&)\s+{_NAME})?"
            r"(?:\s*,?\s+et\s+al\.?)?")
_YEAR = r"(?:19|20)\d\d[a-z]?"
_PIN = r"(?:\s*,?\s*(?:at\s+)?(?:pp?\.|pg\.?|pages?)?\s*[\d–-]+)?"
_LEAD = r"(?:(?:see|cf\.)(?:\s*,?\s*e\.\s*g\.)?\s*,?\s+)?"
_END = r"\s*[.,;]?\s*$"

AUTHOR_YEAR_RE = re.compile(
    rf"^\s*{_LEAD}{_AUTHORS}\s*,?\s*(?:\(\s*{_YEAR}{_PIN}\s*\)|\[?\s*{_YEAR}\]?{_PIN}){_END}")
LEGAL_GUARD_RE = re.compile(r"\b(Act|MPEP|U\.?S\.?C|C\.?F\.?R|Stat|Pub\.?\s*L)\b|§")
# Words that mean the "names" are really a title ("OpenAI, Technical Report (2023)").
TITLE_GUARD_RE = re.compile(
    r"\b(Report|Paper|Review|Journal|Guide|Guidance|Framework|Study|Survey|Brief|"
    r"Handbook|Proceedings|Conference|Memorandum|Memo|Order|Rule|Notice|Plan|Strategy|Model)s?\b", re.I)
SHORT_FORM_RE = re.compile(
    rf"^\s*{_LEAD}(?!(?:19|20)\d\d)[^\d\s][^\d;]{{0,80}}?,?\s+at\s+(?:pp?\.\s*)?\*?[\d,–-]+"
    rf"(?:\s*(?:,|and|&)\s*[\d–-]+)*{_END}", re.I)
_XREF_LABEL = (r"(?:sec(?:tion)?s?\.?|§+|pg\.?|pp?\.|pages?|footnotes?|notes?|nn?\.|"
               r"fig(?:ure)?\.?|table|part|annex|art(?:icle)?s?\.?)")
_XREF_ITEM = rf"(?:{_XREF_LABEL}\s*(?=[\w.]*\d)[\w.–-]+|(?:appendix|app\.)\s*[A-Z\d][\w.–-]*)"
INTERNAL_XREF_RE = re.compile(
    rf"^\s*{_LEAD}{_XREF_ITEM}(?:\s*(?:,|and|&|at)\s*(?:{_XREF_ITEM}|[\d–-]+))*{_END}", re.I)
ROMAN_SEE_RE = re.compile(r"^\s*-?\s*[ivxlc]+\.?\s+see\s+[ivxlc]+\.?\s*$", re.I)
BARE_MARKER_RE = re.compile(r"^\s*(?:\[\s*\d+\s*\]|\d{1,4}\.?|[A-Za-z]\.?|see\.?|-+)\s*$", re.I)
BRACKET_KEYS_RE = re.compile(
    r"^\s*\[\s*[A-Za-z][\w+]*\d{2}[a-z]?(?:\s*,\s*[A-Za-z][\w+]*\d{2}[a-z]?)*\s*\]\s*[.,;]?\s*$")
YEAR_ONLY_RE = re.compile(rf"^\s*\(?\s*{_YEAR}\s*\)?{_END}")
FR_FRAGMENT_RE = re.compile(
    r"^\s*(?:see\s+)?Fed\.?\s*Reg\.?\s*([\d,]+)(?:\s*\([^)]*\))?\s*[.,;]?\s*$", re.I)
NAME_ONLY_RE = re.compile(rf"^\s*{_W}(?:\s+{_W})?\s*\.?\s*$")
LAW_ACRONYMS = {
    "COPPA", "FERPA", "FISMA", "GLBA", "CCPA", "CPRA", "ITAR", "EAR", "HIPAA",
    "HITECH", "GDPR", "ECPA", "CFAA", "DMCA", "FCRA", "ECOA", "ADA", "TCPA",
    "FTCA", "FOIA", "APA", "ADEA", "CAN-SPAM", "FDCA", "BIPA", "VPPA", "CHIPS",
    "NDAA", "IEEPA", "CISA", "PRA", "EEOA", "SOX",
}


# ── pass 3 removals ──────────────────────────────────────────────────────────
# PDF-to-markdown leftovers: "![](_page_2_Picture_6.jpeg)", bare tags.
GARBAGE_RE = re.compile(r"^\s*(?:!\[[^\]]*\]\([^)]*\)|_page_\d+_\w+|(?:&lt;|<)/?\w+>)\s*[.,;]?\s*$")
# At most 3 letters/digits and not an all-caps acronym ("ch/", "pp.", "e.g").
JUNK_RE = re.compile(r"^\W*(?:\w\W*){1,3}$")


def is_junk(text: str) -> bool:
    letters = re.sub(r"\W", "", text)
    return bool(JUNK_RE.match(text)) and not (letters.isupper() and letters.isalpha() and len(letters) > 1)


def is_author_year(text: str, category: str | None) -> bool:
    return (category != "legal_citation" and not LEGAL_GUARD_RE.search(text)
            and not TITLE_GUARD_RE.search(text) and bool(AUTHOR_YEAR_RE.match(text)))


def is_name_only(text: str) -> bool:
    return bool(NAME_ONLY_RE.match(text))


def is_law_name(text: str, category: str | None) -> bool:
    return category == "legal_citation" or text.strip(" .").upper() in LAW_ACRONYMS


def fr_fragment_own_docket(texts: list[str], docket) -> bool:
    """True when a volume-less "Fed. Reg. <page>" falls within 50 pages of the
    comment's own docket notice -- i.e. it's almost certainly a pinpoint into
    the RFI itself. Informational only (review column)."""
    own = _parse_fr_citation(docket.fr_citation or "") if docket else None
    if not own:
        return False
    for t in texts:
        m = FR_FRAGMENT_RE.match(t)
        if m and 0 <= int(m.group(1).replace(",", "")) - own[1] < 50:
            return True
    return False


# (name, predicate(text, category)) in priority order; first filter whose
# predicate holds for every non-empty text field wins.
PASS1_FILTERS = [
    ("id_ibid_only", lambda t, c: is_id_ibid_only(t)),
    ("author_only",  lambda t, c: bool(AUTHOR_ONLY_RE.match(t))),
]
PASS2_FILTERS = [
    ("id_ibid_only",        lambda t, c: is_id_ibid_only(t, see_eg=True)),
    ("author_year",         is_author_year),
    ("short_form_pinpoint", lambda t, c: bool(SHORT_FORM_RE.match(t))),
    ("internal_xref",       lambda t, c: bool(INTERNAL_XREF_RE.match(t))),
    ("roman_see",           lambda t, c: bool(ROMAN_SEE_RE.match(t))),
    ("bare_marker",         lambda t, c: bool(BARE_MARKER_RE.match(t))),
    ("bracket_keys",        lambda t, c: bool(BRACKET_KEYS_RE.match(t))),
    ("year_only",           lambda t, c: bool(YEAR_ONLY_RE.match(t))),
    ("fr_fragment",         lambda t, c: bool(FR_FRAGMENT_RE.match(t))),
    ("name_only",           lambda t, c: is_name_only(t)),
]
PASS3_FILTERS = [
    ("garbage", lambda t, c: bool(GARBAGE_RE.match(t))),
    *PASS2_FILTERS,
    ("junk_fragment", lambda t, c: is_junk(t)),
]
# Filters whose hit on citation_reference_text alone still counts when every
# other field just quotes that reference inside prose, e.g. reference
# "Yackee and Yackee 2011" + footnote "...most rules (Yackee and Yackee 2011)."
# Only multi-word fragments: "Id."/years/single names are substrings of real citations.
ECHO_FILTERS = {"author_year", "short_form_pinpoint"}


def _echo_hit(row, texts, filters, category):
    ref = (row["citation_reference_text"] or "").strip()
    if not ref or len(texts) < 2:
        return None
    others = [t for t in texts if t != ref]
    if not all(ref.rstrip(".").lower() in t.lower() for t in others):
        return None
    for name, pred in filters:
        if name in ECHO_FILTERS and pred(ref, category):
            return name
    return None


def row_texts(row) -> list[str]:
    """The citation's own text fields; when all are empty (sentence-only
    rows from the main extraction), fall back to citation_sentence."""
    texts = [row[f].strip() for f in TEXT_FIELDS if row[f] and row[f].strip()]
    if not texts and row["citation_sentence"] and row["citation_sentence"].strip():
        texts = [row["citation_sentence"].strip()]
    return texts


def classify_row(row: sqlite3.Row, docket, filters, echo: bool = False) -> tuple[str, str] | None:
    if row["citation_link"] and row["citation_link"].strip():
        return None
    texts = row_texts(row)
    if docket is not None:
        rfi_text = " ".join(texts)
        is_rfi, reason = classify_rfi_self_reference(rfi_text, rfi_text, docket)
        if is_rfi:
            return "rfi_self_reference", reason
    if not texts:
        return None
    category = row["cited_source_catagory"]
    for name, pred in filters:
        if all(pred(t, category) for t in texts):
            if name == "name_only" and all(is_law_name(t, category) for t in texts):
                return KEPT_LAW, "law/statute name -- not deleted"
            if name == "fr_fragment" and fr_fragment_own_docket(texts, docket):
                return name, "within 50 pages of own docket's FR citation"
            return name, ""
    if echo:
        name = _echo_hit(row, texts, filters, category)
        if name:
            return name, "reference is a fragment; other fields only quote it"
    return None


def find_flagged(con, dockets, comment_docket, filters, pass_no):
    """-> list of dicts: action ('delete' / 'keep' / 'reclassify'), filter,
    reason, row, docket_id, and for reclassify new_category/new_link/new_domain."""
    rows = con.execute("SELECT * FROM comment_citations").fetchall()
    ctx = reclassify.Context(rows) if pass_no >= 3 else None
    flagged = []
    for row in rows:
        docket_id = comment_docket.get(row["comment_id"])
        hit = classify_row(row, dockets.get(docket_id) if docket_id else None, filters, echo=pass_no >= 3)
        if hit:
            filt, reason = hit
            flagged.append({"action": "keep" if filt == KEPT_LAW else "delete", "filter": filt,
                            "reason": reason, "row": row, "docket_id": docket_id})
            continue
        if ctx is not None:
            prop = reclassify.propose(row, row_texts(row), ctx)
            if prop:
                rule, cat, link, domain = prop
                flagged.append({"action": "reclassify", "filter": rule, "reason": "", "row": row,
                                "docket_id": docket_id, "new_category": cat, "new_link": link,
                                "new_domain": domain})
    return flagged


def _first_text(row):
    return (row["citation_reference_text"] or row["citation_footnote_text"]
            or row["unmatched_citation_reference_text"] or "")


def write_review(con, flagged, dependents, path):
    flagged = sorted(flagged, key=lambda x: (x["action"], x["filter"], x.get("new_category") or "",
                                             _first_text(x["row"])))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["keep", "action", "filter", "reason", "cited_source_catagory", "new_category",
                    "citation_link", "new_link", "citation_id", "comment_id", "docket_id",
                    *TEXT_FIELDS, "citation_sentence", *[f"n_{t}" for t in dependents]])
        for x in flagged:
            row = x["row"]
            counts = [con.execute(f"SELECT COUNT(*) FROM {t} WHERE citation_id=?", (row["citation_id"],)).fetchone()[0]
                      for t in dependents]
            w.writerow(["y" if x["action"] == "keep" else "", x["action"], x["filter"], x["reason"],
                        row["cited_source_catagory"], x.get("new_category") or "", row["citation_link"],
                        x.get("new_link") or "", row["citation_id"], row["comment_id"], x["docket_id"],
                        *[row[fld] for fld in TEXT_FIELDS], row["citation_sentence"], *counts])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="pass_no", type=int, choices=(1, 2, 3, 4), default=4)
    ap.add_argument("--apply", action="store_true", help="apply flagged changes (backs up the DB first)")
    ap.add_argument("--review-file", type=Path,
                    help="reviewed CSV from a dry run; rows marked keep=y are left untouched")
    args = ap.parse_args()
    if args.apply and args.pass_no >= 2 and not args.review_file:
        ap.error(f"--pass {args.pass_no} --apply requires --review-file (run a dry run and review it first)")

    filters = {1: PASS1_FILTERS, 2: PASS2_FILTERS, 3: PASS3_FILTERS, 4: PASS3_FILTERS}[args.pass_no]
    today = date.today().isoformat()
    dockets = load_dockets(DB_PATH)
    comment_docket = load_comment_docket_map(DB_PATH)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    dependents = [t for t in DEPENDENT_TABLES if t in tables]
    total = con.execute("SELECT COUNT(*) FROM comment_citations").fetchone()[0]
    flagged = find_flagged(con, dockets, comment_docket, filters, args.pass_no)

    groups: dict[tuple, list] = {}
    for x in flagged:
        groups.setdefault((x["action"], x["filter"], x.get("new_category") or ""), []).append(x)
    print(f"comment_citations rows: {total}")
    for (action, filt, cat), items in sorted(groups.items()):
        print(f"\n  {action:10s} {filt:20s} {('-> ' + cat) if cat else '':36s} {len(items)}")
        random.seed(0)
        for x in random.sample(items, min(6, len(items))):
            text = " || ".join(t[:80] for t in row_texts(x["row"]))
            link = x.get("new_link") or x["row"]["citation_link"] or ""
            print(f"      {text[:150]}" + (f"  [{link[:60]}]" if link else ""))
    n = collections.Counter(x["action"] for x in flagged)
    print(f"\n  to delete {n['delete']}, to reclassify {n['reclassify']}, kept laws listed {n['keep']}")

    AUDIT_DIR.mkdir(exist_ok=True)
    if not args.apply:
        review_path = AUDIT_DIR / f"pass{args.pass_no}_review_{today}.csv"
        write_review(con, flagged, dependents, review_path)
        print(f"review file written to {review_path}")
        print("dry run -- nothing changed. Mark keep=y on rows to leave alone, then rerun with "
              f"--pass {args.pass_no} --apply --review-file {review_path.relative_to(ROOT)}")
        return

    actionable = [x for x in flagged if x["action"] != "keep"]
    if args.review_file:
        with open(args.review_file, encoding="utf-8") as f:
            reviewed = list(csv.DictReader(f))
        approved = {r["citation_id"] for r in reviewed
                    if (r.get("keep") or "").strip().lower() not in KEEP_VALUES and r.get("action") != "keep"
                    and r["filter"] != KEPT_LAW}
        # Guard: only act on rows the review approved AND that still flag now.
        stale = approved - {x["row"]["citation_id"] for x in actionable}
        actionable = [x for x in actionable if x["row"]["citation_id"] in approved]
        print(f"review file: {len(reviewed)} rows, {len(reviewed) - len(approved)} kept, "
              f"{len(approved)} approved" + (f", {len(stale)} no longer flagged (skipped)" if stale else ""))
    to_delete = [x for x in actionable if x["action"] == "delete"]
    to_update = [x for x in actionable if x["action"] == "reclassify"]

    backup = DB_PATH.with_name(f"dockets.db.bak_pre_citation_filters_pass{args.pass_no}_{today}")
    con.close()
    shutil.copy2(DB_PATH, backup)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Full JSON backup of every deleted row (with its dependent rows nested)
    # and of every reclassified row's previous values.
    deleted_records = []
    for x in to_delete:
        rec = {"filter": x["filter"], "reason": x["reason"], "docket_id": x["docket_id"],
               "comment_citation": dict(x["row"]), "dependents": {}}
        for t in dependents:
            dep = con.execute(f"SELECT * FROM {t} WHERE citation_id=?", (x["row"]["citation_id"],)).fetchall()
            if dep:
                rec["dependents"][t] = [dict(d) for d in dep]
        deleted_records.append(rec)
    deleted_path = AUDIT_DIR / f"pass{args.pass_no}_deleted_rows_{today}.json"
    with open(deleted_path, "w", encoding="utf-8") as f:
        json.dump(deleted_records, f, indent=2, ensure_ascii=False, default=str)
    print(f"deleted-row backup written to {deleted_path}")
    if to_update:
        reclass_path = AUDIT_DIR / f"pass{args.pass_no}_reclassified_rows_{today}.json"
        with open(reclass_path, "w", encoding="utf-8") as f:
            json.dump([{"rule": x["filter"], "new_category": x["new_category"], "new_link": x.get("new_link"),
                        "new_domain": x.get("new_domain"), "before": dict(x["row"])} for x in to_update],
                      f, indent=2, ensure_ascii=False, default=str)
        print(f"reclassified-row backup (previous values) written to {reclass_path}")

    ids = [(x["row"]["citation_id"],) for x in to_delete]
    with con:
        for t in dependents:
            con.executemany(f"DELETE FROM {t} WHERE citation_id=?", ids)
        con.executemany("DELETE FROM comment_citations WHERE citation_id=?", ids)
        for x in to_update:
            con.execute("UPDATE comment_citations SET cited_source_catagory=?, classifier_used=? WHERE citation_id=?",
                        (x["new_category"], f"cleanup_pass{args.pass_no}_{x['filter']}", x["row"]["citation_id"]))
            if x.get("new_link"):
                con.execute("UPDATE comment_citations SET citation_link=?, domain=?, domain_registered=? "
                            "WHERE citation_id=?",
                            (x["new_link"], x["new_domain"], x["new_domain"], x["row"]["citation_id"]))
    remaining = con.execute("SELECT COUNT(*) FROM comment_citations").fetchone()[0]
    print(f"deleted {len(ids)} rows (+ dependents), reclassified {len(to_update)}; "
          f"comment_citations now {remaining}; backup at {backup}")

    # Large random sample of what is still 'unclassified', skipping rows that
    # earlier samples already showed.
    seen = set()
    for prev in AUDIT_DIR.glob("*unclassified_sample_*.csv"):
        with open(prev, encoding="utf-8") as f:
            seen |= {r["citation_id"] for r in csv.DictReader(f)}
    unclassified = con.execute(
        "SELECT * FROM comment_citations WHERE cited_source_catagory = 'unclassified'"
    ).fetchall()
    unseen = [r for r in unclassified if r["citation_id"] not in seen]
    random.seed(42)
    sample = random.sample(unseen, min(UNCLASSIFIED_SAMPLE_SIZE, len(unseen)))
    sample_path = AUDIT_DIR / f"pass{args.pass_no}_unclassified_sample_{today}.csv"
    with open(sample_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        cols = ["citation_id", "comment_id", "citation_link", *TEXT_FIELDS, "citation_sentence"]
        w.writerow(cols)
        for r in sample:
            w.writerow([r[c] for c in cols])
    print(f"unclassified remaining: {len(unclassified)} ({len(unseen)} never sampled); "
          f"sample of {len(sample)} written to {sample_path}")


if __name__ == "__main__":
    main()
