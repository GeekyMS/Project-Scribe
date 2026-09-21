"""
Per-entry, repairable enrichment cache.

Replaces the all-or-nothing list-JSON caches previously written by
abstract_fetcher.enrich_bib_entries() and
full_text_fetcher.fetch_and_extract_full_texts(): if the cache file
existed, it was returned verbatim and nothing was re-fetched — so one
run with zero successful lookups permanently poisoned every future
run against that bib.

This module stores entries keyed by bib key, in
{"version": 2, "entries": {key: {...}}}, and writes to disk after
every entry so an interrupted run keeps its progress. A legacy
top-level-list cache (version 1, the old format) is detected on load
and migrated: each item is wrapped with resolver_version "0" and
marked retryable rather than trusted, so existing poisoned caches
heal on the next run instead of needing manual deletion.
"""

import json
from pathlib import Path

CACHE_VERSION = 2

# Statuses that mean "try again" rather than "trust this result" —
# used by needs_refetch() to decide whether a cached entry should be
# re-resolved even though a cache file exists.
_RETRYABLE_STATUSES = {
    "unresolved",
    "not_found",
    "fetch_error",
    "error",
}


def _wrap_legacy_entry(item: dict) -> dict:
    """
    Wrap a v1 (bare enriched-entry) cache item in the v2 shape.

    v1 entries have no `evidence` block — they're just the bib fields
    plus `abstract` / `full_text` / `semantic_scholar_id`. Infer a
    coarse status from whether an abstract was found, but mark the
    resolver version "0" so needs_refetch() treats it as retryable:
    a v1 "found" entry was never title-gated or DOI-checked, so it
    should not be trusted as-is.
    """
    item = dict(item)  # don't mutate caller's dict
    has_abstract = bool(item.get("abstract"))
    item["evidence"] = {
        "status": "matched_title" if has_abstract else "unresolved",
        "source": "semantic_scholar" if has_abstract else None,
        "matched_by": "title_search" if has_abstract else None,
        "matched_title": "",
        "title_similarity": None,
        "year_matched": None,
        "external_ids": {},
        "abstract_chars": len(item.get("abstract", "")),
        "full_text": {
            "status": "ok" if item.get("full_text") else "not_attempted",
            "source_url": "",
            "chars": len(item.get("full_text", "")),
            "error": None,
        },
        "attempts": [],
        "fetched_at": None,
        "resolver_version": "0",
    }
    return item


def load_cache(path: str | Path) -> dict[str, dict]:
    """
    Load a cache file, migrating a legacy v1 list format in place.

    Returns a dict keyed by bib entry key. Returns {} if the file
    doesn't exist. Does not write anything back — callers should
    call save_entry() as they process entries, which upgrades the
    on-disk file to v2 incrementally.
    """
    path = Path(path)
    if not path.exists():
        return {}

    raw = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(raw, list):
        # Legacy v1: a bare list of enriched entries.
        entries = {}
        for i, item in enumerate(raw):
            key = item.get("key", f"entry_{i}")
            entries[key] = _wrap_legacy_entry(item)
        return entries

    if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
        return raw.get("entries", {})

    # Unknown shape — treat as empty rather than crash; the caller
    # will re-fetch everything, which is the safe fallback.
    return {}


def save_entry(path: str | Path, key: str, entry: dict) -> None:
    """
    Write/update one entry in the cache and persist to disk.

    Reads the current on-disk cache, updates one key, writes the
    whole file back atomically (temp file + rename). Doing this per
    entry — rather than once at the end of a run — is what makes the
    cache repairable: an interrupted run keeps every entry resolved
    so far instead of losing all of them.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    entries = load_cache(path)
    entries[key] = entry

    payload = {"version": CACHE_VERSION, "entries": entries}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    tmp_path.replace(path)


def save_all(path: str | Path, entries: dict[str, dict]) -> None:
    """Write a full entries dict at once (e.g. after migration)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "entries": entries}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    tmp_path.replace(path)


def needs_refetch(cached: dict | None, *, force: bool = False) -> bool:
    """
    Decide whether a cached entry should be re-resolved.

    True if there is no cached entry, if force is set, if the entry
    predates the current resolver (so it was never title-gated /
    DOI-checked), or if its status is in the retryable set.

    Deliberately does NOT consider evidence["full_text"]["status"].
    This function has exactly one caller —
    abstract_fetcher.enrich_entries — and that path resolves *abstracts*;
    full_text_fetcher.py does its own cache accounting and never calls
    here. The previous version returned True whenever full text was
    "not_attempted", which resolve_entry() sets unconditionally and the
    abstract path never changes, so every cached entry looked stale and
    the cache never hit. service/README.md recorded the symptom ("the
    cached statuses turned out non-retryable-eligible in a way that
    still triggered a live re-fetch; harmless") without spotting that
    the cache was simply inert — it is not harmless when the provider
    starts rate-limiting.
    """
    if force or cached is None:
        return True

    evidence = cached.get("evidence", {})
    if evidence.get("resolver_version") != "1":
        return True
    if evidence.get("status") in _RETRYABLE_STATUSES:
        return True
    return False
