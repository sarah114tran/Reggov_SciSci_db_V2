"""
loaders/citation_context.py
Builds citation_context: for every comment_citations row, the sentence before
and after its citation_sentence, taken verbatim from the comment's marker-pdf
markdown (the same text the LLM extracted citations from).

Not part of main.py — the markdown lives outside this repo (Dropbox), so run
it separately after a build:

    REGGOV_MD_ROOT=~/Dropbox/processed_pdfs python -m loaders.citation_context

Approach
--------
1. Each .md is parsed line-by-line into units: heading, body, footnote,
   table, noise (images, page numbers, empty spans, table rules).
   Headings are ATX "#" lines, whole-line bold/italic lines, short numbered
   section titles, and short Title Case lines without terminal punctuation.
2. Markup is stripped from each unit while keeping a char-level map back to
   the raw markdown, then split into sentences with pysbd. Body sentences
   broken across a page break (no terminal punctuation, next body unit starts
   lowercase) are merged.
3. citation_sentence is cleaned the same way and located with a cascade:
   exact -> exact against body text only (bridges page-bottom footnotes)
   -> head/tail anchors (long sentences) -> fuzzy alignment (score >= 85).
   When a sentence occurs more than once (marker often repeats the
   regulations.gov "General Comment" text), the first hit at or after the
   previous citation's position in the comment is used.
4. Context comes from reading order. Body text flows across paragraphs and
   skips page-bottom footnotes / noise, but STOPS at headings and tables: the
   neighbour is NULL and prev_boundary / next_boundary records why. Footnote
   and table-row citations only take neighbours from the same footnote/row.

source_sentence / prev_sentence / next_sentence are raw markdown slices
(markup such as ** or <sup> included).
"""
import os
import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

import pysbd
from rapidfuzz import fuzz
from sqlalchemy import text
from sqlalchemy.orm import Session

from models import CitationContext

MD_ROOT = Path(os.path.expanduser(os.environ.get("REGGOV_MD_ROOT", "~/Dropbox/processed_pdfs")))

FUZZY_CUTOFF  = 85
ANCHOR_CUTOFF = 90
ANCHOR_LEN    = 100
LONG_NEEDLE   = 400   # normalized chars; above this try anchors before full fuzzy
SHORT_NEEDLE  = 25    # below this only exact matches are trusted
MIN_COVER_RATIO = 0.5 # fuzzy/anchor: matched sentences must span >= this share of the query

_SEG = pysbd.Segmenter(language="en", clean=False)


# ── Mapped substitution ──────────────────────────────────────────────────────
# Text is carried as (string, map) where map[i] is the raw-markdown offset of
# string[i]. Replacements are either a literal (mapped to the match start) or
# a group number (that group's chars are kept with their original offsets).

def _msub(rx: re.Pattern, repl, s: str, m: list[int]) -> tuple[str, list[int]]:
    out, om, pos = [], [], 0
    for mt in rx.finditer(s):
        a, b = mt.span()
        if a == b:
            continue
        out.append(s[pos:a]); om.extend(m[pos:a])
        if isinstance(repl, int):
            ga, gb = mt.span(repl)
            if ga != -1:
                out.append(s[ga:gb]); om.extend(m[ga:gb])
        elif repl:
            out.append(repl); om.extend([m[a]] * len(repl))
        pos = b
    out.append(s[pos:]); om.extend(m[pos:])
    return "".join(out), om


def _strip(s: str, m: list[int]) -> tuple[str, list[int]]:
    a, b = 0, len(s)
    while a < b and s[a].isspace():
        a += 1
    while b > a and s[b - 1].isspace():
        b -= 1
    return s[a:b], m[a:b]


_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}

_CLEAN_STEPS = [
    (re.compile(r"<span[^>]*>\s*</span>"), ""),
    (re.compile(r"<sup>[^<]{0,12}</sup>"), ""),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),
    (re.compile(r"\[((?:\\.|[^\]\\])*)\]\((?:[^()\s]|\([^)]*\))*\)"), 1),  # [text](url) -> text
    (re.compile(r"</?[a-zA-Z][^>]*>"), ""),
    (re.compile(r"^\s*#{1,6}\s*"), ""),
    (re.compile(r"^\s*[-*+•]\s+"), ""),
    (re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>])"), 1),                 # markdown escapes
    (re.compile(r"\*{1,3}"), ""),
    (re.compile(r"(?<![A-Za-z0-9])_{1,3}|_{1,3}(?![A-Za-z0-9])"), ""),
    (re.compile(r"\|"), " "),
]
_ENTITY_RX = re.compile("|".join(map(re.escape, _ENTITIES)))
_WS_RX = re.compile(r"\s+")


def _clean(s: str, m: list[int]) -> tuple[str, list[int]]:
    """Markdown -> readable text, keeping the offset map."""
    for rx, repl in _CLEAN_STEPS:
        s, m = _msub(rx, repl, s, m)
    out, om, pos = [], [], 0
    for mt in _ENTITY_RX.finditer(s):
        out.append(s[pos:mt.start()]); om.extend(m[pos:mt.start()])
        out.append(_ENTITIES[mt.group()]); om.append(m[mt.start()])
        pos = mt.end()
    out.append(s[pos:]); om.extend(m[pos:])
    s, m = _msub(_WS_RX, " ", "".join(out), om)
    return _strip(s, m)


_CHAR_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "„": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ",
})
_FN_GLUE_RX = re.compile(r"(?<=[a-z.,;:!?\"')])\d{1,3}(?=[\s).,;:]|$)")   # "formula,26 " -> "formula, "
_MD_PUNCT_RX = re.compile(r"[*_#>|\[\]]")


def _norm(s: str, m: list[int]) -> tuple[str, list[int]]:
    """Clean text -> matching key (NFKC, folded quotes/dashes, lowercase)."""
    out, om = [], []
    for ch, off in zip(s, m):
        for c in unicodedata.normalize("NFKC", ch).translate(_CHAR_FOLD).lower():
            out.append(c); om.append(off)
    s, m = "".join(out), om
    s, m = _msub(_MD_PUNCT_RX, "", s, m)
    s, m = _msub(_FN_GLUE_RX, "", s, m)
    s, m = _msub(_WS_RX, " ", s, m)
    return _strip(s, m)


def _norm_query(s: str) -> str:
    m = list(range(len(s)))
    return _norm(*_clean(s, m))[0]


# ── Document parsing ─────────────────────────────────────────────────────────

_ATX_RX        = re.compile(r"^#{1,6}\s")
_TABLE_RULE_RX = re.compile(r"^\|[\s:|-]*\|?$")
_FN_SUP_RX     = re.compile(r"^(?:<span[^>]*>\s*</span>\s*)*<sup>\s*\d{1,3}\s*</sup>")
_FN_PLAIN_RX   = re.compile(r"^(?:<span[^>]*>\s*</span>\s*)*\d{1,3}\s+\S")
_FN_HINT_RX    = re.compile(r"https?://|www\.|\b(?:19|20)\d{2}\b|\bId\.|\bSee\b|supra|available at", re.I)
_PAGE_NOISE_RX = re.compile(r"^(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)
_LIST_PREFIX_RX = re.compile(r"^(?:[-*+•]\s+|(?:\d+(?:\.\d+)*|[IVXLC]+|[A-Za-z])[.)]\s+)")
_WHOLE_EMPH_RX = re.compile(r"^(\*\*\*|\*\*|__|\*|_)(?:(?!\1).)+\1\s*[:.]?$")
_NUMBERED_TITLE_RX = re.compile(r"^(?:\d+(?:\.\d+)*|[IVXLC]+|[A-H])[.)]\s+\S.{0,118}$")
_TERMINAL_RX   = re.compile(r"[.!?][\"'”’)\]]*$")
_LABEL_RX      = re.compile(r"^\W*(?:[A-Za-z]|[ivxIVX]+|\d+(?:[.-][A-Za-z\d]+)*)[.)]\W*$|^\W*\([\w.]+\)\W*$")
_LEAD_RX       = re.compile(r"[*_\[]+$")
_TRAIL_RX      = re.compile(r"(?:[*_]+|\]\((?:[^()\s]|\([^)]*\))*\)|<sup>[^<]{0,12}</sup>)+")
_STOPWORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with", "vs", "via"}


def _is_pseudo_heading(raw: str, clean: str) -> bool:
    words = clean.split()
    if not words:
        return False
    # Link-only resource lines and emphasised quotations are body text.
    if "http" in raw or clean[:1] in "\"“'‘":
        return False
    body = _LIST_PREFIX_RX.sub("", raw)
    if _WHOLE_EMPH_RX.match(body) and len(clean) <= 200:
        # A bolded full sentence is emphasis, not a heading.
        return not (_TERMINAL_RX.search(clean) and len(words) >= 12)
    if _NUMBERED_TITLE_RX.match(clean) and len(words) <= 15 and not clean.endswith((".", ";", ",")):
        return True
    if len(clean) <= 80 and len(words) <= 10 and not re.search(r"[.!?;,:\"”]$", clean):
        content = [w for w in words if w.lower() not in _STOPWORDS]
        caps = sum(w[:1].isupper() or w[:1].isdigit() for w in content)
        return bool(content) and caps / len(content) >= 0.6
    return False


class Doc:
    """Parsed markdown: units, sentences, reading-order flow, match strings."""

    def __init__(self, raw: str):
        self.raw = raw
        self.sents: list[dict] = []          # {utype, unit, pieces[(rs,re)], clean}
        self.flow: list = []                 # body sentence ids and ("B", kind) boundaries
        self.flow_pos: dict[int, int] = {}
        self.local: dict[int, list[int]] = {}  # unit -> sentence ids (footnote/table/heading)
        units = self._parse_units()
        self._split(units)
        self._build_flow(units)
        self.all_key,  self.all_map  = self._key(units, {"body", "footnote", "table", "heading"})
        self.body_key, self.body_map = self._key(units, {"body"})
        starts = [(p[0], i) for i, s in enumerate(self.sents) for p in s["pieces"]]
        starts.sort()
        self._piece_starts = [p for p, _ in starts]
        self._piece_sids = [i for _, i in starts]

    def _parse_units(self) -> list[dict]:
        units, off = [], 0
        for line in self.raw.splitlines(keepends=True):
            stripped = line.strip()
            lead = len(line) - len(line.lstrip())
            start, off = off + lead, off + len(line)
            if not stripped:
                continue
            if stripped.startswith("|"):
                utype = "noise" if _TABLE_RULE_RX.match(stripped) else "table"
            elif _ATX_RX.match(stripped):
                utype = "heading"
            elif _FN_SUP_RX.match(stripped) or (
                _FN_PLAIN_RX.match(stripped) and _FN_HINT_RX.search(stripped) and len(stripped) <= 1500
            ):
                utype = "footnote"
            else:
                utype = None
            clean, cmap = _clean(stripped, list(range(start, start + len(stripped))))
            if not clean or _PAGE_NOISE_RX.match(clean):
                utype = "noise"
            elif utype is None:
                utype = "heading" if _is_pseudo_heading(stripped, clean) else "body"
            units.append({"utype": utype, "clean": clean, "map": cmap, "sids": []})
        return units

    def _split(self, units: list[dict]) -> None:
        for ui, u in enumerate(units):
            if u["utype"] == "noise":
                continue
            clean, cmap = u["clean"], u["map"]
            segs = [clean] if u["utype"] == "heading" else _SEG.segment(clean)
            cur, carry = 0, None
            for seg in segs:
                seg = seg.strip()
                if not seg:
                    continue
                a = clean.find(seg, cur)
                if a == -1:
                    a, seg = cur, clean[cur:].strip()
                    if not seg:
                        break
                b = a + len(seg)
                cur = b
                if not re.search(r"[A-Za-z]{2}", seg):
                    if u["sids"] and not _LABEL_RX.match(seg) and carry is None:
                        # trailing footnote number / "[5]": attach to previous sentence
                        prev = self.sents[u["sids"][-1]]
                        prev["pieces"][-1] = (prev["pieces"][-1][0], self._expand(cmap[a], cmap[b - 1] + 1)[1])
                        prev["clean"] += " " + seg
                    elif carry is None:
                        carry = a   # list label ("b.", "(1.d.i)"): prepend to next sentence
                    continue
                if carry is not None:
                    a, seg, carry = carry, clean[carry:b], None
                sid = len(self.sents)
                self.sents.append({"utype": u["utype"], "unit": ui,
                                   "pieces": [self._expand(cmap[a], cmap[b - 1] + 1)], "clean": seg})
                u["sids"].append(sid)

    def _expand(self, rs: int, re_: int) -> tuple[int, int]:
        """Widen a raw span over markup hugging its edges (** / [ before;
        ** / ](url) / <sup>n</sup> after) so slices stay well-formed."""
        lead = _LEAD_RX.search(self.raw, self.raw.rfind("\n", 0, rs) + 1, rs)
        trail = _TRAIL_RX.match(self.raw, re_)
        return (lead.start() if lead else rs), (trail.end() if trail else re_)

    def _build_flow(self, units: list[dict]) -> None:
        pending = None   # body sentence that may continue in the next body unit
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
            if pending is not None and sids and self.sents[sids[0]]["clean"][:1].islower():
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

    def _key(self, units: list[dict], types: set) -> tuple[str, list[int]]:
        parts, pmap = [], []
        for u in units:
            if u["utype"] not in types:
                continue
            k, km = _norm(u["clean"], u["map"])
            if not k:
                continue
            if parts:
                parts.append(" "); pmap.append(-1)
            parts.append(k); pmap.extend(km)
        return "".join(parts), pmap

    # ── lookups ──

    def resolve(self, sid: int) -> int:
        while "merged_into" in self.sents[sid]:
            sid = self.sents[sid]["merged_into"]
        return sid

    def covered(self, raw_positions: list[int]) -> list[int]:
        """Sentence ids substantially covered by the matched raw positions."""
        pos = sorted(raw_positions)
        counts: dict[int, int] = defaultdict(int)
        for sid, s in enumerate(self.sents):
            for rs, re_ in s["pieces"]:
                n = bisect_left(pos, re_) - bisect_left(pos, rs)
                if n:
                    counts[self.resolve(sid)] += n
        hits = [sid for sid, n in counts.items()
                if n >= min(15, 0.5 * len(self.sents[sid]["clean"]))]
        if not hits and pos:
            mid = pos[len(pos) // 2]
            i = bisect_left(self._piece_starts, mid + 1) - 1
            if i >= 0:
                hits = [self.resolve(self._piece_sids[i])]
        return sorted(hits, key=lambda sid: self.sents[sid]["pieces"][0][0])

    def neighbour(self, sid: int, step: int) -> tuple[int | None, str | None]:
        s = self.sents[sid]
        if s["utype"] == "body":
            k = self.flow_pos[sid] + step
            if not 0 <= k < len(self.flow):
                return None, "document_edge"
            item = self.flow[k]
            return (None, item[1]) if isinstance(item, tuple) else (item, None)
        if s["utype"] == "heading":
            return None, "heading"
        sids = self.local.get(s["unit"], [])
        k = sids.index(sid) + step
        return (sids[k], None) if 0 <= k < len(sids) else (None, f"{s['utype']}_edge")

    def text(self, sid: int | None) -> str | None:
        if sid is None:
            return None
        return " ".join(self.raw[rs:re_].strip() for rs, re_ in self.sents[sid]["pieces"])


# ── Matching ─────────────────────────────────────────────────────────────────

def _find_all(hay: str, needle: str) -> list[int]:
    out, i = [], hay.find(needle)
    while i != -1:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def _span_positions(pmap: list[int], a: int, b: int) -> list[int]:
    return [p for p in pmap[a:b] if p >= 0]


def _locate(doc: Doc, q: str) -> list[tuple[str, float, list[list[int]]]]:
    """Return [(method, score, [raw_positions per hit])] — first successful stage."""
    for method, key, pmap in (("exact", doc.all_key, doc.all_map),
                              ("exact_body_flow", doc.body_key, doc.body_map)):
        hits = _find_all(key, q)
        if hits:
            return [(method, 100.0, [_span_positions(pmap, h, h + len(q)) for h in hits])]
    if len(q) < SHORT_NEEDLE or not doc.all_key:
        return []
    if len(q) > LONG_NEEDLE:
        head = fuzz.partial_ratio_alignment(q[:ANCHOR_LEN], doc.all_key, score_cutoff=ANCHOR_CUTOFF)
        tail = fuzz.partial_ratio_alignment(q[-ANCHOR_LEN:], doc.all_key, score_cutoff=ANCHOR_CUTOFF)
        if head and tail and tail.dest_end > head.dest_start:
            span = tail.dest_end - head.dest_start
            if 0.6 <= span / len(q) <= 1.6:
                return [("anchor", min(head.score, tail.score),
                         [_span_positions(doc.all_map, head.dest_start, tail.dest_end)])]
    al = fuzz.partial_ratio_alignment(q, doc.all_key, score_cutoff=FUZZY_CUTOFF)
    if al:
        return [("fuzzy", al.score, [_span_positions(doc.all_map, al.dest_start, al.dest_end)])]
    return []


# ── Loader ───────────────────────────────────────────────────────────────────

def _index_md(md_root: Path) -> dict[str, list[Path]]:
    idx: dict[str, list[Path]] = defaultdict(list)
    for p in md_root.rglob("*.md"):
        idx[re.sub(r"_\d+$", "", p.stem)].append(p)
    return idx


def _empty_row(cit_id: str, comment_id: str, method: str, path: str | None = None) -> dict:
    return {"citation_id": cit_id, "comment_id": comment_id, "source_path": path,
            "match_method": method}


def load_citation_context(session: Session, md_root: str | Path = MD_ROOT) -> None:
    md_root = Path(md_root)
    md_index = _index_md(md_root)
    print(f"[citation_context] indexed {sum(map(len, md_index.values()))} .md files under {md_root}")

    # rowid order follows extraction order, i.e. reading order within a comment
    rows = session.execute(text(
        "SELECT citation_id, comment_id, citation_sentence FROM comment_citations ORDER BY rowid"
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
        if len(paths) > 1:
            print(f"  [citation_context] {comment_id}: {len(paths)} attachments, using {paths[0].name}")
        rel = str(paths[0].relative_to(md_root))
        doc = Doc(paths[0].read_text(encoding="utf-8", errors="ignore"))
        cache: dict[str, list] = {}
        last_start = -1
        for c in cits:
            q = _norm_query(c.citation_sentence or "")
            if not q:
                out.append(_empty_row(c.citation_id, comment_id, "empty_sentence", rel))
                continue
            if q not in cache:
                cache[q] = _locate(doc, q)
            found = cache[q]
            if not found:
                out.append(_empty_row(c.citation_id, comment_id, "unmatched", rel))
                continue
            method, score, hits = found[0]
            hits = [h for h in hits if h]
            if not hits:
                out.append(_empty_row(c.citation_id, comment_id, "unmatched", rel))
                continue
            chosen = next((h for h in hits if min(h) >= last_start), hits[0])
            sids = doc.covered(chosen)
            if not sids:
                out.append(_empty_row(c.citation_id, comment_id, "unmatched", rel))
                continue
            # Body sentences win if a match straddles body + footnote text.
            body = [s for s in sids if doc.sents[s]["utype"] == "body"]
            sids = body or sids
            # A fuzzy hit whose sentences are far shorter than the query
            # landed on unrelated text (e.g. a short heading).
            if method in ("fuzzy", "anchor") and \
                    sum(len(doc.sents[s]["clean"]) for s in sids) < MIN_COVER_RATIO * len(q):
                out.append(_empty_row(c.citation_id, comment_id, "unmatched", rel))
                continue
            first, last = sids[0], sids[-1]
            prev_sid, prev_b = doc.neighbour(first, -1)
            next_sid, next_b = doc.neighbour(last, +1)
            pieces = [p for s in sids for p in doc.sents[s]["pieces"]]
            last_start = min(chosen)
            out.append({
                "citation_id":     c.citation_id,
                "comment_id":      comment_id,
                "source_path":     rel,
                "match_method":    method,
                "match_score":     round(score, 1),
                "n_hits":          len(hits),
                "char_start":      min(p[0] for p in pieces),
                "char_end":        max(p[1] for p in pieces),
                "unit_type":       doc.sents[first]["utype"],
                "source_sentence": " ".join(doc.text(s) for s in sids),
                "prev_sentence":   doc.text(prev_sid),
                "next_sentence":   doc.text(next_sid),
                "prev_boundary":   prev_b,
                "next_boundary":   next_b,
            })
        if n % 100 == 0:
            print(f"  [citation_context] {n}/{len(by_comment)} comments")

    session.query(CitationContext).delete()
    session.bulk_insert_mappings(CitationContext, out)
    session.commit()

    counts: dict[str, int] = defaultdict(int)
    for r in out:
        counts[r["match_method"]] += 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    print(f"[citation_context] rows={len(out)}  {summary}")


if __name__ == "__main__":
    import db as _db
    _db.init_db()
    with _db.Session() as s:
        load_citation_context(s)
