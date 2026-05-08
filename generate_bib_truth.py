"""
Semi-automated ground truth generator for bibliography extraction testing.

For each eligible paper (Stage 1-3 PASS, non-null RW):
  1. Extracts full text from PDF
  2. Extracts Related Work text using ground truth boundaries
  3. Runs citation parsing + bibliography matching (NO LLM calls)
  4. Outputs candidate bib_ground_truth for spot-checking

Usage:
    python generate_bib_truth.py              # Print candidate truth table
    python generate_bib_truth.py --write      # Write into _truth.json files
"""

import json
import re
import sys
from pathlib import Path

# Import pipeline functions
from pipeline.extractor import extract_text_and_page_offsets
from pipeline.bib_extractor import (
    parse_citations,
    extract_bibliography_section,
    match_citations_to_entries,
)

TEST_DATA_DIR = Path("test_data")

# Papers where Stage 1-3 PASS and RW is non-null
ELIGIBLE_PAPERS = [1, 2, 4, 7, 8, 9, 10, 14, 15, 17, 19]


def extract_rw_text_from_truth(full_text: str, truth: dict) -> str | None:
    """
    Extract Related Work text using ground truth page boundaries.
    
    Uses PAGE markers in the full_text to isolate the correct pages,
    then returns all text between the start and end pages (inclusive).
    """
    rw = truth.get("related_work")
    if rw is None:
        return None
    
    start_page = rw.get("start_page", 0)
    end_page = rw.get("end_page", 0)
    
    if start_page == 0 or end_page == 0:
        return None
    
    # Find text between PAGE markers
    lines = full_text.split("\n")
    capturing = False
    captured = []
    
    for line in lines:
        # Check for page markers like "--- PAGE 2 ---"
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


def generate_candidate_truth(paper_num: int) -> dict | None:
    """
    Generate candidate bib_ground_truth for a single paper.
    
    Returns dict with citation_style, keys, counts, and match details.
    Returns None if paper can't be processed.
    """
    pdf_path = TEST_DATA_DIR / f"paper{paper_num}.pdf"
    truth_path = TEST_DATA_DIR / f"paper{paper_num}_truth.json"
    
    if not pdf_path.exists() or not truth_path.exists():
        return None
    
    with open(truth_path, "r", encoding="utf-8") as f:
        truth = json.load(f)
    
    if truth.get("related_work") is None:
        return None
    
    # Step 1: Extract full text
    full_text, _ = extract_text_and_page_offsets(str(pdf_path))
    
    # Step 2: Get RW text from truth boundaries
    rw_text = extract_rw_text_from_truth(full_text, truth)
    if not rw_text:
        return {"error": "Could not extract RW text from truth boundaries"}
    
    # Step 3: Parse citations
    citation_info = parse_citations(rw_text)
    
    # Step 4: Extract bibliography and match
    bib_section = extract_bibliography_section(full_text)
    matched_entries = []
    if bib_section and citation_info.keys:
        matched_entries = match_citations_to_entries(citation_info, bib_section)
    
    # Build candidate ground truth
    # For numerical keys, convert to sorted list of ints
    # For author-date keys, convert to sorted list of strings
    if citation_info.style == "numerical":
        sorted_keys = sorted(citation_info.keys)
    else:
        sorted_keys = sorted(citation_info.keys, key=str)
    
    return {
        "citation_style": citation_info.style,
        "expected_citation_keys": sorted_keys,
        "expected_citation_count": len(citation_info.keys),
        "expected_match_count": len(matched_entries),
        "has_bibliography_section": bib_section is not None,
        "rw_text_length": len(rw_text),
        # Debug info (not saved to truth files)
        "_matched_entries_preview": [e[:80] + "..." for e in matched_entries[:5]],
    }


def main():
    write_mode = "--write" in sys.argv
    
    print("=" * 80)
    print("📚 Bibliography Ground Truth Generator")
    print("=" * 80)
    
    results = {}
    
    for paper_num in ELIGIBLE_PAPERS:
        print(f"\n▶️  Processing paper{paper_num}...")
        candidate = generate_candidate_truth(paper_num)
        
        if candidate is None:
            print(f"   ⚠️  Skipped (no PDF or null RW)")
            continue
        
        if "error" in candidate:
            print(f"   ❌ {candidate['error']}")
            continue
        
        results[paper_num] = candidate
        style = candidate["citation_style"]
        cite_count = candidate["expected_citation_count"]
        match_count = candidate["expected_match_count"]
        has_bib = candidate["has_bibliography_section"]
        rw_len = candidate["rw_text_length"]
        
        match_rate = (match_count / cite_count * 100) if cite_count > 0 else 0
        
        print(f"   Style: {style}")
        print(f"   Citations found: {cite_count}")
        print(f"   Bibliography section: {'✅ Found' if has_bib else '❌ Not found'}")
        print(f"   Matched: {match_count}/{cite_count} ({match_rate:.0f}%)")
        print(f"   RW text: {rw_len} chars")
        
        if candidate["_matched_entries_preview"]:
            print(f"   Sample matches:")
            for entry in candidate["_matched_entries_preview"][:3]:
                print(f"      • {entry}")
    
    # Summary table
    print("\n" + "=" * 80)
    print("📊 SUMMARY TABLE — Review these numbers before approving")
    print("=" * 80)
    print(f"{'Paper':<10} {'Style':<14} {'Citations':<11} {'Matched':<10} {'Rate':<8} {'Bib?':<6}")
    print("-" * 60)
    
    for paper_num, candidate in results.items():
        style = candidate["citation_style"]
        cite_count = candidate["expected_citation_count"]
        match_count = candidate["expected_match_count"]
        has_bib = "✅" if candidate["has_bibliography_section"] else "❌"
        match_rate = (match_count / cite_count * 100) if cite_count > 0 else 0
        
        print(f"paper{paper_num:<5} {style:<14} {cite_count:<11} {match_count:<10} {match_rate:<7.0f}% {has_bib}")
    
    print("-" * 60)
    total_cite = sum(c["expected_citation_count"] for c in results.values())
    total_match = sum(c["expected_match_count"] for c in results.values())
    overall_rate = (total_match / total_cite * 100) if total_cite > 0 else 0
    print(f"{'TOTAL':<10} {'':<14} {total_cite:<11} {total_match:<10} {overall_rate:.0f}%")
    
    # Write mode
    if write_mode:
        print(f"\n✏️  Writing ground truth into _truth.json files...")
        for paper_num, candidate in results.items():
            truth_path = TEST_DATA_DIR / f"paper{paper_num}_truth.json"
            
            with open(truth_path, "r", encoding="utf-8") as f:
                truth = json.load(f)
            
            # Add bib_ground_truth (without debug fields)
            truth["bib_ground_truth"] = {
                "citation_style": candidate["citation_style"],
                "expected_citation_keys": candidate["expected_citation_keys"],
                "expected_citation_count": candidate["expected_citation_count"],
                "expected_match_count": candidate["expected_match_count"],
            }
            
            with open(truth_path, "w", encoding="utf-8") as f:
                json.dump(truth, f, indent=4, ensure_ascii=False)
            
            print(f"   ✅ Updated {truth_path}")
        
        print(f"\n🎉 Ground truth written to {len(results)} files.")
    else:
        print(f"\n💡 Run with --write to save these into _truth.json files.")
        print(f"   python generate_bib_truth.py --write")


if __name__ == "__main__":
    main()
