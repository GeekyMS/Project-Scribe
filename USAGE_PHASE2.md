# Project Scribe Phase 2 — Usage Guide

This document covers how to run every aspect of the Related Works generation and evaluation pipeline.

---

## Prerequisites

Make sure you have the base dependencies installed:
```bash
pip install pymupdf openai httpx python-dotenv
```

Optional (for Tier 3 text similarity metrics):
```bash
pip install rouge-score bert-score
```

Your `.env` file must contain:
```
OPENROUTER_API_KEY=your_key_here
```

---

## Quick Start — Full Pipeline

Generate a Related Works section and evaluate it against the original:

```bash
python generate_related_work.py \
    --paper outputs/paper_without_related.md \
    --bib outputs/references.bib \
    --ground-truth outputs/related_work.txt
```

This will:
1. Enrich references with abstracts from Semantic Scholar (cached after first run)
2. Generate the Related Works section using Claude Sonnet 4
3. Run all three evaluation tiers (citation metrics, LLM-as-judge, text similarity)
4. Save everything to `outputs/generation/`

---

## Running with Multiple Models

Compare outputs across different LLMs:

```bash
python generate_related_work.py \
    --paper outputs/paper_without_related.md \
    --bib outputs/references.bib \
    --ground-truth outputs/related_work.txt \
    --model anthropic/claude-sonnet-4-20250514 \
    --model google/gemini-2.5-flash-preview-05-20 \
    --model openai/gpt-4o
```

Each model gets its own output files (e.g., `generated_related_work_claude-sonnet-4.md`, `evaluation_report_gpt-4o.json`).

---

## Testing with Web-Based LLMs (Online / Manual Mode)

This is for testing whether allowing the LLM to **search the web** and look up papers improves quality.

### Step 1: Export the Prompt

```bash
python generate_related_work.py \
    --paper outputs/paper_without_related.md \
    --bib outputs/references.bib \
    --export-prompt
```

This creates two files in `outputs/generation/`:
- `prompt_api.txt` — The full prompt (same as what gets sent via API)
- `prompt_manual_web_llm.txt` — A simplified prompt formatted for copy-paste

### Step 2: Use in a Web LLM

1. Open **ChatGPT**, **Claude**, or **Gemini** in your browser
2. Open `outputs/generation/prompt_manual_web_llm.txt`
3. Copy the entire contents and paste into the chat
4. Send the message and wait for the response
5. Copy the response and save it as a `.md` file (e.g., `outputs/generation/chatgpt_response.md`)

> **Note:** Web-based LLMs may use their search/browsing capabilities to look up the cited papers. This is intentional — comparing web LLM output vs. API output tells you whether access to the actual papers improves generation quality.

### Step 3: Evaluate the Response

```bash
python generate_related_work.py \
    --eval-only outputs/generation/chatgpt_response.md \
    --ground-truth outputs/related_work.txt \
    --paper outputs/paper_without_related.md \
    --bib outputs/references.bib
```

This runs the full evaluation (citation metrics + LLM-as-judge + text similarity) on the web LLM's output.

---

## Evaluation-Only Mode

Evaluate any existing generated Related Works file:

```bash
python generate_related_work.py \
    --eval-only path/to/generated_section.md \
    --ground-truth outputs/related_work.txt \
    --paper outputs/paper_without_related.md \
    --bib outputs/references.bib
```

### Skip expensive evaluations:

```bash
# Skip LLM-as-judge (saves API tokens)
python generate_related_work.py \
    --eval-only generated.md \
    --ground-truth outputs/related_work.txt \
    --bib outputs/references.bib \
    --no-judge

# Skip ROUGE/BERTScore
python generate_related_work.py \
    --eval-only generated.md \
    --ground-truth outputs/related_work.txt \
    --bib outputs/references.bib \
    --no-similarity
```

---

## Human Evaluation

Generate questionnaires for author review:

```bash
python generate_related_work.py \
    --eval-only outputs/generation/generated_related_work_claude-sonnet-4.md \
    --ground-truth outputs/related_work.txt \
    --human-eval
```

This produces:
- `human_eval_blind_ab.md` — **Blind A/B comparison**: both versions shown without labels (randomized order). The author picks which is better on each dimension.
- `human_eval_likert.md` — **Dimension scoring**: author scores the generated section on 7 dimensions (1-5 scale).
- `human_eval_schema.json` — JSON schema for structured response collection.
- `blind_ab_mapping.json` — **Secret file** revealing which version is A/B (DO NOT share with evaluator until after they complete the questionnaire).

---

## Controlling Enrichment

By default, references are enriched with abstracts from Semantic Scholar. This makes the generation better but you may want to test without it:

```bash
# Without enrichment (bib metadata only)
python generate_related_work.py \
    --paper outputs/paper_without_related.md \
    --bib outputs/references.bib \
    --ground-truth outputs/related_work.txt \
    --no-enrich
```

After the first run, abstracts are cached in `outputs/generation/enrichment_cache.json`. Subsequent runs load from cache (no API calls to Semantic Scholar).

---

## Output Files Reference

After a full run, `outputs/generation/` contains:

| File | Description |
|---|---|
| `generated_related_work_<model>.tex` | LaTeX output with `\cite{}` commands |
| `generated_related_work_<model>.md` | Markdown output with author-date citations |
| `generated_raw_<model>.txt` | Raw LLM response (both formats) |
| `evaluation_report_<model>.json` | Full three-tier evaluation report |
| `side_by_side_<model>.md` | Human-readable comparison document |
| `prompt_<model>.txt` | Exact prompt sent to the API |
| `prompt_manual_web_llm.txt` | Prompt for web LLM copy-paste |
| `generation_log_<model>.json` | Token usage, timing, metadata |
| `enrichment_cache.json` | Cached Semantic Scholar abstracts |
| `ground_truth.txt` | Copy of original Related Works |
| `human_eval_blind_ab.md` | Blind A/B questionnaire |
| `human_eval_likert.md` | Likert scoring form |
| `human_eval_schema.json` | Response collection schema |
| `blind_ab_mapping.json` | Secret A/B mapping (researcher only) |

---

## Cheat Detection / Knowledge Leakage

The pipeline includes multiple layers of cheat detection:

1. **Citation Precision (Tier 1)**: If the model cites papers NOT in the `.bib`, it used outside knowledge. This is flagged as "hallucinated citations" in the evaluation report.

2. **Knowledge Leakage (Tier 2)**: The LLM-as-judge specifically checks whether the generated text contains suspiciously detailed descriptions of cited papers — details that go beyond what's in the title/abstract/venue provided. Flagged as "minor" or "major" leakage.

3. **Full Prompt Logging**: The exact prompt sent to the model is saved in `prompt_<model>.txt`. You can inspect it to verify exactly what information the model received.

4. **Web LLM Comparison**: By comparing API output (no web access) vs. web LLM output (search enabled), you can quantify how much web access improves generation quality — and by extension, how much the API model relied on pre-training knowledge.

---

## End-to-End Example (from Phase 1)

If you're starting from a raw PDF and don't have Phase 1 outputs yet:

```bash
# Step 1: Run Phase 1 extraction
python process_paper_v4.py path/to/paper.pdf

# Step 2: Run Phase 2 generation + evaluation
python generate_related_work.py \
    --paper outputs/paper_without_related.md \
    --bib outputs/references.bib \
    --ground-truth outputs/related_work.txt \
    --human-eval
```
