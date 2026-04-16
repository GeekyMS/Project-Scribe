"""
Stage 1 — PDF text extraction + LLM boundary detection.

Ported from process_paper_v2.py with the following changes:
- Uses prompts from pipeline.prompts (supports correction context)
- Returns typed Boundaries instead of raw dict
- Keeps the sliding window + retry logic intact
"""

import os
import re
import json
import time

import httpx
import fitz
from openai import OpenAI
from dotenv import load_dotenv

from .prompts import build_extraction_prompt
from .schemas import Boundaries

load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("Missing OPENROUTER_API_KEY in .env")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=API_KEY,
    timeout=httpx.Timeout(120.0, connect=10.0),
)
MODEL = "anthropic/claude-opus-4.6"

# LLM retry settings (for transport/parse failures within a single call)
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


def extract_text_and_page_offsets(pdf_path: str) -> tuple[str, list[int]]:
    """
    Extract full text from a PDF with page markers and margin clipping.
    
    Returns:
        (full_text, page_offsets) — full_text contains PAGE markers,
        page_offsets is a list of character offsets for each page start.
    """
    doc = fitz.open(pdf_path)
    full_text = ""
    page_offsets = []

    for page in doc:
        page_offsets.append(len(full_text))
        full_text += f"\n--- PAGE {page.number + 1} ---\n"

        page_rect = page.rect
        margin = page_rect.height * 0.10
        clip_rect = fitz.Rect(
            0,
            margin,
            page_rect.width,
            page_rect.height - margin,
        )
        page_text = page.get_text("text", clip=clip_rect, sort=True)
        full_text += page_text + "\n"

    doc.close()
    return full_text, page_offsets


def _call_llm_single(
    snippet: str,
    correction_context: str = None,
) -> dict | None:
    """
    Make a single LLM call with retry logic.
    
    Returns parsed dict or None on failure (after all retries exhausted).
    """
    prompt = build_extraction_prompt(snippet, correction_context)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                timeout=90.0,
            )

            raw = response.choices[0].message.content
            if raw is None:
                print(f"   ⚠️ LLM returned empty content on attempt {attempt}")
                if attempt < MAX_RETRIES:
                    print(f"   Retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                continue

            # Filter out <think>...</think> tags (reasoning models)
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)

            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                    print(f"   LLM Raw (attempt {attempt}): {raw[:300]}...")

                    # Normalize key variants
                    if "related_works" in data and "related_work" not in data:
                        data["related_work"] = data.pop("related_works")

                    if isinstance(data.get("related_work"), str):
                        print("   ⚠️ LLM returned related_work as string. Recovering...")
                        data["related_work"] = {
                            "start_page": 1,
                            "end_page": 1,
                            "start_snippet": data["related_work"][:100],
                            "end_snippet": "",
                        }

                    return data
                except json.JSONDecodeError:
                    print(f"   ⚠️ JSON parse failed on attempt {attempt}")
            else:
                print(f"   ⚠️ No JSON found on attempt {attempt}")

        except Exception as e:
            print(f"   ⚠️ LLM call failed on attempt {attempt}: {e}")

        if attempt < MAX_RETRIES:
            print(f"   Retrying in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)

    return None


def extract_boundaries(
    text: str,
    correction_context: str = None,
    max_chars: int = 25000,
    overlap: int = 5000,
) -> dict:
    """
    Extract Related Work boundaries using sliding windows + LLM.
    
    This is the Stage 1 entry point. Returns the raw dict with 
    "related_work" key (which may be None).
    
    Args:
        text: Full paper text with PAGE markers.
        correction_context: Optional feedback from a previous iteration.
        max_chars: Maximum characters per window.
        overlap: Overlap between windows.
    
    Returns:
        dict with "related_work" key — either a boundaries dict or None.
    """
    text_len = len(text)

    # Build sliding windows
    windows = []
    start = 0
    while start < text_len:
        end = min(start + max_chars, text_len)
        windows.append((start, end))
        if end >= text_len:
            break
        start += max_chars - overlap

    print(f"   📊 Text: {text_len} chars → {len(windows)} window(s)")

    explicit_results = []
    fallback_results = []
    null_confidences = []  # Track confidence scores for null responses

    for i, (win_start, win_end) in enumerate(windows):
        snippet = text[win_start:win_end]
        print(f"   🔍 Window {i+1}/{len(windows)}: chars [{win_start}–{win_end}]")

        result = _call_llm_single(snippet, correction_context)

        if result is None:
            print(f"   ❌ Window {i+1}: no valid response")
            continue

        rw = result.get("related_work")
        if rw is None:
            confidence = result.get("null_confidence", 90)  # default to 90 if not provided
            null_confidences.append(confidence)
            print(f"   ℹ️ Window {i+1}: no Related Work found (confidence: {confidence}%)")
            continue

        if isinstance(rw, dict) and (rw.get("start_snippet") or rw.get("start_page")):
            source = rw.get("source", "explicit")
            if source == "fallback":
                print(f"   📎 Window {i+1}: fallback (intro-based) result")
                fallback_results.append(result)
            else:
                print(f"   ✅ Window {i+1}: explicit section found!")
                explicit_results.append(result)
        else:
            print(f"   ⚠️ Window {i+1}: empty/invalid related_work object")

    # Prefer explicit over fallback
    if explicit_results:
        def get_page(r, key):
            rw = r.get("related_work", {})
            return int(rw.get(key, 0)) if isinstance(rw, dict) else 0

        explicit_results.sort(key=lambda r: get_page(r, "start_page"))
        first = explicit_results[0]
        last = explicit_results[-1]

        merged_rw = dict(first.get("related_work", {}))
        last_rw = last.get("related_work", {})
        merged_rw["end_page"] = max(get_page(first, "end_page"), get_page(last, "end_page"))
        if last_rw.get("end_snippet"):
            merged_rw["end_snippet"] = last_rw["end_snippet"]

        chosen = {"related_work": merged_rw}
        print(f"   🏆 Explicit section (merged from {len(explicit_results)} hit(s), "
              f"pages {merged_rw.get('start_page')}-{merged_rw.get('end_page')})")
        return chosen

    if fallback_results:
        chosen = fallback_results[0]
        print(f"   📎 Using fallback intro-based result")
        return chosen

    # Use MINIMUM confidence — if any single window is uncertain, 
    # it's worth a second look (avoids high-confidence body windows 
    # drowning out an uncertain intro window)
    min_confidence = min(null_confidences) if null_confidences else 90
    avg_confidence = (
        sum(null_confidences) / len(null_confidences)
        if null_confidences else 90
    )
    print(f"   ℹ️ No Related Work section found in any window "
          f"(min confidence: {min_confidence}%, avg: {avg_confidence:.0f}%)")
    return {"related_work": None, "min_null_confidence": min_confidence}

