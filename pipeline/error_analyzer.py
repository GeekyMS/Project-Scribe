"""
Stage 3B — Error Analysis LLM.

Diagnoses why extraction failed (hallucinated snippets, wrong pages, etc.)
and produces correction context for the next extraction attempt.
"""

import json
import re
import time

from .extractor import client, MAX_RETRIES, RETRY_DELAY

ERROR_ANALYSIS_MODEL = "anthropic/claude-sonnet-4.6"
from .prompts import build_error_analysis_prompt
from .schemas import ErrorAnalysis


def analyze_error(
    paper_text: str,
    boundaries_dict: dict,
    error_details: str,
) -> ErrorAnalysis:
    """
    Stage 3B entry point.
    
    Diagnoses why the extraction gate failed and suggests corrections.
    
    Args:
        paper_text: Full paper text with PAGE markers.
        boundaries_dict: The failed boundaries as a dict.
        error_details: Description of what went wrong from the gate.
    
    Returns:
        ErrorAnalysis with diagnostic information and suggested corrections.
    """
    prompt = build_error_analysis_prompt(paper_text, boundaries_dict, error_details)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=ERROR_ANALYSIS_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                timeout=90.0,
            )

            raw = response.choices[0].message.content
            if raw is None:
                print(f"   ⚠️ Error analysis LLM returned empty on attempt {attempt}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            # Filter reasoning tags
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)

            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                analysis = ErrorAnalysis.from_dict(data)
                print(f"   🔬 Error diagnosis: {analysis.diagnosis[:200]}")
                print(f"   🔬 Section exists: {analysis.correct_section_exists}")
                if analysis.suggested_start_page:
                    print(f"   🔬 Suggested pages: {analysis.suggested_start_page}-{analysis.suggested_end_page}")
                return analysis

        except Exception as e:
            print(f"   ⚠️ Error analysis call failed on attempt {attempt}: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    # If all retries fail, return a minimal analysis
    print("   ⚠️ Error analysis failed after all retries")
    return ErrorAnalysis(diagnosis="Error analysis LLM unavailable", correct_section_exists=False)


def build_correction_from_error(analysis: ErrorAnalysis) -> str | None:
    """
    Convert an ErrorAnalysis into correction context for the next extraction attempt.
    
    Returns None if the analysis indicates no section exists.
    """
    if not analysis.correct_section_exists:
        return None

    parts = [f"Previous extraction failed: {analysis.diagnosis}"]

    if analysis.suggested_start_page and analysis.suggested_end_page:
        parts.append(
            f"The Related Work section appears to be on pages "
            f"{analysis.suggested_start_page}-{analysis.suggested_end_page}."
        )

    if analysis.suggested_start_text:
        parts.append(
            f"The section likely starts with this text (copied from the paper): "
            f'"{analysis.suggested_start_text}"'
        )

    if analysis.suggested_end_text:
        parts.append(
            f"The section likely ends with this text (copied from the paper): "
            f'"{analysis.suggested_end_text}"'
        )

    if analysis.reasoning:
        parts.append(f"Reasoning: {analysis.reasoning}")

    parts.append(
        "IMPORTANT: Make sure your start_snippet and end_snippet are EXACT text "
        "from the paper. Do NOT hallucinate or paraphrase."
    )

    return "\n".join(parts)
