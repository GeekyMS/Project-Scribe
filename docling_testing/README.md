# Docling Testing Setup

This directory contains code to test IBM's **Docling** as an alternative to PyMuPDF for the V4 extraction pipeline. 

The goal is to see if Docling's ML-based layout detection fixes the recurring issues with:
1. Two-column interleaving (e.g., paper1, paper8)
2. Figure and caption bleeding (e.g., paper19)
3. Bibliography parsing errors caused by garbled column text (e.g., paper2, paper4)

## Files

- `docling_extractor.py`: A drop-in replacement for the current `pipeline.extractor.extract_text_and_page_offsets()`. It uses Docling to convert the PDF and then groups the output by page, inserting `--- PAGE X ---` markers so the LLM prompts still work identically.
- `test_docling_pipeline.py`: A test script that takes a single paper, runs the Docling extractor, tests the Stage 1 Boundary Extraction (LLM), and tests the Stage 4 Bibliography Extraction to see if the improved text helps the matcher.

## How to Run

1. **Install Docling** (Requires Python 3.10+)
   ```bash
   # From your virtual environment
   pip install docling
   ```

2. **Run a Test**
   Test one of the problem papers (like paper4.pdf for bibliography issues, or paper1.pdf for two-column extraction):
   ```bash
   cd docling_testing
   python test_docling_pipeline.py --paper paper4.pdf
   ```

## Next Steps

If Docling produces better output on the trouble papers, we can integrate it properly into the orchestrator (replacing PyMuPDF for the `extract_text` step) while keeping PyMuPDF for the `extraction_gate` where we need pixel/coordinate-level validation.
