"""
Project Scribe — Iterative LLM Pipeline for Related Work Extraction (v4)

Stages:
    1. Extraction LLM — Find Related Work boundaries (pages, snippets)
    2. Extraction Gate — Validate boundaries against actual PDF text
    3A. Verification LLM — Confirm extracted text is correct and complete
    3B. Error Analysis LLM — Diagnose extraction failures, suggest corrections
    4. Bibliography Extraction — Parse citations, match to references, generate .bib
"""

from .orchestrator import run_pipeline

__all__ = ["run_pipeline"]

