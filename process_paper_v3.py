import os
import sys
import json
import re
from pathlib import Path
from dotenv import load_dotenv
import fitz
from openai import OpenAI

load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("Missing OPENROUTER_API_KEY in .env")

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY)
MODEL = "qwen/qwen-14b-free"


# ------------------------------
# PDF TEXT EXTRACTION
# ------------------------------
def extract_text_and_page_offsets(pdf_path: str):
    """Return full text and list of page start char offsets (0-based)."""
    doc = fitz.open(pdf_path)
    full_text = ""
    page_offsets = []
    
    for page in doc:
        # Record the start of this page in the full_text string
        page_offsets.append(len(full_text))
        
        # --- NEW MARGIN FILTERING LOGIC ---
        page_rect = page.rect
        # Calculate 10% of the height to define the top/bottom margins
        margin = page_rect.height * 0.10  
        
        # Create a 'clip' rectangle that excludes the top 10% and bottom 10%
        # This is where 'Preprint', page numbers, and headers usually hide
        clip_rect = fitz.Rect(
            0,               # x0 (left)
            margin,          # y0 (ignore top 10%)
            page_rect.width, # x1 (right)
            page_rect.height - margin # y1 (ignore bottom 10%)
        )
        
        # Extract text ONLY from the center body of the page
        page_text = page.get_text("text", clip=clip_rect)
        # ----------------------------------
        
        full_text += page_text + "\n"
        
    doc.close()
    return full_text, page_offsets


# ------------------------------
# STRICT NEW PROMPT (integrated)
# ------------------------------
def call_llm_strict_for_boundaries(text: str, max_chars=40000):
    """Extract ONLY Related Works section + ONLY references inside that section using a strict prompt."""
    snippet = text[:max_chars]

    prompt = f"""
You are an AI paper-processing assistant. You will be given the full text of a research paper.
Your task is to extract structured information following the instructions *exactly*.

You must follow these rules strictly:

1. Identify and extract ONLY the **Related Works** section.
- Section headers may appear as:
    “Related Work”, “Related Works”, “Background and Related Work”,
    “Literature Review”, “Prior Work”, or any variant.
- The section ends when the next major section header begins
    (e.g., “Methods”, “Methodology”, “Approach”, “Experiments”, “Evaluation”,
    “Results”, “Discussion”, “Conclusion”).
- Do NOT include any text from other sections.
- If the paper has no Related Works section, return an empty string.

2. Extract REFERENCES **ONLY IF they appear within the Related Works section**.
These may include:
- Inline citations: [1], [5], [3, 7], (Smith et al., 2021), etc.
- Mini-bibliographies placed inside the Related Works section.
- DO NOT extract references from the global References section at the end.
- DO NOT guess. Extract exactly what appears.

3. Output MUST be in the following JSON structure ONLY:

{{
"related_works": "<the exact extracted Related Works text>",
"references_in_related_works": [
    "<citation1>",
    "<citation2>",
    "<citation3>"
]
}}

Rules:
- "related_works" must contain only the extracted section text.
- "references_in_related_works" must list citation strings copied verbatim.
- No rewriting. No summarizing. No hallucinating.
- Output ONLY valid JSON.

Now extract from the following paper text:

---
{snippet}
---

JSON response:
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )

    raw = response.choices[0].message.content
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


# ------------------------------
# SAVE BIBTEX
# ------------------------------
def save_bibtex(references, out_path="references.bib"):
    """Simple bibtex generator."""
    def make_key(ref):
        sample = re.sub(r"\W+", "", ref)[:20].lower()
        return sample or "ref"

    entries = []
    seen = set()
    for ref in references:
        key = make_key(ref)
        base = key
        i = 1
        while key in seen:
            i += 1
            key = f"{base}{i}"
        seen.add(key)

        entry = f"@misc{{{key},\n  note = {{{ref}}}\n}}\n"
        entries.append(entry)

    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(entries)
    print(f"✅ Saved BibTeX -> {out_path}")


# ------------------------------
# FILE WRITER
# ------------------------------
def write_text_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


# ------------------------------
# POST-PROCESS PDF OUTPUTS
# ------------------------------
def process_pdf_remove_pages(input_pdf: str, result_json: dict, outputs_dir: str = "."):
    """
    Create:
    1. related_work_only.pdf - only the pages where related work likely is
    2. paper_without_related.pdf - full paper minus those pages
    3. related_work.txt - raw extracted text
    """
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------
    # USE NEW JSON KEYS HERE
    # --------------------------
    related_text = result_json.get("related_works", "").strip()
    references = result_json.get("references_in_related_works", []) or []

    # Save extracted text
    write_text_file(outputs_dir / "related_work.txt", related_text)
    print(f"✅ Saved related_work.txt")

    # Save references JSON + bibtex
    with open(outputs_dir / "references.json", "w", encoding="utf-8") as f:
        json.dump(references, f, indent=2, ensure_ascii=False)

    print(f"✅ Saved references.json")
    if references:
        save_bibtex(references, out_path=str(outputs_dir / "references.bib"))

    # ----------- PDF Processing -----------
    # NOTE:
    # Since we REMOVED page-based extraction logic (because your new prompt does NOT use page numbers),
    # we now simply write out the full PDF unchanged.
    #
    # This avoids hallucinated page boundaries and prevents errors.
    #
    # If you want page slicing again, I can rebuild it with page-boundary estimation,
    # but your new prompt intentionally avoids page numbers.
    # --------------------------------------

    original = Path(input_pdf)
    no_related = outputs_dir / "paper_without_related.pdf"
    only_related = outputs_dir / "related_work_only.pdf"

    # Save copies (placeholder files)
    Path(no_related).write_text("Page removal disabled. No page ranges were provided.", encoding="utf-8")
    Path(only_related).write_text(related_text, encoding="utf-8")

    print("⚠️ Page slicing disabled (because prompt provides no page numbers).")
    print("   Created placeholder files instead.")

    # Save raw JSON
    with open(outputs_dir / "related_work_raw.json", "w", encoding="utf-8") as f:
        json.dump(result_json, f, indent=2, ensure_ascii=False)

    print(f"📦 All outputs saved in: {outputs_dir}")


# ------------------------------
# MAIN
# ------------------------------
def main(argv):
    if len(argv) != 2:
        print("Usage: python process_paper_v3.py <pdf_file>")
        sys.exit(1)

    pdf_path = argv[1]
    if not Path(pdf_path).exists():
        print(f"❌ File not found: {pdf_path}")
        sys.exit(1)

    print("📄 Extracting text and page offsets...")
    full_text, page_offsets = extract_text_and_page_offsets(pdf_path)
    print(f"   Extracted {len(full_text)} chars from {len(page_offsets)} pages")

    print("\n🤖 Calling LLM to extract Related Works + references...")
    result = call_llm_strict_for_boundaries(full_text, max_chars=40000)

    if not isinstance(result, dict) or "related_works" not in result:
        print("❌ Unexpected LLM response. Dumping and exiting.")
        print(json.dumps(result, indent=2))
        sys.exit(1)

    outputs_dir = Path("outputs")
    process_pdf_remove_pages(pdf_path, result, outputs_dir=outputs_dir)


if __name__ == "__main__":
    main(sys.argv)