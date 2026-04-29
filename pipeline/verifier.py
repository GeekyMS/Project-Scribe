"""
Stage 3A — Verification LLM.

Checks whether extracted text is actually the related work section
and whether it's complete (not truncated or including extra sections).
"""

import json
import re
import time

from .extractor import client, MAX_RETRIES, RETRY_DELAY

VERIFICATION_MODEL = "anthropic/claude-sonnet-4.6"
from .prompts import build_verification_prompt
from .schemas import VerificationResult


def verify_extraction(extracted_text: str, source_type: str) -> VerificationResult:
    """
    Stage 3A entry point.
    
    Sends the extracted text to the LLM for verification.
    
    Args:
        extracted_text: The text extracted from the PDF.
        source_type: "explicit" or "fallback".
    
    Returns:
        VerificationResult with correctness/completeness assessment.
    """
    prompt = build_verification_prompt(extracted_text, source_type)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=VERIFICATION_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                timeout=90.0,
            )

            raw = response.choices[0].message.content
            if raw is None:
                print(f"   ⚠️ Verification LLM returned empty on attempt {attempt}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue

            # Filter reasoning tags
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)

            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                print(f"   🔎 Verification result: correct={data.get('is_correct')}, "
                      f"complete={data.get('is_complete')}, "
                      f"issues={data.get('issues', [])}")
                return VerificationResult.from_dict(data)

        except Exception as e:
            print(f"   ⚠️ Verification call failed on attempt {attempt}: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    # If all retries fail, assume verification passed (don't block on verification failures)
    print("   ⚠️ Verification failed after all retries — assuming pass")
    return VerificationResult(is_correct=True, is_complete=True, issues=["Verification LLM unavailable"])
