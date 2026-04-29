import os
import json
import csv
import sys
import re
import random
import argparse
from pathlib import Path

from difflib import SequenceMatcher

# Import your existing functions
# Ensure process_paper_v2.py is in the same directory
try:
    from pipeline import run_pipeline
except ImportError:
    print("❌ Error: Could not import 'pipeline'. Make sure the module is in the same directory.")
    sys.exit(1)

# Configuration
TEST_DATA_DIR = Path("test_data")  # Folder containing .pdf and _truth.json files
OUTPUT_CSV = "test_results.csv"
SIMILARITY_THRESHOLD = 0.85  # Fallback if substring fails (85% match)

def normalize_text(text):
    """Aggressively normalize text: lowercase, remove special chars/whitespace."""
    if not text: return ""
    # Strip ellipsis markers that indicate truncation
    text = re.sub(r'\.{2,}', ' ', text)   # "..." or ".."
    text = re.sub(r'…', ' ', text)         # unicode ellipsis
    # Remove non-alphanumeric characters and collapse whitespace
    return re.sub(r'\W+', ' ', text).lower().strip()

def check_snippet_match(ground_truth, predicted, snippet_type="start"):
    """
    Returns (is_match (bool), score (float), note (str))
    
    snippet_type: "start" or "end" — controls directional anchor matching.
    
    Logic:
    1. Exact Substring: If GT is inside Pred or Pred is inside GT (normalized).
    2. Directional Anchor: For "start" snippets, check if the beginning of the
       shorter text matches the beginning of the longer. For "end" snippets,
       check if the ending matches. This handles truncated GT/Pred gracefully.
    3. Fuzzy Match: If SequenceMatcher ratio > Threshold.
    """
    norm_gt = normalize_text(ground_truth)
    norm_pred = normalize_text(predicted)

    if not norm_gt: 
        return True, 1.0, "Empty GT" # If GT is empty, we assume we don't care, or valid skip

    # 1. Substring Check
    if norm_gt in norm_pred:
        return True, 1.0, "GT inside Pred"
    if norm_pred in norm_gt:
        return True, 1.0, "Pred inside GT"

    # 2. Directional Anchor Check
    # Use the shorter text and check if a large portion anchors into the longer text
    shorter, longer = (norm_gt, norm_pred) if len(norm_gt) <= len(norm_pred) else (norm_pred, norm_gt)
    anchor_len = max(int(len(shorter) * 0.7), min(20, len(shorter)))  # at least 70% or 20 chars
    
    if snippet_type == "start":
        # For start snippets: the beginning of both should match
        anchor = shorter[:anchor_len]
        if longer.startswith(anchor):
            return True, 1.0, "Start anchor match"
    elif snippet_type == "end":
        # For end snippets: the ending of both should match
        anchor = shorter[-anchor_len:]
        if longer.endswith(anchor):
            return True, 1.0, "End anchor match"

    # 3. Fuzzy Similarity Check
    matcher = SequenceMatcher(None, norm_gt, norm_pred)
    score = matcher.ratio()
    
    if score >= SIMILARITY_THRESHOLD:
        return True, score, f"Fuzzy Match ({score:.2f})"
    
    return False, score, f"Mismatch ({score:.2f})"

def run_tests(paper_filter=None):
    if not TEST_DATA_DIR.exists():
        print(f"❌ Error: Directory '{TEST_DATA_DIR}' not found. Please create it and add your PDFs + JSONs.")
        return

    # Find all pairs of PDF and JSON
    pdf_files = list(TEST_DATA_DIR.glob("*.pdf"))
    random.shuffle(pdf_files)
    
    # Filter to a specific paper or papers if requested
    if paper_filter:
        filters = [p.strip() for p in paper_filter.split(',')]
        # Support both "paper4" and "paper4.pdf"
        filters = [p if p.endswith(".pdf") else p + ".pdf" for p in filters]
        pdf_files = [p for p in pdf_files if p.name in filters]
        if not pdf_files:
            print(f"❌ Error: Papers '{paper_filter}' not found in {TEST_DATA_DIR}")
            return

    if not pdf_files:
        print("⚠️ No PDF files found in test_data.")
        return

    results = []
    print(f"🔍 Found {len(pdf_files)} PDFs to test...\n")

    for pdf_path in pdf_files:
        # Construct expected JSON path (e.g. paper25.pdf -> paper25_truth.json)
        json_path = pdf_path.with_name(f"{pdf_path.stem}_truth.json")
        
        if not json_path.exists():
            print(f"⚠️ Skipping {pdf_path.name}: No matching _truth.json found.")
            continue

        print(f"▶️ Testing {pdf_path.name}...")

        # Load Ground Truth
        with open(json_path, "r", encoding="utf-8") as f:
            gt_data = json.load(f)
            gt_rw = gt_data.get("related_work")

        # Handle null ground truth (paper has no Related Work section)
        gt_is_null = gt_rw is None

        # 1. Run Pipeline v4
        try:
            pipeline_result = run_pipeline(str(pdf_path), outputs_dir="test_outputs")
            if pipeline_result.final_status == "failed" or pipeline_result.final_status == "no_related_work" or not pipeline_result.boundaries:
                pred_rw = None
                llm_response = {"related_work": None, "v4_logs": pipeline_result.to_log_dict()}
            else:
                pred_rw = pipeline_result.boundaries.to_dict()
                llm_response = {"related_work": pred_rw, "v4_logs": pipeline_result.to_log_dict()}
            
            # Log the LLM JSON response for this paper
            log_path = pdf_path.with_name(f"{pdf_path.stem}_llm_response.json")
            with open(log_path, "w", encoding="utf-8") as lf:
                json.dump(llm_response, lf, indent=2, ensure_ascii=False)
            print(f"   📄 Pipeline v4 log saved to {log_path}")

        except Exception as e:
            print(f"   ❌ Pipeline v4 execution failed: {e}")
            results.append({
                "File": pdf_path.name,
                "Status": "ERROR",
                "GT_Pages": "null" if gt_is_null else "",
                "Pred_Pages": "",
                "Page_Match": "",
                "Start_Snip_Match": "",
                "Start_Snip_Score": "",
                "Start_Snip_Note": "",
                "End_Snip_Match": "",
                "End_Snip_Score": "",
                "End_Snip_Note": "",
                "Error": "Pipeline Failed"
            })
            continue

        # 3. Compare Results
        pred_is_null = pred_rw is None

        # Case: Both null — correct negative
        if gt_is_null and pred_is_null:
            print("   ✅ PASS (correctly identified no Related Work section)")
            results.append({
                "File": pdf_path.name,
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
                "Error": ""
            })
            continue

        # Case: GT is null but LLM found something (false positive)
        if gt_is_null and not pred_is_null:
            print("   ❌ FAIL (false positive — found RW where none exists)")
            results.append({
                "File": pdf_path.name,
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
                "Error": ""
            })
            continue

        # Case: GT exists but LLM returned null (false negative)
        if not gt_is_null and pred_is_null:
            print("   ❌ FAIL (false negative — missed the RW section)")
            results.append({
                "File": pdf_path.name,
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
                "Error": ""
            })
            continue

        # Case: Both have data — compare normally
        if not isinstance(gt_rw, dict):
            gt_rw = {}
        if not isinstance(pred_rw, dict):
            pred_rw = {}

        # Page Comparison
        gt_start_page = int(gt_rw.get("start_page", 0))
        gt_end_page = int(gt_rw.get("end_page", 0))
        pred_start_page = int(pred_rw.get("start_page", 0))
        pred_end_page = int(pred_rw.get("end_page", 0))

        page_match = (gt_start_page == pred_start_page) and (gt_end_page == pred_end_page)

        # Snippet Comparison
        start_match, start_score, start_note = check_snippet_match(
            gt_rw.get("start_snippet", ""), 
            pred_rw.get("start_snippet", ""),
            snippet_type="start"
        )
        end_match, end_score, end_note = check_snippet_match(
            gt_rw.get("end_snippet", ""), 
            pred_rw.get("end_snippet", ""),
            snippet_type="end"
        )

        # Determine Overall Status
        if page_match and start_match and end_match:
            status = "PASS"
            print("   ✅ PASS")
        else:
            status = "FAIL"
            print("   ❌ FAIL")

        # Compile Row
        row = {
            "File": pdf_path.name,
            "Status": status,
            "GT_Pages": f"{gt_start_page}-{gt_end_page}",
            "Pred_Pages": f"{pred_start_page}-{pred_end_page}",
            "Page_Match": page_match,
            "Start_Snip_Match": start_match,
            "Start_Snip_Score": f"{start_score:.2f}",
            "Start_Snip_Note": start_note,
            "End_Snip_Match": end_match,
            "End_Snip_Score": f"{end_score:.2f}",
            "End_Snip_Note": end_note,
            "Error": ""
        }
        results.append(row)

    # Write CSV
    if results:
        keys = results[0].keys()
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(results)
        print(f"\n📝 Report saved to {OUTPUT_CSV}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the Related Work extraction pipeline.")
    parser.add_argument("--paper", type=str, default=None,
                        help="Test a single paper (e.g. --paper paper4 or --paper paper4.pdf)")
    args = parser.parse_args()
    run_tests(paper_filter=args.paper)