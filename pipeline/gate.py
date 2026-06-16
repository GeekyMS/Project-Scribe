"""
Stage 2 — Extraction Gate.

Validates that the LLM-provided boundaries (start/end snippets and pages) 
actually correspond to real text in the PDF. This is the deterministic 
"some code" step from the DataFlow diagram.

If snippets are found, the actual related work text is extracted.
If they're not found, the gate reports an error with details for Stage 3B.
"""

import fitz
from .schemas import Boundaries, GateResult


def _search_snippet_on_page(page, snippet: str, mode: str = "first") -> fitz.Rect | None:
    """
    Search for a snippet on a page with fallback to shorter anchors.
    
    Args:
        page: PyMuPDF page object.
        snippet: Text to search for.
        mode: "first" to return first match, "last" to return last match.
    
    Returns:
        fitz.Rect or None.
    """
    if not snippet:
        return None
    snippet = snippet.strip()

    # Attempt 1: Full snippet
    rects = page.search_for(snippet, quads=False)
    if rects:
        return rects[0] if mode == "first" else rects[-1]

    # Attempt 2: First/last 50 characters (more stable anchor)
    anchor = snippet[:50] if mode == "first" else snippet[-50:]
    if len(anchor) > 10:
        rects = page.search_for(anchor, quads=False)
        if rects:
            return rects[0] if mode == "first" else rects[-1]

    # Attempt 3: Even shorter — first/last 35 characters
    anchor = snippet[:35] if mode == "first" else snippet[-35:]
    if len(anchor) > 10:
        rects = page.search_for(anchor, quads=False)
        if rects:
            return rects[0] if mode == "first" else rects[-1]

    return None


def _search_nearby_pages(doc, target_page: int, snippet: str, mode: str, radius: int = 1) -> tuple[fitz.Rect | None, int]:
    """
    Search for a snippet on the target page and nearby pages (±radius).
    
    Returns:
        (rect, actual_page_number) or (None, -1).
    """
    n_pages = len(doc)
    # Try the target page first
    pages_to_try = [target_page]
    for offset in range(1, radius + 1):
        if target_page + offset < n_pages:
            pages_to_try.append(target_page + offset)
        if target_page - offset >= 0:
            pages_to_try.append(target_page - offset)

    for p in pages_to_try:
        rect = _search_snippet_on_page(doc[p], snippet, mode)
        if rect is not None:
            return rect, p

    return None, -1


def _normalize_for_search(text: str) -> str:
    """Collapse whitespace for fuzzy substring matching."""
    import re
    return re.sub(r'\s+', ' ', text).strip().lower()


def _trim_to_snippets(full_text: str, start_snippet: str, end_snippet: str) -> str | None:
    """
    Trim extracted text to only the content between start_snippet and end_snippet.
    
    Uses normalized matching to handle whitespace/newline differences between
    the PDF text extraction and the LLM-provided snippets.
    
    Returns the trimmed text, or None if neither snippet was found.
    """
    if not full_text:
        return None

    norm_text = _normalize_for_search(full_text)
    
    start_idx = 0
    end_idx = len(full_text)
    found_any = False

    # Find start position
    if start_snippet and start_snippet.strip():
        # Try progressively shorter anchors
        for anchor_len in [len(start_snippet.strip()), 80, 50, 35]:
            anchor = start_snippet.strip()[:anchor_len]
            if len(anchor) < 10:
                break
            norm_anchor = _normalize_for_search(anchor)
            pos = norm_text.find(norm_anchor)
            if pos >= 0:
                # Map back to original text position (approximate)
                # Find the corresponding position in the original text
                start_idx = _map_norm_pos_to_original(full_text, norm_text, pos)
                found_any = True
                break

    # Find end position
    if end_snippet and end_snippet.strip():
        for anchor_len in [len(end_snippet.strip()), 80, 50, 35]:
            anchor = end_snippet.strip()[-anchor_len:]
            if len(anchor) < 10:
                break
            norm_anchor = _normalize_for_search(anchor)
            pos = norm_text.rfind(norm_anchor)
            if pos >= 0:
                end_of_match = pos + len(norm_anchor)
                end_idx = _map_norm_pos_to_original(full_text, norm_text, end_of_match)
                found_any = True
                break

    if not found_any:
        return None

    return full_text[start_idx:end_idx].strip()


def _map_norm_pos_to_original(original: str, normalized: str, norm_pos: int) -> int:
    """
    Map a position in normalized text back to the approximate position 
    in the original text.
    """
    # Walk through original text, counting normalized characters
    norm_count = 0
    import re
    in_whitespace = False
    
    for i, ch in enumerate(original):
        if norm_count >= norm_pos:
            return i
        
        if ch in ' \t\n\r':
            if not in_whitespace:
                norm_count += 1  # collapsed whitespace = 1 space
                in_whitespace = True
        else:
            norm_count += 1
            in_whitespace = False
    
    return len(original)


def run_extraction_gate(
    pdf_path: str,
    boundaries: Boundaries,
    full_text: str | None = None,
) -> GateResult:
    """
    Stage 2 entry point.
    
    Validates that the LLM boundaries correspond to real content in the PDF
    and extracts the actual related work text.
    
    Uses an "or" strategy: first tries PyMuPDF coordinate-level snippet search,
    then falls back to searching within the Docling/PyMuPDF ``full_text`` string
    (which may have different whitespace but preserves content). This makes the
    gate robust to extraction-backend differences.
    
    Args:
        pdf_path: Path to the PDF file.
        boundaries: LLM-provided boundaries (pages + snippets).
        full_text: Optional pre-extracted full paper text (with PAGE markers).
            When provided, used as a fallback source for snippet search and
            text extraction if PyMuPDF coordinate search fails.
    
    Returns:
        GateResult with status, extracted text, and error details.
    """
    doc = fitz.open(pdf_path)
    n_pages = len(doc)

    # Clamp pages to valid range
    start_page_idx = max(0, min(boundaries.start_page - 1, n_pages - 1))
    end_page_idx = max(0, min(boundaries.end_page - 1, n_pages - 1))

    errors = []

    # ------------------------------------------------------------------
    # Primary search: PyMuPDF coordinate-level snippet search
    # ------------------------------------------------------------------
    start_rect, actual_start_page = _search_nearby_pages(
        doc, start_page_idx, boundaries.start_snippet, mode="first", radius=1
    )
    start_found = start_rect is not None

    end_rect, actual_end_page = _search_nearby_pages(
        doc, end_page_idx, boundaries.end_snippet, mode="last", radius=1
    )
    end_found = end_rect is not None

    # ------------------------------------------------------------------
    # Fallback search: try full_text string matching (Docling or PyMuPDF)
    # ------------------------------------------------------------------
    start_found_in_text = False
    end_found_in_text = False

    if full_text and (not start_found or not end_found):
        norm_full = _normalize_for_search(full_text)

        if not start_found and boundaries.start_snippet:
            for anchor_len in [len(boundaries.start_snippet.strip()), 80, 50, 35]:
                anchor = boundaries.start_snippet.strip()[:anchor_len]
                if len(anchor) < 10:
                    break
                if _normalize_for_search(anchor) in norm_full:
                    start_found_in_text = True
                    print(f"   📎 start_snippet found in full_text (fallback)")
                    break

        if not end_found and boundaries.end_snippet:
            for anchor_len in [len(boundaries.end_snippet.strip()), 80, 50, 35]:
                anchor = boundaries.end_snippet.strip()[-anchor_len:]
                if len(anchor) < 10:
                    break
                if _normalize_for_search(anchor) in norm_full:
                    end_found_in_text = True
                    print(f"   📎 end_snippet found in full_text (fallback)")
                    break

    # Combine both search results
    effective_start_found = start_found or start_found_in_text
    effective_end_found = end_found or end_found_in_text

    if not effective_start_found:
        errors.append(
            f"start_snippet not found on page {boundaries.start_page} (±1). "
            f"Searched for: {repr(boundaries.start_snippet[:80])}"
        )
    if not effective_end_found:
        errors.append(
            f"end_snippet not found on page {boundaries.end_page} (±1). "
            f"Searched for: {repr(boundaries.end_snippet[:80])}"
        )

    # Determine status
    if effective_start_found and effective_end_found:
        status = "success"
    elif effective_start_found or effective_end_found:
        status = "partial"
    else:
        status = "error"

    # ------------------------------------------------------------------
    # Extract text — prefer PyMuPDF coordinate extraction, fall back to
    # full_text string slicing
    # ------------------------------------------------------------------
    extracted_text = None
    if status in ("success", "partial"):
        # Try PyMuPDF coordinate-based extraction first
        if start_found or end_found:
            extract_start = actual_start_page if start_found else start_page_idx
            extract_end = actual_end_page if end_found else end_page_idx

            if extract_start > extract_end:
                extract_start, extract_end = extract_end, extract_start

            text_parts = []
            for p in range(extract_start, extract_end + 1):
                text_parts.append(doc[p].get_text("text"))
            full_page_text = "\n".join(text_parts).strip()

            extracted_text = _trim_to_snippets(
                full_page_text, boundaries.start_snippet, boundaries.end_snippet
            )

            if not extracted_text:
                extracted_text = full_page_text

        # Fall back to full_text string extraction if PyMuPDF didn't work
        if not extracted_text and full_text:
            extracted_text = _trim_to_snippets(
                full_text, boundaries.start_snippet, boundaries.end_snippet
            )

        if not extracted_text:
            status = "error"
            errors.append("Extracted text is empty despite snippet(s) being found.")

    doc.close()

    return GateResult(
        status=status,
        extracted_text=extracted_text,
        start_found=effective_start_found,
        end_found=effective_end_found,
        error_details="; ".join(errors) if errors else None,
    )
