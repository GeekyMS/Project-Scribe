"""
Project Scribe v4 — Iterative LLM Pipeline for Related Work Extraction.

Usage:
    python process_paper_v4.py <path_to_pdf>

This replaces the single-shot pipeline of v2 with an iterative
self-correcting loop:
    1. Extract boundaries (LLM) 
    2. Validate extraction (code)
    3a. Verify correctness (LLM) — or 3b. Diagnose errors (LLM)
    → Loop back to step 1 with corrections if needed (max 3 iterations)

Outputs (in ./outputs/):
    - related_work_only.pdf    — PDF with only the related work section
    - paper_without_related.pdf — PDF with the section removed
    - related_work.txt          — Extracted section text
    - related_work_raw.json     — LLM boundaries
    - references.bib            — BibTeX file of cited papers (new in v4.1)
    - references.json           — Raw matched bibliography entries (new in v4.1)
    - pipeline_log.json         — Full iteration history (new in v4)
"""

import sys
import argparse
from pathlib import Path

from pipeline import run_pipeline


def main():
    parser = argparse.ArgumentParser(
        description="Project Scribe v4 — Iterative LLM Pipeline for Related Work Extraction.",
    )
    parser.add_argument("pdf", help="Path to the input PDF file")
    parser.add_argument(
        "--extractor",
        choices=["docling", "pymupdf"],
        default="docling",
        help="Text extraction backend (default: docling)",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Directory for output files (default: outputs)",
    )
    args = parser.parse_args()

    if not Path(args.pdf).exists():
        print(f"❌ File not found: {args.pdf}")
        sys.exit(1)

    result = run_pipeline(args.pdf, outputs_dir=args.output_dir, extractor=args.extractor)

    # Exit code based on result
    if result.final_status == "failed":
        sys.exit(1)
    elif result.final_status == "accepted_with_flag":
        sys.exit(0)  # Success but flagged for review
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()

