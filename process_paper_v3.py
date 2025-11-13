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
MODEL = "qwen/qwen3-14b:free"

def extract_text_and_page_offsets(pdf_path: str):
    """Return full text and list of page start char offsets (0-based)."""
    doc = fitz.open(pdf_path)
    full_text = ""
    page_offsets = []
    for page in doc:
        page_offsets.append(len(full_text))
        full_text += page.get_text("text") + "\n"
    doc.close()
    return full_text, page_offsets

def call_llm_strict_for_boundaries(text: str, max_chars=40000):
    """Strict prompt that asks for JSON with related_work boundaries and references."""
    snippet = text[:max_chars]
    prompt = f"""You are an assistant that extracts the Related Work section from an academic paper.

CRITICAL INSTRUCTIONS:
1. The Related Work section is typically titled "Related Work", "Related Literature", or "Background"
2. It appears AFTER the Introduction section (usually pages 2-4 in most papers)
3. It is NOT the abstract (page 1)
4. It is NOT the introduction (usually page 1-2)
5. It is NOT the references/bibliography section at the end
6. Look for a clear section heading like "2 RELATED WORK" or "Related Work"

Return ONLY valid JSON with exactly two keys: "related_work" and "references".

related_work must be an object:
{{
  "start_page": <integer - 1-based page number where Related Work section BEGINS>,
  "end_page": <integer - 1-based page number where Related Work section ENDS>,
  "start_snippet": "<exact first 12-20 words immediately after the 'Related Work' heading>",
  "end_snippet": "<exact last 12-20 words before the next section begins>"
}}

IMPORTANT for snippets:
- start_snippet should be the first sentence or two AFTER the section heading
- end_snippet should be the last sentence before the next section
- Remove ALL newline characters and collapse whitespace to single spaces
- Make snippets unique and searchable

references must be a list of cited works mentioned in the Related Work section:
[
  {{
    "title": "paper title",
    "author": "author names",
    "venue": "conference or journal",
    "date": "year",
    "link": "url if available or empty string"
  }}
]

Do not output any explanation. Output ONLY the JSON object.

Paper text (first {max_chars} characters):

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

def save_bibtex(references, out_path="references.bib"):
    """Simple bibtex generator."""
    def make_key(ref):
        auth = (ref.get("author") or "anon").split(",")[0].strip()
        surname = auth.split()[-1].lower() if auth else "anon"
        year_match = re.search(r"(19|20)\d{2}", ref.get("date", "") or "")
        year = year_match.group(0) if year_match else "n.d."
        shorttitle = re.sub(r"\W+", "", (ref.get("title") or ""))[:20].lower() or "untitled"
        return f"{surname}{year}{shorttitle}"
    
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
        
        etype = "article" if ref.get("venue") else "misc"
        fields = []
        if ref.get("author"): fields.append(f"  author = {{{ref['author']}}}")
        if ref.get("title"): fields.append(f"  title = {{{ref['title']}}}")
        if ref.get("venue"): fields.append(f"  journal = {{{ref['venue']}}}")
        if ref.get("date"): fields.append(f"  year = {{{ref['date']}}}")
        if ref.get("link"): fields.append(f"  url = {{{ref['link']}}}")
        
        entry = f"@{etype}{{{key},\n" + ",\n".join(fields) + "\n}\n"
        entries.append(entry)
    
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(entries)
    print(f"✅ Saved BibTeX -> {out_path}")

def write_text_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")

def process_pdf_remove_pages(input_pdf: str, result_json: dict, outputs_dir: str = "."):
    """
    Extract Related Work section and create:
    1. related_work_only.pdf - only the Related Work pages
    2. paper_without_related.pdf - original paper with Related Work pages removed
    3. related_work.txt - text of Related Work section
    """
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    
    related = result_json.get("related_work", {})
    start_page = int(related.get("start_page", 1))
    end_page = int(related.get("end_page", start_page))
    start_snip = related.get("start_snippet", "").strip()
    end_snip = related.get("end_snippet", "").strip()
    
    doc = fitz.open(input_pdf)
    n_pages = len(doc)
    
    # Clamp pages to valid range
    start_page = max(1, min(start_page, n_pages))
    end_page = max(1, min(end_page, n_pages))
    
    print(f"\n📍 LLM detected Related Work:")
    print(f"   Pages: {start_page} → {end_page}")
    print(f"   Start snippet: {repr(start_snip[:80])}")
    print(f"   End snippet: {repr(end_snip[:80])}")
    
    # Validation: warn if it thinks page 1 is Related Work (likely wrong!)
    if start_page == 1:
        print("\n⚠️  WARNING: LLM thinks Related Work starts on page 1!")
        print("   This is usually WRONG - page 1 typically contains title/abstract/intro.")
        print("   Consider reviewing the output or adjusting the max_chars parameter.\n")
    
    # PART 1: Create related_work_only.pdf
    related_doc = fitz.open()
    for p in range(start_page - 1, end_page):
        related_doc.insert_pdf(doc, from_page=p, to_page=p)
    
    related_output = outputs_dir / "related_work_only.pdf"
    related_doc.save(str(related_output))
    related_doc.close()
    print(f"✅ Saved related-only PDF → {related_output}")
    
    # PART 2: Extract text from Related Work pages
    related_text_parts = []
    for p in range(start_page - 1, end_page):
        page_text = doc[p].get_text("text")
        related_text_parts.append(page_text)
    
    full_related_text = "\n".join(related_text_parts).strip()
    
    # Try to refine text extraction using snippets
    try:
        # Normalize the full text for better matching
        normalized_text = re.sub(r'\s+', ' ', full_related_text)
        normalized_start = re.sub(r'\s+', ' ', start_snip)
        normalized_end = re.sub(r'\s+', ' ', end_snip)
        
        if normalized_start and normalized_start in normalized_text:
            start_idx = normalized_text.find(normalized_start)
            if normalized_end and normalized_end in normalized_text:
                end_idx = normalized_text.find(normalized_end) + len(normalized_end)
                full_related_text = normalized_text[start_idx:end_idx].strip()
            else:
                full_related_text = normalized_text[start_idx:].strip()
    except Exception as e:
        print(f"⚠️  Could not refine text extraction: {e}")
    
    write_text_file(outputs_dir / "related_work.txt", full_related_text)
    print(f"✅ Saved related_work.txt")
    
    # PART 3: Create paper_without_related.pdf
    doc2 = fitz.open(input_pdf)
    
    # Delete pages in reverse order to avoid index shifting
    for p in range(end_page - 1, start_page - 2, -1):
        try:
            doc2.delete_page(p)
        except Exception as e:
            print(f"⚠️  Failed to delete page {p+1}: {e}")
    
    out_path = outputs_dir / "paper_without_related.pdf"
    doc2.save(str(out_path))
    doc2.close()
    print(f"✅ Saved paper without related → {out_path}")
    
    doc.close()
    
    # PART 4: Save references
    refs = result_json.get("references", []) or []
    with open(outputs_dir / "references.json", "w", encoding="utf-8") as f:
        json.dump(refs, f, indent=2, ensure_ascii=False)
    print(f"✅ Saved references.json")
    
    if refs:
        save_bibtex(refs, out_path=str(outputs_dir / "references.bib"))
    
    # Save raw JSON for debugging
    with open(outputs_dir / "related_work_raw.json", "w", encoding="utf-8") as f:
        json.dump(result_json, f, indent=2, ensure_ascii=False)
    print(f"✅ Saved related_work_raw.json (for debugging)")
    
    print(f"\n✅ All done! Outputs in: {outputs_dir}")

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
    
    print("\n🤖 Calling LLM to detect Related Work boundaries + references...")
    result = call_llm_strict_for_boundaries(full_text, max_chars=40000)
    
    # Basic validation
    if not isinstance(result, dict) or "related_work" not in result:
        print("❌ Unexpected LLM response. Dumping and exiting.")
        print(json.dumps(result, indent=2))
        sys.exit(1)
    
    outputs_dir = Path("outputs")
    process_pdf_remove_pages(pdf_path, result, outputs_dir=outputs_dir)

if __name__ == "__main__":
    main(sys.argv)
