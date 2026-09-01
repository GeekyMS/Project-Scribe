"""
Full-Text Fetcher — attempts to retrieve the cited paper's full text
via an open-access PDF cascade, extracted with Docling.

Previously this only tried arXiv PDFs via the bib's `eprint` field,
and every failure mode (no arXiv id, HTTP 404, Docling crash) collapsed
to the same `full_text = ""` — measured 0/22 on the project's non-arXiv
corpus (systems papers with no arXiv ids at all). This cascade adds
OpenAlex's best_oa_location and Unpaywall as PDF sources for non-arXiv
entries, and records a distinct status per failure mode instead of
silently overwriting them all with "".
"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from . import evidence_cache
from .abstract_fetcher import enrich_bib_entries
from .docling_extractor import extract_text_docling
from .resolvers import openalex_by_doi, unpaywall_oa_pdf

MAX_FULL_TEXT_CHARS = 400_000  # sanity cap; Docling output on a normal paper is far below this


def _pdf_candidates(entry: dict, client: httpx.Client, mailto: str | None, openalex_api_key: str | None) -> list[tuple[str, str]]:
    """
    Return [(source_label, pdf_url), ...] to try in order, best first.

    Only trusts an arXiv PDF URL if the resolver actually confirmed
    the arXiv id (evidence.status == "matched_arxiv") — a raw `eprint`
    field from an OCR'd bib entry may be wrong, and a wrong arXiv PDF
    silently becomes the wrong paper's "full text". For everything
    else, DOI-based OA lookups (OpenAlex, then Unpaywall) are the only
    path — this is what makes non-arXiv full text retrievable at all.
    """
    candidates = []
    evidence = entry.get("evidence", {})

    if evidence.get("status") == "matched_arxiv":
        arxiv_id = entry.get("eprint", "").strip()
        if arxiv_id:
            candidates.append(("arxiv", f"https://arxiv.org/pdf/{arxiv_id}.pdf"))

    doi = evidence.get("external_ids", {}).get("DOI") or entry.get("doi", "")
    if not doi:
        return candidates

    try:
        work = openalex_by_doi(doi, client, mailto=mailto, api_key=openalex_api_key)
    except Exception:
        work = None
    if work:
        best_loc = work.get("best_oa_location") or {}
        pdf_url = best_loc.get("pdf_url")
        if pdf_url:
            candidates.append(("openalex_oa", pdf_url))

    if mailto:
        try:
            up_url = unpaywall_oa_pdf(doi, mailto, client)
        except Exception:
            up_url = None
        if up_url:
            candidates.append(("unpaywall", up_url))

    return candidates


def fetch_and_extract_full_texts(
    bib_path: str | Path,
    cache_path: str | Path | None = None,
    *,
    force_refresh: bool = False,
    mailto: str | None = None,
) -> list[dict]:
    """
    Enrich entries with abstracts (via enrich_bib_entries), then
    attempt full-text retrieval for each via an OA-PDF cascade,
    extracted with Docling.

    Args:
        bib_path: Path to the .bib file.
        cache_path: Per-entry cache path (see pipeline/evidence_cache.py).
        force_refresh: Re-attempt full text even for entries whose
            cache says it already failed permanently.
        mailto: Contact email for Unpaywall's polite pool. Falls back
            to CONTACT_EMAIL in the environment.

    Returns:
        List of entry dicts (bib order) with 'full_text' (empty string
        if none found) and evidence.full_text.status set to one of
        "ok" | "no_pdf" | "download_failed" | "extract_failed" |
        "not_attempted".
    """
    bib_path = Path(bib_path)
    mailto = mailto or os.getenv("CONTACT_EMAIL")
    openalex_api_key = os.getenv("OPENALEX_API_KEY")

    # Abstract enrichment shares the same cache file and resolver —
    # this ensures full_text runs never re-do title/DOI resolution
    # abstract enrichment already did.
    entries = enrich_bib_entries(bib_path, cache_path=cache_path)

    print(f"\n   📚 Attempting FULL TEXT retrieval for {len(entries)} entries "
          f"(arXiv → OpenAlex OA → Unpaywall)...")
    if not mailto:
        print(f"      ℹ️ No CONTACT_EMAIL set — Unpaywall lookups will be skipped "
              f"(it requires a contact email).")

    status_counts: dict[str, int] = {}

    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        for i, entry in enumerate(entries):
            key = entry.get("key", f"entry_{i}")
            fts = entry.setdefault("evidence", {}).setdefault(
                "full_text", {"status": "not_attempted", "source_url": "", "chars": 0, "error": None},
            )

            if not force_refresh and fts.get("status") == "ok":
                entry["full_text"] = entry.get("full_text", "")
                print(f"      [{i+1}/{len(entries)}] {key}: cached full text "
                      f"({fts.get('chars', 0)} chars)")
                status_counts["ok"] = status_counts.get("ok", 0) + 1
                continue

            candidates = _pdf_candidates(entry, client, mailto, openalex_api_key)

            if not candidates:
                fts["status"] = "no_pdf"
                fts["source_url"] = ""
                entry["full_text"] = ""
                print(f"      [{i+1}/{len(entries)}] {key}: no OA PDF found")
                status_counts["no_pdf"] = status_counts.get("no_pdf", 0) + 1
                if cache_path:
                    evidence_cache.save_entry(cache_path, key, entry)
                continue

            source_label, pdf_url = candidates[0]
            print(f"      [{i+1}/{len(entries)}] {key}: downloading via {source_label}...")
            try:
                resp = client.get(pdf_url)
                if resp.status_code != 200:
                    fts["status"] = "download_failed"
                    fts["error"] = f"HTTP {resp.status_code}"
                    entry["full_text"] = ""
                    print(f"      [{i+1}/{len(entries)}] {key}: ⚠️ download failed ({resp.status_code})")
                    status_counts["download_failed"] = status_counts.get("download_failed", 0) + 1
                else:
                    with NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                        tmp.write(resp.content)
                        tmp_path = tmp.name
                    try:
                        full_text, _ = extract_text_docling(tmp_path)
                        full_text = full_text[:MAX_FULL_TEXT_CHARS]
                        entry["full_text"] = full_text
                        fts["status"] = "ok"
                        fts["source_url"] = pdf_url
                        fts["chars"] = len(full_text)
                        fts["error"] = None
                        print(f"      [{i+1}/{len(entries)}] {key}: ✅ extracted {len(full_text)} chars")
                        status_counts["ok"] = status_counts.get("ok", 0) + 1
                    except Exception as e:
                        fts["status"] = "extract_failed"
                        fts["error"] = str(e)
                        entry["full_text"] = ""
                        print(f"      [{i+1}/{len(entries)}] {key}: ⚠️ extraction failed: {e}")
                        status_counts["extract_failed"] = status_counts.get("extract_failed", 0) + 1
                    finally:
                        os.unlink(tmp_path)
            except Exception as e:
                fts["status"] = "download_failed"
                fts["error"] = str(e)
                entry["full_text"] = ""
                print(f"      [{i+1}/{len(entries)}] {key}: ⚠️ download error: {e}")
                status_counts["download_failed"] = status_counts.get("download_failed", 0) + 1

            entry["evidence"]["fetched_at"] = datetime.now(timezone.utc).isoformat()
            if cache_path:
                evidence_cache.save_entry(cache_path, key, entry)

            time.sleep(1)  # be nice to arXiv / publisher hosts

    print(f"\n   ✅ Full-text status breakdown: {status_counts}")
    return entries
