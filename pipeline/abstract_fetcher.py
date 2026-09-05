"""
Abstract Fetcher — Enriches .bib entries with abstracts via a
DOI -> arXiv -> title-search resolution cascade.

Previously this resolved by arXiv eprint only, then fell back to an
unaudited Semantic Scholar title search (papers[0], no similarity
check) and discarded the identifiers needed to audit a match. Measured
coverage on the project's real bibliographies was 6/38 abstracts
(16%). The cascade now lives in pipeline/resolvers.py (multi-provider,
title-similarity gated); this module wires it to the per-entry
repairable cache in pipeline/evidence_cache.py.

No API key is required — see resolvers.py's docstring for what keys
raise the ceiling. Rate limiting is per-provider and configurable via
the RATE_* module constants below, since documented budgets change
(and did: OpenAlex began requiring a key on 13 Feb 2026).
"""

import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import evidence_cache
from .resolvers import resolve_entry

# Per-provider request spacing (seconds). S2's documented unauthenticated
# budget is a shared pool across all callers — 3.0s is a conservative
# guess, not a measured number; with an S2_API_KEY the documented rate
# is 1 req/s. Crossref/OpenAlex are fast in the "polite pool" (mailto set).
RATE_S2_UNAUTH = 3.0
RATE_S2_AUTH = 1.0
RATE_POLITE_POOL = 0.15


def parse_bib_file(bib_path: str | Path) -> list[dict]:
    """
    Parse a .bib file into a list of entry dicts.

    Each dict has keys: 'key', 'type', 'title', 'author', 'year', and
    all other fields present in the entry.
    """
    bib_path = Path(bib_path)
    return parse_bib_content(bib_path.read_text(encoding="utf-8"))


def parse_bib_content(text: str) -> list[dict]:
    """
    Same as parse_bib_file, but from an in-memory BibTeX string —
    the entry point for callers that already have the content (e.g. a
    service that received it over HTTP rather than reading it off disk).
    """
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


def enrich_bib_entries(
    bib_path: str | Path,
    cache_path: str | Path | None = None,
    *,
    force_refresh: bool = False,
    retry_failed: bool = True,
    mailto: str | None = None,
    s2_api_key: str | None = None,
) -> list[dict]:
    """
    Enrich .bib entries with abstracts via the DOI/arXiv/title cascade
    in pipeline/resolvers.py.

    Args:
        bib_path: Path to the .bib file.
        cache_path: Path to the per-entry cache (JSON). Entries already
            resolved with the current resolver version and a
            non-retryable status are reused without a network call;
            everything else is (re-)fetched. Pass None to always fetch.
        force_refresh: Re-resolve every entry, ignoring the cache.
        retry_failed: If False, don't retry entries the cache already
            marked as retryable (unresolved/fetch_error/etc) — useful
            to avoid hammering a provider that's currently down.
        mailto: Contact email for Crossref/OpenAlex/Unpaywall's polite
            pool. Falls back to CONTACT_EMAIL in the environment.
        s2_api_key: Semantic Scholar API key. Falls back to
            S2_API_KEY in the environment. Unauthenticated works too,
            just slower and shared with everyone else.

    Returns:
        List of entry dicts (bib order), each with 'abstract' (empty
        string if none found) and an 'evidence' block recording how
        the resolution went — see resolvers.py / evidence_cache.py.
    """
    bib_entries = parse_bib_file(bib_path)
    return enrich_entries(
        bib_entries,
        cache_path,
        force_refresh=force_refresh,
        retry_failed=retry_failed,
        mailto=mailto,
        s2_api_key=s2_api_key,
    )


def enrich_entries(
    bib_entries: list[dict],
    cache_path: str | Path | None = None,
    *,
    force_refresh: bool = False,
    retry_failed: bool = True,
    mailto: str | None = None,
    s2_api_key: str | None = None,
    progress: Callable | None = None,
) -> list[dict]:
    """
    Same as enrich_bib_entries, but takes already-parsed entries instead
    of a bib path — the entry point for callers holding bib content
    in-memory (e.g. a service that received it over HTTP).

    Args:
        progress: optional callback invoked as progress(done, total, key,
            status) after each entry (cached or freshly resolved), for
            callers that want to report incremental status (e.g. a job's
            polling endpoint) rather than waiting for the full list.
        (all other args: see enrich_bib_entries)
    """
    mailto = mailto or os.getenv("CONTACT_EMAIL")
    s2_api_key = s2_api_key or os.getenv("S2_API_KEY")
    openalex_api_key = os.getenv("OPENALEX_API_KEY")

    cfg = {"mailto": mailto, "s2_api_key": s2_api_key, "openalex_api_key": openalex_api_key}
    rate_delay = RATE_S2_AUTH if s2_api_key else RATE_S2_UNAUTH

    cached_entries = evidence_cache.load_cache(cache_path) if cache_path else {}

    print(f"\n   🔍 Enriching {len(bib_entries)} bib entries "
          f"(DOI → arXiv → title search cascade)...")
    if not s2_api_key:
        print(f"      ℹ️ No S2_API_KEY set — using conservative unauthenticated "
              f"pacing ({RATE_S2_UNAUTH}s/req). See METRICS_CONTRACT prerequisites.")

    status_counts: dict[str, int] = {}
    enriched = []

    with httpx.Client() as client:
        for i, entry in enumerate(bib_entries):
            key = entry.get("key", f"entry_{i}")
            cached = cached_entries.get(key)

            needs_fetch = evidence_cache.needs_refetch(cached, force=force_refresh)
            if needs_fetch and cached is not None and not retry_failed:
                needs_fetch = False  # respect retry_failed=False for already-attempted entries

            if not needs_fetch and cached is not None:
                result = cached
                print(f"      [{i+1}/{len(bib_entries)}] {key}: cached "
                      f"({cached.get('evidence', {}).get('status', 'unknown')})")
            else:
                result = resolve_entry(entry, client, cfg)
                result["evidence"]["fetched_at"] = datetime.now(timezone.utc).isoformat()
                status = result["evidence"]["status"]
                src = result["evidence"]["source"] or "none"
                print(f"      [{i+1}/{len(bib_entries)}] {key}: {status} via {src}")
                if cache_path:
                    evidence_cache.save_entry(cache_path, key, result)
                time.sleep(rate_delay if result["evidence"].get("attempts") else 0.05)

            status_counts[result["evidence"]["status"]] = status_counts.get(result["evidence"]["status"], 0) + 1
            enriched.append(result)
            if progress:
                progress(i + 1, len(bib_entries), key, result["evidence"]["status"])

    found_count = sum(1 for e in enriched if e.get("abstract"))
    print(f"\n   ✅ Enriched {found_count}/{len(bib_entries)} entries with abstracts "
          f"({found_count / len(bib_entries):.0%})" if bib_entries else "")
    print(f"      Status breakdown: {status_counts}")

    return enriched
