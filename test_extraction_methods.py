"""
Side-by-side comparison of PDF text extraction methods.

Tests three extraction strategies on academic papers and compares
their effectiveness for Related Work section detection via LLM.

Methods:
  1. baseline_sort     — page.get_text("text", sort=True)
  2. column_stitch     — split two-column pages down the middle, stitch right below left
  3. pymupdf4llm       — pymupdf4llm.to_markdown() with page_chunks

Usage:
  python test_extraction_methods.py                          # all papers
  python test_extraction_methods.py --papers paper8 paper3   # specific papers
  python test_extraction_methods.py --methods baseline_sort column_stitch  # specific methods
"""

import os
import sys
import json
import csv
import re
import time
import argparse
from pathlib import Path
from difflib import SequenceMatcher

import fitz

# Import LLM calling logic from existing pipeline
try:
    from process_paper_v2 import (
        _build_prompt,
        _call_llm_single,
        call_llm_strict_for_boundaries,
    )
except ImportError:
    print("❌ Error: Could not import 'process_paper_v2.py'. Make sure it is in the same directory.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TEST_DATA_DIR = Path("test_data")
OUTPUT_CSV = "extraction_comparison.csv"
SIMILARITY_THRESHOLD = 0.85

# Default subset of trouble papers
DEFAULT_SUBSET = ["paper8", "paper3", "paper5", "paper11", "paper20"]

ALL_METHODS = ["baseline_sort", "column_stitch", "pymupdf4llm"]


# ===========================================================================
# EXTRACTION METHOD 1: Baseline sort=True  (current approach)
# ===========================================================================
def extract_baseline(pdf_path: str):
    """Current approach: page.get_text('text', sort=True) with margin clipping."""
    doc = fitz.open(pdf_path)
    full_text = ""
    page_offsets = []

    for page in doc:
        page_offsets.append(len(full_text))
        full_text += f"\n--- PAGE {page.number + 1} ---\n"

        page_rect = page.rect
        margin = page_rect.height * 0.10

        clip_rect = fitz.Rect(
            0, margin,
            page_rect.width, page_rect.height - margin,
        )
        page_text = page.get_text("text", clip=clip_rect, sort=True)
        full_text += page_text + "\n"

    doc.close()
    return full_text, page_offsets


# ===========================================================================
# EXTRACTION METHOD 2: Column-Split-and-Stitch
# ===========================================================================
def _is_two_column(page, margin):
    """
    Heuristic: extract text blocks, check if blocks are distributed
    across left and right halves.  If both halves contain ≥ 30% of
    the body-region blocks, the page is likely two-column.
    """
    page_rect = page.rect
    mid_x = page_rect.width / 2

    blocks = page.get_text("blocks", sort=True)
    # Filter to body region (skip header/footer) and text blocks only (type 0)
    body_blocks = [
        b for b in blocks
        if b[1] > margin
        and b[3] < page_rect.height - margin
        and b[6] == 0
    ]

    if not body_blocks:
        return False

    left_count = sum(1 for b in body_blocks if (b[0] + b[2]) / 2 < mid_x)
    right_count = sum(1 for b in body_blocks if (b[0] + b[2]) / 2 >= mid_x)
    total = left_count + right_count

    if total == 0:
        return False

    return (left_count / total) >= 0.30 and (right_count / total) >= 0.30


def extract_column_stitch(pdf_path: str):
    """
    Split two-column pages down the middle. Extract left column text,
    then right column text, and concatenate — producing a single-column
    reading flow.  Falls back to normal extraction for single-column pages.
    """
    doc = fitz.open(pdf_path)
    full_text = ""
    page_offsets = []

    for page in doc:
        page_offsets.append(len(full_text))
        full_text += f"\n--- PAGE {page.number + 1} ---\n"

        page_rect = page.rect
        margin = page_rect.height * 0.10
        mid_x = page_rect.width / 2

        if _is_two_column(page, margin):
            # Left column
            left_rect = fitz.Rect(0, margin, mid_x, page_rect.height - margin)
            left_text = page.get_text("text", clip=left_rect, sort=True)

            # Right column
            right_rect = fitz.Rect(mid_x, margin, page_rect.width, page_rect.height - margin)
            right_text = page.get_text("text", clip=right_rect, sort=True)

            full_text += left_text + "\n" + right_text + "\n"
        else:
            # Single column — extract normally
            clip_rect = fitz.Rect(0, margin, page_rect.width, page_rect.height - margin)
            full_text += page.get_text("text", clip=clip_rect, sort=True) + "\n"

    doc.close()
    return full_text, page_offsets


# ===========================================================================
# EXTRACTION METHOD 3: PyMuPDF4LLM (Markdown)
# ===========================================================================
def extract_pymupdf4llm(pdf_path: str):
    """
    Use pymupdf4llm.to_markdown with page_chunks=True.
    Inject --- PAGE X --- markers for consistency with the LLM prompt.
    """
    import pymupdf4llm

    chunks = pymupdf4llm.to_markdown(pdf_path, page_chunks=True)

    full_text = ""
    page_offsets = []

    for i, chunk in enumerate(chunks):
        page_offsets.append(len(full_text))
        full_text += f"\n--- PAGE {i + 1} ---\n"
        full_text += chunk["text"] + "\n"

    return full_text, page_offsets


# ===========================================================================
# Extraction dispatcher
# ===========================================================================
EXTRACTORS = {
    "baseline_sort": extract_baseline,
    "column_stitch": extract_column_stitch,
    "pymupdf4llm": extract_pymupdf4llm,
}


# ===========================================================================
# Evaluation helpers (adapted from test.py)
# ===========================================================================
def normalize_text(text):
    if not text:
        return ""
    text = re.sub(r'\.{2,}', ' ', text)
    text = re.sub(r'…', ' ', text)
    return re.sub(r'\W+', ' ', text).lower().strip()


def check_snippet_match(ground_truth, predicted, snippet_type="start"):
    norm_gt = normalize_text(ground_truth)
    norm_pred = normalize_text(predicted)

    if not norm_gt:
        return True, 1.0, "Empty GT"

    # Substring check
    if norm_gt in norm_pred:
        return True, 1.0, "GT inside Pred"
    if norm_pred in norm_gt:
        return True, 1.0, "Pred inside GT"

    # Directional anchor check
    shorter, longer = (norm_gt, norm_pred) if len(norm_gt) <= len(norm_pred) else (norm_pred, norm_gt)
    anchor_len = max(int(len(shorter) * 0.7), min(20, len(shorter)))

    if snippet_type == "start":
        anchor = shorter[:anchor_len]
        if longer.startswith(anchor):
            return True, 1.0, "Start anchor match"
    elif snippet_type == "end":
        anchor = shorter[-anchor_len:]
        if longer.endswith(anchor):
            return True, 1.0, "End anchor match"

    # Fuzzy similarity
    score = SequenceMatcher(None, norm_gt, norm_pred).ratio()
    if score >= SIMILARITY_THRESHOLD:
        return True, score, f"Fuzzy Match ({score:.2f})"

    return False, score, f"Mismatch ({score:.2f})"


def evaluate_result(gt_rw, pred_rw):
    """
    Compare predicted related_work against ground truth.
    Returns a dict with match info.
    """
    gt_is_null = gt_rw is None
    pred_is_null = pred_rw is None

    if gt_is_null and pred_is_null:
        return {
            "Status": "PASS",
            "GT_Pages": "null",
            "Pred_Pages": "null",
            "Page_Match": True,
            "Start_Snip_Match": True,
            "Start_Snip_Score": "1.00",
            "Start_Snip_Note": "Both null",
            "End_Snip_Match": True,
            "End_Snip_Score": "1.00",
            "End_Snip_Note": "Both null",
        }

    if gt_is_null and not pred_is_null:
        return {
            "Status": "FAIL",
            "GT_Pages": "null",
            "Pred_Pages": f"{pred_rw.get('start_page', '?')}-{pred_rw.get('end_page', '?')}",
            "Page_Match": False,
            "Start_Snip_Match": False,
            "Start_Snip_Score": "0.00",
            "Start_Snip_Note": "False positive",
            "End_Snip_Match": False,
            "End_Snip_Score": "0.00",
            "End_Snip_Note": "False positive",
        }

    if not gt_is_null and pred_is_null:
        return {
            "Status": "FAIL",
            "GT_Pages": f"{gt_rw.get('start_page', '?')}-{gt_rw.get('end_page', '?')}",
            "Pred_Pages": "null",
            "Page_Match": False,
            "Start_Snip_Match": False,
            "Start_Snip_Score": "0.00",
            "Start_Snip_Note": "False negative",
            "End_Snip_Match": False,
            "End_Snip_Score": "0.00",
            "End_Snip_Note": "False negative",
        }

    # Both have data
    if not isinstance(gt_rw, dict):
        gt_rw = {}
    if not isinstance(pred_rw, dict):
        pred_rw = {}

    gt_sp = int(gt_rw.get("start_page", 0))
    gt_ep = int(gt_rw.get("end_page", 0))
    pred_sp = int(pred_rw.get("start_page", 0))
    pred_ep = int(pred_rw.get("end_page", 0))

    page_match = (gt_sp == pred_sp) and (gt_ep == pred_ep)

    start_match, start_score, start_note = check_snippet_match(
        gt_rw.get("start_snippet", ""),
        pred_rw.get("start_snippet", ""),
        snippet_type="start",
    )
    end_match, end_score, end_note = check_snippet_match(
        gt_rw.get("end_snippet", ""),
        pred_rw.get("end_snippet", ""),
        snippet_type="end",
    )

    status = "PASS" if (page_match and start_match and end_match) else "FAIL"

    return {
        "Status": status,
        "GT_Pages": f"{gt_sp}-{gt_ep}",
        "Pred_Pages": f"{pred_sp}-{pred_ep}",
        "Page_Match": page_match,
        "Start_Snip_Match": start_match,
        "Start_Snip_Score": f"{start_score:.2f}",
        "Start_Snip_Note": start_note,
        "End_Snip_Match": end_match,
        "End_Snip_Score": f"{end_score:.2f}",
        "End_Snip_Note": end_note,
    }


# ===========================================================================
# Main runner
# ===========================================================================
def run_comparison(paper_names=None, methods=None):
    if not TEST_DATA_DIR.exists():
        print(f"❌ Error: Directory '{TEST_DATA_DIR}' not found.")
        return

    # Resolve papers
    if paper_names:
        pdf_files = []
        for name in paper_names:
            if not name.endswith(".pdf"):
                name += ".pdf"
            p = TEST_DATA_DIR / name
            if p.exists():
                pdf_files.append(p)
            else:
                print(f"⚠️ Paper '{name}' not found in {TEST_DATA_DIR}, skipping.")
    else:
        pdf_files = sorted(TEST_DATA_DIR.glob("*.pdf"))

    if not pdf_files:
        print("⚠️ No PDF files to test.")
        return

    methods = methods or ALL_METHODS
    print(f"📋 Testing {len(pdf_files)} paper(s) × {len(methods)} method(s) = {len(pdf_files) * len(methods)} run(s)\n")
    print(f"   Papers:  {', '.join(p.stem for p in pdf_files)}")
    print(f"   Methods: {', '.join(methods)}")
    print()

    all_results = []
    method_stats = {m: {"pass": 0, "fail": 0, "error": 0} for m in methods}

    for pdf_path in pdf_files:
        json_path = pdf_path.with_name(f"{pdf_path.stem}_truth.json")
        if not json_path.exists():
            print(f"⚠️ Skipping {pdf_path.name}: No matching _truth.json found.")
            continue

        with open(json_path, "r", encoding="utf-8") as f:
            gt_data = json.load(f)
        gt_rw = gt_data.get("related_work")

        gt_desc = "null" if gt_rw is None else f"pages {gt_rw.get('start_page')}-{gt_rw.get('end_page')}"
        print("=" * 70)
        print(f"📄 {pdf_path.name}  (GT: {gt_desc})")
        print("=" * 70)

        for method in methods:
            print(f"\n  🔬 Method: {method}")
            extractor = EXTRACTORS.get(method)
            if not extractor:
                print(f"     ❌ Unknown method '{method}'")
                continue

            # --- Extraction phase ---
            try:
                t0 = time.time()
                full_text, page_offsets = extractor(str(pdf_path))
                extraction_ms = (time.time() - t0) * 1000
                text_len = len(full_text)
                print(f"     📊 Extracted {text_len} chars in {extraction_ms:.0f}ms ({len(page_offsets)} pages)")
            except Exception as e:
                print(f"     ❌ Extraction failed: {e}")
                all_results.append({
                    "File": pdf_path.name,
                    "Method": method,
                    "Status": "ERROR",
                    "GT_Pages": "",
                    "Pred_Pages": "",
                    "Page_Match": "",
                    "Start_Snip_Match": "",
                    "Start_Snip_Score": "",
                    "Start_Snip_Note": "",
                    "End_Snip_Match": "",
                    "End_Snip_Score": "",
                    "End_Snip_Note": "",
                    "Text_Length": "",
                    "Extraction_Time_ms": "",
                    "Error": str(e),
                })
                method_stats[method]["error"] += 1
                continue

            # --- Save extracted text for manual review ---
            debug_dir = TEST_DATA_DIR / "extraction_debug"
            debug_dir.mkdir(exist_ok=True)
            debug_file = debug_dir / f"{pdf_path.stem}_{method}.txt"
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(full_text)

            # --- LLM phase ---
            try:
                llm_response = call_llm_strict_for_boundaries(full_text, max_chars=25000)
                pred_rw = llm_response.get("related_work")

                # Save LLM response
                resp_file = debug_dir / f"{pdf_path.stem}_{method}_llm_response.json"
                with open(resp_file, "w", encoding="utf-8") as f:
                    json.dump(llm_response, f, indent=2, ensure_ascii=False)

            except Exception as e:
                print(f"     ❌ LLM call failed: {e}")
                all_results.append({
                    "File": pdf_path.name,
                    "Method": method,
                    "Status": "ERROR",
                    "GT_Pages": "",
                    "Pred_Pages": "",
                    "Page_Match": "",
                    "Start_Snip_Match": "",
                    "Start_Snip_Score": "",
                    "Start_Snip_Note": "",
                    "End_Snip_Match": "",
                    "End_Snip_Score": "",
                    "End_Snip_Note": "",
                    "Text_Length": str(text_len),
                    "Extraction_Time_ms": f"{extraction_ms:.0f}",
                    "Error": str(e),
                })
                method_stats[method]["error"] += 1
                continue

            # --- Evaluation phase ---
            eval_result = evaluate_result(gt_rw, pred_rw)
            status = eval_result["Status"]

            icon = "✅" if status == "PASS" else "❌"
            print(f"     {icon} {status}  |  GT: {eval_result['GT_Pages']}  Pred: {eval_result['Pred_Pages']}  |  Pages: {eval_result['Page_Match']}  Start: {eval_result['Start_Snip_Note']}  End: {eval_result['End_Snip_Note']}")

            row = {
                "File": pdf_path.name,
                "Method": method,
                **eval_result,
                "Text_Length": str(text_len),
                "Extraction_Time_ms": f"{extraction_ms:.0f}",
                "Error": "",
            }
            all_results.append(row)

            if status == "PASS":
                method_stats[method]["pass"] += 1
            else:
                method_stats[method]["fail"] += 1

    # --- Write CSV ---
    if all_results:
        fieldnames = list(all_results[0].keys())
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_results)
        print(f"\n📝 Detailed results saved to {OUTPUT_CSV}")

    # --- Summary table ---
    print(f"\n{'='*70}")
    print("📊 SUMMARY")
    print(f"{'='*70}")
    print(f"{'Method':<20} | {'Pass':>4} | {'Fail':>4} | {'Error':>5} | {'Accuracy':>8}")
    print(f"{'-'*20}-+-{'-'*4}-+-{'-'*4}-+-{'-'*5}-+-{'-'*8}")

    for method in methods:
        s = method_stats[method]
        total = s["pass"] + s["fail"] + s["error"]
        acc = (s["pass"] / total * 100) if total > 0 else 0
        print(f"{method:<20} | {s['pass']:>4} | {s['fail']:>4} | {s['error']:>5} | {acc:>7.1f}%")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Compare PDF extraction methods for Related Work detection."
    )
    parser.add_argument(
        "--papers",
        nargs="+",
        default=DEFAULT_SUBSET,
        help="Paper names to test (e.g. paper8 paper3). Defaults to trouble subset.",
    )
    parser.add_argument(
        "--methods",
        nargs="+",
        choices=ALL_METHODS,
        default=None,
        help="Methods to test. Defaults to all three.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Test all papers in test_data/ instead of default subset.",
    )
    args = parser.parse_args()

    papers = None if args.all else args.papers
    run_comparison(paper_names=papers, methods=args.methods)
