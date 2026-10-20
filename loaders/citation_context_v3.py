"""
loaders/citation_context_v3.py
citation_context_v2 plus an analysis scope for every citation. Builds on
loaders/citation_context_v2.py (left unchanged so validation set 3 stays
reproducible); writes citation_context_v3.

    REGGOV_MD_ROOT=~/Dropbox/processed_pdfs python -m loaders.citation_context_v3

Changes from v2 (motivated by validation set 3)
-----------------------------------------------
Markers
  * marker-pdf page-link markers with escaped brackets ("[\\[150,](#page-22-3)
    [151\\]](#page-22-2)", "[[4\\]](#page-5-2)") and lowercase roman markers
    ("[viii]") are dropped before sentence splitting, and are recognised as
    footnote markers. v2 left them in, so pysbd split "Xu et al. [150, 151]
    and …" into fragments ("51] employed …") and the marker in the LLM's own
    sentence went unseen, which sent the core to another sentence.
  * Sentences split after an abbreviation ("et al.", "v.", "Fig.") are rejoined
    when the next piece starts lowercase, with a digit, a bracket or a
    leftover marker fragment.

Reference text
  * New unit type "reference": everything under a References / Bibliography /
    Works Cited / Endnotes / Notes heading, and runs of 3+ consecutive lines
    that look like bibliography or legal-citation entries. Reference units
    are skipped in the body flow (like footnotes) and never become context.

Relocation
  * The core moves to the sentence carrying the citation's marker only when
    the LLM sentence matched footnote or reference text. A body match keeps
    its core inside the matched span.

analysis_scope (pass 2, per comment)
  in_scope            LLM sentence matched body text; core is a body sentence
  recovered           LLM sentence matched a footnote/reference; its marker
                      sentence is in the body and no in_scope row's LLM span
                      covers it (a citing sentence the LLM missed)
  excluded_duplicate  as recovered, but an in_scope row already covers that
                      sentence (duplicate_of names it)
  excluded_footnote   footnote match with no body marker sentence
  excluded_reference  reference match with no body marker sentence
  excluded_table      matched a table (table text is never analysed)
  excluded_heading    matched a heading
  excluded_bare_link  in-body match whose core is a bare URL line
  unmatched / empty_sentence / no_source_file  as before
"""
import re
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from loaders.citation_context import (
    MIN_COVER_RATIO, _ATX_RX, _FN_HINT_RX, _FN_PLAIN_RX, _PAGE_NOISE_RX, _TABLE_RULE_RX, _TERMINAL_RX,
    _clean, _empty_row, _index_md, _is_pseudo_heading, _msub, _norm,
)
from loaders.citation_context_v2 import (
    BODY_SHARE, MD_ROOT, URL_SHARE, DocV2, _AUTHOR_YEAR_RX, _CONT_RX, _FN_PLAIN_NUM_RX, _FN_SUP_V2_RX,
    _PAGE_HEADER_RX, _URL_RX, RUNNING_HEADER_MIN, _expand_numbers, _locate_v2, _positions_per_sid, _start,
    _stitch, _try_repair, _url_share, build_row,
)
from models import CitationContextV3

RELOCATE_REACH = 8000    # raw chars from a footnote/reference match to its marker sentence
REF_RUN_MIN    = 3       # consecutive entry-like lines that make a reference block
REF_LINE_CUES  = 2       # cues for a line to count towards a run
REF_SOLO_CUES  = 4       # cues for a lone line to count as a reference by itself
REF_LINE_MAX   = 1500
DUP_SHARE      = 0.5     # share of a recovered core an in_scope LLM span must cover
BARE_LINK_WORDS = 4      # a core with fewer real words once URLs are removed is a bare link


# ── Cleaning ────────────────────────────────────────────────────────────────

_NUMS = r"\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*"
_ROMAN = r"(?:x{0,2}(?:ix|iv|v?i{0,3}))"
# page-link marker, escaped brackets allowed: [10](#p), [\[150,](#p), [151\]](#p), [[4\]](#p)
_PAGE_MARK = rf"\[\\?\[?\s*({_NUMS})\s*,?\s*(?:\\?\])?\]\(#[^)]*\)"

_PRE_CLEAN_V3 = [
    (re.compile(r"<sup>&</sup>lt;sup>"), "<sup>"),                         # broken marker-pdf footnote prefix
    (re.compile(_PAGE_MARK), " "),
    (re.compile(rf"\\\[{_NUMS}\\\]"), " "),                                # escaped plain marker \[12\]
    (re.compile(rf"\[{_NUMS}\](?!\()"), " "),                              # plain marker [2], [5, 6]
    (re.compile(rf"\\?\[(?=[ivx])({_ROMAN})\\?\](?:\(#[^)]*\))?(?![A-Za-z(])"), " "),   # [viii]
]


def _clean_v3(s: str, m: list[int]) -> tuple[str, list[int]]:
    for rx, repl in _PRE_CLEAN_V3:
        s, m = _msub(rx, repl, s, m)
    return _clean(s, m)


def clean_text(raw: str | None) -> str | None:
    return _clean_v3(raw, list(range(len(raw))))[0] if raw else None


def _norm_query_v3(s: str) -> str:
    return _norm(*_clean_v3(s, list(range(len(s)))))[0]


# ── Citation markers ────────────────────────────────────────────────────────

_MARKER_NUM_V3_RX = re.compile(
    rf"<sup>([^<]{{1,20}})</sup>"                                           # <sup>17</sup>
    rf"|{_PAGE_MARK}"                                                       # [22](#page-7-5), [[4\]](#…)
    rf"|\\?\[({_NUMS})\\?\]"                                                # [5], [5, 6], \[5\]
    r"|(?<=[a-z.,;:!?\"”’')\]])(\d{1,3})(?=[\s,;:)\-–]|$)"                  # guidance.8 / Framework,10
    r"|(?<=[.!?\"”])\s(\d{1,3})(?=\s|$)"                                    # 1990s. 15
)
_GAP_MARKER_V3_RX = re.compile(rf"[ \t]*(?:<sup>([^<]{{1,20}})</sup>|{_PAGE_MARK}|\\?\[({_NUMS})\\?\])")
_LEAD_MARKER_V3_RX = re.compile(
    rf"^[\s'\"”’)\]]*(?:<sup>([^<]{{1,20}})</sup>|{_PAGE_MARK}|\\?\[({_NUMS})\\?\]|(\d{{1,3}})(?=\s+[A-Z]))")


def _first_group(mt: re.Match) -> str:
    return next(g for g in mt.groups() if g is not None)


def marker_numbers(raw: str) -> set[int]:
    nums: set[int] = set()
    for mt in _MARKER_NUM_V3_RX.finditer(_URL_RX.sub(" ", raw)):
        nums |= _expand_numbers(_first_group(mt))
    return nums


def has_marker(raw: str) -> bool:
    return bool(marker_numbers(raw)) or bool(_AUTHOR_YEAR_RX.search(raw))


# ── Reference detection ─────────────────────────────────────────────────────

_REF_WORD = (r"(?:references?(?: cited| list)?|bibliography|works cited|literature cited|sources(?: cited)?|"
             r"citations|cited works|endnotes|notes|footnotes)")
# "References", "7. Bibliography", "REFERENCES/BIBLIOGRAPHY – Works Cited (Page 2 of 2)"
_REF_HEAD_RX = re.compile(
    rf"^(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.|[A-Z]\.)?\s*{_REF_WORD}(?:\s*[/&,–—-]\s*{_REF_WORD}|\s+and\s+{_REF_WORD})*"
    r"\s*(?:\(?(?:page\s+)?\d+\s+of\s+\d+\)?)?\s*:?$", re.I)
_REF_CUES = [
    re.compile(r"^(?:\[?\d{1,3}[.)\]]?\s+)?[A-Z][A-Za-z'\-]+,?\s+(?:[A-Z]{1,3}|[A-Z]\.(?:\s?[A-Z]\.)*)"
               r"(?:,|\s+and\b|\s+&|\.)"),                                   # Ho CWL, / Smith, J. / Lee AB and
    re.compile(r"\((?:19|20)\d{2}[a-z]?\)|\(n\.d\.\)"),                     # (2019) / (n.d.)
    re.compile(r"\b\d{1,4}\s*\(\d{1,4}\)\s*[:,]\s*\d+|;\s*\d+\s*\(\d+\)|\b\d+\s*:\s*\d+\s*[-–‐]\s*\d+"
               r"|\bpp?\.\s*\d+"),                                          # 74(5):329-337 / pp. 4
    re.compile(r"\bdoi\b|doi\.org|\bretrieved\b|\bavailable (?:at|from)\b|\baccessed\b", re.I),
    re.compile(r"\bet al\.?"),
    re.compile(r"\b\d+\s+(?:F\.\s?(?:\dd|Supp\.?)|U\.S\.|S\.\s?Ct\.|L\.\s?Ed|C\.C\.P\.A\.|Fed\.\s?Reg\.|"
               r"U\.S\.C\.|C\.F\.R\.)|\bv\.\s+[A-Z]|§"),                     # legal citations
    re.compile(r"\bsupra\b|^\s*(?:Id|Ibid)\.|\bibid\b", re.I),
    re.compile(r"https?://|www\."),
    re.compile(r"\bJournal\b|\bJ\.\s|\bProc(?:eedings)?\b|\bConference\b|\bRev(?:iew)?\.|\bPress\b|\barXiv\b"
               r"|\bvol\.\s*\d|\bno\.\s*\d|\bWorking Paper\b", re.I),
]
_VERB_RX = re.compile(
    r"\b(?:is|are|was|were|be|been|has|have|had|will|would|can|could|should|may|must|do|does|did|"
    r"provides?|shows?|suggests?|found|finds|argues?|requires?|recommends?|believes?|helps?|"
    r"includes?|allows?|means|seems?|makes?|uses?|needs?)\b", re.I)
_LIST_LABEL_RX = re.compile(r"^(?:[-*+•]\s+)?(?:\[(\d{1,3})\]|(\d{1,3})[.)])\s+")


def ref_cues(clean: str) -> int:
    """Number of bibliographic / legal-citation cues in one line of text."""
    body = re.sub(r"^[-*+•]\s+", "", clean)
    n = sum(bool(rx.search(body)) for rx in _REF_CUES)
    return n + (n > 0 and not _VERB_RX.search(_URL_RX.sub(" ", body)))


def _mark_references(units: list[dict]) -> None:
    """Retype body lines in reference sections and reference-entry runs."""
    # 1) sections under a References-type heading, until a heading of the same
    #    or higher level (pseudo headings end a section only when prose follows)
    i = 0
    while i < len(units):
        u = units[i]
        if u["utype"] in ("heading", "noise") and _REF_HEAD_RX.match(u["clean"].strip("*_ ").rstrip(":")):
            level, j = u["level"], i + 1
            while j < len(units):
                v = units[j]
                if v["utype"] == "heading" and v["level"] <= level and v["level"] < 7:
                    break
                if v["utype"] == "heading" and level == 7 and v["level"] < 7:
                    break
                if v["utype"] == "heading" and v["level"] == 7:
                    nxt = next((w for w in units[j + 1:] if w["utype"] != "noise"), None)
                    if nxt is not None and nxt["utype"] == "body" and len(nxt["clean"]) >= 150 \
                            and ref_cues(nxt["clean"]) == 0:
                        break
                    v["utype"] = "reference"       # short entry line mistaken for a heading
                elif v["utype"] == "body":
                    v["utype"] = "reference"
                j += 1
            i = j
            continue
        i += 1
    # 2) runs of entry-like lines anywhere (footnotes keep their type but extend a run)
    real = [k for k, u in enumerate(units) if u["utype"] != "noise"]
    run: list[int] = []

    def flush() -> None:
        body = [k for k in run if units[k]["utype"] == "body"]
        if len(run) >= REF_RUN_MIN and body:
            for k in body:
                units[k]["utype"] = "reference"
        run.clear()

    for k in real:
        u = units[k]
        entry = u["utype"] in ("footnote", "reference") or (
            u["utype"] == "body" and len(u["clean"]) <= REF_LINE_MAX and ref_cues(u["clean"]) >= REF_LINE_CUES)
        if entry:
            run.append(k)
            if u["utype"] == "body" and ref_cues(u["clean"]) >= REF_SOLO_CUES:
                u["utype"] = "reference"
        else:
            flush()
    flush()
    # numbered entries ("5. Ho CWL …", "[5] …") keep their number for footnote matching
    for u in units:
        if u["utype"] == "reference" and u["fn_num"] is None and (mt := _LIST_LABEL_RX.match(u["raw"])):
            u["fn_num"] = int(mt.group(1) or mt.group(2))


# ── Parsing ─────────────────────────────────────────────────────────────────

_ABBREV_END_RX = re.compile(
    r"\b(?:et al|e\.g|i\.e|cf|Figs?|Nos?|vs?|U\.S|Inc|Co|Corp|Ltd|Dr|Mr|Ms|Mrs|Prof|Jr|Sr|St|Vol|pp?|"
    r"Sec|Art|Ch|eds?|Rep|Stat|Reg)\.$", re.I)
_ALWAYS_JOIN_RX = re.compile(r"\b(?:v|vs|Nos?|Figs?|pp?|Sec|Art|Vol)\.$")
_JOIN_START_RX = re.compile(rf"^(?:[a-z0-9(\[]|\\?\]|{_NUMS}\s*\\?\])")
# a citation set off as its own sentence: "(Source: https://…)", "(https://…)"
_SOURCE_ONLY_RX = re.compile(
    r"^\(?\s*(?:sources?|see|via|from|link|available at|retrieved from)?\s*:?\s*<?(?:https?://|www\.)[^()<>]*>?\s*\)?\.?$",
    re.I)


class DocV3(DocV2):

    def __init__(self, raw: str):
        super().__init__(raw)
        self.all_key, self.all_map = self._key(self._units, {"body", "footnote", "table", "heading", "reference"})

    def _parse_units(self) -> list[dict]:
        units, off = [], 0
        for line in self.raw.splitlines(keepends=True):
            stripped = line.strip()
            lead = len(line) - len(line.lstrip())
            start, off = off + lead, off + len(line)
            if not stripped:
                continue
            fn_num, level = None, 7
            if stripped.startswith("|"):
                utype = "noise" if _TABLE_RULE_RX.match(stripped) else "table"
            elif _ATX_RX.match(stripped):
                utype, level = "heading", len(stripped) - len(stripped.lstrip("#"))
            elif (mt := _FN_SUP_V2_RX.match(stripped)):
                nums = re.findall(r"\b\d{1,3}\b", mt.group(1))
                utype, fn_num = "footnote", (int(nums[-1]) if nums else None)
            elif _FN_PLAIN_RX.match(stripped) and _FN_HINT_RX.search(stripped) and len(stripped) <= 1500:
                utype, fn_num = "footnote", int(_FN_PLAIN_NUM_RX.match(stripped).group(1))
            else:
                utype = None
            clean, cmap = _clean_v3(stripped, list(range(start, start + len(stripped))))
            if not clean or _PAGE_NOISE_RX.match(clean) or (len(clean) <= 200 and _PAGE_HEADER_RX.search(clean)):
                utype = "noise"
            elif utype is None:
                utype = "heading" if _is_pseudo_heading(stripped, clean) else "body"
            if utype == "body" and _url_share(clean) >= URL_SHARE:
                utype = "footnote"
            units.append({"utype": utype, "clean": clean, "map": cmap, "sids": [], "fn_num": fn_num,
                          "level": level, "raw": stripped})
        seen: dict[str, int] = defaultdict(int)
        for u in units:
            if u["utype"] == "body" and len(u["clean"]) <= 150:
                seen[u["clean"]] += 1
        for u in units:
            if u["utype"] == "body" and seen.get(u["clean"], 0) >= RUNNING_HEADER_MIN:
                u["utype"] = "noise"
        real = [i for i, u in enumerate(units) if u["utype"] != "noise"]
        for k, i in enumerate(real):
            u = units[i]
            if u["utype"] != "body" or len(u["clean"]) > 1500 or not re.search(r"https?://|www\.", u["clean"]):
                continue
            near = [units[real[j]]["utype"] for j in (k - 1, k + 1) if 0 <= j < len(real)]
            if "footnote" in near and _FN_HINT_RX.search(_URL_RX.sub(" ", u["clean"])):
                u["utype"] = "footnote"
        _mark_references(units)
        self._units = units
        return units

    def _split(self, units: list[dict]) -> None:
        """v1 split, then rejoin sentences pysbd broke after an abbreviation,
        and attach a stand-alone "(Source: URL)" to the sentence it cites."""
        super()._split(units)
        for u in units:
            if len(u["sids"]) < 2:
                continue
            keep = [u["sids"][0]]
            for sid in u["sids"][1:]:
                head, cur = self.sents[keep[-1]], self.sents[sid]
                tail = head["clean"].rstrip()
                if (_ABBREV_END_RX.search(tail) and (_ALWAYS_JOIN_RX.search(tail) or _JOIN_START_RX.match(cur["clean"]))) \
                        or _SOURCE_ONLY_RX.match(cur["clean"]):
                    head["pieces"][-1] = (head["pieces"][-1][0], max(p[1] for p in cur["pieces"]))
                    head["clean"] += " " + cur["clean"]
                    cur["merged_into"], cur["pieces"] = keep[-1], []
                    continue
                keep.append(sid)
            u["sids"] = keep

    def _build_flow(self, units: list[dict]) -> None:
        """v2 flow; reference units are boundaries, like headings."""
        pending = None
        for ui, u in enumerate(units):
            t = u["utype"]
            if t in ("heading", "table", "reference"):
                if not (self.flow and self.flow[-1] == ("B", t)):
                    self.flow.append(("B", t))
                pending = None
                if t != "heading":
                    self.local[ui] = u["sids"]
                continue
            if t == "footnote":
                self.local[ui] = u["sids"]
                continue
            if t != "body":
                continue
            sids = list(u["sids"])
            if pending is not None and sids and _CONT_RX.match(self.sents[sids[0]]["clean"]):
                head = self.sents[pending]
                cont = self.sents[sids.pop(0)]
                head["pieces"].extend(cont["pieces"])
                head["clean"] += " " + cont["clean"]
                cont["merged_into"] = pending
            for sid in sids:
                self.flow_pos[sid] = len(self.flow)
                self.flow.append(sid)
            last = self.flow[-1] if self.flow else None
            pending = (last if isinstance(last, int)
                       and not _TERMINAL_RX.search(self.sents[last]["clean"].rstrip(":")) else None)

    def marker_index(self) -> dict[int, list[int]]:
        """footnote number -> body sentence ids carrying that marker (tables
        excluded: table text is never analysed)."""
        if self._mindex is None:
            idx: dict[int, set[int]] = defaultdict(set)
            for sid, s in enumerate(self.sents):
                if "merged_into" in s or s["utype"] != "body" or not s["pieces"]:
                    continue
                own = self.text(sid)
                if (lead := _LEAD_MARKER_V3_RX.match(own)):
                    own = own[lead.end():]
                nums = marker_numbers(own)
                end = max(p[1] for p in s["pieces"])
                if (gap := _GAP_MARKER_V3_RX.match(self.raw, end)):
                    nums |= _expand_numbers(_first_group(gap))
                k = self.flow_pos.get(sid)
                nxt = self.flow[k + 1] if k is not None and k + 1 < len(self.flow) else None
                if isinstance(nxt, int) and (lead := _LEAD_MARKER_V3_RX.match(self.text(nxt))):
                    nums |= _expand_numbers(_first_group(lead))
                for n in nums:
                    idx[n].add(sid)
            self._mindex = {n: sorted(v) for n, v in idx.items()}
        return self._mindex

    def matched_footnote_number(self, sids: list[int]) -> int | None:
        for sid in sids:
            ui = self.sents[sid]["unit"]
            while ui >= 0 and self._units[ui]["utype"] in ("footnote", "reference", "noise"):
                if self._units[ui]["fn_num"] is not None:
                    return self._units[ui]["fn_num"]
                ui -= 1
        return None


# ── Core selection ──────────────────────────────────────────────────────────

def citation_number(doc: DocV3, citation_sentence: str, footnote_text: str | None) -> tuple[int | None, str | None]:
    nums = marker_numbers(citation_sentence or "")
    if len(nums) == 1:
        return nums.pop(), "llm_marker"
    if footnote_text:
        n = doc.footnote_number(footnote_text)
        if n is not None:
            return n, "footnote_text"
    return None, None


def choose_core(doc: DocV3, sids: list[int], match_unit: str, anchor_pos: int,
                number: int | None, link: str | None) -> tuple[int, str, bool]:
    """Return (core sid, core_method, relocated). Only footnote/reference
    matches move to the marker sentence; body matches stay in their span."""
    if number is not None:
        cands = [doc.resolve(s) for s in doc.marker_index().get(number, [])]
        in_span = [s for s in cands if s in sids]
        if in_span:
            return in_span[0], ("single" if len(sids) == 1 else "footnote_number"), False
        if match_unit in ("footnote", "reference"):
            near = sorted((abs(_start(doc, s) - anchor_pos), s) for s in cands)
            if near and near[0][0] <= RELOCATE_REACH:
                return near[0][1], "footnote_number", True
    if len(sids) == 1:
        return sids[0], "single", False
    if link:
        key = re.sub(r"^https?://(www\.)?", "", link.strip())[:60]
        with_link = [s for s in sids if key and key in doc.text(s)]
        if len(with_link) == 1:
            return with_link[0], "link", False
    marked = [s for s in sids if has_marker(doc.text(s))]
    if len(marked) == 1:
        return marked[0], "marker", False
    return sids[-1], "span_last", False


# ── Matching ────────────────────────────────────────────────────────────────

def match_citation(doc: DocV3, c, last_start: int, cache: dict) -> tuple[dict | None, str, int]:
    """Locate one citation. Returns (row fields or None, method/failure, new last_start)."""
    q = _norm_query_v3(c.citation_sentence or "")
    if not q:
        return None, "empty_sentence", last_start
    if q not in cache:
        cache[q] = _locate_v2(doc, q)
    found = cache[q]
    if not found:
        return None, "unmatched", last_start
    hits = [h for h in found["hits"] if h]
    if not hits:
        return None, "unmatched", last_start
    chosen = next((h for h in hits if min(h) >= last_start), hits[0])
    sids = doc.covered(chosen)
    if not sids:
        return None, "unmatched", last_start
    counts = _positions_per_sid(doc, sids, chosen)
    body = [s for s in sids if doc.sents[s]["utype"] == "body"]
    if body and len(body) < len(sids):
        body_share = sum(counts[s] for s in body) / max(1, sum(counts.values()))
        sids = body if body_share >= BODY_SHARE else [s for s in sids if s not in body]
    match_unit = doc.sents[sids[0]]["utype"]
    if found["method"] in ("fuzzy", "anchor") and \
            sum(len(doc.sents[s]["clean"]) for s in sids) < MIN_COVER_RATIO * len(q):
        return None, "unmatched", last_start
    repaired = _try_repair(doc, q, found, chosen, sids) | _stitch(doc, q, sids)
    if repaired:
        sids = sorted({doc.resolve(s) for s in sids}, key=lambda s: _start(doc, s))
    number, number_source = citation_number(doc, c.citation_sentence, c.citation_footnote_text)
    if number is None and match_unit in ("footnote", "reference"):
        number = doc.matched_footnote_number(sids)
        number_source = "matched_footnote" if number is not None else None
    core, core_method, relocated = choose_core(doc, sids, match_unit, min(chosen), number, c.citation_link)
    row = build_row(doc, sids, core, core_method)
    for k in ("core", "prev", "next"):
        row[f"{k}_clean"] = clean_text(row[f"{k}_sentence"])
    row.update({
        "match_method":    found["method"],
        "match_score":     round(found["score"], 1),
        "n_hits":          len(hits),
        "match_unit_type": match_unit,
        "repaired":        repaired,
        "citation_number": number,
        "number_source":   number_source,
        "relocated":       relocated,
    })
    return row, found["method"], min(chosen)


def assign_scope(rows: list[dict]) -> None:
    """Pass 2 over one comment's rows: set analysis_scope / duplicate_of."""
    spans = [(r["span_char_start"], r["span_char_end"], r["citation_id"]) for r in rows
             if r.get("match_unit_type") == "body" and r.get("unit_type") == "body"]
    for r in rows:
        mu, ut = r.get("match_unit_type"), r.get("unit_type")
        r["recovered"], r["duplicate_of"] = False, None
        if mu is None:
            r["analysis_scope"] = r["match_method"]
        elif mu == "body" and ut == "body":
            words = re.findall(r"[A-Za-z]{2,}", re.sub(r"\S*(?:https?://|www\.|\.com|\.org|\.gov)\S*", " ",
                                                     r["core_clean"] or ""))
            bare = len(words) < BARE_LINK_WORDS and bool(re.search(r"https?://|www\.", r["core_sentence"] or ""))
            r["analysis_scope"] = "excluded_bare_link" if bare else "in_scope"
        elif mu in ("footnote", "reference") and r["relocated"] and ut == "body":
            cs, ce = r["core_char_start"], r["core_char_end"]
            best = max(((min(ce, e) - max(cs, s), cid) for s, e, cid in spans), default=(0, None))
            if best[0] >= DUP_SHARE * (ce - cs):
                r["analysis_scope"], r["duplicate_of"] = "excluded_duplicate", best[1]
            else:
                r["analysis_scope"], r["recovered"] = "recovered", True
        elif "table" in (mu, ut):
            r["analysis_scope"] = "excluded_table"
        elif mu == "heading":
            r["analysis_scope"] = "excluded_heading"
        else:
            r["analysis_scope"] = f"excluded_{mu}"     # excluded_footnote / excluded_reference


# ── Loader ──────────────────────────────────────────────────────────────────

def load_citation_context_v3(session: Session, md_root: str | Path = MD_ROOT, only: set[str] | None = None) -> list[dict]:
    """Build citation_context_v3. With `only` (citation ids), rows are computed
    for those citations' comments and returned without touching the DB."""
    md_root = Path(md_root)
    md_index = _index_md(md_root)
    print(f"[citation_context_v3] indexed {sum(map(len, md_index.values()))} .md files under {md_root}")

    rows = session.execute(text(
        "SELECT citation_id, comment_id, citation_sentence, citation_footnote_text, citation_link "
        "FROM comment_citations ORDER BY rowid"
    )).fetchall()
    by_comment: dict[str, list] = defaultdict(list)
    for r in rows:
        by_comment[r.comment_id].append(r)
    if only is not None:
        keep = {r.comment_id for r in rows if r.citation_id in only}
        by_comment = {k: v for k, v in by_comment.items() if k in keep}

    out: list[dict] = []
    for n, (comment_id, cits) in enumerate(by_comment.items(), 1):
        paths = sorted(md_index.get(comment_id, []))
        if not paths:
            empty = [_empty_row(c.citation_id, comment_id, "no_source_file") for c in cits]
            assign_scope(empty)
            out += empty
            continue
        rel = str(paths[0].relative_to(md_root))
        doc = DocV3(paths[0].read_text(encoding="utf-8", errors="ignore"))
        cache: dict[str, dict | None] = {}
        last_start = -1
        comment_rows = []
        for c in cits:
            row, method, last_start = match_citation(doc, c, last_start, cache)
            if row is None:
                comment_rows.append(_empty_row(c.citation_id, comment_id, method, rel))
                continue
            ws, we = row.pop("window_start"), row.pop("window_end")
            comment_rows.append({"citation_id": c.citation_id, "comment_id": comment_id, "source_path": rel,
                                 "window_key": f"{comment_id}:{ws}-{we}", **row})
        assign_scope(comment_rows)
        out += comment_rows
        if n % 100 == 0:
            print(f"  [citation_context_v3] {n}/{len(by_comment)} comments")

    if only is not None:
        return out
    bind = session.get_bind()
    CitationContextV3.__table__.drop(bind, checkfirst=True)
    CitationContextV3.__table__.create(bind)
    session.bulk_insert_mappings(CitationContextV3, out)
    session.commit()

    counts: dict[str, int] = defaultdict(int)
    for r in out:
        counts[r["analysis_scope"]] += 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    print(f"[citation_context_v3] rows={len(out)}  {summary}")
    print(f"  relocated={sum(bool(r.get('relocated')) for r in out)}  "
          f"repaired={sum(bool(r.get('repaired')) for r in out)}")
    return out


if __name__ == "__main__":
    import db as _db
    _db.init_db()
    with _db.Session() as s:
        load_citation_context_v3(s)
