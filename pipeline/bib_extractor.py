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

BIB_MODEL = "anthropic/claude-sonnet-4.6"


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


def _parse_author_date(text: str) -> set:
    """Extract author-date citation keys from parenthetical or bracket citations."""
    keys = set()

    # Pattern 1: Multi-citations in parens — (Hou et al., 2025; Xu et al., 2025)
    multi_paren = re.finditer(r'\(([^)]+\d{4}[^)]*)\)', text)
    for m in multi_paren:
        inner = m.group(1)
        # Split by semicolons for multi-citations
        for part in inner.split(';'):
            part = part.strip()
            if re.search(r'\d{4}', part) and re.search(r'[A-Z]', part):
                keys.add(part)

    # Pattern 2: Author et al. (YYYY) — outside parentheses
    for m in re.finditer(r'([\w\-]+\s+et\s+al\.?)\s*\((\d{4}[a-z]?)\)', text):
        keys.add(f"{m.group(1)}, {m.group(2)}")

    # Pattern 3: Author & Author (YYYY)
    for m in re.finditer(r'([\w\-]+\s+&\s+[\w\-]+)\s*[\(,]\s*(\d{4}[a-z]?)\)?', text):
        keys.add(f"{m.group(1)}, {m.group(2)}")

    # Pattern 4: Bracket-style author-date [Author et al., YYYY]
    for m in re.finditer(r'\[([^\]]*[A-Z][\w\-]*[^\]]*\d{4}[a-z]?[^\]]*)\]', text):
        inner = m.group(1)
        for part in inner.split(';'):
            part = part.strip()
            if re.search(r'\d{4}', part) and re.search(r'[A-Z]', part):
                keys.add(part)

    return keys


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
    """
    # Search for the section heading
    pattern = re.compile(
        r'^[\s]*(?:\d+\.?\s*)?(?:References|Bibliography|REFERENCES|BIBLIOGRAPHY)\s*$',
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(full_text)
    if not match:
        # Try less strict: heading might be inline
        pattern2 = re.compile(
            r'(?:--- PAGE \d+ ---\n)?[\s]*(?:References|Bibliography)\s*\n',
            re.IGNORECASE,
        )
        match = pattern2.search(full_text)

    if not match:
        return None

    bib_text = full_text[match.start():]

    # Trim at Appendix if present
    appendix_match = re.search(
        r'^\s*(?:\d+\.?\s*)?(?:Appendix|APPENDIX|Supplementary)',
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
    parts = re.split(r'(?=\[\d+\])', bibliography_text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r'\[(\d+)\]', part)
        if m:
            num = int(m.group(1))
            entries[num] = part.strip()

    matched = []
    for key in sorted(citation_keys):
        if key in entries:
            matched.append(entries[key])

    return matched


def match_author_date_citations(citation_keys: set, bibliography_text: str) -> list[str]:
    """Match author-date citations against bibliography entries."""
    # Split bibliography into individual entries (by blank lines or [N] markers)
    all_entries = re.split(r'\n\s*\n', bibliography_text)
    # Filter out very short entries (likely noise)
    all_entries = [e.strip() for e in all_entries if len(e.strip()) > 20]

    matched = []
    for key in citation_keys:
        # Extract author name and year
        m = re.search(r'([\w\-]+).*?(\d{4})', key)
        if not m:
            continue
        author_name, year = m.group(1), m.group(2)

        for entry in all_entries:
            if author_name.lower() in entry.lower() and year in entry:
                if entry not in matched:
                    matched.append(entry)
                break

    return matched


def match_citations_to_entries(citation_info: CitationInfo, bibliography_text: str) -> list[str]:
    """Route to the correct matching function based on citation style."""
    if citation_info.style == "numerical":
        return match_numerical_citations(citation_info.keys, bibliography_text)
    elif citation_info.style == "author-date":
        return match_author_date_citations(citation_info.keys, bibliography_text)
    return []


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
    print(f"   📖 Matched {len(matched_entries)}/{len(citation_info.keys)} citations to bibliography entries")

    if not matched_entries:
        result.errors.append("No bibliography entries matched the citations")
        print(f"   ⚠️ No entries matched — skipping BibTeX generation")
        return result

    # 4C: Generate BibTeX via LLM
    print(f"\n   📖 Stage 4C: Generating BibTeX via LLM...")
    bib_content = generate_bibtex(matched_entries, related_work_text)

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
