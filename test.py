import os
import json
import csv
import sys
import re
from pathlib import Path
from difflib import SequenceMatcher

# Import your existing functions
# Ensure process_paper_v2.py is in the same directory
try:
    from process_paper_v2 import extract_text_and_page_offsets, call_llm_strict_for_boundaries
except ImportError:
    print("❌ Error: Could not import 'process_paper_v2.py'. Make sure it is in the same directory.")
    sys.exit(1)

# Configuration
TEST_DATA_DIR = Path("test_data")  # Folder containing .pdf and _truth.json files
OUTPUT_CSV = "test_results.csv"
SIMILARITY_THRESHOLD = 0.85  # Fallback if substring fails (85% match)

def normalize_text(text):
    """Aggressively normalize text: lowercase, remove special chars/whitespace."""
    if not text: return ""
    # Remove non-alphanumeric characters and collapse whitespace
    return re.sub(r'\W+', ' ', text).lower().strip()

def check_snippet_match(ground_truth, predicted):
    """
    Returns (is_match (bool), score (float), note (str))
    
    Logic:
    1. Exact Substring: If GT is inside Pred or Pred is inside GT (normalized).
    2. Fuzzy Match: If SequenceMatcher ratio > Threshold.
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

    # 2. Fuzzy Similarity Check
    matcher = SequenceMatcher(None, norm_gt, norm_pred)
    score = matcher.ratio()
    
    if score >= SIMILARITY_THRESHOLD:
        return True, score, f"Fuzzy Match ({score:.2f})"
    
    return False, score, f"Mismatch ({score:.2f})"

def run_tests():
    if not TEST_DATA_DIR.exists():
        print(f"❌ Error: Directory '{TEST_DATA_DIR}' not found. Please create it and add your PDFs + JSONs.")
        return

    # Find all pairs of PDF and JSON
    pdf_files = list(TEST_DATA_DIR.glob("*.pdf"))
    if not pdf_files:
        print("⚠️ No PDF files found in test_data.")
        return

    results = []
    print(f"🔍 Found {len(pdf_files)} PDFs to test...\n")

    for pdf_path in pdf_files:
        # Construct expected JSON path (e.g. paper1.pdf -> paper1_truth.json)
        json_path = pdf_path.with_name(f"{pdf_path.stem}_truth.json")
        
        if not json_path.exists():
            print(f"⚠️ Skipping {pdf_path.name}: No matching _truth.json found.")
            continue

        print(f"▶️ Testing {pdf_path.name}...")

        # Load Ground Truth
        with open(json_path, "r", encoding="utf-8") as f:
            gt_data = json.load(f)
            gt_rw = gt_data.get("related_work", {})

        # 1. Run Extraction
        try:
            full_text, _ = extract_text_and_page_offsets(str(pdf_path))
        except Exception as e:
            print(f"   ❌ PDF Extraction failed: {e}")
            results.append({
                "File": pdf_path.name,
                "Status": "ERROR",
                "Error": "PDF Extraction Failed"
            })
            continue

        # 2. Call LLM
        try:
            # Note: Using your existing function logic
            llm_response = call_llm_strict_for_boundaries(full_text, max_chars=25000)
            pred_rw = llm_response.get("related_work", {})
        except Exception as e:
            print(f"   ❌ LLM Call failed: {e}")
            results.append({
                "File": pdf_path.name,
                "Status": "ERROR",
                "Error": "LLM Call Failed"
            })
            continue

        # 3. Compare Results
        # Page Comparison
        gt_start_page = int(gt_rw.get("start_page", 0))
        gt_end_page = int(gt_rw.get("end_page", 0))
        pred_start_page = int(pred_rw.get("start_page", 0))
        pred_end_page = int(pred_rw.get("end_page", 0))

        page_match = (gt_start_page == pred_start_page) and (gt_end_page == pred_end_page)

        # Snippet Comparison
        start_match, start_score, start_note = check_snippet_match(
            gt_rw.get("start_snippet", ""), 
            pred_rw.get("start_snippet", "")
        )
        end_match, end_score, end_note = check_snippet_match(
            gt_rw.get("end_snippet", ""), 
            pred_rw.get("end_snippet", "")
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
    run_tests()