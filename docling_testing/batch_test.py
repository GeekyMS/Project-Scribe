"""
Batch test: Run the Docling pipeline on all 25 papers and produce a summary.

Outputs a CSV + console table with per-paper metrics:
- Docling extraction time
- Related Work found (pages)
- Citation style & count
- Entries matched / resolved
- Errors
"""

import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import csv
import time
import traceback
from pathlib import Path

from pipeline.extractor import extract_boundaries
from pipeline.bib_extractor import (
    parse_citations,
    extract_bibliography_section,
    match_citations_to_entries,
    run_bib_extraction,
)
from pipeline.schemas import Boundaries
from docling_extractor import extract_text_docling

TEST_DATA_DIR = Path("../test_data")
RESULTS_CSV = Path("batch_results.csv")


def run_single_paper(pdf_name: str) -> dict:
    """Run the full Docling pipeline on one paper. Returns a metrics dict."""
    result = {
        "paper": pdf_name,
        "docling_time_s": None,
        "chars_extracted": None,
        "pages": None,
        "rw_found": False,
        "rw_pages": "",
        "citation_style": "",
        "citations_found": 0,
        "entries_matched": 0,
        "entries_resolved": 0,
        "bib_section_chars": 0,
        "errors": "",
    }

    pdf_path = TEST_DATA_DIR / pdf_name
    if not pdf_path.exists():
        result["errors"] = "PDF not found"
        return result

    # --- Step 1: Docling extraction ---
    try:
        t0 = time.time()
        full_text, page_offsets = extract_text_docling(str(pdf_path))
        result["docling_time_s"] = round(time.time() - t0, 1)
        result["chars_extracted"] = len(full_text)
        result["pages"] = len(page_offsets)
    except Exception as e:
        result["errors"] = f"Docling extraction failed: {e}"
        return result

    # --- Step 2: LLM Boundary Extraction ---
    extracted_rw_text = None
    try:
        raw_result = extract_boundaries(full_text)
        boundaries = Boundaries.from_dict(raw_result)

        if boundaries:
            result["rw_found"] = True
            result["rw_pages"] = f"{boundaries.start_page}-{boundaries.end_page}"

            start_idx = full_text.find(boundaries.start_snippet.strip()[:35])
            end_idx = full_text.find(boundaries.end_snippet.strip()[-35:])
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                extracted_rw_text = full_text[start_idx:end_idx + len(boundaries.end_snippet)]
            else:
                extracted_rw_text = full_text  # Fallback
        else:
            null_conf = raw_result.get("min_null_confidence", "?")
            result["errors"] = f"No RW found (null_conf={null_conf}%)"
    except Exception as e:
        result["errors"] = f"Boundary extraction failed: {e}"

    # --- Step 3: Bibliography Extraction ---
    if extracted_rw_text:
        try:
            # Save per-paper outputs
            paper_stem = pdf_name.replace(".pdf", "")
            outputs_dir = Path("test_outputs") / paper_stem
            outputs_dir.mkdir(parents=True, exist_ok=True)

            # Save raw text for inspection
            with open(outputs_dir / "docling_raw_text.txt", "w") as f:
                f.write(full_text)

            bib_result = run_bib_extraction(full_text, extracted_rw_text, outputs_dir)

            result["citation_style"] = bib_result.citation_style
            result["citations_found"] = bib_result.citations_found
            result["entries_matched"] = bib_result.entries_matched
            result["entries_resolved"] = bib_result.entries_resolved

            # Get bib section size for diagnostics
            bib_section = extract_bibliography_section(full_text)
            if bib_section:
                result["bib_section_chars"] = len(bib_section)

            if bib_result.errors:
                result["errors"] = "; ".join(bib_result.errors)

        except Exception as e:
            result["errors"] = f"Bib extraction failed: {e}"

    return result


def main():
    # Find all papers
    papers = sorted(
        [f.name for f in TEST_DATA_DIR.glob("paper*.pdf")],
        key=lambda x: int(x.replace("paper", "").replace(".pdf", "")),
    )

    print(f"\n{'='*80}")
    print(f"📊 Batch Docling Pipeline Test — {len(papers)} papers")
    print(f"{'='*80}\n")

    results = []
    for i, pdf_name in enumerate(papers, 1):
        print(f"\n{'─'*60}")
        print(f"[{i}/{len(papers)}] 📄 {pdf_name}")
        print(f"{'─'*60}")

        try:
            metrics = run_single_paper(pdf_name)
        except Exception as e:
            metrics = {
                "paper": pdf_name,
                "errors": f"FATAL: {traceback.format_exc()[-200:]}",
            }

        results.append(metrics)

        # Print quick summary
        if metrics.get("rw_found"):
            match_str = f"{metrics['entries_matched']}/{metrics['citations_found']}"
            resolved_str = str(metrics.get("entries_resolved", "?"))
            status = "✅" if metrics["entries_matched"] == metrics["citations_found"] and not metrics.get("errors") else "⚠️"
            print(f"   {status} RW pages {metrics['rw_pages']} | {metrics['citation_style']} | "
                  f"matched {match_str} | resolved {resolved_str}")
        else:
            print(f"   ❌ {metrics.get('errors', 'Unknown error')}")

    # --- Write CSV ---
    fieldnames = [
        "paper", "docling_time_s", "chars_extracted", "pages",
        "rw_found", "rw_pages", "citation_style",
        "citations_found", "entries_matched", "entries_resolved",
        "bib_section_chars", "errors",
    ]
    with open(RESULTS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    print(f"\n\n{'='*80}")
    print(f"📊 SUMMARY — {len(results)} papers")
    print(f"{'='*80}")

    # Summary table
    print(f"\n{'Paper':<12} {'RW?':<5} {'Pages':<8} {'Style':<12} {'Found':<6} {'Match':<6} {'Resolved':<9} {'Errors'}")
    print(f"{'─'*12} {'─'*5} {'─'*8} {'─'*12} {'─'*6} {'─'*6} {'─'*9} {'─'*30}")

    total_found = 0
    total_matched = 0
    total_resolved = 0
    rw_found_count = 0
    perfect_count = 0
    error_papers = []

    for r in results:
        rw = "✅" if r.get("rw_found") else "❌"
        pages = r.get("rw_pages", "—")
        style = r.get("citation_style", "—") or "—"
        found = r.get("citations_found", 0)
        matched = r.get("entries_matched", 0)
        resolved = r.get("entries_resolved", 0)
        errors = r.get("errors", "")

        if r.get("rw_found"):
            rw_found_count += 1
        total_found += found
        total_matched += matched
        total_resolved += resolved

        is_perfect = (r.get("rw_found") and matched == found and found > 0
                      and resolved == matched and not errors)
        if is_perfect:
            perfect_count += 1

        if errors:
            error_papers.append(r["paper"])
            errors_short = errors[:30] + "..." if len(errors) > 30 else errors
        else:
            errors_short = ""

        print(f"{r['paper']:<12} {rw:<5} {pages:<8} {style:<12} {found:<6} {matched:<6} {resolved:<9} {errors_short}")

    print(f"\n{'─'*80}")
    print(f"Related Work found: {rw_found_count}/{len(results)}")
    print(f"Total citations found: {total_found}")
    print(f"Total entries matched: {total_matched}")
    print(f"Total entries resolved: {total_resolved}")
    print(f"Perfect papers (RW + all matched + all resolved, no errors): {perfect_count}/{len(results)}")
    if error_papers:
        print(f"Papers with errors: {', '.join(error_papers)}")

    print(f"\n✅ Results saved to {RESULTS_CSV}")


if __name__ == "__main__":
    main()
