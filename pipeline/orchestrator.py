"""
Pipeline Orchestrator — ties all stages together with iteration logic.

This implements the full DataFlow loop:
    1. Extract boundaries (LLM #1)
    2. Validate via extraction gate (deterministic code)
    3a. If gate passes → verify with LLM #2
    3b. If gate fails → diagnose with LLM #3
    Both 3a/3b can feed corrections back into step 1 for re-extraction.
"""

import json
import re
from pathlib import Path

import fitz

from .schemas import (
    Boundaries,
    GateResult,
    IterationRecord,
    PipelineResult,
)
from .extractor import extract_text_and_page_offsets, extract_boundaries, _call_llm_single
from .docling_extractor import DOCLING_AVAILABLE
from .gate import run_extraction_gate
from .verifier import verify_extraction
from .error_analyzer import analyze_error, build_correction_from_error
from .bib_extractor import run_bib_extraction

# Maximum number of extraction attempts (iterations through the loop)
MAX_ITERATIONS = 3

# If all windows return null with average confidence below this threshold,
# trigger the null challenger (a more permissive second-opinion check)
NULL_CONFIDENCE_THRESHOLD = 85


def _run_null_challenger(full_text: str) -> dict | None:
    """
    Stage 3C — Null Challenger.
    
    Makes one extra LLM call with a permissive prompt focused on the 
    introduction. Returns boundaries dict if prior work is found, 
    or None if it confirms no related work.
    """
    from .prompts import build_null_challenger_prompt
    
    print(f"\n🔄 Stage 3C: Null Challenger (more permissive check)")
    
    prompt = build_null_challenger_prompt(full_text)
    
    # Reuse the same LLM call infrastructure
    import re, json, time
    from .extractor import client, MAX_RETRIES, RETRY_DELAY
    
    CHALLENGER_MODEL = "anthropic/claude-sonnet-4.6"
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=CHALLENGER_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                timeout=90.0,
            )
            raw = response.choices[0].message.content
            if raw is None:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue
            
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                # Normalize key
                if "related_works" in data and "related_work" not in data:
                    data["related_work"] = data.pop("related_works")
                
                rw = data.get("related_work")
                if rw is not None and isinstance(rw, dict):
                    print(f"   ✅ Challenger found related work! "
                          f"Pages {rw.get('start_page')}-{rw.get('end_page')}")
                    return data
                else:
                    print(f"   ℹ️ Challenger confirms: no related work in introduction")
                    return None
                    
        except Exception as e:
            print(f"   ⚠️ Challenger call failed on attempt {attempt}: {e}")
        
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    
    print(f"   ⚠️ Challenger failed after all retries")
    return None


def run_pipeline(pdf_path: str, outputs_dir: str = "outputs", extractor: str = "docling") -> PipelineResult:
    """
    Run the full iterative pipeline on a single PDF.
    
    Args:
        pdf_path: Path to the input PDF.
        outputs_dir: Directory for output files.
        extractor: Extraction backend — "docling" (default, ML-based layout
            detection) or "pymupdf" (fast, coordinate-based).
    
    Returns:
        PipelineResult with final status, boundaries, extracted text,
        and full iteration history.
    """
    paper_name = Path(pdf_path).name
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"📄 Processing: {paper_name}")
    print(f"{'='*60}")

    # ── Extract text ──
    use_docling = (extractor == "docling")
    if use_docling and not DOCLING_AVAILABLE:
        print("   ⚠️ Docling not installed, falling back to PyMuPDF")
        use_docling = False

    if use_docling:
        from .docling_extractor import extract_text_docling
        print("\n📝 Stage 0: Extracting text from PDF (Docling)...")
        full_text, page_offsets = extract_text_docling(pdf_path)
    else:
        print("\n📝 Stage 0: Extracting text from PDF (PyMuPDF)...")
        full_text, page_offsets = extract_text_and_page_offsets(pdf_path)
    print(f"   Extracted {len(full_text)} chars from {len(page_offsets)} pages")

    result = PipelineResult(paper=paper_name, final_status="failed")
    result.full_text = full_text  # kept for Stage 4 bibliography extraction
    correction_context = None
    best_result = None  # Track the best partial result across iterations

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n{'─'*50}")
        print(f"🔄 Iteration {iteration}/{MAX_ITERATIONS}")
        print(f"{'─'*50}")

        record = IterationRecord(attempt=iteration)

        # ── Stage 1: Extract boundaries ──
        print(f"\n🤖 Stage 1: Extraction LLM" + 
              (" (with corrections)" if correction_context else ""))
        
        raw_result = extract_boundaries(
            full_text,
            correction_context=correction_context,
        )

        boundaries = Boundaries.from_dict(raw_result)
        record.boundaries = raw_result.get("related_work")

        # If LLM says no related work section
        if boundaries is None:
            avg_confidence = raw_result.get("min_null_confidence", 90)
            print(f"   ℹ️ LLM returned null — no Related Work section "
                  f"(min confidence: {avg_confidence:.0f}%)")
            
            # ── Null Challenger (Stage 3C) ──
            # If confidence is low, run a more permissive second-opinion check
            # on the introduction. Useful for older/non-CS papers (e.g. Shannon)
            # where the intro serves as related work but doesn't match standard patterns.
            if avg_confidence < NULL_CONFIDENCE_THRESHOLD and iteration == 1:
                print(f"   ⚠️ Confidence {avg_confidence:.0f}% < {NULL_CONFIDENCE_THRESHOLD}% threshold")
                challenger_result = _run_null_challenger(full_text)
                if challenger_result is not None:
                    boundaries = Boundaries.from_dict(challenger_result)
                    record.boundaries = challenger_result.get("related_work")
                    if boundaries is not None:
                        print(f"   🔄 Challenger overrode null — proceeding with extraction")
                    else:
                        record.gate_status = "null_confirmed"
                        result.iterations.append(record)
                        result.final_status = "no_related_work"
                        result.total_iterations = iteration
                        break
                else:
                    record.gate_status = "null_confirmed"
                    result.iterations.append(record)
                    result.final_status = "no_related_work"
                    result.total_iterations = iteration
                    break
            else:
                # Accept null directly (high confidence or not first iteration)
                record.gate_status = "null"
                result.iterations.append(record)
                result.final_status = "no_related_work"
                result.total_iterations = iteration
                break

        print(f"   📍 Boundaries: pages {boundaries.start_page}-{boundaries.end_page}, "
              f"source={boundaries.source}")

        # ── Stage 2: Extraction gate ──
        print(f"\n⚙️ Stage 2: Extraction Gate")
        gate_result = run_extraction_gate(pdf_path, boundaries, full_text=full_text)
        record.gate_status = gate_result.status
        print(f"   Gate result: {gate_result.status} "
              f"(start_found={gate_result.start_found}, end_found={gate_result.end_found})")

        if gate_result.passed:
            # ── Stage 3A: Verification ──
            print(f"\n🔎 Stage 3A: Verification LLM")
            verification = verify_extraction(
                gate_result.extracted_text,
                boundaries.source,
            )
            record.verification = {
                "is_correct": verification.is_correct,
                "is_complete": verification.is_complete,
                "issues": verification.issues,
                "suggested_correction": verification.suggested_correction,
            }

            # Track as best result so far (even if not perfect)
            best_result = (boundaries, gate_result.extracted_text)

            if verification.passed:
                # ✅ Success!
                print(f"\n   ✅ Verification PASSED — accepting result")
                result.final_status = "accepted"
                result.boundaries = boundaries
                result.extracted_text = gate_result.extracted_text
                result.total_iterations = iteration
                result.iterations.append(record)
                break
            else:
                # Verification found issues — build correction context
                issues_str = "; ".join(verification.issues) if verification.issues else "Unknown issues"
                correction_parts = [
                    f"Previous extraction (pages {boundaries.start_page}-{boundaries.end_page}) "
                    f"was verified but found ISSUES: {issues_str}"
                ]
                if verification.suggested_correction:
                    correction_parts.append(f"Suggested fix: {verification.suggested_correction}")
                
                correction_context = "\n".join(correction_parts)
                record.correction_context = correction_context
                print(f"   ⚠️ Verification found issues: {issues_str}")

                # If this is the last iteration, accept with flag
                if iteration >= MAX_ITERATIONS:
                    print(f"   ⚠️ Max iterations reached — accepting best result with flag")
                    result.final_status = "accepted_with_flag"
                    result.boundaries = boundaries
                    result.extracted_text = gate_result.extracted_text
                    result.total_iterations = iteration
                    result.iterations.append(record)
                    break

        else:
            # ── Stage 3B: Error analysis ──
            print(f"\n🔬 Stage 3B: Error Analysis LLM")
            print(f"   Error: {gate_result.error_details}")

            analysis = analyze_error(
                full_text,
                boundaries.to_dict(),
                gate_result.error_details or "Unknown error",
            )
            record.error_analysis = {
                "diagnosis": analysis.diagnosis,
                "correct_section_exists": analysis.correct_section_exists,
                "suggested_start_page": analysis.suggested_start_page,
                "suggested_end_page": analysis.suggested_end_page,
            }

            correction_context = build_correction_from_error(analysis)
            record.correction_context = correction_context

            if correction_context is None:
                # Error analysis says no section exists
                print(f"   ℹ️ Error analysis confirms: no Related Work section exists")
                result.final_status = "no_related_work"
                result.total_iterations = iteration
                result.iterations.append(record)
                break

            if iteration >= MAX_ITERATIONS:
                print(f"   ❌ Max iterations reached — marking as failed")
                # If we have a best partial result, use it
                if best_result:
                    result.final_status = "accepted_with_flag"
                    result.boundaries = best_result[0]
                    result.extracted_text = best_result[1]
                else:
                    result.final_status = "failed"
                result.total_iterations = iteration
                result.iterations.append(record)
                break

        result.iterations.append(record)


    # ── Generate output files if we have a result ──
    if result.boundaries and result.final_status in ("accepted", "accepted_with_flag"):
        _generate_outputs(pdf_path, result, outputs_dir)

    # ── Save pipeline log (after Stage 4 so bib results are included) ──
    log_path2 = outputs_dir / "pipeline_log.json"
    with open(log_path2, "w", encoding="utf-8") as f:
        json.dump(result.to_log_dict(), f, indent=2, ensure_ascii=False)
    print(f"\n📋 Pipeline log saved → {log_path2}")

    print(f"\n{'='*60}")
    print(f"🏁 Final status: {result.final_status} "
          f"(after {result.total_iterations} iteration(s))")
    print(f"{'='*60}\n")

    return result


def _generate_outputs(pdf_path: str, result: PipelineResult, outputs_dir: Path):
    """Generate the PDF and text output files from a successful pipeline run."""
    boundaries = result.boundaries
    
    # Save raw JSON
    raw_json = {"related_work": boundaries.to_dict()}
    with open(outputs_dir / "related_work_raw.json", "w", encoding="utf-8") as f:
        json.dump(raw_json, f, indent=2, ensure_ascii=False)

    # Save related work text
    if result.extracted_text:
        with open(outputs_dir / "related_work.txt", "w", encoding="utf-8") as f:
            f.write(result.extracted_text)
        print(f"   ✅ Saved related_work.txt")

    # Save paper text without related work (markdown) for Phase 2 generation input.
    # This avoids the lossy PDF → re-extract round-trip.
    if result.full_text and result.extracted_text:
        paper_without = result.full_text.replace(result.extracted_text, "")
        # Clean up any resulting double-blank-lines from the removal
        import re as _re
        paper_without = _re.sub(r'\n{3,}', '\n\n', paper_without)
        with open(outputs_dir / "paper_without_related.md", "w", encoding="utf-8") as f:
            f.write(paper_without)
        print(f"   ✅ Saved paper_without_related.md")

    # Generate PDFs using the existing hybrid removal logic
    try:
        _pdf_hybrid_removal(pdf_path, boundaries, outputs_dir)
    except Exception as e:
        print(f"   ⚠️ PDF generation error: {e}")

    # ── Stage 4: Bibliography extraction ──
    if result.extracted_text and result.full_text:
        try:
            bib_result = run_bib_extraction(
                result.full_text, result.extracted_text, outputs_dir
            )
            result.bib_result = bib_result
        except Exception as e:
            print(f"   ⚠️ Bibliography extraction error: {e}")


def _find_first_rect(page, snippet: str):
    """Search for snippet on page with fallback to short anchors."""
    if not snippet:
        return None
    snippet = snippet.strip()
    rects = page.search_for(snippet, quads=False)
    if rects:
        return rects[0]
    anchor = snippet[:35]
    if len(anchor) > 10:
        rects = page.search_for(anchor, quads=False)
        if rects:
            return rects[0]
    return None


def _find_last_rect(page, snippet: str):
    """Search for snippet on page with fallback to short anchors."""
    if not snippet:
        return None
    snippet = snippet.strip()
    rects = page.search_for(snippet, quads=False)
    if rects:
        return rects[-1]
    anchor = snippet[-35:]
    if len(anchor) > 10:
        rects = page.search_for(anchor, quads=False)
        if rects:
            return rects[-1]
    return None


def _pdf_hybrid_removal(pdf_path: str, boundaries: Boundaries, outputs_dir: Path):
    """
    Create related_work_only.pdf and paper_without_related.pdf.
    
    Ported from process_paper_v2.process_pdf_hybrid_removal.
    """
    doc = fitz.open(pdf_path)
    n_pages = len(doc)
    start_page = max(1, min(boundaries.start_page, n_pages))
    end_page = max(1, min(boundaries.end_page, n_pages))

    print(f"   📄 Creating PDFs: pages {start_page}–{end_page}")

    # ── related_work_only.pdf ──
    related_doc = fitz.open()
    for p in range(start_page - 1, end_page):
        related_doc.insert_pdf(doc, from_page=p, to_page=p)

    try:
        if related_doc.page_count > 0:
            sp_idx = 0
            ep_idx = related_doc.page_count - 1

            start_rect = _find_first_rect(doc[start_page - 1], boundaries.start_snippet)
            end_rect = _find_last_rect(doc[end_page - 1], boundaries.end_snippet)

            if start_rect is None:
                start_rect = _find_first_rect(related_doc[sp_idx], boundaries.start_snippet)
            if end_rect is None:
                end_rect = _find_last_rect(related_doc[ep_idx], boundaries.end_snippet)

            if start_rect:
                redact_rect = fitz.Rect(
                    related_doc[sp_idx].rect.x0, related_doc[sp_idx].rect.y0,
                    related_doc[sp_idx].rect.x1, start_rect.y0,
                )
                related_doc[sp_idx].add_redact_annot(redact_rect, fill=(1, 1, 1))

            if end_rect:
                redact_rect = fitz.Rect(
                    related_doc[ep_idx].rect.x0, end_rect.y1,
                    related_doc[ep_idx].rect.x1, related_doc[ep_idx].rect.y1,
                )
                related_doc[ep_idx].add_redact_annot(redact_rect, fill=(1, 1, 1))

            for p in range(related_doc.page_count):
                related_doc[p].apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
    except Exception as e:
        print(f"   ⚠️ Trimming related_doc: {e}")

    related_doc.save(str(outputs_dir / "related_work_only.pdf"))
    related_doc.close()
    print(f"   ✅ Saved related_work_only.pdf")

    # ── paper_without_related.pdf ──
    doc2 = fitz.open(pdf_path)
    try:
        if start_page == end_page:
            page = doc2[start_page - 1]
            s_rect = _find_first_rect(page, boundaries.start_snippet)
            e_rect = _find_last_rect(page, boundaries.end_snippet)
            if s_rect and e_rect:
                redact_rect = fitz.Rect(page.rect.x0, s_rect.y0, page.rect.x1, e_rect.y1)
                page.add_redact_annot(redact_rect, fill=(1, 1, 1))
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            else:
                doc2.delete_page(start_page - 1)
        else:
            s_page = doc2[start_page - 1]
            s_rect = _find_first_rect(s_page, boundaries.start_snippet)
            if s_rect:
                r = fitz.Rect(s_page.rect.x0, s_rect.y0, s_page.rect.x1, s_page.rect.y1)
                s_page.add_redact_annot(r, fill=(1, 1, 1))
                s_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

            e_page = doc2[end_page - 1]
            e_rect = _find_last_rect(e_page, boundaries.end_snippet)
            if e_rect:
                r = fitz.Rect(e_page.rect.x0, e_page.rect.y0, e_page.rect.x1, e_rect.y1)
                e_page.add_redact_annot(r, fill=(1, 1, 1))
                e_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

            if end_page - start_page > 1:
                for p in range(end_page - 2, start_page, -1):
                    try:
                        doc2.delete_page(p)
                    except Exception as e:
                        print(f"   ⚠️ Failed to delete page {p+1}: {e}")
    except Exception as e:
        print(f"   ⚠️ Redaction/deletion error: {e}")

    doc2.save(str(outputs_dir / "paper_without_related.pdf"))
    doc2.close()
    doc.close()
    print(f"   ✅ Saved paper_without_related.pdf")
