"""
Docling-based PDF text extraction.

Replaces PyMuPDF's extract_text_and_page_offsets() with IBM's Docling
DocumentConverter, which provides ML-based layout detection for better
handling of two-column academic papers, figures, and tables.

Produces output in the same (full_text, page_offsets) format as
pipeline.extractor.extract_text_and_page_offsets() so it can be
swapped in without changing downstream stages.

Install:  pip install docling
"""

import os
import time

# Force PyTorch to fallback to CPU for operations unsupported by Apple Silicon (MPS),
# which fixes the float64 crash during layout extraction.
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

try:
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    DOCLING_AVAILABLE = True
except ImportError:
    DOCLING_AVAILABLE = False


# ---------------------------------------------------------------------------
# Module-level converter (reuse across calls to avoid reinitializing models)
# ---------------------------------------------------------------------------
_converter = None


def _get_converter() -> DocumentConverter:
    """Lazy-init a shared DocumentConverter instance configured for CPU."""
    if not DOCLING_AVAILABLE:
        raise ImportError(
            "Docling is not installed.\n"
            "   Run:  pip install docling\n"
            "   Or for full features:  pip install 'docling[all]'"
        )
    global _converter
    if _converter is None:
        pipeline_options = PdfPipelineOptions()
        # Force CPU to avoid PyTorch Apple Silicon MPS float64 crash in layout models
        pipeline_options.accelerator_options.device = "cpu"

        _converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
    return _converter


# ---------------------------------------------------------------------------
# Primary extraction function
# ---------------------------------------------------------------------------
def extract_text_docling(pdf_path: str) -> tuple[str, list[int]]:
    """
    Extract text from a PDF using Docling with ``--- PAGE X ---`` markers.

    Uses Docling's layout-aware document converter to extract text,
    grouping content items by their source page and inserting PAGE
    markers compatible with the pipeline's LLM prompts.

    Returns
    -------
    full_text : str
        Concatenated text with ``--- PAGE N ---`` markers.
    page_offsets : list[int]
        Character offset where each page starts in *full_text*.
    """
    converter = _get_converter()

    print(f"   🔄 Converting with Docling...")
    t0 = time.time()
    result = converter.convert(pdf_path)
    conversion_time = time.time() - t0
    print(f"   ✅ Docling conversion completed in {conversion_time:.1f}s")

    doc = result.document

    # ------------------------------------------------------------------
    # Group content items by page number
    # ------------------------------------------------------------------
    pages_content: dict[int, list[str]] = {}

    for item, depth in doc.iterate_items():
        # --- Resolve page number from provenance ---
        page_no = 1  # fallback if no provenance
        if hasattr(item, "prov") and item.prov:
            page_no = item.prov[0].page_no

        if page_no not in pages_content:
            pages_content[page_no] = []

        # --- Extract text based on item type ---
        text = ""

        # Section headers — preserve as markdown headings so the LLM
        # can identify section boundaries clearly
        if hasattr(item, "label") and item.label == "section_header":
            raw = getattr(item, "text", "")
            level = getattr(item, "level", 0)
            if raw:
                prefix = "#" * min(level + 1, 6)
                text = f"{prefix} {raw}"

        # Regular text, list items, captions, etc.
        elif hasattr(item, "text") and item.text:
            text = item.text

            # Reconstruct list markers for bibliographies/lists (e.g. "[1]")
            if hasattr(item, "label") and item.label == "list_item":
                marker = getattr(item, "marker", "") or getattr(item, "enumerated_marker", "")
                if marker:
                    text = f"{marker} {text}"

        # Tables — convert to markdown table representation
        elif hasattr(item, "export_to_markdown"):
            try:
                text = item.export_to_markdown()
            except Exception:
                text = ""

        if text and text.strip():
            pages_content[page_no].append(text.strip())

    # ------------------------------------------------------------------
    # Build full_text with PAGE markers (same format as the pipeline)
    # ------------------------------------------------------------------
    full_text = ""
    page_offsets = []

    if pages_content:
        for page_no in sorted(pages_content.keys()):
            page_offsets.append(len(full_text))
            full_text += f"\n--- PAGE {page_no} ---\n"
            full_text += "\n".join(pages_content[page_no]) + "\n"
    else:
        # Fallback: use Docling's markdown export as a single block
        print("   ⚠️ No structured items found, falling back to full markdown export")
        markdown = doc.export_to_markdown()
        page_offsets.append(0)
        full_text = f"\n--- PAGE 1 ---\n{markdown}\n"

    num_pages = len(pages_content)
    print(f"   📊 Extracted {len(full_text)} chars across {num_pages} page(s)")

    return full_text, page_offsets


# ---------------------------------------------------------------------------
# Convenience: raw markdown export (for manual inspection / debugging)
# ---------------------------------------------------------------------------
def extract_markdown_docling(pdf_path: str) -> str:
    """
    Return Docling's raw Markdown export (no PAGE markers).

    Useful for side-by-side comparison with PyMuPDF output.
    """
    converter = _get_converter()
    result = converter.convert(pdf_path)
    return result.document.export_to_markdown()
