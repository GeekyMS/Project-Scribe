"""
Multi-provider paper resolution — DOI/arXiv-first, title-search last.

Root cause of the 16%-abstract / 0%-full-text coverage this replaces:
abstract_fetcher.py resolved by arXiv eprint, then fell back to a
Semantic Scholar title search that took papers[0] with no similarity
check, discarded externalIds/matched title (making wrong matches
unauditable), and never used the `doi` field bib entries already
carry.

This module is pure lookup — no caching, no retry-sleep loop beyond
one 429 backoff per call. Every function returns raw-ish provider
dicts plus a `_source` tag; resolve_entry() is the cascade that turns
those into one auditable decision per bib entry.

No API key is required for any provider here — everything degrades to
conservative unauthenticated rate limits and (for OpenAlex) a small
daily credit budget if no key is configured. Set S2_API_KEY,
OPENALEX_API_KEY, and CONTACT_EMAIL (used as Crossref/OpenAlex/
Unpaywall's polite-pool `mailto`) in .env to raise the ceiling —
nothing here breaks without them.
"""

import difflib
import os
import re
import time

import httpx

S2_API = "https://api.semanticscholar.org/graph/v1"
CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2"

S2_FIELDS = "paperId,title,abstract,year,authors,externalIds"

# Title-similarity thresholds. The bib is LLM-generated from OCR'd
# reference strings, so identifiers can themselves be wrong — these
# gates exist so a near-miss title doesn't silently attach the wrong
# paper's abstract.
SIM_THRESHOLD_ARXIV = 0.75    # eprint-id lookups: ID could be wrong
SIM_THRESHOLD_TITLE = 0.85    # title search: no identifier at all
SIM_THRESHOLD_TITLE_YEAR_MATCH = 0.75  # relaxed if year also matches


# ── Title similarity ────────────────────────────────────────────────

_NORM_RE = re.compile(r'[^a-z0-9\s]')
_LATEX_CMD_RE = re.compile(r'\\[a-zA-Z]+\{([^}]*)\}')


def norm_title(s: str) -> str:
    """Lowercase, strip LaTeX commands/braces and punctuation, collapse whitespace."""
    if not s:
        return ""
    s = _LATEX_CMD_RE.sub(r'\1', s)
    s = s.replace('{', '').replace('}', '')
    s = s.lower()
    s = _NORM_RE.sub(' ', s)
    return ' '.join(s.split())


def title_sim(a: str, b: str) -> float:
    """Similarity in [0, 1]: max of sequence-ratio and token-set Jaccard."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    seq_ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return max(seq_ratio, jaccard)


# ── HTTP helper ──────────────────────────────────────────────────────

def _get_json(
    url: str,
    params: dict,
    client: httpx.Client,
    *,
    provider: str,
    headers: dict | None = None,
    timeout: float = 15.0,
) -> tuple[dict | None, int | None, str | None]:
    """
    GET with one 429 backoff retry. Never swallows failures silently —
    returns (data, http_status, error) so callers can log what
    actually happened instead of collapsing every failure to None.
    """
    try:
        resp = client.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code == 429:
            time.sleep(10)
            resp = client.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return None, resp.status_code, f"HTTP {resp.status_code}"
        return resp.json(), resp.status_code, None
    except Exception as e:
        return None, None, str(e)


# ── Semantic Scholar ─────────────────────────────────────────────────

def s2_by_id(idtype: str, ident: str, client: httpx.Client, *, api_key: str | None = None) -> dict | None:
    """idtype in {'DOI', 'ARXIV', 'CorpusId'}."""
    headers = {"x-api-key": api_key} if api_key else None
    data, status, err = _get_json(
        f"{S2_API}/paper/{idtype}:{ident}", {"fields": S2_FIELDS}, client,
        provider="semantic_scholar", headers=headers,
    )
    if data:
        data["_source"] = "semantic_scholar"
    return data


def s2_search(title: str, client: httpx.Client, *, limit: int = 5, api_key: str | None = None) -> list[dict]:
    headers = {"x-api-key": api_key} if api_key else None
    data, status, err = _get_json(
        f"{S2_API}/paper/search",
        {"query": title, "limit": limit, "fields": S2_FIELDS},
        client, provider="semantic_scholar", headers=headers,
    )
    papers = (data or {}).get("data", [])
    for p in papers:
        p["_source"] = "semantic_scholar"
    return papers


# ── Crossref ─────────────────────────────────────────────────────────

def crossref_by_doi(doi: str, client: httpx.Client, *, mailto: str | None = None) -> dict | None:
    params = {"mailto": mailto} if mailto else {}
    data, status, err = _get_json(
        f"{CROSSREF_API}/{doi}", params, client, provider="crossref",
    )
    msg = (data or {}).get("message")
    if msg:
        msg["_source"] = "crossref"
    return msg


def crossref_search(title: str, client: httpx.Client, *, limit: int = 5, mailto: str | None = None) -> list[dict]:
    params = {"query.bibliographic": title, "rows": limit}
    if mailto:
        params["mailto"] = mailto
    data, status, err = _get_json(CROSSREF_API, params, client, provider="crossref")
    items = ((data or {}).get("message") or {}).get("items", [])
    for it in items:
        it["_source"] = "crossref"
    return items


def _crossref_title(item: dict) -> str:
    titles = item.get("title") or []
    return titles[0] if titles else ""


def _title_of(item: dict) -> str:
    """
    Extract a plain-string title regardless of provider shape.
    Crossref's `title` field is a list; S2/OpenAlex use a plain string
    (OpenAlex: `display_name` / `title`).
    """
    source = item.get("_source")
    if source == "crossref":
        return _crossref_title(item)
    t = item.get("title")
    if isinstance(t, list):
        return t[0] if t else ""
    return t or item.get("display_name") or ""


# ── OpenAlex ─────────────────────────────────────────────────────────

def invert_abstract(inverted_index: dict | None) -> str:
    """Reconstruct plaintext from OpenAlex's abstract_inverted_index."""
    if not inverted_index:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    if not positions:
        return ""
    return " ".join(positions[i] for i in sorted(positions))


def openalex_by_doi(doi: str, client: httpx.Client, *, mailto: str | None = None, api_key: str | None = None) -> dict | None:
    params = {}
    if mailto:
        params["mailto"] = mailto
    if api_key:
        params["api_key"] = api_key
    data, status, err = _get_json(
        f"{OPENALEX_API}/https://doi.org/{doi}", params, client, provider="openalex",
    )
    if data:
        data["_source"] = "openalex"
    return data


def openalex_search(title: str, client: httpx.Client, *, limit: int = 5, mailto: str | None = None, api_key: str | None = None) -> list[dict]:
    params = {"search": title, "per_page": limit}
    if mailto:
        params["mailto"] = mailto
    if api_key:
        params["api_key"] = api_key
    data, status, err = _get_json(OPENALEX_API, params, client, provider="openalex")
    results = (data or {}).get("results", [])
    for r in results:
        r["_source"] = "openalex"
    return results


# ── Unpaywall (OA PDF only) ─────────────────────────────────────────

def unpaywall_oa_pdf(doi: str, email: str, client: httpx.Client) -> str | None:
    if not email:
        return None
    data, status, err = _get_json(
        f"{UNPAYWALL_API}/{doi}", {"email": email}, client, provider="unpaywall",
    )
    if not data:
        return None
    best = data.get("best_oa_location") or {}
    return best.get("url_for_pdf") or best.get("url")


# ── Resolution cascade ───────────────────────────────────────────────

def _extract_doi_or_arxiv_from_url(url: str) -> tuple[str | None, str | None]:
    """Best-effort DOI/arXiv-id extraction from a bib `url` field."""
    if not url:
        return None, None
    doi_match = re.search(r'10\.\d{4,9}/[^\s"\'<>]+', url)
    if doi_match:
        return doi_match.group(0).rstrip('.,;'), None
    arxiv_match = re.search(r'arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$', url)
    if arxiv_match:
        return None, arxiv_match.group(1)
    return None, None


def resolve_entry(entry: dict, client: httpx.Client, cfg: dict) -> dict:
    """
    Resolve one bib entry to (abstract, full_text-ready metadata,
    evidence provenance), stopping at the first accepted match.

    cfg keys (all optional): s2_api_key, openalex_api_key, mailto.

    Returns a dict with the ORIGINAL entry fields plus:
      - abstract: str (unchanged contract — "" if none)
      - evidence: dict — see evidence_cache / resolvers docstrings
    `full_text` is NOT set here — that's full_text_fetcher's job, run
    only for entries this function marks resolved.
    """
    title = entry.get("title", "")
    doi = (entry.get("doi") or "").strip()
    arxiv_id = (entry.get("eprint") or "").strip()
    url = entry.get("url", "")

    if not doi and not arxiv_id and url:
        url_doi, url_arxiv = _extract_doi_or_arxiv_from_url(url)
        doi = doi or url_doi or ""
        arxiv_id = arxiv_id or url_arxiv or ""

    s2_key = cfg.get("s2_api_key")
    oa_key = cfg.get("openalex_api_key")
    mailto = cfg.get("mailto")

    attempts = []
    result = dict(entry)

    def _accept(paper: dict, *, status: str, matched_by: str, sim: float | None, year_matched: bool | None) -> dict:
        abstract = paper.get("abstract") or invert_abstract(paper.get("abstract_inverted_index"))
        result["abstract"] = abstract or ""
        external_ids = paper.get("externalIds") or {}
        # Normalize OpenAlex/Crossref id shapes into the same bucket.
        if paper.get("_source") == "openalex":
            ids = paper.get("ids", {})
            external_ids = {
                "DOI": (ids.get("doi") or "").replace("https://doi.org/", ""),
                "OpenAlex": ids.get("openalex"),
            }
        elif paper.get("_source") == "crossref":
            external_ids = {"DOI": paper.get("DOI")}

        # Backfill: Crossref frequently resolves the right paper but
        # rarely carries an abstract (publisher-dependent deposit).
        # Rather than lose the abstract just because Crossref was the
        # best title match, re-query S2/OpenAlex by the now-known DOI.
        backfill_source = None
        if not result["abstract"]:
            backfill_doi = external_ids.get("DOI")
            if backfill_doi:
                for backfill_fn, backfill_kwargs, backfill_src in [
                    (s2_by_id, dict(idtype="DOI", ident=backfill_doi, client=client, api_key=s2_key), "semantic_scholar"),
                    (openalex_by_doi, dict(doi=backfill_doi, client=client, mailto=mailto, api_key=oa_key), "openalex"),
                ]:
                    if backfill_src == paper.get("_source"):
                        continue  # already tried this provider above
                    try:
                        alt = backfill_fn(**backfill_kwargs)
                    except Exception as e:
                        attempts.append({"provider": backfill_src, "method": "abstract_backfill", "error": str(e)})
                        continue
                    alt_abstract = (alt or {}).get("abstract") or invert_abstract((alt or {}).get("abstract_inverted_index")) if alt else ""
                    attempts.append({"provider": backfill_src, "method": "abstract_backfill", "n_results": 1 if alt else 0, "found_abstract": bool(alt_abstract)})
                    if alt_abstract:
                        result["abstract"] = alt_abstract
                        backfill_source = backfill_src
                        break

        matched_title = _title_of(paper)
        result["evidence"] = {
            "status": status,
            "source": paper.get("_source"),
            "matched_by": matched_by,
            "matched_title": matched_title,
            "title_similarity": sim,
            "year_matched": year_matched,
            "external_ids": external_ids,
            "abstract_chars": len(result["abstract"]),
            "abstract_backfilled_from": backfill_source,
            "full_text": {
                "status": "not_attempted", "source_url": "", "chars": 0, "error": None,
            },
            "attempts": attempts,
            "fetched_at": None,  # set by caller with a real timestamp
            "resolver_version": "1",
        }
        return result

    def _reject_all() -> dict:
        result["abstract"] = ""
        result["evidence"] = {
            "status": "unresolved",
            "source": None, "matched_by": None, "matched_title": "",
            "title_similarity": None, "year_matched": None,
            "external_ids": {}, "abstract_chars": 0,
            "full_text": {"status": "not_attempted", "source_url": "", "chars": 0, "error": None},
            "attempts": attempts, "fetched_at": None, "resolver_version": "1",
        }
        return result

    # 1. DOI — exact identifier, accepted unconditionally, sim logged for audit
    if doi:
        for fn, kwargs, source in [
            (s2_by_id, dict(idtype="DOI", ident=doi, client=client, api_key=s2_key), "semantic_scholar"),
            (crossref_by_doi, dict(doi=doi, client=client, mailto=mailto), "crossref"),
            (openalex_by_doi, dict(doi=doi, client=client, mailto=mailto, api_key=oa_key), "openalex"),
        ]:
            try:
                paper = fn(**kwargs)
            except Exception as e:
                attempts.append({"provider": source, "method": "doi", "error": str(e)})
                continue
            if paper:
                sim = title_sim(title, _title_of(paper))
                attempts.append({"provider": source, "method": "doi", "http_status": 200, "n_results": 1, "best_similarity": sim})
                status = "matched_doi" if sim >= 0.5 or not title else "matched_low_confidence"
                return _accept(paper, status=status, matched_by="doi", sim=sim, year_matched=None)
            attempts.append({"provider": source, "method": "doi", "n_results": 0})

    # 2. arXiv eprint — gated, since OCR'd bib entries can carry a wrong id
    if arxiv_id:
        try:
            paper = s2_by_id("ARXIV", arxiv_id, client, api_key=s2_key)
        except Exception as e:
            paper = None
            attempts.append({"provider": "semantic_scholar", "method": "arxiv", "error": str(e)})
        if paper:
            sim = title_sim(title, paper.get("title", ""))
            attempts.append({"provider": "semantic_scholar", "method": "arxiv", "http_status": 200, "n_results": 1, "best_similarity": sim})
            if sim >= SIM_THRESHOLD_ARXIV or not title:
                return _accept(paper, status="matched_arxiv", matched_by="arxiv", sim=sim, year_matched=None)
        else:
            attempts.append({"provider": "semantic_scholar", "method": "arxiv", "n_results": 0})

    # 3. Title search across providers — best-similarity match wins, not papers[0]
    if title:
        year = str(entry.get("year", "")).strip()
        best = None
        best_sim = 0.0
        best_source = None
        for fn, kwargs, source in [
            (s2_search, dict(title=title, client=client, api_key=s2_key), "semantic_scholar"),
            (crossref_search, dict(title=title, client=client, mailto=mailto), "crossref"),
            (openalex_search, dict(title=title, client=client, mailto=mailto, api_key=oa_key), "openalex"),
        ]:
            try:
                candidates = fn(**kwargs)
            except Exception as e:
                attempts.append({"provider": source, "method": "title_search", "error": str(e)})
                continue
            local_best_sim = 0.0
            for c in candidates:
                c_title = _title_of(c)
                sim = title_sim(title, c_title)
                local_best_sim = max(local_best_sim, sim)
                if sim > best_sim:
                    best, best_sim, best_source = c, sim, source
            attempts.append({"provider": source, "method": "title_search", "n_results": len(candidates), "best_similarity": local_best_sim})

        if best is not None:
            year_matched = None
            if year:
                c_year = str(best.get("year") or best.get("published-print", {}).get("date-parts", [[None]])[0][0] or best.get("publication_year") or "")
                year_matched = bool(c_year) and abs(_safe_int(c_year, 0) - _safe_int(year, 0)) <= 1

            if best_sim >= SIM_THRESHOLD_TITLE or (best_sim >= SIM_THRESHOLD_TITLE_YEAR_MATCH and year_matched):
                status = "matched_title" if best_sim >= SIM_THRESHOLD_TITLE else "matched_fuzzy"
                return _accept(best, status=status, matched_by="title_search", sim=best_sim, year_matched=year_matched)
            elif best_sim >= 0.5:
                # Kept, but excluded from Tier 4 evidence by default — see
                # evaluate_evidence_support()'s low_confidence_match handling.
                return _accept(best, status="matched_low_confidence", matched_by="title_search", sim=best_sim, year_matched=year_matched)

    return _reject_all()


def _safe_int(s, default):
    try:
        return int(str(s)[:4])
    except (ValueError, TypeError):
        return default
