import os
import sys
import json
import re
from pathlib import Path
from dotenv import load_dotenv
import fitz
from openai import OpenAI
import re


load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("Missing OPENROUTER_API_KEY in .env")

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY)
MODEL = "qwen/qwen3-14b"



def extract_text_and_page_offsets(pdf_path: str):
    """Return full text and list of page start char offsets (0-based)."""
    doc = fitz.open(pdf_path)
    full_text = ""
    page_offsets = []
    
    for page in doc:

        page_offsets.append(len(full_text))
        

        page_rect = page.rect

        margin = page_rect.height * 0.10  
        
        clip_rect = fitz.Rect(
            0,               
            margin,          
            page_rect.width,
            page_rect.height - margin
        )
        
        # Extract text ONLY from the center body of the page
        page_text = page.get_text("text", clip=clip_rect)
        
        full_text += page_text + "\n"
        
    doc.close()
    return full_text, page_offsets

def call_llm_strict_for_boundaries(text: str, max_chars=20000): #could break up into two functions, what if the related works section spans multiple pages?
    """Strict prompt that asks for JSON with related_work boundaries and references."""
    snippet = text[:max_chars]
#     prompt = f"""
# You are an assistant that extracts the Related Work section boundaries and references from an academic paper.
# Return ONLY valid JSON with exactly two keys: "related_work" and "references".

# related_work must be an object:
# {{
#     "start_page": <integer - 1-based>,
#     "end_page": <integer - 1-based>,
#     "start_snippet": "<the first 1-2 sentences of the Related Work section (exact text)>",
#     "end_snippet": "<the last 1-2 sentences of the Related Work section (exact text)>"
# }}

# CRITICAL: When selecting the start_snippet and end_snippet, only choose sentences that are part of the main body text. Avoid page numbers, headers, or watermarks like 'Preprint', as these are being filtered out of my system and will cause a coordinate mismatch.

# references must be a list of objects with fields: title, author, venue, date, link.

# If a field is not found, return an empty string for that field.
# Do not output any explanation or anything outside the JSON.

# Now analyze the following paper text (only the first {max_chars} characters are provided here for safety):
# ---
# {snippet}
# ---
# Respond with JSON only.
# """
    #edit prompt to request all parts of related works section(if it has multiple pages)
    prompt = f"""
You are an assistant that extracts the Related Work section boundaries and references from an academic paper.
Return ONLY valid JSON with exactly two keys: "related_work".

related_work must be an object:
{{
    "start_page": <integer - 1-based>,
    "end_page": <integer - 1-based>,
    "start_snippet": "<the first 1-2 sentences of the Related Work section (exact text)>",
    "end_snippet": "<the last 1-2 sentences of the Related Work section (exact text)>"
}}

CRITICAL: When selecting the start_snippet and end_snippet, only choose sentences that are part of the main body text. Avoid page numbers, headers, or watermarks like 'Preprint', as these are being filtered out of my system and will cause a coordinate mismatch.

If a field is not found, return an empty string for that field.
Do not output any explanation or anything outside the JSON.

Now analyze the following paper text (only the first {max_chars} characters are provided here for safety):
---
{snippet}
---
Respond with JSON only.
"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )


    # raw = response.choices[0].message.content
    # #inspect the json structure, and see what kinds of errors we are running into and put guardrails in for that
    # #functions to handle other raw output errors
    # try:
    #     print("LLM Raw Response:", raw)
    #     data = json.loads(raw) if isinstance(raw, str) else raw
        
    #     # If the LLM put the info in "related_works" (plural) instead of "related_work"
    #     if "related_works" in data and "related_work" not in data:
    #         data["related_work"] = data.pop("related_works")
            
    #     # If "related_work" is a string, we need to handle that or it will crash later
    #     if isinstance(data.get("related_work"), str):
    #         print("⚠️ LLM returned related_work as a string. Attempting to recover...")
    #         data["related_work"] = {
    #             "start_page": 1, 
    #             "end_page": 1, 
    #             "start_snippet": data["related_work"][:100], 
    #             "end_snippet": data["related_work"][-100:]
    #         }
    #     return data
    # except Exception as e:
    #     print(f"❌ JSON Parsing Error: {e}")
    #     return {"related_work": {"start_page": 1}, "references": []}

    raw = response.choices[0].message.content

    # 1. Regex to find the JSON object (ignoring garbage like "k>")
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    
    data = None
    if match:
        try:
            print("LLM Raw Response:", raw)
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            pass # Fall through to failure handling below

    # 2. If valid JSON was found, normalize keys
    if data:
        if "related_works" in data and "related_work" not in data:
            data["related_work"] = data.pop("related_works")
            
        if isinstance(data.get("related_work"), str):
            print("⚠️ LLM returned related_work as a string. Attempting to recover...")
            data["related_work"] = {
                "start_page": 1, 
                "end_page": 1, 
                "start_snippet": data["related_work"][:100], 
                "end_snippet": ""
            }
        return data

    # 3. Fallback if no JSON found or parsing failed
    print(f"❌ JSON Parsing Error or No JSON found. Raw start: {repr(raw[:50])}")
    return {"related_work": {"start_page": 1}, "references": []}

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

# -------- PDF redaction + page deletion logic --------
def redact_between_rects(page, rect_top, rect_bottom):
    """Add a redaction annotation that covers vertical slice from rect_top.y0 to rect_bottom.y1 spanning page width."""
    page_rect = page.rect
    redact_rect = fitz.Rect(page_rect.x0, rect_top.y0, page_rect.x1, rect_bottom.y1)
    page.add_redact_annot(redact_rect, fill=(1, 1, 1))
    # Note: do not apply redactions here; apply after collecting for page

def redact_from_top_to(rect, page):
    """Redact from top of page to bottom of rect."""
    page_rect = page.rect
    redact_rect = fitz.Rect(page_rect.x0, page_rect.y0, page_rect.x1, rect.y1)
    page.add_redact_annot(redact_rect, fill=(1, 1, 1))

def redact_from_rect_to_bottom(rect, page):
    """Redact from top of rect to bottom of page (i.e., remove from rect downward)."""
    page_rect = page.rect
    redact_rect = fitz.Rect(page_rect.x0, rect.y0, page_rect.x1, page_rect.y1)
    page.add_redact_annot(redact_rect, fill=(1, 1, 1))

def find_first_rect_for_snippet(page, snippet):
    """Search on page for start of section with fallback to short anchors."""
    if not snippet: return None
    snippet = snippet.strip()
    
    # Attempt 1: Exact match of the full snippet
    rects = page.search_for(snippet, quads=False)
    if rects: return rects[0]
    
    # Attempt 2: Fallback to the first 35 characters (more stable anchor)
    anchor = snippet[:35]
    if len(anchor) > 10:
        rects = page.search_for(anchor, quads=False)
        if rects: return rects[0]
            
    return None

def find_last_rect_for_snippet(page, snippet):
    """Search on page for end of section with fallback to short anchors."""
    if not snippet: return None
    snippet = snippet.strip()
    
    # Attempt 1: Exact match
    rects = page.search_for(snippet, quads=False)
    if rects: return rects[-1]
    
    # Attempt 2: Fallback to the last 35 characters
    anchor = snippet[-35:]
    if len(anchor) > 10:
        rects = page.search_for(anchor, quads=False)
        if rects: return rects[-1]
            
    return None

def process_pdf_hybrid_removal(input_pdf: str, result_json: dict, outputs_dir: str = "."):
    """
    result_json must contain:
    result_json["related_work"]["start_page"] (1-based)
    result_json["related_work"]["end_page"]   (1-based)
    result_json["related_work"]["start_snippet"] (string)
    result_json["related_work"]["end_snippet"]   (string)
    result_json["references"] -> list
    """
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    related = result_json.get("related_work", {})
    
    if isinstance(related, str):
        related = {"start_page": 1, "end_page": 1} 

    start_page = int(related.get("start_page", 1))
    end_page = int(related.get("end_page", start_page))
    start_snip = related.get("start_snippet", "").strip()
    end_snip = related.get("end_snippet", "").strip()

    # clamp later with doc length
    doc = fitz.open(input_pdf)

    # clamp pages
    n_pages = len(doc)
    start_page = max(1, min(start_page, n_pages))
    end_page = max(1, min(end_page, n_pages))

    print(f"LLM indicated Related Work: pages {start_page} -> {end_page}")
    print(f"start_snippet (len {len(start_snip)}): {repr(start_snip[:80])}")
    print(f"end_snippet   (len {len(end_snip)}): {repr(end_snip[:80])}")

    # 1) Create 'related_work_only.pdf' by copying pages start..end then trimming top/bottom based on snippets.
    related_doc = fitz.open()
    for p in range(start_page - 1, end_page):
        related_doc.insert_pdf(doc, from_page=p, to_page=p)

    # On related_doc (which starts at page 0), we will redact areas *outside* the real Related Work so only the section remains.
    # For start page: remove everything above the start_snippet (i.e., redact top->start_rect.y0)
    # For end page: remove everything below the end_snippet (i.e., redact end_rect.y1->bottom)
    # For single-page related work, redact both sides leaving only between start and end.
    try:
        if related_doc.page_count > 0:
            # start page handling
            sp_idx = 0
            ep_idx = related_doc.page_count - 1
            # find rects on the copied pages (note: these pages are copies so content coords remain)
            start_rect = None
            end_rect = None
            # Attempt locate on start page in the original doc first (more reliable)
            orig_start_page = doc[start_page - 1]
            orig_end_page = doc[end_page - 1]

            start_rect = find_first_rect_for_snippet(orig_start_page, start_snip)
            end_rect = find_last_rect_for_snippet(orig_end_page, end_snip)

            # If we couldn't find on original, try on the copied related_doc pages
            if start_rect is None:
                start_rect = find_first_rect_for_snippet(related_doc[sp_idx], start_snip)
            if end_rect is None and (start_page != end_page):
                end_rect = find_last_rect_for_snippet(related_doc[ep_idx], end_snip)
            if start_rect is None and end_rect is None and start_page == end_page:
                # try both on single page from related_doc
                start_rect = find_first_rect_for_snippet(related_doc[0], start_snip)
                end_rect = find_last_rect_for_snippet(related_doc[0], end_snip)

            # redact above start_rect on first related page
            if start_rect:
                # redact from top of page to just above start_rect (keep the snippet and below)
                redact_rect = fitz.Rect(related_doc[sp_idx].rect.x0, related_doc[sp_idx].rect.y0,
                                        related_doc[sp_idx].rect.x1, start_rect.y0)
                related_doc[sp_idx].add_redact_annot(redact_rect, fill=(1, 1, 1))
            else:
                print("⚠️ start_snippet not found on the expected start page; keeping page top intact.")

            # redact below end_rect on last related page
            if end_rect:
                redact_rect = fitz.Rect(related_doc[ep_idx].rect.x0, end_rect.y1,
                                        related_doc[ep_idx].rect.x1, related_doc[ep_idx].rect.y1)
                related_doc[ep_idx].add_redact_annot(redact_rect, fill=(1, 1, 1))
            else:
                print("⚠️ end_snippet not found on the expected end page; keeping page bottom intact.")

            # apply redactions for related_doc
            for p in range(related_doc.page_count):
                page = related_doc[p]
                annots = page.annots()
                # apply only if there are redaction annots
                # safe to call applyRedactions even if none; PyMuPDF will ignore
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        else:
            print("⚠️ No pages copied to related_doc (empty).")
    except Exception as e:
        print("⚠️ Warning while trimming/cropping related_doc:", e)

    related_output = outputs_dir / "related_work_only.pdf"
    related_doc.save(str(related_output))
    related_doc.close()
    print(f"✅ Saved related-only PDF -> {related_output}")

    # Also save related text by extracting the text between start/end pages and attempting substring match
    # We'll extract from original doc to a text file for convenience
    related_text_parts = []
    for p in range(start_page - 1, end_page):
        related_text_parts.append(doc[p].get_text("text"))
    related_text = "\n".join(related_text_parts).strip()
    write_text_file(outputs_dir / "related_work.txt", related_text)
    print(f"✅ Saved related_work.txt")

    # 2) Create 'paper_without_related.pdf':
    # Strategy:
    #  - Redact partial pages (start_page, end_page) to remove the snippet region
    #  - Delete full middle pages (start_page+1 .. end_page-1)
    # Note: operations mutate doc, so be careful with page indexing when deleting.

    # Work on a copy of doc to preserve original variable
    doc2 = fitz.open(input_pdf)

    # First: redact on start_page in doc2 to remove from start_snippet downwards
    try:
        if start_page == end_page:
            # single page case: find start and end rect on that single page and redact between them
            page = doc2[start_page - 1]
            s_rect = find_first_rect_for_snippet(page, start_snip)
            e_rect = find_last_rect_for_snippet(page, end_snip)
            if s_rect and e_rect:
                # redact between s_rect.y0 (top of start) and e_rect.y1 (bottom of end)
                redact_rect = fitz.Rect(page.rect.x0, s_rect.y0, page.rect.x1, e_rect.y1)
                page.add_redact_annot(redact_rect, fill=(1,1,1))
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            else:
                print("⚠️ Could not locate both start/end snippets on the same page; falling back to full-page removal of that page.")
                # delete that single page entirely
                doc2.delete_page(start_page - 1)
                out_path = outputs_dir / "paper_without_related.pdf"
                doc2.save(str(out_path))
                doc2.close()
                print(f"✅ Saved -> {out_path}")
                # done
                doc.close()
                return
        else:
            # multi-page case
            # start page redact from start_snippet to page bottom
            s_page = doc2[start_page - 1]
            s_rect = find_first_rect_for_snippet(s_page, start_snip)
            if s_rect:
                redact_from_rect_to_bottom(s_rect, s_page)
                s_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            else:
                print("⚠️ start_snippet not found on start page; will delete whole start page later.")

            # end page redact from page top to end_snippet bottom
            e_page = doc2[end_page - 1]
            e_rect = find_last_rect_for_snippet(e_page, end_snip)
            if e_rect:
                redact_from_top_to(e_rect, e_page)
                e_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            else:
                print("⚠️ end_snippet not found on end page; will delete whole end page later.")

            # Now delete full middle pages (do reverse order to prevent index shift)
            if end_page - start_page > 1:
                for p in range(end_page - 2, start_page, -1):  # pages between start+1 .. end-1
                    try:
                        doc2.delete_page(p)
                    except Exception as e:
                        print(f"⚠️ Failed to delete page {p+1}: {e}")

            # If start or end snippet wasn't found, we may want to delete those pages entirely to be safe
            # If start_rect missing -> delete that entire page's content (we'll delete the page)
            if s_rect is None:
                try:
                    doc2.delete_page(start_page - 1)
                    # after deletion, indexes shift; nothing more to do for start
                except Exception as e:
                    print(f"⚠️ Could not delete start page {start_page}: {e}")
            # If end_rect missing -> delete its page; note: if start page deleted above, indexes changed
            # Compute current end page index conservatively:
            # We'll find pages with original page numbers by mapping, but for simplicity, if end_rect missing delete page at current (start_page) index if deletion shifted; safer: try to delete original end_page or end_page-1
            if e_rect is None and end_page <= doc2.page_count:
                try:
                    doc2.delete_page(end_page - 1)
                except Exception as e:
                    # try alternative index
                    try:
                        doc2.delete_page(max(0, end_page - 2))
                    except Exception as e2:
                        print(f"⚠️ Could not delete end page {end_page}: {e} / {e2}")

    except Exception as e:
        print("⚠️ Error during redaction/deletion on doc2:", e)

    out_path = outputs_dir / "paper_without_related.pdf"
    doc2.save(str(out_path))
    doc2.close()
    doc.close()
    print(f"✅ Saved paper without related -> {out_path}")

    # Save references.json and references.bib
    refs = result_json.get("references", []) or []
    with open(outputs_dir / "references.json", "w", encoding="utf-8") as f:
        json.dump(refs, f, indent=2, ensure_ascii=False)
    save_bibtex(refs, out_path=str(outputs_dir / "references.bib"))

    # Save raw JSON for debugging
    with open(outputs_dir / "related_work_raw.json", "w", encoding="utf-8") as f:
        json.dump(result_json, f, indent=2, ensure_ascii=False)

    print("✅ All done. Outputs in:", outputs_dir)

# -------- main --------
def main(argv):
    if len(argv) != 2:
        print("Usage: python process_paper_llm_v3.py <path_to_pdf>")
        sys.exit(1)
    pdf_path = argv[1]
    if not Path(pdf_path).exists():
        print("File not found:", pdf_path)
        sys.exit(1)

    print("Extracting text and page offsets...")
    full_text, page_offsets = extract_text_and_page_offsets(pdf_path)
    print(f"Extracted text length {len(full_text)} chars, pages: {len(page_offsets)}")

    print("Calling LLM (strict JSON) to detect related work boundaries + references...")
    result = call_llm_strict_for_boundaries(full_text, max_chars=25000)
    # Basic validation
    if not isinstance(result, dict) or "related_work" not in result:
        print("❌ Unexpected LLM response. Dumping and exiting.")
        print(result)
        sys.exit(1)

    outputs_dir = Path("outputs")
    process_pdf_hybrid_removal(pdf_path, result, outputs_dir=outputs_dir)

if __name__ == "__main__":
    main(sys.argv)
