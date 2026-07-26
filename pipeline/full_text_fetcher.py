import json
import os
import time
import httpx
from pathlib import Path
from tempfile import NamedTemporaryFile

from pipeline.abstract_fetcher import enrich_bib_entries
from pipeline.docling_extractor import extract_text_docling

def fetch_and_extract_full_texts(bib_path: str | Path, cache_path: str | Path | None = None) -> list[dict]:
    """
    First enriches entries with abstracts, then attempts to download
    and extract full texts for any arXiv papers to overload the context.
    """
    bib_path = Path(bib_path)
    
    if cache_path:
        cache_path = Path(cache_path)
        if cache_path.exists():
            print(f"   📦 Loading cached full-text enrichment from {cache_path}")
            return json.loads(cache_path.read_text(encoding="utf-8"))
            
    # First get the abstracts as a baseline
    entries = enrich_bib_entries(bib_path, cache_path=None)
    
    print(f"\n   📚 Attempting to fetch FULL TEXT for {len(entries)} entries...")
    
    with httpx.Client() as client:
        for i, entry in enumerate(entries):
            arxiv_id = entry.get("eprint", "")
            key = entry.get("key", f"entry_{i}")
            
            # For now, only attempt arXiv papers as they have guaranteed open access PDFs
            if arxiv_id:
                print(f"      [{i+1}/{len(entries)}] {key}: Downloading arXiv PDF {arxiv_id}...")
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                
                try:
                    resp = client.get(pdf_url, follow_redirects=True, timeout=30.0)
                    if resp.status_code == 200:
                        # Save to temp file
                        with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                            tmp.write(resp.content)
                            tmp_path = tmp.name
                            
                        print(f"      [{i+1}/{len(entries)}] {key}: Extracting text with Docling...")
                        try:
                            full_text, _ = extract_text_docling(tmp_path)
                            entry["full_text"] = full_text
                            print(f"      [{i+1}/{len(entries)}] {key}: ✅ Extracted {len(full_text)} chars")
                        except Exception as e:
                            print(f"      [{i+1}/{len(entries)}] {key}: ⚠️ Extraction failed: {e}")
                            entry["full_text"] = ""
                        finally:
                            os.unlink(tmp_path)
                    else:
                        print(f"      [{i+1}/{len(entries)}] {key}: ⚠️ Failed to download PDF (Status {resp.status_code})")
                        entry["full_text"] = ""
                except Exception as e:
                    print(f"      [{i+1}/{len(entries)}] {key}: ⚠️ Download error: {e}")
                    entry["full_text"] = ""
            else:
                print(f"      [{i+1}/{len(entries)}] {key}: No arXiv ID, falling back to abstract only.")
                entry["full_text"] = ""
                
            time.sleep(1) # Be nice to arXiv API
            
    # Save cache
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"   📦 Cached full-text enrichment → {cache_path}")
        
    return entries
