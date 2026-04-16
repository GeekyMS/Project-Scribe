import os
import sys
import json
import re
import time
from pathlib import Path
from dotenv import load_dotenv
import httpx
import fitz
from openai import OpenAI


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

# LLM retry settings
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between retries


def extract_text_and_page_offsets(pdf_path: str):
    """Return full text and list of page start char offsets (0-based)."""
    doc = fitz.open(pdf_path)
    full_text = ""
    page_offsets = []
    
    for page in doc:

        page_offsets.append(len(full_text))

        # Insert page marker so the LLM knows the correct page number
        full_text += f"\n--- PAGE {page.number + 1} ---\n"

        page_rect = page.rect

        margin = page_rect.height * 0.10  
        
        clip_rect = fitz.Rect(
            0,               
            margin,          
            page_rect.width,
            page_rect.height - margin
        )
        
        # Extract text ONLY from the center body of the page
        page_text = page.get_text("text", clip=clip_rect, sort=True)
        
        full_text += page_text + "\n"
        
    doc.close()
    return full_text, page_offsets


def _build_prompt(snippet: str) -> str:
    """Build the LLM prompt for extracting Related Work boundaries."""
    return f"""You are an assistant that extracts the Related Work section boundaries from an academic paper.

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
  - references a general scientific theory or framework as background (e.g. "Maxwell's electrodynamics") without citing specific prior solutions or approaches,
  - mentions prior work only in a single sentence, or
  - has no substantive discussion of what previous approaches did and why they fall short.

When in doubt about whether the discussion is substantive enough, err on the side of including it.

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
    "related_work": null
}}

CRITICAL RULES:
- The text contains explicit page markers in the format "--- PAGE X ---". Use these markers to determine the correct page numbers.
- start_snippet and end_snippet must be EXACT text copied from the paper — do not paraphrase.
- Only choose sentences from the main body text. Avoid page numbers, headers, footers, or watermarks like 'Preprint'.
- The section starts AFTER the section heading and ends BEFORE the next major section heading.
- If the text provided is only a portion of the paper and you cannot find the section, return null.
- If you use the Introduction fallback, the start_snippet should be the first sentence of the Introduction, and the end_snippet should be the LAST sentence of the Introduction (i.e., the sentence just before the next major section heading begins). Include the entire Introduction — do not try to trim it to only the sentences that mention prior work.

Now analyze the following paper text:
---
{snippet}
---
Respond with JSON only."""


def _call_llm_single(snippet: str) -> dict:
    """Make a single LLM call with retry logic. Returns parsed dict or None on failure."""
    prompt = _build_prompt(snippet)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                timeout=90.0,  # per-request timeout
            )

            raw = response.choices[0].message.content

            if raw is None:
                print(f"   ⚠️ LLM returned empty content on attempt {attempt}")
                if attempt < MAX_RETRIES:
                    print(f"   Retrying in {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                continue

            # Filter out <think>...</think> tags (reasoning models)
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)

            # Regex to find the JSON object
            match = re.search(r'\{.*\}', raw, re.DOTALL)

            if match:
                try:
                    data = json.loads(match.group(0))
                    print(f"   LLM Raw Response (attempt {attempt}): {raw[:500]}...")

                    # Normalize keys
                    if "related_works" in data and "related_work" not in data:
                        data["related_work"] = data.pop("related_works")

                    if isinstance(data.get("related_work"), str):
                        print("   ⚠️ LLM returned related_work as a string. Attempting to recover...")
                        data["related_work"] = {
                            "start_page": 1,
                            "end_page": 1,
                            "start_snippet": data["related_work"][:100],
                            "end_snippet": "",
                        }
                    return data
                except json.JSONDecodeError:
                    print(f"   ⚠️ JSON parse failed on attempt {attempt}. Raw: {repr(raw[:100])}")

            else:
                print(f"   ⚠️ No JSON found in response on attempt {attempt}. Raw: {repr(raw[:100])}")

        except Exception as e:
            print(f"   ⚠️ LLM call failed on attempt {attempt}: {e}")

        if attempt < MAX_RETRIES:
            print(f"   Retrying in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)

    return None


def call_llm_strict_for_boundaries(text: str, max_chars=25000, overlap=5000):
    """
    Extract Related Work boundaries using a sliding window approach.
    
    Splits the text into overlapping chunks and calls the LLM on each.
    Collects all valid results, then prefers explicitly-labeled sections
    over fallback (intro-based) results.
    """
    text_len = len(text)
    
    # Build list of windows
    windows = []
    start = 0
    while start < text_len:
        end = min(start + max_chars, text_len)
        windows.append((start, end))
        if end >= text_len:
            break
        start += max_chars - overlap  # advance by (window - overlap)
    
    print(f"   📊 Text length: {text_len} chars → {len(windows)} window(s)")
    
    explicit_results = []   # results from labeled section headings
    fallback_results = []   # results from intro-based fallback
    
    for i, (win_start, win_end) in enumerate(windows):
        snippet = text[win_start:win_end]
        print(f"   🔍 Window {i+1}/{len(windows)}: chars [{win_start}–{win_end}]")
        
        result = _call_llm_single(snippet)
        
        if result is None:
            print(f"   ❌ Window {i+1} returned no valid response")
            continue
        
        rw = result.get("related_work")
        
        # If LLM says null (no related work found in this chunk), try next chunk
        if rw is None:
            print(f"   ℹ️ Window {i+1}: LLM found no Related Work section in this chunk")
            continue
        
        # Validate that we got meaningful data
        if isinstance(rw, dict) and (rw.get("start_snippet") or rw.get("start_page")):
            source = rw.get("source", "explicit")  # default to explicit if field missing
            if source == "fallback":
                print(f"   📎 Window {i+1}: Found fallback (intro-based) Related Work")
                fallback_results.append(result)
            else:
                print(f"   ✅ Window {i+1}: Found explicitly-labeled Related Work section!")
                explicit_results.append(result)
        else:
            print(f"   ⚠️ Window {i+1}: LLM returned empty/invalid related_work object")
    
    # Prefer explicit results over fallback
    if explicit_results:
        # Merge overlapping explicit results across windows:
        # Use earliest start_page, latest end_page, start_snippet from earliest, end_snippet from latest
        def get_page(r, key):
            rw = r.get("related_work", {})
            return int(rw.get(key, 0)) if isinstance(rw, dict) else 0
        
        # Sort by start_page
        explicit_results.sort(key=lambda r: get_page(r, "start_page"))
        
        first = explicit_results[0]
        last = explicit_results[-1]
        
        # Merge: take start info from first, end info from last
        merged_rw = dict(first.get("related_work", {}))
        last_rw = last.get("related_work", {})
        merged_rw["end_page"] = max(get_page(first, "end_page"), get_page(last, "end_page"))
        if last_rw.get("end_snippet"):
            merged_rw["end_snippet"] = last_rw["end_snippet"]
        
        chosen = {"related_work": merged_rw}
        print(f"   🏆 Using explicitly-labeled section (merged from {len(explicit_results)} hit(s), pages {merged_rw.get('start_page')}-{merged_rw.get('end_page')})")
        return chosen
    
    if fallback_results:
        chosen = fallback_results[0]
        print(f"   📎 Using fallback intro-based result (no explicit section found)")
        return chosen
    
    # If no window found anything, return null (paper has no related work section)
    print(f"   ℹ️ No Related Work section found in any window")
    return {"related_work": None}

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

    if result.get("related_work") is None:
        print("ℹ️ No Related Work section found in this paper.")
        # Save raw JSON for debugging
        outputs_dir = Path("outputs")
        outputs_dir.mkdir(parents=True, exist_ok=True)
        with open(outputs_dir / "related_work_raw.json", "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"📦 Raw response saved to {outputs_dir / 'related_work_raw.json'}")
        return

    outputs_dir = Path("outputs")
    process_pdf_hybrid_removal(pdf_path, result, outputs_dir=outputs_dir)

if __name__ == "__main__":
    main(sys.argv)
