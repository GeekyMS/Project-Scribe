"""
Generate a FULL bibliography .bib file from ALL entries in a paper's
References section — not just those cited in the Related Works.

Usage:
    python generate_full_bib.py <output_dir>

Reads the Docling-extracted full text from <output_dir>/, extracts the
bibliography section, splits into individual entries, and uses the LLM
to convert ALL of them into BibTeX format.

Outputs:
    <output_dir>/references_full.bib  — BibTeX with ALL references
"""

import sys
from pathlib import Path

from pipeline.bib_extractor import (
    extract_bibliography_section,
    _split_bibliography_entries,
    generate_bibtex,
)


def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_full_bib.py <output_dir>")
        print("  e.g. python generate_full_bib.py outputs/acl_2026_full_bib")
        sys.exit(1)

    output_dir = Path(sys.argv[1])

    # ── Load the full paper text ──
    # The orchestrator saves paper_without_related.md, but we need the FULL
    # text (with related work included) to find the References section.
    # We'll re-extract from the Docling cache or fall back to re-running Docling.
    # Easiest: read related_work.txt + paper_without_related.md to reconstruct,
    # OR just re-extract.  But the full text is also available from the pipeline
    # run — let's just re-extract the bibliography from the PDF directly.

    # Strategy: find the PDF that was processed (check pipeline_log.json)
    import json
    log_path = output_dir / "pipeline_log.json"
    if log_path.exists():
        with open(log_path) as f:
            log = json.load(f)
        paper_name = log.get("paper", "")
    else:
        print("❌ No pipeline_log.json found. Run Phase 1 first.")
        sys.exit(1)

    # Try to reconstruct full text from the outputs
    rw_path = output_dir / "related_work.txt"
    paper_md_path = output_dir / "paper_without_related.md"

    if rw_path.exists() and paper_md_path.exists():
        # We have both pieces — but for bibliography extraction we just need
        # the paper_without_related.md since the References section is NOT
        # part of Related Work.
        full_text = paper_md_path.read_text(encoding="utf-8")
    else:
        # Fall back to re-extracting from PDF
        print("⚠️  Missing output files. Re-extracting from PDF...")
        from pipeline.docling_extractor import extract_text_docling
        pdf_path = str(Path(paper_name))
        if not Path(pdf_path).exists():
            # Try in the project root
            for candidate in [paper_name, f"./{paper_name}"]:
                if Path(candidate).exists():
                    pdf_path = candidate
                    break
        full_text, _ = extract_text_docling(pdf_path)

    # ── Extract bibliography section ──
    print(f"\n📚 Extracting full bibliography from paper text ({len(full_text)} chars)...")
    bib_section = extract_bibliography_section(full_text)

    if not bib_section:
        print("❌ Could not find References/Bibliography section in paper")
        sys.exit(1)

    print(f"   📖 Bibliography section: {len(bib_section)} chars")

    # ── Split into individual entries ──
    all_entries = _split_bibliography_entries(bib_section)
    print(f"   📖 Found {len(all_entries)} bibliography entries (ALL refs, not just Related Work)")

    if not all_entries:
        print("❌ No entries found in bibliography section")
        sys.exit(1)

    # ── Generate BibTeX for ALL entries via LLM ──
    print(f"\n   📖 Generating BibTeX for ALL {len(all_entries)} entries via LLM...")
    # The LLM can handle batches, but may need chunking for large bibliographies
    CHUNK_SIZE = 30  # entries per LLM call
    all_bibtex_parts = []

    for i in range(0, len(all_entries), CHUNK_SIZE):
        chunk = all_entries[i:i + CHUNK_SIZE]
        chunk_num = i // CHUNK_SIZE + 1
        total_chunks = (len(all_entries) + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f"   📖 Chunk {chunk_num}/{total_chunks}: {len(chunk)} entries...")

        bib_content = generate_bibtex(chunk, "")  # empty related_work_text since we want all
        if bib_content:
            all_bibtex_parts.append(bib_content)
        else:
            print(f"   ⚠️ Chunk {chunk_num} failed — some entries may be missing")

    if not all_bibtex_parts:
        print("❌ BibTeX generation failed for all chunks")
        sys.exit(1)

    full_bib = "\n\n".join(all_bibtex_parts)
    entry_count = full_bib.count("@")

    # ── Save ──
    out_path = output_dir / "references_full.bib"
    out_path.write_text(full_bib, encoding="utf-8")
    print(f"\n   ✅ Saved {entry_count} BibTeX entries → {out_path}")
    print(f"   (compare: references.bib has only Related Work citations)")


if __name__ == "__main__":
    main()
