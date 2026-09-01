"""
Citation span location — deterministic, no LLM.

Locates every citation *mention* in a generated Related Works section
(LaTeX \\cite-family commands, or author-date citations in the Markdown
output path) and anchors each one to the sentence that makes a claim
about it. This is the input to Tier 4 (evidence support) and is also
used to rewrite Tier 1's citation counting so \\citep/\\citet stop being
invisible.

Nothing here calls an LLM. Sentence splitting is a regex heuristic
with an abbreviation guard — good enough for locating citation context,
not a linguistically complete sentence boundary detector.
"""

import re
from dataclasses import dataclass, field


@dataclass
class CitationSpan:
    """One citation mention, anchored to its sentence."""
    citation_id: str        # f"{key}#{ordinal}" — unique per mention
    key: str                # bib key (may be a hallucinated key not in bib)
    surface: str             # raw matched text, e.g. "\\citep{smith2021}"
    style: str                # "latex" | "author_date"
    char_start: int
    char_end: int
    sentence: str
    sentence_index: int
    sentence_char_start: int
    co_cited_keys: list[str] = field(default_factory=list)
    context: str = ""         # prev + current + next sentence
    resolution: str = "exact"  # "exact" | "author_year_matched" | "unmatched"


# ── Sentence splitting ──────────────────────────────────────────────

# Abbreviations that must not be treated as sentence-final periods.
_ABBREVIATIONS = {
    "e.g.", "i.e.", "et al.", "etc.", "cf.", "vs.", "approx.",
    "fig.", "figs.", "eq.", "eqs.", "no.", "nos.", "vol.", "vols.",
    "pp.", "p.", "ch.", "sec.", "secs.", "dr.", "mr.", "mrs.", "ms.",
    "prof.", "st.",
}

# Matches a candidate sentence boundary: punctuation, whitespace, then
# a capital letter or opening quote/paren starting the next sentence.
_BOUNDARY_RE = re.compile(r'(?<=[.!?])[\s]+(?=[A-Z0-9"\'(\u201c])')

# Single trailing initial like "A." — not an abbreviation list match,
# but also not a sentence end.
_INITIAL_RE = re.compile(r'\b[A-Z]\.$')


def _has_unbalanced_brace_or_paren(text: str) -> bool:
    """True if text has an unclosed \\cite{...} or ( at the end."""
    depth_brace = text.count('{') - text.count('}')
    depth_paren = text.count('(') - text.count(')')
    return depth_brace > 0 or depth_paren > 0


def split_sentences(text: str) -> list[tuple[str, int, int]]:
    """
    Split text into sentences.

    Returns a list of (sentence_text, char_start, char_end) tuples,
    char offsets into the original text (sentence_text is stripped,
    but offsets point at the stripped span).
    """
    if not text:
        return []

    # Candidate boundaries
    boundaries = [m.start() + 1 for m in _BOUNDARY_RE.finditer(text)]
    # +1 lands right after the punctuation character (before whitespace)

    sentences = []
    start = 0
    for b in boundaries:
        candidate = text[start:b]
        stripped = candidate.rstrip()

        # Guard 1: abbreviation immediately before the boundary
        tail_word = stripped.split()[-1].lower() if stripped.split() else ""
        if tail_word in _ABBREVIATIONS:
            continue
        # Guard 2: single capital initial (e.g. "A.")
        if _INITIAL_RE.search(stripped):
            continue
        # Guard 3: boundary falls inside an unclosed \cite{...} or (...)
        if _has_unbalanced_brace_or_paren(candidate):
            continue

        sentences.append((start, b))
        start = b

    # Final sentence
    if start < len(text):
        sentences.append((start, len(text)))

    result = []
    for s_start, s_end in sentences:
        raw = text[s_start:s_end]
        stripped = raw.strip()
        if not stripped:
            continue
        # Recompute offsets to point at the stripped text
        lead_ws = len(raw) - len(raw.lstrip())
        trail_ws = len(raw) - len(raw.rstrip())
        real_start = s_start + lead_ws
        real_end = s_end - trail_ws
        result.append((stripped, real_start, real_end))

    return result


# ── Citation extraction ─────────────────────────────────────────────

_LATEX_CITE_RE = re.compile(
    r'\\(cite|citep|citet|citealp|citeyear|autocite)\*?'
    r'(?:\[[^\]]*\])*'
    r'\{([^}]+)\}'
)

# Author-date fallback for the markdown output path, e.g.
# "(Smith et al., 2021)" or "(Smith & Jones, 2022)" or "Smith (2021)"
_AUTHOR_DATE_RE = re.compile(
    r'\(?\b([A-Z][A-Za-z\'\-]+)(?:\s+(?:et al\.|&|and)\s+[A-Z][A-Za-z\'\-]+)?'
    r',?\s+(\d{4}[a-z]?)\)?'
)


def _build_author_year_index(bib_entries: list[dict]) -> dict[tuple[str, str], str]:
    """Map (lowercased first-author surname, year) -> bib key."""
    index = {}
    for entry in bib_entries:
        author_field = entry.get("author", "")
        year = str(entry.get("year", "")).strip()
        if not author_field or not year:
            continue
        # BibTeX author field: "Last, First and Last2, First2" or "First Last and ..."
        first_author = author_field.split(" and ")[0].strip()
        if "," in first_author:
            surname = first_author.split(",")[0].strip()
        else:
            parts = first_author.split()
            surname = parts[-1].strip() if parts else first_author
        surname = surname.lower()
        if surname:
            index[(surname, year)] = entry.get("key", "")
    return index


def find_citation_spans(text: str, bib_entries: list[dict]) -> list[CitationSpan]:
    """
    Locate every citation mention in `text` and anchor it to its
    sentence.

    Args:
        text: generated Related Works text (LaTeX or Markdown/author-date).
        bib_entries: parsed .bib entries (from parse_bib_file), used to
            resolve author-date citations back to keys.

    Returns:
        List of CitationSpan, one per (key, mention) pair — a sentence
        citing two keys in one \\cite{a,b} produces two spans sharing
        the sentence, each listing the other key in co_cited_keys.
    """
    sentences = split_sentences(text)
    if not sentences:
        return []

    def _sentence_for_offset(offset: int) -> tuple[int, str, int]:
        """Return (sentence_index, sentence_text, sentence_char_start)."""
        for idx, (sent, s_start, s_end) in enumerate(sentences):
            if s_start <= offset < s_end:
                return idx, sent, s_start
        # Fall back to nearest sentence (offset landed in whitespace/gap)
        for idx, (sent, s_start, s_end) in enumerate(sentences):
            if offset < s_end:
                return idx, sent, s_start
        idx = len(sentences) - 1
        sent, s_start, _ = sentences[idx]
        return idx, sent, s_start

    def _context_for(idx: int) -> str:
        parts = []
        if idx > 0:
            parts.append(sentences[idx - 1][0])
        parts.append(sentences[idx][0])
        if idx + 1 < len(sentences):
            parts.append(sentences[idx + 1][0])
        return " ".join(parts)

    spans: list[CitationSpan] = []
    ordinal_by_key: dict[str, int] = {}

    def _next_ordinal(key: str) -> int:
        n = ordinal_by_key.get(key, 0) + 1
        ordinal_by_key[key] = n
        return n

    # ── LaTeX \cite-family ──
    seen_latex_mention = False
    for match in _LATEX_CITE_RE.finditer(text):
        seen_latex_mention = True
        keys = [k.strip() for k in match.group(2).split(',') if k.strip()]
        if not keys:
            continue
        char_start, char_end = match.start(), match.end()
        sent_idx, sent_text, sent_start = _sentence_for_offset(char_start)
        context = _context_for(sent_idx)

        for key in keys:
            co_cited = [k for k in keys if k != key]
            spans.append(CitationSpan(
                citation_id=f"{key}#{_next_ordinal(key)}",
                key=key,
                surface=match.group(0),
                style="latex",
                char_start=char_start,
                char_end=char_end,
                sentence=sent_text,
                sentence_index=sent_idx,
                sentence_char_start=sent_start,
                co_cited_keys=co_cited,
                context=context,
                resolution="exact",
            ))

    if seen_latex_mention:
        return spans

    # ── Author-date fallback (markdown output) ──
    author_year_index = _build_author_year_index(bib_entries)

    for match in _AUTHOR_DATE_RE.finditer(text):
        surname = match.group(1).lower()
        year = match.group(2)
        char_start, char_end = match.start(), match.end()
        sent_idx, sent_text, sent_start = _sentence_for_offset(char_start)
        context = _context_for(sent_idx)

        key = author_year_index.get((surname, year))
        resolution = "author_year_matched" if key else "unmatched"
        resolved_key = key or f"unresolved:{surname}{year}"

        spans.append(CitationSpan(
            citation_id=f"{resolved_key}#{_next_ordinal(resolved_key)}",
            key=resolved_key,
            surface=match.group(0),
            style="author_date",
            char_start=char_start,
            char_end=char_end,
            sentence=sent_text,
            sentence_index=sent_idx,
            sentence_char_start=sent_start,
            co_cited_keys=[],
            context=context,
            resolution=resolution,
        ))

    return spans
