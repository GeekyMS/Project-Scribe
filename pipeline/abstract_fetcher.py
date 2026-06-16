"""
Abstract Fetcher — Enriches .bib entries with abstracts from Semantic Scholar.

Uses the free Semantic Scholar Academic Graph API (no auth required for
basic usage) to look up papers by title and retrieve their abstracts.

Rate limit: ~100 requests/5 minutes for unauthenticated access.
"""

import json
import re
import time
from pathlib import Path

import httpx


SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1"
REQUEST_DELAY = 0.5  # seconds between requests (respect rate limits)
REQUEST_TIMEOUT = 15.0  # seconds


def parse_bib_file(bib_path: str | Path) -> list[dict]:
    """
    Parse a .bib file into a list of entry dicts.
    
    Each dict has keys: 'key', 'type', 'title', 'author', 'year', and
    all other fields present in the entry.
    """
    bib_path = Path(bib_path)
    text = bib_path.read_text(encoding="utf-8")
    
    entries = []
    # Match @type{key, ... } blocks
    pattern = re.compile(
        r'@(\w+)\s*\{([^,]+),\s*(.*?)\n\}',
        re.DOTALL,
    )
    
    for match in pattern.finditer(text):
        entry_type = match.group(1).lower()
        entry_key = match.group(2).strip()
        body = match.group(3)
        
        entry = {
            "key": entry_key,
            "type": entry_type,
        }
        
        # Parse fields: name = {value} or name = value
        field_pattern = re.compile(
            r'(\w+)\s*=\s*(?:\{((?:[^{}]|\{[^{}]*\})*)\}|"([^"]*)"|(\S+))',
        )
        for fm in field_pattern.finditer(body):
            field_name = fm.group(1).lower()
            field_value = fm.group(2) or fm.group(3) or fm.group(4) or ""
            # Clean up LaTeX escapes
            field_value = field_value.replace("\\&", "&").strip()
            entry[field_name] = field_value
        
        entries.append(entry)
    
    return entries


def _search_paper(title: str, client: httpx.Client) -> dict | None:
    """
    Search Semantic Scholar for a paper by title.
    
    Returns the best-matching paper dict with 'paperId', 'title',
    'abstract', 'year', 'authors', 'externalIds', or None if not found.
    """
    try:
        resp = client.get(
            f"{SEMANTIC_SCHOLAR_API}/paper/search",
            params={
                "query": title,
                "limit": 3,
                "fields": "paperId,title,abstract,year,authors,externalIds",
            },
            timeout=REQUEST_TIMEOUT,
        )
        
        if resp.status_code == 429:
            # Rate limited — wait and retry once
            print(f"      ⏳ Rate limited, waiting 10s...")
            time.sleep(10)
            resp = client.get(
                f"{SEMANTIC_SCHOLAR_API}/paper/search",
                params={
                    "query": title,
                    "limit": 3,
                    "fields": "paperId,title,abstract,year,authors,externalIds",
                },
                timeout=REQUEST_TIMEOUT,
            )
        
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        papers = data.get("data", [])
        
        if not papers:
            return None
        
        # Return the first result (best match by Semantic Scholar ranking)
        return papers[0]
        
    except Exception as e:
        print(f"      ⚠️ Search failed: {e}")
        return None


def _get_paper_by_arxiv_id(arxiv_id: str, client: httpx.Client) -> dict | None:
    """
    Look up a paper directly by arXiv ID (more reliable than title search).
    """
    try:
        resp = client.get(
            f"{SEMANTIC_SCHOLAR_API}/paper/ARXIV:{arxiv_id}",
            params={
                "fields": "paperId,title,abstract,year,authors,externalIds",
            },
            timeout=REQUEST_TIMEOUT,
        )
        
        if resp.status_code == 429:
            print(f"      ⏳ Rate limited, waiting 10s...")
            time.sleep(10)
            resp = client.get(
                f"{SEMANTIC_SCHOLAR_API}/paper/ARXIV:{arxiv_id}",
                params={
                    "fields": "paperId,title,abstract,year,authors,externalIds",
                },
                timeout=REQUEST_TIMEOUT,
            )
        
        if resp.status_code != 200:
            return None
        
        return resp.json()
        
    except Exception as e:
        print(f"      ⚠️ arXiv lookup failed: {e}")
        return None


def enrich_bib_entries(
    bib_path: str | Path,
    cache_path: str | Path | None = None,
) -> list[dict]:
    """
    Enrich .bib entries with abstracts from Semantic Scholar.
    
    For each entry in the .bib file:
    1. If it has an arXiv eprint ID, look up directly by arXiv ID
    2. Otherwise, search by title
    3. Attach the abstract (if found) to the entry dict
    
    Args:
        bib_path: Path to the .bib file.
        cache_path: Optional path to cache enrichment results (JSON).
            If the cache exists, it's loaded instead of re-fetching.
    
    Returns:
        List of entry dicts, each with an added 'abstract' field
        (empty string if not found).
    """
    bib_path = Path(bib_path)
    
    # Check cache
    if cache_path:
        cache_path = Path(cache_path)
        if cache_path.exists():
            print(f"   📦 Loading cached enrichment from {cache_path}")
            return json.loads(cache_path.read_text(encoding="utf-8"))
    
    entries = parse_bib_file(bib_path)
    print(f"\n   🔍 Enriching {len(entries)} bib entries with abstracts...")
    
    enriched = []
    found_count = 0
    
    with httpx.Client() as client:
        for i, entry in enumerate(entries):
            title = entry.get("title", "")
            arxiv_id = entry.get("eprint", "")
            key = entry.get("key", f"entry_{i}")
            
            print(f"      [{i+1}/{len(entries)}] {key}: ", end="")
            
            paper = None
            
            # Strategy 1: Direct arXiv lookup (most reliable)
            if arxiv_id:
                paper = _get_paper_by_arxiv_id(arxiv_id, client)
                if paper:
                    print(f"found via arXiv:{arxiv_id}")
            
            # Strategy 2: Title search
            if not paper and title:
                paper = _search_paper(title, client)
                if paper:
                    print(f"found via title search")
            
            if paper:
                abstract = paper.get("abstract", "") or ""
                entry["abstract"] = abstract
                entry["semantic_scholar_id"] = paper.get("paperId", "")
                if abstract:
                    found_count += 1
                else:
                    print(f"      (no abstract available)")
            else:
                entry["abstract"] = ""
                print(f"not found")
            
            enriched.append(entry)
            time.sleep(REQUEST_DELAY)
    
    print(f"   ✅ Enriched {found_count}/{len(entries)} entries with abstracts")
    
    # Save cache
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(enriched, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"   📦 Cached enrichment → {cache_path}")
    
    return enriched
