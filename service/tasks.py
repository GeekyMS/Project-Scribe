"""
The actual pipeline work each job runs. Kept separate from jobs.py (generic
job machinery) and app.py (HTTP layer) so each stays easy to read on its own.

Every function here takes a `Job` and mutates job.progress as it goes,
setting job.result on success. Exceptions propagate to jobs.create_job's
wrapper, which records them as job.error — these functions don't need
their own try/except for that.
"""

import re
import time
from pathlib import Path

from openai import OpenAI

from pipeline.abstract_fetcher import parse_bib_content, enrich_entries
from pipeline.evaluator import evaluate_citations, evaluate_evidence_support
from pipeline.gen_prompts import build_generation_prompt
from pipeline.generator import client as generation_client, DEFAULT_MODEL, MAX_RETRIES, RETRY_DELAY

from .jobs import Job

# Shared across requests so repeat runs on the same bib don't re-pay
# retrieval latency (RATE_S2_UNAUTH = 3s/uncached entry — see
# pipeline/abstract_fetcher.py). One file, atomic per-entry writes via
# pipeline/evidence_cache.py.
ENRICHMENT_CACHE_PATH = Path(__file__).resolve().parent.parent / "outputs" / "service" / "enrichment_cache.json"
ENRICHMENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _enrich_with_progress(job: Job, bib_entries: list[dict], stage: str) -> list[dict]:
    total = len(bib_entries)

    def progress(done, total_, key, status):
        job.progress = {"stage": stage, "done": done, "total": total_, "current_key": key}

    job.progress = {"stage": stage, "done": 0, "total": total}
    return enrich_entries(bib_entries, ENRICHMENT_CACHE_PATH, progress=progress)


def _parse_dual_output(raw: str) -> tuple[str, str]:
    """Mirrors pipeline/generator.py::_parse_dual_output (not exported there)."""
    separator = "---FORMAT_SEPARATOR---"
    if separator in raw:
        parts = raw.split(separator, 1)
        latex_part = re.sub(r'^(?:FORMAT\s*1\s*\(?LaTeX\)?\s*:?\s*)', '', parts[0].strip(), flags=re.IGNORECASE).strip()
        md_part = re.sub(r'^(?:FORMAT\s*2\s*\(?Markdown\)?\s*:?\s*)', '', parts[1].strip(), flags=re.IGNORECASE).strip()
        return latex_part, md_part
    if "\\cite{" in raw:
        return raw.strip(), ""
    return "", raw.strip()


def run_generate_job(
    job: Job,
    tex_source: str,
    bib_content: str,
    target_word_count: int | None = None,
    model: str | None = None,
) -> None:
    model = model or DEFAULT_MODEL

    job.progress = {"stage": "parsing_bib", "done": 0, "total": 1}
    bib_entries = parse_bib_content(bib_content)

    enriched_entries = _enrich_with_progress(job, bib_entries, "enriching_references")

    job.progress = {"stage": "building_prompt", "done": 0, "total": 1}
    prompt = build_generation_prompt(
        paper_text=tex_source,
        bib_content=bib_content,
        enriched_entries=enriched_entries,
        target_word_count=target_word_count,
    )

    job.progress = {"stage": "generating", "done": 0, "total": MAX_RETRIES}
    start_time = time.time()
    raw = None
    usage = None
    for attempt in range(1, MAX_RETRIES + 1):
        job.progress = {"stage": "generating", "done": attempt - 1, "total": MAX_RETRIES}
        response = generation_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=300.0,
        )
        candidate = response.choices[0].message.content
        if candidate is None:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            continue
        raw = re.sub(r'<think>.*?</think>', '', candidate, flags=re.DOTALL)
        usage = response.usage
        break

    if raw is None:
        raise RuntimeError(f"generation produced an empty response after {MAX_RETRIES} attempts")

    latex_output, markdown_output = _parse_dual_output(raw)
    job.progress = {"stage": "done", "done": 1, "total": 1}
    job.result = {
        "model": model,
        "latex_output": latex_output,
        "markdown_output": markdown_output,
        "raw_output": raw,
        "prompt_tokens": (usage.prompt_tokens or 0) if usage else 0,
        "completion_tokens": (usage.completion_tokens or 0) if usage else 0,
        "total_tokens": (usage.total_tokens or 0) if usage else 0,
        "generation_time_s": time.time() - start_time,
        "enriched_entries": enriched_entries,
    }


def run_check_job(job: Job, text: str, bib_content: str) -> None:
    job.progress = {"stage": "parsing_bib", "done": 0, "total": 1}
    bib_entries = parse_bib_content(bib_content)

    job.progress = {"stage": "checking_citation_keys", "done": 0, "total": 1}
    tier1 = evaluate_citations(text, bib_entries=bib_entries)

    enriched_entries = _enrich_with_progress(job, bib_entries, "enriching_references")

    job.progress = {"stage": "checking_evidence_support", "done": 0, "total": 1}
    tier4 = evaluate_evidence_support(
        text,
        None,
        enriched_entries,
        bib_entries=bib_entries,
    )

    job.progress = {"stage": "done", "done": 1, "total": 1}
    job.result = {
        "tier_1_citation_metrics": tier1.to_dict(),
        "tier_4_evidence_support": tier4.to_dict(),
    }
