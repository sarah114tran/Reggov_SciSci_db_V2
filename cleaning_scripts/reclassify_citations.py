"""
cleaning_scripts/reclassify_citations.py
Pass-3 reclassification rules for comment_citations rows still marked
'unclassified' (or with no category). Used by apply_citation_filters.py
--pass 3; returns proposed changes, never writes.

Rules, first match wins:
  gov_domain        link on a .gov/.mil domain -> government_resource (beats
                    link_sibling: some sibling rows are mislabeled website).
  link_sibling      the row has a citation_link that other, already-classified
                    rows share (typically an unmatched_bibliography entry --
                    those never went through linkClassifier) -> the category
                    those rows got.
  journal_citation  "<vol> <Journal Abbrev.> <page> (<year>)" law-review /
                    journal style, not a court reporter -> research_article.
  legal_case        court-reporter cites, incl. volume-less "U.S. 340 (U.S.
                    1991)", and bare case names "Alario et al. v. Knudsen."
                    -> legal_citation.
  domain_majority   link's domain is >= 60% one category over >= 3 classified
                    rows -> that category.
  url_in_text       no citation_link but a URL in the text ("www.uspto.gov/...",
                    OCR'd "htps://"): fills citation_link/domain, then
                    link_sibling -> domain_majority -> TLD fallback.
  tld_fallback      .gov/.mil -> government_resource, .edu ->
                    educational_institution_resource, else website.
"""
import collections
import re

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=())

URL_RE = re.compile(r"(?:\bh[\wƩ;]{0,3}ps?://|\bwww\.)[^\s\"'<>\]]+", re.I)
COURT_REPORTER = r"(?:U\.\s?S\.|S\.\s?Ct\.|L\.\s?Ed\.(?:\s?2d)?|F\.(?:\s?(?:2d|3d|4th)|\s?Supp\.(?:\s?[23]d)?)|F\.\s?App'x)"
LEGAL_CASE_RES = [
    re.compile(rf"^\s*(?:\d+\s+)?{COURT_REPORTER}\s+\d+", re.I),                      # 499 U.S. 340 / U.S. 340
    re.compile(r"\((?:U\.\s?S\.|[A-Z][\w.\s]{0,20}Cir\.|[A-Z]\.\s?D\.[^)]*)\s*(?:19|20)\d\d\)"),  # (U.S. 1991) / (9th Cir. 2000)
    re.compile(r"^\s*(?:see\s+)?[A-Z][\w.,&'’ -]{0,80}?\s+v\.\s+[A-Z][\w.,&'’ -]{0,80}$"),  # X v. Y
]
NOT_JOURNAL = rf"(?:{COURT_REPORTER}|Fed\.?\s?Reg|F\.\s?R\.|FR\b|Stat\.|U\.\s?S\.\s?C|C\.\s?F\.\s?R|Cong\.\s?Rec)"
JOURNAL_RE = re.compile(
    rf"\b\d{{1,4}}\s+(?!{NOT_JOURNAL})[A-Z][A-Za-z.&'’ ]{{2,80}}?\s+\d{{1,5}}(?:[-–]\d+)?(?:,\s*\d+)?\s*\((?:[A-Za-z. ]+\s)?(?:19|20)\d\d\)")
UNCLASSIFIED = {None, "", "unclassified"}


def norm_link(url: str | None) -> str:
    u = (url or "").strip().split("#")[0].rstrip("./,;").lower()
    u = re.sub(r"^h[\wƩ;]{0,3}ps?://", "", u)
    return u[4:] if u.startswith("www.") else u


def fix_url(url: str) -> str:
    # OCR ligature damage: \x11 / Ɵ = "ti", Ʃ = "tt", "h;ps" / "hps" = "https".
    url = url.replace("\x11", "ti").replace("Ɵ", "ti").replace("Ʃ", "tt").rstrip(".,;)")
    url = re.sub(r"^h[\w;]{0,3}p(s?)://", lambda m: f"http{m.group(1)}://", url, flags=re.I)
    return url if re.match(r"https?://", url, re.I) else "https://" + url


def registered_domain(url: str) -> str | None:
    ext = _extract(url)
    return ext.top_domain_under_public_suffix or ext.domain or None


class Context:
    def __init__(self, rows):
        self.by_link = collections.defaultdict(collections.Counter)
        self.by_domain = collections.defaultdict(collections.Counter)
        for r in rows:
            cat = r["cited_source_catagory"]
            if cat in UNCLASSIFIED or not (r["citation_link"] or "").strip():
                continue
            self.by_link[norm_link(r["citation_link"])][cat] += 1
            if r["domain"]:
                self.by_domain[r["domain"]][cat] += 1

    def link_category(self, url):
        c = self.by_link.get(norm_link(url))
        return c.most_common(1)[0][0] if c else None

    def domain_category(self, domain):
        c = self.by_domain.get(domain)
        if not c:
            return None
        cat, n = c.most_common(1)[0]
        total = sum(c.values())
        return cat if total >= 3 and n / total >= 0.6 else None


def _is_gov(domain: str | None) -> bool:
    d = (domain or "").lower()
    return d.endswith((".gov", ".mil")) or ".gov." in d


def _tld_category(domain: str | None) -> str:
    d = (domain or "").lower()
    if _is_gov(d):
        return "government_resource"
    if d.endswith(".edu"):
        return "educational_institution_resource"
    return "website"


def propose(row, texts: list[str], ctx: Context):
    """-> (rule, new_category, new_link, new_domain) or None."""
    if row["cited_source_catagory"] not in UNCLASSIFIED:
        return None
    joined = " ".join(texts)
    link = (row["citation_link"] or "").strip()

    if link:
        domain = row["domain"] or registered_domain(fix_url(link))
        if _is_gov(domain):
            return "gov_domain", "government_resource", None, None
        cat = ctx.link_category(link)
        if cat:
            return "link_sibling", cat, None, None
    if JOURNAL_RE.search(joined):
        return "journal_citation", "research_article", None, None
    if not URL_RE.search(joined) and any(rx.search(t) for t in texts for rx in LEGAL_CASE_RES):
        return "legal_case", "legal_citation", None, None
    if link:
        cat = ctx.domain_category(domain)
        if cat:
            return "domain_majority", cat, None, None
        return "tld_fallback", _tld_category(domain), None, None

    m = URL_RE.search(joined)
    if m:
        url = fix_url(m.group(0))
        domain = registered_domain(url)
        if not domain or "." not in url.split("//", 1)[-1]:
            return None
        if _is_gov(domain):
            return "url_in_text", "government_resource", url, domain
        cat = ctx.link_category(url) or ctx.domain_category(domain) or _tld_category(domain)
        return "url_in_text", cat, url, domain
    return None
