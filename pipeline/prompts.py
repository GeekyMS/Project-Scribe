"""
All LLM prompts for the pipeline, centralized for easy tuning.
"""

import json


def build_extraction_prompt(snippet: str, correction_context: str = None) -> str:
    """
    Stage 1 — Boundary extraction prompt.
    
    Ported from process_paper_v2._build_prompt() with support for
    correction context from previous iterations.
    """
    prompt = f"""You are an assistant that extracts the Related Work section boundaries from an academic paper.

Your task: Find the section that reviews prior/related research.

STEP 1 — Look for an explicitly labeled section. It may be titled:
- "Related Work" / "Related Works"
- "Background" / "Background and Related Work"  
- "Literature Review" / "Literature Survey"
- "Previous Work" / "Prior Work" / "Prior Art"
- "State of the Art"
- Or any similar variant

STEP 2 — FALLBACK (only if Step 1 finds nothing):
Some papers (especially older or non-CS papers) do not have a dedicated Related Work section.
In that case, check the **Introduction** (or the opening section of the paper).
The Introduction qualifies as a related-work equivalent if it:
  - cites and discusses **specific prior works, systems, or approaches** by name, AND
  - critiques their limitations, compares them, or explains why they are insufficient for the problem at hand.

The Introduction does NOT qualify if it:
  - discusses only abstract mathematical principles without referencing ANY specific prior methods, systems, or techniques by name,
  - mentions prior work only in a single sentence, or
  - has no substantive discussion of what previous approaches did and why they fall short.

When in doubt about whether the discussion is substantive enough, err on the side of including it. Papers from before ~1990 often embed their related work discussion into the Introduction without a separate section — be especially lenient with older papers.

If neither Step 1 nor Step 2 yields a qualifying section, return null.

Return ONLY valid JSON with exactly one key: "related_work".

If a section is found (from Step 1 or Step 2), related_work must be an object:
{{
    "source": "explicit" or "fallback",
    "start_page": <integer - 1-based page number>,
    "end_page": <integer - 1-based page number>,
    "start_snippet": "<the first 1-2 sentences of the section (exact text from the paper)>",
    "end_snippet": "<the last 1-2 sentences of the section (exact text from the paper)>"
}}

Set "source" to "explicit" if you found a labeled section heading (Step 1).
Set "source" to "fallback" if you used the Introduction (Step 2).

If the paper does NOT discuss prior work anywhere, return:
{{
    "related_work": null,
    "null_confidence": <integer 0-100, how confident you are that there is NO related work discussion>
}}

For null_confidence:
- 95-100: Absolutely certain — paper is purely theoretical/mathematical with zero references to prior work
- 80-94: Very likely no related work — paper mentions some context but doesn't discuss prior approaches
- 60-79: Uncertain — there might be some discussion of prior work in the introduction or elsewhere
- 0-59: Low confidence — there's probably related work discussion but it doesn't fit the standard patterns

CRITICAL RULES:
- The text contains explicit page markers in the format "--- PAGE X ---". Use these markers to determine the correct page numbers.
- start_snippet and end_snippet must be EXACT text copied from the paper — do not paraphrase.
- Only choose sentences from the main body text. Avoid page numbers, headers, footers, or watermarks like 'Preprint'.
- The section starts AFTER the section heading and ends BEFORE the next major section heading.
- If the text provided is only a portion of the paper and you cannot find the section, return null.
- If you use the Introduction fallback, the start_snippet should be the first sentence of the Introduction, and the end_snippet should be the LAST sentence of the Introduction (i.e., the sentence just before the next major section heading begins). Include the entire Introduction — do not try to trim it to only the sentences that mention prior work."""

    if correction_context:
        prompt += f"""

═══════════════════════════════════════════════════════════════
IMPORTANT — CORRECTION CONTEXT FROM A PREVIOUS ATTEMPT:
═══════════════════════════════════════════════════════════════
{correction_context}

Use this feedback to correct your extraction. Pay special attention 
to the issues described above. Make sure your start_snippet and 
end_snippet are EXACT text that actually appears in the paper below.
═══════════════════════════════════════════════════════════════"""

    prompt += f"""

Now analyze the following paper text:
---
{snippet}
---
Respond with JSON only."""

    return prompt


def build_verification_prompt(extracted_text: str, source_type: str) -> str:
    """
    Stage 3A — Verification prompt.
    
    Receives the extracted text and checks it for correctness/completeness.
    """
    # Limit the text to avoid token overflow (15K chars is ~4K tokens)
    text_preview = extracted_text[:15000]
    if len(extracted_text) > 15000:
        text_preview += "\n\n[... text truncated for verification ...]"

    return f"""You are a verification assistant for academic paper processing.

You have been given text that was extracted as the "Related Work" section 
from an academic paper. The extraction was marked as "{source_type}" 
(explicit = labeled section heading found, fallback = introduction used as equivalent).

Your task: Evaluate whether this extraction is correct and complete.

CHECK THE FOLLOWING:
1. Does this text actually discuss related/prior work, systems, or approaches?
   (It should cite and discuss multiple prior works.)
2. Does it appear to start at the beginning of the section (not mid-sentence, 
   not including content from a previous section)?
3. Does it appear to end at the end of the section (not cut off mid-paragraph, 
   not bleeding into the next section like Methodology or Experiments)?
4. Is there content that clearly belongs to a DIFFERENT section (e.g., methodology, 
   experiments, results, conclusion) that should NOT be included?
5. For "fallback" extractions: The introduction was ALREADY determined to serve as a 
   related work equivalent by the extraction stage. Do NOT re-evaluate whether it qualifies.
   Instead, only check that the extraction boundaries capture the FULL introduction 
   (from first sentence to last sentence before the next major section heading).
   Even brief mentions of prior methods, techniques, or works are acceptable for fallback.

Return ONLY valid JSON:
{{
    "is_correct": true or false,
    "is_complete": true or false,
    "issues": [
        "description of any issue found"
    ],
    "suggested_correction": "specific guidance for re-extraction if needed, or null if all looks good"
}}

- "is_correct" = true means the text IS the related work section (not some other section).
- "is_complete" = true means the text appears to include the FULL section (not truncated).
- If both are true and there are no issues, return an empty "issues" array and null for "suggested_correction".

Extracted text to verify:
---
{text_preview}
---
Respond with JSON only."""


def build_error_analysis_prompt(
    paper_text: str,
    boundaries: dict,
    error_details: str,
) -> str:
    """
    Stage 3B — Error analysis prompt.
    
    Diagnoses why extraction failed (snippets not found, etc.) and 
    suggests corrections for the next attempt.
    """
    # Limit paper text for the prompt
    paper_preview = paper_text[:25000]
    if len(paper_text) > 25000:
        paper_preview += "\n\n[... paper text truncated ...]"

    return f"""You are a diagnostic assistant for academic paper processing.

An extraction attempt FAILED with the following details:

ATTEMPTED BOUNDARIES:
{json.dumps(boundaries, indent=2)}

ERROR DETAILS:
{error_details}

The extraction failed because the start/end snippets could not be located 
in the actual PDF text. This usually means one of:
1. The snippets were hallucinated (not actual text from the paper)
2. The page numbers were incorrect  
3. The paper's text extraction has OCR or formatting issues in that region
4. The section heading was missed or has an unusual format

Your task: Look at the paper text below and provide a corrective diagnosis.

Return ONLY valid JSON:
{{
    "diagnosis": "explanation of what went wrong",
    "correct_section_exists": true or false,
    "suggested_start_page": <integer or null>,
    "suggested_end_page": <integer or null>,
    "suggested_start_text": "actual text from the paper that starts the section (copy verbatim)",
    "suggested_end_text": "actual text from the paper that ends the section (copy verbatim)",
    "reasoning": "why these corrections should work"
}}

- If you believe the paper genuinely has no related work section, set "correct_section_exists" to false.
- The "suggested_start_text" and "suggested_end_text" MUST be exact text copied from the paper below.
- Use the "--- PAGE X ---" markers to determine correct page numbers.

Paper text:
---
{paper_preview}
---
Respond with JSON only."""


def build_null_challenger_prompt(intro_text: str) -> str:
    """
    Stage 3C — Null Challenger prompt.
    
    Called when all extraction windows return null with confidence < threshold.
    Uses a more permissive framing focused specifically on the introduction.
    """
    # Use the first ~15K chars which should cover the introduction
    text_preview = intro_text[:15000]

    return f"""You are an assistant that determines whether an academic paper's Introduction 
contains a discussion of prior/related work.

IMPORTANT CONTEXT: A previous analysis of this paper concluded it has NO 
Related Work section, but that conclusion had low confidence. You are providing 
a second opinion with MORE PERMISSIVE criteria.

Look at the Introduction (or opening section) of the paper below and determine 
whether it discusses prior work. Be PERMISSIVE — include cases where:
- The introduction mentions prior methods, techniques, or systems by name
- The introduction cites previous papers and briefly describes what they did
- The introduction discusses the history or evolution of approaches in the field
- The introduction mentions limitations of existing methods (even briefly)
- The opening references specific modulation methods, algorithms, protocols, etc.

Only return null if the introduction truly contains ZERO discussion of any prior 
work, methods, or approaches.

Return ONLY valid JSON with exactly one key: "related_work".

If prior work IS discussed in the introduction:
{{
    "related_work": {{
        "source": "fallback",
        "start_page": <integer - 1-based page number>,
        "end_page": <integer - 1-based page number>,
        "start_snippet": "<first 1-2 sentences of the introduction (exact text)>",
        "end_snippet": "<last 1-2 sentences of the introduction (exact text)>"
    }}
}}

If the introduction genuinely has NO discussion of prior work at all:
{{
    "related_work": null
}}

CRITICAL RULES:
- start_snippet and end_snippet must be EXACT text copied from the paper.
- Use the "--- PAGE X ---" markers for page numbers.
- If used, include the ENTIRE introduction (first sentence to last sentence 
  before the next major section heading).

Paper text (beginning of paper):
---
{text_preview}
---
Respond with JSON only."""


def build_bibtex_generation_prompt(raw_entries: list[str], related_work_text: str) -> str:
    """
    Stage 4C — BibTeX generation prompt.
    
    Converts raw bibliography entries (extracted from the paper's References
    section) into properly formatted BibTeX.
    """
    entries_text = "\n\n".join(f"--- Entry {i+1} ---\n{entry}" for i, entry in enumerate(raw_entries))
    
    # Truncate related work text to avoid token overflow
    rw_preview = related_work_text[:8000]
    if len(related_work_text) > 8000:
        rw_preview += "\n[... truncated ...]"

    return f"""You are a BibTeX formatting assistant for academic papers.

You are given raw bibliography entries extracted from a research paper's References section. 
These entries were cited in the paper's Related Work section (provided below for context).

Your task: Convert each raw entry into a properly formatted BibTeX entry.

RULES:
1. Use the correct BibTeX entry type:
   - @article for journal papers
   - @inproceedings for conference papers  
   - @book for books
   - @phdthesis / @mastersthesis for dissertations
   - @techreport for technical reports
   - @misc for everything else (websites, preprints, arXiv papers)
2. Include ALL available fields from the raw entry: author, title, year, 
   journal/booktitle, volume, number, pages, doi, url, publisher
3. Generate citation keys in the format: firstauthorlastname + year 
   (e.g., smith2021). If there are duplicates, append a letter (smith2021a, smith2021b).
4. Do NOT hallucinate or invent any information not present in the raw entries.
   If a field is not available in the raw text, OMIT it entirely.
5. Preserve author names exactly as they appear (do not expand initials you don't know).
6. For arXiv preprints, use @misc with an eprint field.

Return ONLY valid BibTeX content. No markdown code fences, no explanations, just BibTeX entries.

RAW BIBLIOGRAPHY ENTRIES:
{entries_text}

RELATED WORK SECTION (for context on how these are cited):
---
{rw_preview}
---

Output BibTeX entries only:"""
