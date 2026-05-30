import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import os
import json
import argparse
from pathlib import Path

# Import the existing pipeline modules we want to test
from pipeline.extractor import extract_boundaries
from pipeline.bib_extractor import run_bib_extraction
from pipeline.schemas import Boundaries

# Import our new docling extractor
from docling_extractor import extract_text_docling

TEST_DATA_DIR = Path("../test_data")

def test_docling_on_paper(pdf_name):
    pdf_path = TEST_DATA_DIR / pdf_name
    if not pdf_path.exists():
        print(f"❌ Error: {pdf_path} not found.")
        return

    print(f"\n{'='*60}")
    print(f"📄 Testing Docling Pipeline on: {pdf_name}")
    print(f"{'='*60}")

    # 1. Extraction with Docling
    print("\n📝 Extracting text with Docling...")
    try:
        full_text, page_offsets = extract_text_docling(str(pdf_path))
    except Exception as e:
        print(f"❌ Docling extraction failed: {e}")
        return

    print(f"   ✅ Extracted {len(full_text)} characters.")

    # 2. Test Related Work Boundary Extraction (LLM Stage 1)
    print("\n🤖 Testing LLM Boundary Extraction...")
    try:
        raw_result = extract_boundaries(full_text)
        boundaries = Boundaries.from_dict(raw_result)
        
        if boundaries:
            print(f"   ✅ LLM Found Related Work: pages {boundaries.start_page}-{boundaries.end_page}")
            print(f"   Start Snippet: {repr(boundaries.start_snippet[:80])}")
            print(f"   End Snippet:   {repr(boundaries.end_snippet[:80])}")
            
            # Since Docling preserves layout well, we can extract the related work text 
            # by simply searching the full_text string or extracting the pages
            # For this test, we'll just mock the extracted_text to be a portion of full_text
            # where the snippets are.
            start_idx = full_text.find(boundaries.start_snippet.strip()[:35])
            end_idx = full_text.find(boundaries.end_snippet.strip()[-35:])
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                extracted_rw_text = full_text[start_idx:end_idx + len(boundaries.end_snippet)]
            else:
                extracted_rw_text = full_text  # Fallback
        else:
            print(f"   ℹ️ LLM returned null (confidence: {raw_result.get('min_null_confidence')}%)")
            extracted_rw_text = None
            
    except Exception as e:
        print(f"❌ Boundary extraction failed: {e}")
        extracted_rw_text = None

    # Write the docling extracted raw text to outputs for user inspection
    outputs_dir = Path("test_outputs")
    outputs_dir.mkdir(exist_ok=True)
    raw_text_path = outputs_dir / "docling_raw_text.txt"
    try:
        with open(raw_text_path, "w", encoding="utf-8") as f:
            f.write(full_text)
        print(f"   💾 Saved raw Docling text to {raw_text_path}")
    except Exception as e:
        print(f"   ⚠️ Failed to save raw text: {e}")

    # 3. Test Bibliography Extraction (Stage 4)
    # We want to see if Docling's cleaner text helps the regex/LLM bib matcher
    print("\n📚 Testing Bibliography Extraction (Stage 4)...")
    if extracted_rw_text:
        try:
            # Import helper functions directly to display detailed diagnostic information
            from pipeline.bib_extractor import parse_citations, extract_bibliography_section, match_citations_to_entries
            
            citation_info = parse_citations(extracted_rw_text)
            all_citations = sorted(list(citation_info.keys))
            print(f"   🔍 Parsed {len(all_citations)} unique citation keys: {all_citations}")
            
            bib_section = extract_bibliography_section(full_text)
            if bib_section:
                matched_entries = match_citations_to_entries(citation_info, bib_section)
                matched_keys = sorted(list(matched_entries.keys()))
                unmatched_keys = sorted(list(set(all_citations) - set(matched_keys)))
                
                print(f"   ✅ Matched citation keys ({len(matched_keys)}): {matched_keys}")
                if matched_keys:
                    print("      Matched Entries Sample:")
                    for k in matched_keys[:5]:
                        print(f"      - [{k}]: {repr(matched_entries[k][:100])}...")
                if unmatched_keys:
                    print(f"   ❌ Unmatched citation keys ({len(unmatched_keys)}): {unmatched_keys}")
            else:
                print("   ❌ Could not find Bibliography/References section in the text.")

            bib_result = run_bib_extraction(full_text, extracted_rw_text, outputs_dir)
            
            print(f"\n   ✅ Stage 4 Summary:")
            print(f"      Citations found: {bib_result.citations_found}")
            print(f"      Entries matched: {bib_result.entries_matched}")
            print(f"      Entries resolved: {bib_result.entries_resolved}")
            
            if bib_result.errors:
                print("   ⚠️ Bibliography Errors:")
                for err in bib_result.errors:
                    print(f"      - {err}")
                    
        except Exception as e:
            print(f"❌ Bibliography extraction failed: {e}")
    else:
        print("   ⏭️ Skipping bib extraction because Related Work was not found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Docling extraction on the pipeline.")
    parser.add_argument("--paper", type=str, required=True, help="Paper filename (e.g. paper4.pdf)")
    args = parser.parse_args()
    
    # Ensure we run from docling_testing directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_docling_on_paper(args.paper)
