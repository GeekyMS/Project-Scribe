"""
Stage 4 — Bibliography Extraction.

Parses citations from the extracted Related Work text, matches them against
the paper's References/Bibliography section, and generates a proper .bib file.
"""

import json
import re
import time
from pathlib import Path

from .extractor import client, MAX_RETRIES, RETRY_DELAY
from .prompts import build_bibtex_generation_prompt
from .schemas import CitationInfo, BibExtractionResult

BIB_MODEL = "anthropic/claude-opus-4.6"


# ── Stage 4A: Parse citations from related work text ──

def _parse_numerical(text: str) -> set:
    """Extract numerical citation keys from brackets like [1], [3, 7], [3-9]."""
    # Only match brackets that look like citations (not equation refs, section nums)
    # Must appear after text (not at start of line as a list marker)
    matches = re.findall(r'(?<=[a-zA-Z.,;)\s])\[([\d,\s\-–—]+)\]', text)
    cited_numbers = set()
    for match in matches:
        parts = match.split(',')
        for part in parts:
            part = part.strip()
            range_match = re.match(r'(\d+)\s*[-–—]\s*(\d+)', part)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if start <= end <= start + 100:
                    cited_numbers.update(range(start, end + 1))
            elif part.isdigit():
                cited_numbers.add(int(part))
    return cited_numbers


def _clean_citation_key(key: str) -> str | None:
    """
    Clean up a raw author-date citation key extracted from PDF text.
    
    Handles garbage from two-column PDF extraction where surrounding
    text gets merged into the citation (e.g., 
    "Col-\nwords has been an active area...    lobert and Weston, 2008").
    
    Returns cleaned key or None if the key is too mangled to salvage.
    """
    # Collapse all whitespace (newlines, tabs, multi-spaces) to single space
    key = re.sub(r'\s+', ' ', key).strip()
    
    # Discard keys that are too long — real citations are short
    # e.g., "Smith et al., 2024" is ~20 chars; allow up to 60 for
    # multi-author keys like "Tjong Kim Sang and De Meulder, 2003"
    if len(key) > 60:
        return None
    
    # Must contain a 4-digit year
    if not re.search(r'\d{4}', key):
        return None
    
    # Must start with a capital letter (author surname)
    if not re.match(r'[A-Z]', key):
        return None
    
    # Count words — real citations typically have < 10 words
    # (e.g., "Vaswani et al., 2017" = 4 words)
    word_count = len(key.split())
    if word_count > 10:
        return None
    
    return key


def _parse_author_date(text: str) -> set:
    """Extract author-date citation keys from parenthetical or bracket citations."""
    raw_keys = set()

    # Pattern 1: Multi-citations in parens — (Hou et al., 2025; Xu et al., 2025)
    multi_paren = re.finditer(r'\(([^)]+\d{4}[^)]*)\)', text)
    for m in multi_paren:
        inner = m.group(1)
        # Split by semicolons for multi-citations
        for part in inner.split(';'):
            part = part.strip()
            if re.search(r'\d{4}', part) and re.search(r'[A-Z]', part):
                raw_keys.add(part)

    # Pattern 2: Author et al. (YYYY) — outside parentheses
    for m in re.finditer(r'([\w\-]+\s+et\s+al\.?)\s*\((\d{4}[a-z]?)\)', text):
        raw_keys.add(f"{m.group(1)}, {m.group(2)}")

    # Pattern 3: Author & Author (YYYY)
    for m in re.finditer(r'([\w\-]+\s+&\s+[\w\-]+)\s*[\(,]\s*(\d{4}[a-z]?)\)?', text):
        raw_keys.add(f"{m.group(1)}, {m.group(2)}")

    # Pattern 4: Bracket-style author-date [Author et al., YYYY]
    for m in re.finditer(r'\[([^\]]*[A-Z][\w\-]*[^\]]*\d{4}[a-z]?[^\]]*)\]', text):
        inner = m.group(1)
        for part in inner.split(';'):
            part = part.strip()
            if re.search(r'\d{4}', part) and re.search(r'[A-Z]', part):
                raw_keys.add(part)

    # Clean all keys — remove garbage from two-column extraction
    cleaned_keys = set()
    for key in raw_keys:
        cleaned = _clean_citation_key(key)
        if cleaned:
            cleaned_keys.add(cleaned)
    
    # Deduplicate near-identical keys (e.g., "Dai and Le, 2015" appearing
    # twice due to different extraction points in the text)
    # Keep the shorter/cleaner version when two keys share the same
    # author surname + year
    final_keys = set()
    seen_author_years = {}  # Maps (surname, year) -> best key
    
    for key in sorted(cleaned_keys, key=len):  # Process shortest first
        # Extract primary author surname and year
        m = re.match(r'([A-Z][\w\-]+)', key)
        year_m = re.search(r'(\d{4}[a-z]?)', key)
        if m and year_m:
            author_year = (m.group(1).lower(), year_m.group(1))
            if author_year not in seen_author_years:
                seen_author_years[author_year] = key
                final_keys.add(key)
            # else: skip duplicate — we already have a cleaner version
        else:
            final_keys.add(key)
    
    return final_keys


def parse_citations(related_work_text: str) -> CitationInfo:
    """
    Detect citation style and extract all citation keys from the related work text.
    
    Tries both numerical and author-date parsing, then picks the style
    that yielded more matches (avoids false positives from noise).
    """
    numerical_keys = _parse_numerical(related_work_text)
    author_date_keys = _parse_author_date(related_work_text)

    # Pick whichever style found more citations
    # Require numerical to have at least 3 hits to avoid noise from page/section numbers
    use_numerical = (len(numerical_keys) >= 3 and
                     len(numerical_keys) > len(author_date_keys))

    if use_numerical:
        print(f"   📖 Detected numerical citation style ({len(numerical_keys)} unique refs)")
        return CitationInfo(style="numerical", keys=numerical_keys)

    if author_date_keys:
        print(f"   📖 Detected author-date citation style ({len(author_date_keys)} unique refs)")
        return CitationInfo(style="author-date", keys=author_date_keys)

    # Last resort: accept even sparse numerical
    if numerical_keys:
        print(f"   📖 Detected numerical citation style ({len(numerical_keys)} unique refs)")
        return CitationInfo(style="numerical", keys=numerical_keys)

    print("   ⚠️ No citations detected in related work text")
    return CitationInfo(style="unknown", keys=set())


# ── Stage 4B: Extract bibliography section and match entries ──

def extract_bibliography_section(full_text: str) -> str | None:
    """
    Find and extract the References/Bibliography section from the full paper text.
    
    Handles multiple PDF extraction quirks:
    - Heading on its own line: "References\n"
    - Heading with trailing whitespace + first entry on same line (sort=True):
      "References                                  [28] G. M..."
    - Heading preceded by section number: "9. References"
    - ALL CAPS: "REFERENCES"
    """
    # Strategy: try multiple patterns from strict to permissive.
    # We search for the LAST occurrence to avoid matching "References"
    # appearing in body text (e.g., "there are many references to...").
    
    patterns = [
        # Pattern 1: Strict — heading alone on a line (possibly with section number)
        re.compile(
            r'^[\s]*(?:\d+\.?\s*)?(?:References|Bibliography)\s*$',
            re.IGNORECASE | re.MULTILINE,
        ),
        # Pattern 2: Heading followed by content on same line (sort=True artifact)
        # e.g., "References                          [1] Author..."
        # e.g., "REFERENCES                          distributed..."
        re.compile(
            r'^[\s]*(?:\d+\.?\s*)?(?:References|Bibliography)\s{6,}',
            re.IGNORECASE | re.MULTILINE,
        ),
        # Pattern 3: Heading followed by newline (after optional PAGE marker)
        re.compile(
            r'(?:--- PAGE \d+ ---\n)?[\s]*(?:References|Bibliography)\s*\n',
            re.IGNORECASE,
        ),
    ]
    
    match = None
    for pattern in patterns:
        # Find ALL matches — we want the LAST one (the actual bibliography,
        # not a mention of "references" in body text like paper17)
        all_matches = list(pattern.finditer(full_text))
        if all_matches:
            match = all_matches[-1]
            break

    if not match:
        return None

    bib_text = full_text[match.start():]

    # Trim at Appendix / Author Contributions / Acknowledgments if present.
    # Handles multiple formatting styles:
    #   "Appendix A"  /  "## A Appendix"  /  "A.1 Contributions"
    #   "Supplementary Material"  /  "Author Contributions"
    #   "Acknowledgments"  /  "## Acknowledgments"
    appendix_match = re.search(
        r'^\s*(?:#+\s*)?(?:[A-Z]\.?\s+)?(?:Appendix|APPENDIX|Supplementary|Author\s+Contributions?|Acknowledgm?ents?)',
        bib_text[100:],  # skip first 100 chars to avoid matching in heading
        re.IGNORECASE | re.MULTILINE,
    )
    if appendix_match:
        bib_text = bib_text[:100 + appendix_match.start()]

    return bib_text.strip()


def match_numerical_citations(citation_keys: set, bibliography_text: str) -> list[str]:
    """Match numbered citations [N] against bibliography entries."""
    # Split by [N] markers
    entries = {}
    # Pattern: start of line with [N] 
    parts = re.split(r'(?:\r?\n|^)\s*(?=\[\d+\])', bibliography_text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r'\[(\d+)\]', part)
        if m:
            num = int(m.group(1))
            # Don't overwrite the first match (real bibliography) with later appendix table matches
            if num not in entries:
                entries[num] = part.strip()

    matched = {}
    for key in sorted(citation_keys):
        if key in entries:
            matched[key] = entries[key]

    return matched


def _split_bibliography_entries(bibliography_text: str) -> list[str]:
    """
    Split bibliography text into individual entries.
    
    Tries multiple strategies:
    1. Double-newline split (most common in well-formatted papers)
    2. Single-line entries (Docling-style clean output where each entry
       is on its own line with no blank-line separators)
    3. If that yields too few entries, try splitting on lines that look
       like the start of a new entry (capitalized author name pattern).
    """
    # Strategy 1: Double-newline split
    entries = re.split(r'\n\s*\n', bibliography_text)
    entries = [e.strip() for e in entries if len(e.strip()) > 20]
    
    if len(entries) >= 5:
        # Quality check: if entries are suspiciously large on average, they're
        # likely page-blobs (e.g., text split on --- PAGE N --- markers rather
        # than on individual entry boundaries).  Typical bib entries are
        # 100–500 chars; page-blobs are 2000+ chars.
        avg_len = sum(len(e) for e in entries) / len(entries)
        if avg_len <= 2000:
            return entries
    
    # Strategy 2: Single-line entries (Docling-style clean output).
    # Detect when bibliography entries are one-per-line with no blank
    # separators. Heuristic: if > 60% of non-empty, non-heading lines
    # contain a 4-digit year, it's a one-entry-per-line bibliography.
    heading_pattern = re.compile(
        r'^(---|##|#|References|Bibliography|REFERENCES|BIBLIOGRAPHY|Appendix|\d+\.)',
        re.IGNORECASE,
    )
    all_lines = [l.strip() for l in bibliography_text.split('\n') if l.strip()]
    content_lines = [l for l in all_lines if not heading_pattern.match(l)]
    year_lines = [l for l in content_lines if re.search(r'\b\d{4}\b', l)]
    
    if content_lines and len(year_lines) / len(content_lines) > 0.6:
        entries = [l for l in content_lines if len(l) > 20]
        if len(entries) >= 5:
            return entries
    
    # Strategy 3: Split on author-name patterns at start of line.
    # Typical patterns:
    #   "Author, A. B., ..." or "Author, A., and ..."
    #   Lines starting with a capital letter that look like a new entry.
    # We split on lines matching: start-of-line, optional whitespace,
    # then a capital letter followed by a lowercase letter (surname start).
    lines = bibliography_text.split('\n')
    entries = []
    current_entry = []
    
    # Pattern for start of a new bib entry:
    # - Starts with a capital letter (author surname)
    # - Is NOT a section heading or page marker
    entry_start = re.compile(r'^[A-Z][a-z]')
    skip_pattern = re.compile(r'^(---|References|Bibliography|REFERENCES|BIBLIOGRAPHY|Appendix|\d+\.)', re.IGNORECASE)
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        
        if entry_start.match(stripped) and not skip_pattern.match(stripped) and current_entry:
            entry_text = ' '.join(current_entry).strip()
            if len(entry_text) > 20:
                entries.append(entry_text)
            current_entry = [stripped]
        else:
            current_entry.append(stripped)
    
    # Don't forget the last entry
    if current_entry:
        entry_text = ' '.join(current_entry).strip()
        if len(entry_text) > 20:
            entries.append(entry_text)
    
    # If strategy 3 found more entries, use it; otherwise fall back to strategy 1
    if len(entries) >= 5:
        return entries
    
    # Fallback: return whichever gave more
    fallback = re.split(r'\n\s*\n', bibliography_text)
    fallback = [e.strip() for e in fallback if len(e.strip()) > 20]
    return entries if len(entries) >= len(fallback) else fallback


def match_author_date_citations(citation_keys: set, bibliography_text: str) -> dict:
    """Match author-date citations against bibliography entries."""
    all_entries = _split_bibliography_entries(bibliography_text)

    matched = {}
    for key in citation_keys:
        # Extract the primary author surname and year from the citation key
        # Handle various formats:
        #   "Smith et al., 2024" -> author="Smith", year="2024"
        #   "Tjong Kim Sang and De Meulder, 2003" -> author="Tjong", year="2003"
        #   "Brown et al., 2020" -> author="Brown", year="2020"
        m = re.match(r'([A-Z][\w\-]+)', key)
        year_m = re.search(r'(\d{4})', key)
        if not m or not year_m:
            continue
        author_name = m.group(1)
        year = year_m.group(1)

        for entry in all_entries:
            if author_name.lower() in entry.lower() and year in entry:
                if key not in matched:
                    matched[key] = entry
                break

    return matched


def match_citations_to_entries(citation_info: CitationInfo, bibliography_text: str) -> dict:
    """Route to the correct matching function based on citation style."""
    if citation_info.style == "numerical":
        return match_numerical_citations(citation_info.keys, bibliography_text)
    elif citation_info.style == "author-date":
        return match_author_date_citations(citation_info.keys, bibliography_text)
    return {}


# ── Stage 4C: LLM BibTeX generation ──

def generate_bibtex(raw_entries: list[str], related_work_text: str) -> str:
    """
    Use an LLM to convert raw bibliography entries into proper BibTeX format.
    """
    if not raw_entries:
        return ""

    prompt = build_bibtex_generation_prompt(raw_entries, related_work_text)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=BIB_MODEL,
                messages=[{"role": "user", "content": prompt}],
                timeout=90.0,
            )
            raw = response.choices[0].message.content
            if raw is None:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            # Strip thinking tags and markdown fences
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)
            raw = re.sub(r'```(?:bibtex|latex)?\s*\n?', '', raw)
            raw = raw.strip()

            # Validate: should contain at least one @ entry
            if '@' in raw:
                return raw
            else:
                print(f"   ⚠️ BibTeX generation returned no entries on attempt {attempt}")

        except Exception as e:
            print(f"   ⚠️ BibTeX generation failed on attempt {attempt}: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    print("   ⚠️ BibTeX generation failed after all retries")
    return ""


# ── Top-level Stage 4 entry point ──

def run_bib_extraction(
    full_text: str,
    related_work_text: str,
    outputs_dir: str | Path,
) -> BibExtractionResult:
    """
    Stage 4 entry point. Parse citations, match against bibliography, generate BibTeX.
    
    Args:
        full_text: Full paper text with PAGE markers.
        related_work_text: Extracted related work section text.
        outputs_dir: Directory to save references.bib and references.json.
    """
    outputs_dir = Path(outputs_dir)
    result = BibExtractionResult()

    print(f"\n📚 Stage 4: Bibliography Extraction")

    # 4A: Parse citations
    print(f"\n   📖 Stage 4A: Parsing citations from related work text...")
    citation_info = parse_citations(related_work_text)
    result.citation_style = citation_info.style
    result.citations_found = len(citation_info.keys)

    if not citation_info.keys:
        result.errors.append("No citations found in related work text")
        print(f"   ⚠️ No citations found — skipping bibliography extraction")
        return result

    # 4B: Extract bibliography section and match
    print(f"\n   📖 Stage 4B: Extracting bibliography section...")
    bib_section = extract_bibliography_section(full_text)

    if not bib_section:
        result.errors.append("Could not find References/Bibliography section in paper")
        print(f"   ⚠️ No bibliography section found")
        return result

    print(f"   📖 Bibliography section: {len(bib_section)} chars")

    matched_entries = match_citations_to_entries(citation_info, bib_section)
    result.entries_matched = len(matched_entries)
    matched_keys = sorted(list(matched_entries.keys()))
    unmatched_keys = sorted(list(set(citation_info.keys) - set(matched_keys)))
    print(f"   📖 Matched {len(matched_entries)}/{len(citation_info.keys)} citations to bibliography entries")

    if not matched_entries:
        result.errors.append("No bibliography entries matched the citations")
        print(f"   ⚠️ No entries matched — skipping BibTeX generation")
        return result

    # 4C: Generate BibTeX via LLM
    print(f"\n   📖 Stage 4C: Generating BibTeX via LLM...")
    bib_content = generate_bibtex(list(matched_entries.values()), related_work_text)

    if bib_content:
        result.bib_content = bib_content
        result.entries_resolved = bib_content.count('@')
        print(f"   ✅ Generated {result.entries_resolved} BibTeX entries")

        # Save outputs
        bib_path = outputs_dir / "references.bib"
        with open(bib_path, "w", encoding="utf-8") as f:
            f.write(bib_content)
        print(f"   ✅ Saved {bib_path}")

        # Save matched raw entries as JSON for reference
        json_path = outputs_dir / "references.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(matched_entries, f, indent=2, ensure_ascii=False)
        print(f"   ✅ Saved {json_path}")
    else:
        result.errors.append("BibTeX generation failed")

    return result
