"""
loaders/citation_context_v2.py
citation_context with exactly one core sentence per citation and core ± 1
sentence of context. Builds on loaders/citation_context.py (v1), which is
left unchanged; writes citation_context_v2.

    REGGOV_MD_ROOT=~/Dropbox/processed_pdfs python -m loaders.citation_context_v2

Changes from v1 (motivated by validation set 2)
-----------------------------------------------
Parsing (DocV2)
  * Lines made mostly of URLs are footnote material, not body text, wherever
    they sit; a line with a URL plus citation cues ("available at", a year,
    "See") next to a footnote is a footnote continuation. In v1 these lines
    were body units, so the page-break merge glued them onto unfinished
    sentences ("…which cover https://…") and they leaked in as context.
  * In-text markers "[10](#page-8-0)" and "[2]" are dropped before sentence
    splitting; v1 turned "2025.[10](#page-8-0) Further" into "2025.10 Further"
    and left "2026.[2] Confidential", and pysbd split neither.
  * A page-break continuation may start with punctuation ('"school officials"
    with …'); v1 only merged continuations starting with a lowercase letter.
  * Any line opening with <sup>…</sup> is a footnote (including broken
    marker-pdf prefixes "<sup>&</sup>lt;sup>10", "<sup>10, 2025. 35</sup>"),
    and every footnote keeps its number.
  * "Page 2 of 6" lines and short lines repeated 3+ times (running headers)
    are noise, so they no longer interrupt sentences across page breaks.

Matching
  * Body text is searched before footnotes/tables at each stage (exact,
    anchor, fuzzy). A fuzzy body hit is kept unless the all-text hit scores
    more than 3 points higher.
  * Body sentences only win a match that straddles body + footnote text
    when they hold at least half the matched characters.
  * Page-break repair: when a fuzzy/anchor match stops short of the end of
    the LLM sentence, the missing tail is looked up in the body text that
    follows; the sentence found there becomes the continuation and whatever
    v1 had merged in between is dropped. Likewise, when the match covers a
    sentence plus a lowercase continuation, stray pieces that are not in the
    LLM sentence ("…outputs that collaborate.") are dropped and the two joined.

Core selection — every matched citation gets ONE core sentence
  1. footnote_number  the sentence carrying this citation's marker. The
                      number comes from a marker in the LLM sentence, from
                      matching citation_footnote_text to a numbered footnote,
                      or from the footnote the LLM sentence itself matched.
                      If the marker sits outside the matched span the core
                      moves there (relocated = 1) — only for fuzzy/anchor
                      matches and matches that landed in footnotes/tables;
                      an exact body match is never moved.
  2. single           the span is one sentence (number unknown or not found)
  3. link             the span sentence containing citation_link
  4. marker           the only span sentence with any citation marker
  5. span_last        fallback: last sentence of the span
The window is core ± 1 with v1's boundary rules (stops at headings, tables,
footnote and document edges). *_clean columns hold markup-free text for
hedging / sentiment; the other text columns are raw markdown slices.
"""
import re
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

from rapidfuzz import fuzz, process
from sqlalchemy import text
from sqlalchemy.orm import Session

from loaders.citation_context import (
    ANCHOR_CUTOFF, ANCHOR_LEN, FUZZY_CUTOFF, LONG_NEEDLE, MD_ROOT, MIN_COVER_RATIO, SHORT_NEEDLE,
    _ATX_RX, _FN_HINT_RX, _FN_PLAIN_RX, _PAGE_NOISE_RX, _TABLE_RULE_RX, _TERMINAL_RX,
    Doc, _clean, _empty_row, _find_all, _index_md, _is_pseudo_heading, _msub, _norm, _span_positions,
)
from models import CitationContextV2

BODY_PREFERENCE = 3      # fuzzy: keep the body hit unless the all-text hit beats it by more
BODY_SHARE      = 0.5    # body sentences must hold this share of a straddling match
REPAIR_MIN_TAIL = 20     # normalized chars of the LLM sentence left unmatched before repairing
REPAIR_REACH    = 6000   # raw chars after the match searched for the missing tail
RELOCATE_REACH  = {"exact_nonbody": 8000, "anchor": 8000, "fuzzy": 8000}   # exact body matches never move
FN_TEXT_MIN     = 25     # shorter footnote texts ("Id.") are too ambiguous to match
FN_TEXT_CUTOFF  = 90
URL_SHARE       = 0.6    # share of non-space chars in URL-like tokens that makes a line footnote material


# ── Cleaning ────────────────────────────────────────────────────────────────

_PRE_CLEAN = [
    (re.compile(r"<sup>&</sup>lt;sup>"), "<sup>"),            # broken marker-pdf footnote prefix
    (re.compile(r"\[\d{1,3}\]\(#page-[^)]*\)"), " "),        # in-text page-link marker [10](#page-8-0)
    (re.compile(r"\[\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*\](?!\()"), " "),  # plain marker [2], [5, 6]
]


def _clean_v2(s: str, m: list[int]) -> tuple[str, list[int]]:
    for rx, repl in _PRE_CLEAN:
        s, m = _msub(rx, repl, s, m)
    return _clean(s, m)


def clean_text(raw: str | None) -> str | None:
    return _clean_v2(raw, list(range(len(raw))))[0] if raw else None


def _norm_query_v2(s: str) -> str:
    return _norm(*_clean_v2(s, list(range(len(s)))))[0]


# ── Citation markers ────────────────────────────────────────────────────────

_URL_RX = re.compile(r"\](?=\((?!#))\((?:[^()\s]|\([^)]*\))*\)|(?:https?://|www\.)\S+")
_MARKER_NUM_RX = re.compile(
    r"<sup>([^<]{1,20})</sup>"                                              # <sup>17</sup>, <sup>10,11</sup>
    r"|\[(\d{1,3})\]\(#[^)]*\)"                                             # [22](#page-7-5)
    r"|\[(\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*)\]"                               # [5], [5, 6]
    r"|(?<=[a-z.,;:!?\"”’')\]])(\d{1,3})(?=[\s,;:)\-–]|$)"                  # guidance.8 / Framework,10
    r"|(?<=[.!?\"”])\s(\d{1,3})(?=\s|$)"                                    # 1990s. 15
)
_AUTHOR_YEAR_RX = re.compile(
    r"\((?:see\s+)?[A-Z][\w'\-]+(?:\s+et\s+al\.?|\s+(?:and|&)\s+[A-Z][\w'\-]+)?,?\s+(?:19|20)\d{2}[a-z]?\)")


def _expand_numbers(group: str) -> set[int]:
    out = set()
    for a, sep, b in re.findall(r"(\d{1,3})(?:\s*([\-–])\s*(\d{1,3}))?", group):
        if sep and b and 0 < int(b) - int(a) <= 20:
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(a))
    return out


def marker_numbers(raw: str) -> set[int]:
    """Footnote numbers cited in a raw markdown sentence (URLs ignored)."""
    nums: set[int] = set()
    for mt in _MARKER_NUM_RX.finditer(_URL_RX.sub(" ", raw)):
        g = next(x for x in mt.groups() if x is not None)
        nums |= _expand_numbers(g)
    return nums


def has_marker(raw: str) -> bool:
    return bool(marker_numbers(raw)) or bool(_AUTHOR_YEAR_RX.search(raw))


# ── Parsing ─────────────────────────────────────────────────────────────────

# any line opening with <sup>…</sup> is a footnote, including broken ones ("<sup>10, 2025. 35</sup> Fraas")
_FN_SUP_V2_RX = re.compile(r"^(?:<span[^>]*>\s*</span>\s*)*(?:<sup>&</sup>lt;sup>|<sup>)([^<]{0,20})</sup>")
_PAGE_HEADER_RX = re.compile(r"\bpage\s+\d{1,4}\s+of\s+\d{1,4}\b", re.I)
RUNNING_HEADER_MIN = 3   # a short line repeated this often is a running header/footer
_FN_PLAIN_NUM_RX = re.compile(r"^(?:<span[^>]*>\s*</span>\s*)*(\d{1,3})\s")
_CONT_RX = re.compile(r"^(?:[^A-Za-z0-9]*[a-z]|\()")    # continuation: first letter lowercase, or "(LMMs) …"
_GAP_MARKER_RX = re.compile(r"[ \t]*(?:<sup>([^<]{1,20})</sup>|\[(\d{1,3})\](?:\(#[^)]*\))?)")
# a marker opening a sentence (after any closing quotes pysbd split off) belongs to the
# previous one: '" <sup>19</sup> As we wrote …', "56 For instance …"
_LEAD_MARKER_RX = re.compile(
    r"^[\s'\"”’)\]]*(?:<sup>([^<]{1,20})</sup>|\[(\d{1,3})\](?:\(#[^)]*\))?|(\d{1,3})(?=\s+[A-Z]))")


def _url_share(clean: str) -> float:
    toks = clean.split()
    if not toks:
        return 0.0
    urlish = sum(len(t) for t in toks if t.startswith(("http", "www.")) or "%2" in t or t.count("/") >= 2)
    return urlish / sum(len(t) for t in toks)


class DocV2(Doc):
    """v1 Doc with footnote-continuation detection, footnote numbers, page-break
    repair and a marker index."""

    def __init__(self, raw: str):
        self._units: list[dict] = []
        super().__init__(raw)
        self._mindex: dict[int, list[int]] | None = None
        self._fn_index: tuple[list[int], list[str], list[int]] | None = None

    def _parse_units(self) -> list[dict]:
        units, off = [], 0
        for line in self.raw.splitlines(keepends=True):
            stripped = line.strip()
            lead = len(line) - len(line.lstrip())
            start, off = off + lead, off + len(line)
            if not stripped:
                continue
            fn_num = None
            if stripped.startswith("|"):
                utype = "noise" if _TABLE_RULE_RX.match(stripped) else "table"
            elif _ATX_RX.match(stripped):
                utype = "heading"
            elif (mt := _FN_SUP_V2_RX.match(stripped)):
                nums = re.findall(r"\b\d{1,3}\b", mt.group(1))
                utype, fn_num = "footnote", (int(nums[-1]) if nums else None)
            elif _FN_PLAIN_RX.match(stripped) and _FN_HINT_RX.search(stripped) and len(stripped) <= 1500:
                utype, fn_num = "footnote", int(_FN_PLAIN_NUM_RX.match(stripped).group(1))
            else:
                utype = None
            clean, cmap = _clean_v2(stripped, list(range(start, start + len(stripped))))
            if not clean or _PAGE_NOISE_RX.match(clean) or (len(clean) <= 200 and _PAGE_HEADER_RX.search(clean)):
                utype = "noise"
            elif utype is None:
                utype = "heading" if _is_pseudo_heading(stripped, clean) else "body"
            if utype == "body" and _url_share(clean) >= URL_SHARE:
                utype = "footnote"          # bare URL line: footnote spill-over, never prose
            units.append({"utype": utype, "clean": clean, "map": cmap, "sids": [], "fn_num": fn_num})
        # Running headers/footers: short body lines repeated across pages.
        seen: dict[str, int] = defaultdict(int)
        for u in units:
            if u["utype"] == "body" and len(u["clean"]) <= 150:
                seen[u["clean"]] += 1
        for u in units:
            if u["utype"] == "body" and seen.get(u["clean"], 0) >= RUNNING_HEADER_MIN:
                u["utype"] = "noise"
        # Footnote continuations: a body line with a URL and citation cues right
        # next to footnote material ("workers. 35 Annette Bernhardt … (Nov. 2021) https://…").
        real = [i for i, u in enumerate(units) if u["utype"] != "noise"]
        for k, i in enumerate(real):
            u = units[i]
            if u["utype"] != "body" or len(u["clean"]) > 1500 or not re.search(r"https?://|www\.", u["clean"]):
                continue
            near = [units[real[j]]["utype"] for j in (k - 1, k + 1) if 0 <= j < len(real)]
            if "footnote" in near and _FN_HINT_RX.search(_URL_RX.sub(" ", u["clean"])):
                u["utype"] = "footnote"
        self._units = units
        return units

    def _build_flow(self, units: list[dict]) -> None:
        """v1 flow; a continuation may start with punctuation before its
        lowercase first letter."""
        pending = None
        for ui, u in enumerate(units):
            t = u["utype"]
            if t in ("heading", "table"):
                self.flow.append(("B", t))
                pending = None
                if t == "table":
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

    # ── neighbours (page-break repair can point "next" past a merged tail) ──

    def neighbour(self, sid: int, step: int) -> tuple[int | None, str | None]:
        if step > 0 and "next_from" in self.sents[sid]:
            sid = self.sents[sid]["next_from"]
        return super().neighbour(sid, step)

    # ── indexes ──

    def marker_index(self) -> dict[int, list[int]]:
        """footnote number -> body/table sentence ids carrying that marker."""
        if self._mindex is None:
            idx: dict[int, set[int]] = defaultdict(set)
            for sid, s in enumerate(self.sents):
                if "merged_into" in s or s["utype"] not in ("body", "table"):
                    continue
                own = self.text(sid)
                if (lead := _LEAD_MARKER_RX.match(own)):
                    own = own[lead.end():]          # credited to the previous sentence below
                nums = marker_numbers(own)
                # a marker left between two sentences ("AI. <sup>56</sup> For …")
                # or leading the next one belongs to this sentence
                end = max(p[1] for p in s["pieces"])
                gap = _GAP_MARKER_RX.match(self.raw, end)
                if gap:
                    nums |= _expand_numbers(next(g for g in gap.groups() if g))
                k = self.flow_pos.get(sid)
                nxt = self.flow[k + 1] if k is not None and k + 1 < len(self.flow) else None
                if isinstance(nxt, int) and (lead := _LEAD_MARKER_RX.match(self.text(nxt))):
                    nums |= _expand_numbers(next(g for g in lead.groups() if g))
                for n in nums:
                    idx[n].add(sid)
            self._mindex = {n: sorted(v) for n, v in idx.items()}
        return self._mindex

    def footnote_number(self, footnote_text: str) -> int | None:
        """Number of the footnote whose text matches citation_footnote_text."""
        if self._fn_index is None:
            nums, keys, starts = [], [], []
            for u in self._units:
                if u["fn_num"] is not None and u["clean"]:
                    nums.append(u["fn_num"])
                    keys.append(_norm(u["clean"], u["map"])[0])
                    starts.append(u["map"][0])
            self._fn_index = (nums, keys, starts)
        nums, keys, _ = self._fn_index
        q = _norm_query_v2(footnote_text)[:150]
        if len(q) < FN_TEXT_MIN or not keys:
            return None
        exact = [nums[i] for i, k in enumerate(keys) if q[:80] in k]
        if len(set(exact)) == 1:
            return exact[0]
        best = process.extractOne(q, keys, scorer=fuzz.partial_ratio, score_cutoff=FN_TEXT_CUTOFF)
        return nums[best[2]] if best else None

    # ── page-break repair ──

    def matched_footnote_number(self, sids: list[int]) -> int | None:
        """Number of the footnote a footnote-unit match sits in (walking back
        over unnumbered continuation lines)."""
        for sid in sids:
            ui = self.sents[sid]["unit"]
            while ui >= 0 and self._units[ui]["utype"] in ("footnote", "noise"):
                if self._units[ui]["fn_num"] is not None:
                    return self._units[ui]["fn_num"]
                ui -= 1
        return None

    def repair(self, head_sid: int, tail_sid: int, keep: list[tuple[int, int]]) -> None:
        """Make tail_sid the continuation of head_sid, keeping only the given
        pieces of head_sid (drops text v1 merged in by mistake)."""
        head, tail = self.sents[head_sid], self.sents[tail_sid]
        head["pieces"] = (keep or head["pieces"][:1]) + tail["pieces"]
        head["clean"] = " ".join(clean_text(self.raw[a:b]) or "" for a, b in head["pieces"])
        tail["merged_into"] = head_sid
        head["next_from"] = tail_sid
        self._mindex = None


# ── Matching ────────────────────────────────────────────────────────────────

def _locate_v2(doc: DocV2, q: str) -> dict | None:
    """Body text first at every stage. Returns method, score, hits (raw
    positions per hit), and how far into q the match reached (q_end)."""
    for method, key, pmap in (("exact", doc.body_key, doc.body_map),
                              ("exact_nonbody", doc.all_key, doc.all_map)):
        hits = _find_all(key, q)
        if hits:
            return {"method": method, "score": 100.0, "q_end": len(q),
                    "hits": [_span_positions(pmap, h, h + len(q)) for h in hits]}
    if len(q) < SHORT_NEEDLE or not doc.all_key:
        return None
    if len(q) > LONG_NEEDLE:
        for key, pmap in ((doc.body_key, doc.body_map), (doc.all_key, doc.all_map)):
            if not key:
                continue
            head = fuzz.partial_ratio_alignment(q[:ANCHOR_LEN], key, score_cutoff=ANCHOR_CUTOFF)
            tail = fuzz.partial_ratio_alignment(q[-ANCHOR_LEN:], key, score_cutoff=ANCHOR_CUTOFF)
            if head and tail and tail.dest_end > head.dest_start:
                span = tail.dest_end - head.dest_start
                if 0.6 <= span / len(q) <= 1.6:
                    return {"method": "anchor", "score": min(head.score, tail.score), "q_end": len(q),
                            "hits": [_span_positions(pmap, head.dest_start, tail.dest_end)]}
    body = fuzz.partial_ratio_alignment(q, doc.body_key, score_cutoff=FUZZY_CUTOFF) if doc.body_key else None
    allt = fuzz.partial_ratio_alignment(q, doc.all_key, score_cutoff=FUZZY_CUTOFF)
    if body and (not allt or body.score >= allt.score - BODY_PREFERENCE):
        al, pmap = body, doc.body_map
    elif allt:
        al, pmap = allt, doc.all_map
    else:
        return None
    return {"method": "fuzzy", "score": al.score, "q_end": al.src_end,
            "hits": [_span_positions(pmap, al.dest_start, al.dest_end)]}


def _positions_per_sid(doc: DocV2, sids: list[int], pos: list[int]) -> dict[int, int]:
    pos = sorted(pos)
    out = {}
    for sid in sids:
        out[sid] = sum(bisect_left(pos, b) - bisect_left(pos, a) for a, b in doc.sents[sid]["pieces"])
    return out


def _try_repair(doc: DocV2, q: str, found: dict, chosen: list[int], sids: list[int]) -> bool:
    """Find the unmatched tail of q in the body text after the match; if it
    starts a different sentence, make that sentence the continuation."""
    if found["method"] not in ("fuzzy", "anchor") or len(q) - found["q_end"] < REPAIR_MIN_TAIL:
        return False
    body_sids = [s for s in sids if doc.sents[s]["utype"] == "body"]
    if not body_sids or not doc.body_key:
        return False
    tail = q[found["q_end"]:].strip()[:150]
    end = max(chosen)
    starts = [p if p >= 0 else -1 for p in doc.body_map]
    lo = next((i for i, p in enumerate(starts) if p > end), None)
    if lo is None:
        return False
    hi = lo
    while hi < len(starts) and (starts[hi] < 0 or starts[hi] <= end + REPAIR_REACH):
        hi += 1
    region = doc.body_key[lo:hi]
    i = region.find(tail[:60])
    if i >= 0:
        a, b = lo + i, lo + i + min(len(tail), len(region) - i)
    else:
        al = fuzz.partial_ratio_alignment(tail, region, score_cutoff=90) if region else None
        if not al:
            return False
        a, b = lo + al.dest_start, lo + al.dest_end
    tail_pos = _span_positions(doc.body_map, a, b)
    tail_sids = [s for s in doc.covered(tail_pos) if doc.sents[s]["utype"] == "body"]
    if not tail_sids:
        return False
    head_sid, tail_sid = body_sids[-1], tail_sids[0]
    if tail_sid == head_sid or not _CONT_RX.match(doc.sents[tail_sid]["clean"]):
        return False
    pos = sorted(chosen)
    keep = [p for p in doc.sents[head_sid]["pieces"] if bisect_left(pos, p[1]) - bisect_left(pos, p[0]) > 0]
    doc.repair(head_sid, tail_sid, keep)
    return True


def _stitch(doc: DocV2, q: str, sids: list[int]) -> bool:
    """Join consecutive matched sentences that the LLM sentence shows are one
    sentence split by a page break: the first is unfinished (once trailing
    pieces not in q are dropped) or had stray text merged in before a
    lowercase continuation."""
    done = False
    for a, b in zip(sids, sids[1:]):
        sa, sb = doc.sents[a], doc.sents[b]
        if "merged_into" in sa or sa["utype"] != "body" or sb["utype"] != "body":
            continue
        keep = list(sa["pieces"])
        while len(keep) > 1 and fuzz.partial_ratio(_norm_query_v2(doc.raw[keep[-1][0]:keep[-1][1]]), q) < 85:
            keep.pop()
        kept_clean = " ".join(clean_text(doc.raw[x:y]) or "" for x, y in keep)
        dropped = len(keep) < len(sa["pieces"])
        if not _TERMINAL_RX.search(kept_clean) or (dropped and _CONT_RX.match(sb["clean"])):
            doc.repair(a, b, keep)
            done = True
    return done


# ── Core selection ──────────────────────────────────────────────────────────

def citation_number(doc: DocV2, citation_sentence: str, footnote_text: str | None) -> tuple[int | None, str | None]:
    nums = marker_numbers(citation_sentence or "")
    if len(nums) == 1:
        return nums.pop(), "llm_marker"
    if footnote_text:
        n = doc.footnote_number(footnote_text)
        if n is not None:
            return n, "footnote_text"
    return None, None


def _start(doc: DocV2, sid: int) -> int:
    return doc.sents[sid]["pieces"][0][0]


def choose_core(doc: DocV2, sids: list[int], match_method: str, anchor_pos: int,
                number: int | None, link: str | None) -> tuple[int, str, bool]:
    """Return (core sid, core_method, relocated)."""
    if number is not None:
        cands = [doc.resolve(s) for s in doc.marker_index().get(number, [])]
        in_span = [s for s in cands if s in sids]
        if in_span:
            return in_span[0], ("single" if len(sids) == 1 else "footnote_number"), False
        reach = RELOCATE_REACH.get(match_method, 0) if doc.sents[sids[0]]["utype"] == "body" else 8000
        near = sorted((abs(_start(doc, s) - anchor_pos), s) for s in cands)
        if near and near[0][0] <= reach:
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


def _span(doc: DocV2, sid: int | None) -> tuple[int | None, int | None]:
    if sid is None:
        return None, None
    pieces = doc.sents[sid]["pieces"]
    return min(p[0] for p in pieces), max(p[1] for p in pieces)


def build_row(doc: DocV2, sids: list[int], core: int, core_method: str) -> dict:
    prev_sid, prev_b = doc.neighbour(core, -1)
    next_sid, next_b = doc.neighbour(core, +1)
    cs, ce = _span(doc, core)
    ps, pe = _span(doc, prev_sid)
    ns, ne = _span(doc, next_sid)
    core_t, prev_t, next_t = doc.text(core), doc.text(prev_sid), doc.text(next_sid)
    return {
        "unit_type":       doc.sents[core]["utype"],
        "span_sentence":   " ".join(doc.text(s) for s in sids),
        "span_char_start": min(p[0] for s in sids for p in doc.sents[s]["pieces"]),
        "span_char_end":   max(p[1] for s in sids for p in doc.sents[s]["pieces"]),
        "n_span_sents":    len(sids),
        "n_marker_sents":  sum(has_marker(doc.text(s)) for s in sids),
        "core_method":     core_method,
        "core_sentence":   core_t,
        "prev_sentence":   prev_t,
        "next_sentence":   next_t,
        "core_clean":      clean_text(core_t),
        "prev_clean":      clean_text(prev_t),
        "next_clean":      clean_text(next_t),
        "prev_boundary":   prev_b,
        "next_boundary":   next_b,
        "n_window_sents":  1 + (prev_sid is not None) + (next_sid is not None),
        "core_char_start": cs, "core_char_end": ce,
        "prev_char_start": ps, "prev_char_end": pe,
        "next_char_start": ns, "next_char_end": ne,
        "window_start":    min(x for x in (cs, ps) if x is not None),
        "window_end":      max(x for x in (ce, ne) if x is not None),
    }


# ── Loader ──────────────────────────────────────────────────────────────────

def match_citation(doc: DocV2, c, last_start: int, cache: dict) -> tuple[dict | None, str, int]:
    """Locate one citation. Returns (row fields or None, method/failure, new last_start)."""
    q = _norm_query_v2(c.citation_sentence or "")
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
    match_unit = doc.sents[sids[0]]["utype"]
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
    if number is None and match_unit == "footnote":
        number = doc.matched_footnote_number(sids)
        number_source = "matched_footnote" if number is not None else None
    core, core_method, relocated = choose_core(doc, sids, found["method"], min(chosen), number, c.citation_link)
    row = build_row(doc, sids, core, core_method)
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


def load_citation_context_v2(session: Session, md_root: str | Path = MD_ROOT) -> None:
    md_root = Path(md_root)
    md_index = _index_md(md_root)
    print(f"[citation_context_v2] indexed {sum(map(len, md_index.values()))} .md files under {md_root}")

    rows = session.execute(text(
        "SELECT citation_id, comment_id, citation_sentence, citation_footnote_text, citation_link "
        "FROM comment_citations ORDER BY rowid"
    )).fetchall()
    by_comment: dict[str, list] = defaultdict(list)
    for r in rows:
        by_comment[r.comment_id].append(r)

    out: list[dict] = []
    for n, (comment_id, cits) in enumerate(by_comment.items(), 1):
        paths = sorted(md_index.get(comment_id, []))
        if not paths:
            out += [_empty_row(c.citation_id, comment_id, "no_source_file") for c in cits]
            continue
        rel = str(paths[0].relative_to(md_root))
        doc = DocV2(paths[0].read_text(encoding="utf-8", errors="ignore"))
        cache: dict[str, dict | None] = {}
        last_start = -1
        for c in cits:
            row, method, last_start = match_citation(doc, c, last_start, cache)
            if row is None:
                out.append(_empty_row(c.citation_id, comment_id, method, rel))
                continue
            ws, we = row.pop("window_start"), row.pop("window_end")
            out.append({"citation_id": c.citation_id, "comment_id": comment_id, "source_path": rel,
                        "window_key": f"{comment_id}:{ws}-{we}", **row})
        if n % 100 == 0:
            print(f"  [citation_context_v2] {n}/{len(by_comment)} comments")

    bind = session.get_bind()
    CitationContextV2.__table__.drop(bind, checkfirst=True)
    CitationContextV2.__table__.create(bind)
    session.bulk_insert_mappings(CitationContextV2, out)
    session.commit()

    counts: dict[str, int] = defaultdict(int)
    for r in out:
        counts[r.get("core_method") or r["match_method"]] += 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    print(f"[citation_context_v2] rows={len(out)}  {summary}")
    print(f"  relocated={sum(bool(r.get('relocated')) for r in out)}  "
          f"repaired={sum(bool(r.get('repaired')) for r in out)}")


if __name__ == "__main__":
    import db as _db
    _db.init_db()
    with _db.Session() as s:
        load_citation_context_v2(s)
