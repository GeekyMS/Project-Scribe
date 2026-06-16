"""
Project Scribe — Iterative LLM Pipeline for Related Work Extraction (v4)

Phase 1 — Extraction:
    1. Extraction LLM — Find Related Work boundaries (pages, snippets)
    2. Extraction Gate — Validate boundaries against actual PDF text
    3A. Verification LLM — Confirm extracted text is correct and complete
    3B. Error Analysis LLM — Diagnose extraction failures, suggest corrections
    4. Bibliography Extraction — Parse citations, match to references, generate .bib

Phase 2 — Generation & Evaluation:
    5. Abstract Enrichment — Fetch paper abstracts from Semantic Scholar
    6. Related Works Generation — LLM generates the section from paper + bib
    7. Evaluation — Citation metrics, LLM-as-judge, text similarity
    8. Human Evaluation — Blind A/B and Likert questionnaire generation

Extraction backends:
    - "docling" (default) — ML-based layout detection via IBM Docling
    - "pymupdf" — Fast coordinate-based extraction via PyMuPDF
"""

from .orchestrator import run_pipeline
from .docling_extractor import DOCLING_AVAILABLE

__all__ = ["run_pipeline", "DOCLING_AVAILABLE"]

