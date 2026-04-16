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
    - pipeline_log.json         — Full iteration history (new in v4)
"""

import sys
from pathlib import Path

from pipeline import run_pipeline


def main(argv):
    if len(argv) != 2:
        print("Usage: python process_paper_v4.py <path_to_pdf>")
        sys.exit(1)

    pdf_path = argv[1]
    if not Path(pdf_path).exists():
        print(f"❌ File not found: {pdf_path}")
        sys.exit(1)

    result = run_pipeline(pdf_path, outputs_dir="outputs")

    # Exit code based on result
    if result.final_status == "failed":
        sys.exit(1)
    elif result.final_status == "accepted_with_flag":
        sys.exit(0)  # Success but flagged for review
    else:
        sys.exit(0)


if __name__ == "__main__":
    main(sys.argv)
