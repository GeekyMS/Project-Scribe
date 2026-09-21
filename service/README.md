# Scribe Extension Service

Local HTTP service wrapping the Project Scribe pipeline (generation +
citation/evidence checking) for the Overleaf extension. Runs on your own
machine; your `OPENROUTER_API_KEY` stays in `.env` and is never sent to
the browser.

## Run it

From the repo root (required — pipeline modules' `load_dotenv()` resolves
`.env` relative to the current working directory):

```bash
./my_venv/bin/uvicorn service.app:app --port 8765
```

Visit `http://127.0.0.1:8765/docs` for FastAPI's interactive API explorer —
you can exercise both endpoints by hand from there without the extension.

## Endpoints

- `GET /health` — `{ok, has_key}`. `has_key` reflects whether
  `OPENROUTER_API_KEY` is set; the extension should show connected/offline
  based on this before attempting a job.
- `POST /jobs/generate` — `{tex_source, bib_content, target_word_count?, model?}`
  → `{job_id}`. Builds the generation prompt from enriched bib entries and
  calls the model directly (does not use `generate_related_work()`, which
  is path-based and writes eight files per call).
- `POST /jobs/check` — `{text, bib_content}` → `{job_id}`. Runs Tier 1
  (`evaluate_citations`) and Tier 4 (`evaluate_evidence_support`) against
  the given text.
- `GET /jobs/{job_id}` — `{status, progress, result, error}`. Poll this
  until `status` is `done` or `error`. `progress.stage` tracks
  `parsing_bib` → `enriching_references` (the slow part — ~3s per
  *uncached* entry with no `S2_API_KEY`; a bib full of unresolvable
  entries re-pays this every run, since only successfully-resolved
  entries are cached as non-retryable) → `checking_citation_keys` /
  `checking_evidence_support` / `generating` → `done`.

Both job types run in the same 4-worker thread pool (`service/jobs.py`) —
in-memory, no persistence across restarts, appropriate for one local user.

## Reused pipeline surface

- `pipeline.abstract_fetcher.parse_bib_content` / `enrich_entries` — the
  content-based cores added alongside the existing path-based
  `parse_bib_file` / `enrich_bib_entries` (unchanged, still used elsewhere).
- `pipeline.evaluator.evaluate_citations` / `evaluate_evidence_support` —
  both gained an optional `bib_entries` param to skip re-parsing a path
  when the caller already has entries in memory. Existing callers using
  `bib_path` are unaffected.
- `pipeline.generator.client` / `DEFAULT_MODEL` / `MAX_RETRIES` /
  `RETRY_DELAY` — reused directly; `service/tasks.py` builds the prompt via
  `pipeline.gen_prompts.build_generation_prompt` and owns the OpenRouter
  call itself, to avoid `generate_related_work()`'s eight-file-per-call
  side effects.

One shared enrichment cache at `outputs/service/enrichment_cache.json`
(atomic per-entry writes via the existing `pipeline.evidence_cache`) so
repeat requests on the same bibliography don't re-pay retrieval latency —
except for entries that never resolve, which stay retryable by design
(see `evidence_cache.needs_refetch`) and get re-attempted every run.

**Fixed 2026-09-20 — the cache was inert until now.** The note below
(under the reproducibility check) recorded that a run with a warm cache
"did anyway" re-fetch live and called it harmless. It was not a quirk:
`needs_refetch` also returned True whenever `evidence.full_text.status`
was `"not_attempted"`, which `resolvers.resolve_entry` sets
unconditionally and the abstract-enrichment path never updates. So every
cached entry looked stale and **no cache hit was ever possible**, in
this service or in the extension's IndexedDB port of it. Surfaced when
the extension began hitting provider rate limits and 15-minute job
timeouts on a 64-entry bibliography that it was re-fetching from scratch
on every single Check.

## Manual smoke test

```bash
curl -s http://127.0.0.1:8765/health

curl -s -X POST http://127.0.0.1:8765/jobs/check \
  -H 'Content-Type: application/json' \
  -d "{\"text\": $(python3 -c 'import json;print(json.dumps(open("extension/test-fixtures/long_test.tex").read()))'), \
       \"bib_content\": $(python3 -c 'import json;print(json.dumps(open("extension/test-fixtures/refs.bib").read()))')}"
# -> {"job_id": "..."}

curl -s http://127.0.0.1:8765/jobs/<job_id>   # poll until status is "done"
```

Verified end to end (2026-09-04) against `extension/test-fixtures/`:
`/jobs/check`'s Tier 1 output matched a direct `evaluate_citations()` call
exactly (recall/precision 0.9167, `hallucinated_keys: ["wong2024fake"]`,
`missing_keys: ["fournier2015spans"]`); `/jobs/generate` produced real
LaTeX with correctly-cited `\cite{}` keys pulled from the bib in ~11s for a
small smoke-test paper.

## Reproducibility check against a real prior campaign run (2026-09-05)

Ran `/jobs/check` on a real paper already scored by `run_validation_campaign.py`
(`research_test_with_phd/2503.07928v3` → `outputs/phase2_campaign/2503.07928v3/
evaluation_report_2503.07928v3.json`), using that paper's real generated
`.tex` and `.bib`, with the existing per-paper `enrichment_cache.json`
merged into the service's cache so enrichment didn't have to re-resolve
live (it did anyway — the cached statuses turned out non-retryable-eligible
in a way that still triggered a live re-fetch; harmless, just re-confirms
the same resolver outcome rather than reusing it).

**Result: every field matched exactly** — Tier 1 (`bib_keys`, `cited_keys`,
`hallucinated_keys`, `missing_keys`, recall/precision, citation counts) and
Tier 4 (`total_spans`, `verifiable_count`, `evidence_coverage`,
`support_rate`, `verdict_counts`, `unverifiable_reasons`), including every
individual per-citation verdict across all 15 citation mentions. This is
the plan's verification step 5 (service output should match
`run_validation_campaign.py` on the same input) — confirmed: the service
is not a fork of the evaluated pipeline's behavior, it's the same
computation behind an HTTP job.
