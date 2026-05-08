"""
Test harness for Stage 4 — Bibliography Extraction.

Tests citation parsing and bibliography matching against ground truth.
Does NOT call the LLM (no BibTeX generation) — tests are fast and free.

Uses ground truth RW boundaries to extract Related Work text, isolating
Stage 4 testing from Stage 1-3 accuracy.

Usage:
    python test_bib.py                 # Run all eligible papers
    python test_bib.py --paper paper8  # Run a single paper

Output:
    bib_test_results.csv — detailed results per paper
"""

import json
import csv
import re
import sys
import argparse
from pathlib import Path

from pipeline.extractor import extract_text_and_page_offsets
from pipeline.bib_extractor import (
    parse_citations,
    extract_bibliography_section,
    match_citations_to_entries,
)

TEST_DATA_DIR = Path("test_data")
OUTPUT_CSV = "bib_test_results.csv"


def extract_rw_text_from_truth(full_text: str, truth: dict) -> str | None:
    """
    Extract Related Work text using ground truth page boundaries.
    """
    rw = truth.get("related_work")
    if rw is None:
        return None

    start_page = rw.get("start_page", 0)
    end_page = rw.get("end_page", 0)

    if start_page == 0 or end_page == 0:
        return None

    lines = full_text.split("\n")
    capturing = False
    captured = []

    for line in lines:
        page_match = re.match(r'^--- PAGE (\d+) ---$', line.strip())
        if page_match:
            page_num = int(page_match.group(1))
            if page_num == start_page:
                capturing = True
                continue
            elif page_num > end_page:
                break

        if capturing:
            captured.append(line)

    return "\n".join(captured).strip() if captured else None


def compute_set_metrics(expected: set, actual: set) -> dict:
    """Compute precision, recall, F1 for two sets."""
    if not expected and not actual:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    if not expected:
        return {"precision": 0.0, "recall": 1.0, "f1": 0.0}
    if not actual:
        return {"precision": 1.0, "recall": 0.0, "f1": 0.0}

    true_positives = expected & actual
    precision = len(true_positives) / len(actual) if actual else 0
    recall = len(true_positives) / len(expected) if expected else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    return {"precision": precision, "recall": recall, "f1": f1}


def run_bib_tests(paper_filter: str = None):
    """Run bibliography extraction tests against ground truth."""
    if not TEST_DATA_DIR.exists():
        print(f"❌ Error: '{TEST_DATA_DIR}' not found.")
        return

    # Find papers with bib_ground_truth
    pdf_files = sorted(TEST_DATA_DIR.glob("*.pdf"))

    if paper_filter:
        filters = [p.strip() for p in paper_filter.split(",")]
        filters = [p if p.endswith(".pdf") else p + ".pdf" for p in filters]
        pdf_files = [p for p in pdf_files if p.name in filters]

    results = []
    total_pass = 0
    total_tested = 0

    print("=" * 70)
    print("📚 Bibliography Extraction Test Suite")
    print("=" * 70)

    for pdf_path in pdf_files:
        truth_path = pdf_path.with_name(f"{pdf_path.stem}_truth.json")

        if not truth_path.exists():
            continue

        with open(truth_path, "r", encoding="utf-8") as f:
            truth = json.load(f)

        bib_truth = truth.get("bib_ground_truth")
        if bib_truth is None:
            continue  # No bib ground truth for this paper

        total_tested += 1
        print(f"\n▶️  Testing {pdf_path.name}...")

        # Extract full text
        full_text, _ = extract_text_and_page_offsets(str(pdf_path))

        # Extract RW text using ground truth boundaries
        rw_text = extract_rw_text_from_truth(full_text, truth)
        if not rw_text:
            print(f"   ❌ Could not extract RW text from truth boundaries")
            results.append({
                "File": pdf_path.name,
                "Status": "ERROR",
                "Style_Expected": bib_truth["citation_style"],
                "Style_Actual": "",
                "Style_Match": False,
                "Citations_Expected": bib_truth["expected_citation_count"],
                "Citations_Actual": 0,
                "Citation_Recall": "0.00",
                "Citation_Precision": "0.00",
                "Match_Expected": bib_truth["expected_match_count"],
                "Match_Actual": 0,
                "Match_Rate": "0.00",
                "Error": "RW text extraction failed",
            })
            continue

        # Run citation parsing
        citation_info = parse_citations(rw_text)

        # Run bibliography matching
        bib_section = extract_bibliography_section(full_text)
        matched_entries = []
        if bib_section and citation_info.keys:
            matched_entries = match_citations_to_entries(citation_info, bib_section)

        # Compare against ground truth
        expected_style = bib_truth["citation_style"]
        expected_count = bib_truth["expected_citation_count"]
        expected_match = bib_truth["expected_match_count"]
        expected_keys = set(bib_truth.get("expected_citation_keys", []))

        # For numerical keys, ensure types match for set comparison
        if expected_style == "numerical":
            expected_keys = {int(k) for k in expected_keys}

        actual_style = citation_info.style
        actual_keys = citation_info.keys
        actual_count = len(actual_keys)
        actual_match = len(matched_entries)

        # Metrics
        style_match = actual_style == expected_style
        citation_metrics = compute_set_metrics(expected_keys, actual_keys)

        # Match rate: compare against expected match count
        if expected_match > 0:
            match_rate = actual_match / expected_match
        elif actual_match == 0:
            match_rate = 1.0  # Both zero = correct
        else:
            match_rate = 0.0  # Expected 0 but got matches = wrong

        # Determine pass/fail
        # Pass criteria:
        #   1. Style detection correct
        #   2. Citation recall >= 0.85 (numerical) or >= 0.70 (author-date)
        #   3. Match rate >= 0.75
        recall_threshold = 0.85 if expected_style == "numerical" else 0.70
        passed = (
            style_match
            and citation_metrics["recall"] >= recall_threshold
            and match_rate >= 0.75
        )

        if passed:
            total_pass += 1
            print(f"   ✅ PASS — {actual_style}, {actual_count} citations, "
                  f"{actual_match}/{expected_match} matched")
        else:
            reasons = []
            if not style_match:
                reasons.append(f"style: expected {expected_style}, got {actual_style}")
            if citation_metrics["recall"] < recall_threshold:
                reasons.append(f"recall: {citation_metrics['recall']:.2f} < {recall_threshold}")
            if match_rate < 0.75:
                reasons.append(f"match rate: {match_rate:.2f} < 0.75")
            print(f"   ❌ FAIL — {', '.join(reasons)}")

        results.append({
            "File": pdf_path.name,
            "Status": "PASS" if passed else "FAIL",
            "Style_Expected": expected_style,
            "Style_Actual": actual_style,
            "Style_Match": style_match,
            "Citations_Expected": expected_count,
            "Citations_Actual": actual_count,
            "Citation_Recall": f"{citation_metrics['recall']:.2f}",
            "Citation_Precision": f"{citation_metrics['precision']:.2f}",
            "Match_Expected": expected_match,
            "Match_Actual": actual_match,
            "Match_Rate": f"{match_rate:.2f}",
            "Error": "",
        })

    # Write CSV
    if results:
        keys = results[0].keys()
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(results)

    # Summary
    print("\n" + "=" * 70)
    print("📊 RESULTS SUMMARY")
    print("=" * 70)
    print(f"Passed: {total_pass}/{total_tested}")
    print(f"Failed: {total_tested - total_pass}/{total_tested}")
    if total_tested > 0:
        print(f"Pass rate: {total_pass / total_tested * 100:.0f}%")
    print(f"\n📝 Detailed results saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test bibliography extraction pipeline.")
    parser.add_argument("--paper", type=str, default=None,
                        help="Test specific paper(s) (e.g. --paper paper8 or --paper paper1,paper8)")
    args = parser.parse_args()
    run_bib_tests(paper_filter=args.paper)
