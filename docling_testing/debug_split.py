"""Quick debug: trace what _split_bibliography_entries actually does with the
Docling bibliography text for paper19."""

import sys, os, re
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.bib_extractor import (
    extract_bibliography_section,
    _split_bibliography_entries,
    match_author_date_citations,
)

# Load the saved Docling raw text for paper19
with open("test_outputs/docling_raw_text.txt", "r") as f:
    full_text = f.read()

# Step 1: extract bibliography section
bib_section = extract_bibliography_section(full_text)
print(f"=== Bibliography section: {len(bib_section)} chars ===")
print(f"First 300 chars:\n{repr(bib_section[:300])}\n")

# Step 2: count lines
lines = bib_section.split('\n')
non_empty = [l.strip() for l in lines if l.strip()]
print(f"Total lines: {len(lines)}")
print(f"Non-empty lines: {len(non_empty)}")
print(f"First 5 non-empty lines:")
for i, l in enumerate(non_empty[:5]):
    print(f"  [{i}] ({len(l)} chars): {repr(l[:100])}")

# Step 3: Check Strategy 1 (double-newline)
strategy1 = re.split(r'\n\s*\n', bib_section)
strategy1 = [e.strip() for e in strategy1 if len(e.strip()) > 20]
print(f"\n=== Strategy 1 (double-newline split): {len(strategy1)} entries ===")
for i, e in enumerate(strategy1[:3]):
    print(f"  [{i}] ({len(e)} chars): {repr(e[:100])}...")

# Step 4: Check Strategy 2 (my new year-line heuristic)
heading_pattern = re.compile(
    r'^(---|##|#|References|Bibliography|REFERENCES|BIBLIOGRAPHY|Appendix|\d+\.)',
    re.IGNORECASE,
)
all_lines = [l.strip() for l in bib_section.split('\n') if l.strip()]
content_lines = [l for l in all_lines if not heading_pattern.match(l)]
year_lines = [l for l in content_lines if re.search(r'\b\d{4}\b', l)]

print(f"\n=== Strategy 2 (year-line heuristic) ===")
print(f"All non-empty lines: {len(all_lines)}")
print(f"Content lines (after heading filter): {len(content_lines)}")
print(f"Lines with a year: {len(year_lines)}")
if content_lines:
    ratio = len(year_lines) / len(content_lines)
    print(f"Year ratio: {ratio:.2%} (threshold: >60%)")
    print(f"Would trigger Strategy 2: {ratio > 0.6}")

# Show which lines got filtered as headings
filtered = [l for l in all_lines if heading_pattern.match(l)]
print(f"\nFiltered as headings ({len(filtered)}):")
for l in filtered:
    print(f"  {repr(l[:80])}")

# Show which content lines do NOT have a year
no_year = [l for l in content_lines if not re.search(r'\b\d{4}\b', l)]
print(f"\nContent lines WITHOUT a year ({len(no_year)}):")
for l in no_year[:10]:
    print(f"  {repr(l[:100])}")

# Step 5: Actually call the function and see the result
print(f"\n=== Actual _split_bibliography_entries result ===")
entries = _split_bibliography_entries(bib_section)
print(f"Returned {len(entries)} entries")
for i, e in enumerate(entries[:5]):
    print(f"  [{i}] ({len(e)} chars): {repr(e[:100])}...")
if len(entries) > 5:
    print(f"  ... ({len(entries) - 5} more)")
