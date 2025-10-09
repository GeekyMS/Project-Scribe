import re
import fitz
import argparse
import os

def extract_text_from_pdf(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        return text
    except Exception as e:
        print(f"Error: Failed to read or process PDF file '{pdf_path}'. Reason: {e}")
        return None

def find_section_indices(text, section_keywords):
    start_index = -1
    section_title_match = None
    for keyword in section_keywords:
        pattern = re.compile(r'^\d*\.?\s*' + re.escape(keyword) + r'\s*$', re.IGNORECASE | re.MULTILINE)
        match = pattern.search(text)
        if match:
            start_index = match.start()
            section_title_match = match
            break
    if start_index == -1: return None, None
    next_section_pattern = re.compile(r'^\d+\.?\s+[A-Z].*$', re.MULTILINE)
    next_match = next_section_pattern.search(text, pos=section_title_match.end())
    if next_match:
        end_index = next_match.start()
    else:
        fallback_keywords = ['Conclusion', 'References', 'Bibliography', 'Appendix']
        earliest_end_pos = len(text)
        for end_key in fallback_keywords:
            end_pattern = re.compile(r'^\d*\.?\s*' + re.escape(end_key), re.IGNORECASE | re.MULTILINE)
            end_match = end_pattern.search(text, pos=section_title_match.end())
            if end_match and end_match.start() < earliest_end_pos:
                earliest_end_pos = end_match.start()
        end_index = earliest_end_pos
    return start_index, end_index

def redact_section_and_save_pdf(pdf_path, start_idx, end_idx, output_pdf_path):
    doc = fitz.open(pdf_path)
    char_count = 0
    print(f"Redacting section and creating new PDF: {output_pdf_path}")
    for page in doc:
        blocks = page.get_text("blocks")
        for b in blocks:
            block_text = b[4]
            block_rect = fitz.Rect(b[0], b[1], b[2], b[3])
            block_start_index = char_count
            block_end_index = char_count + len(block_text)
            if not (block_end_index < start_idx or block_start_index > end_idx):
                page.add_redact_annot(block_rect)
            char_count += len(block_text) + 1
        page.apply_redactions()
    doc.save(output_pdf_path, garbage=4, deflate=True, clean=True)
    doc.close()

def parse_citations_from_section(text_section):
    """
    Parses a block of text to find citations. Automatically detects if the style
    is numerical (e.g., [1, 2]) or author-date (e.g., [Smith, 2025]).

    Returns a dictionary indicating the citation style and a set of keys to find.
    """
    # First, try to find numerical citations.
    numerical_matches = re.findall(r'\[([\d,\s-]+)\]', text_section)
    cited_numbers = set()
    is_numerical = False

    if numerical_matches:
        temp_numbers = set()
        total_parts = 0
        for match in numerical_matches:
            parts = match.split(',')
            for part in parts:
                part = part.strip()
                total_parts += 1
                if '-' in part:
                    try:
                        start, end = map(int, part.split('-'))
                        if start <= end: temp_numbers.update(range(start, end + 1))
                    except ValueError: continue
                elif part.isdigit():
                    temp_numbers.add(int(part))
        # If we successfully parsed numbers from at least half the matches, assume it's numerical.
        if len(temp_numbers) > 0 and len(temp_numbers) >= total_parts / 2:
            is_numerical = True
            cited_numbers = temp_numbers

    if is_numerical:
        print("Detected numerical citation style.")
        return {'type': 'numerical', 'keys': cited_numbers}

    # If not clearly numerical, switch to author-date parsing.
    print("Detected author-date citation style.")

    author_date_matches = re.findall(r'[\(\[][A-Za-z &.,]+ et al\.?,? \d{4}[\)\]]|[\(\[][A-Za-z &.,]+, \d{4}[\)\]]', text_section)


    cleaned_keys = {re.sub(r'[\(\)\[\]]', '', m).strip() for m in author_date_matches}
    return {'type': 'author-date', 'keys': cleaned_keys}


def extract_references_by_key(full_text, citation_info):
    """
    Extracts full reference strings from the bibliography by matching the citation keys.
    Handles both 'numerical' and 'author-date' styles.
    """
    references_start_match = re.search(r'^\s*(References|Bibliography)\s*$', full_text, re.IGNORECASE | re.MULTILINE)
    if not references_start_match:
        return ["Could not find the 'References' or 'Bibliography' section in the paper."]

    references_text = full_text[references_start_match.start():]


    if citation_info['type'] == 'numerical':
        references_dict = {}
        potential_entries = re.split(r'\n(?=\[\d+\])', references_text)
        for entry in potential_entries:
            entry = entry.strip()
            if not entry: continue
            match = re.match(r'\[(\d+)\]', entry)
            if match:
                num = int(match.group(1))
                references_dict[num] = entry
        return [references_dict[num] for num in sorted(list(citation_info['keys'])) if num in references_dict]


    elif citation_info['type'] == 'author-date':
        found_papers = set()
        # Split references by blank lines
        all_references = re.split(r'\n\s*\n', references_text)

        for key in citation_info['keys']: # e.g., key is "Smith et al., 2025"
            # Extract the first author name and the year from the citation key
            match = re.search(r'([A-Za-z\-]+).*?(\d{4})', key)
            if not match: continue

            author_name, year = match.groups()

            # Search for a matching entry in the bibliography
            for ref_entry in all_references:
                if author_name.lower() in ref_entry.lower() and year in ref_entry:
                    found_papers.add(ref_entry.strip())
                    break
        return sorted(list(found_papers))

    return ["Unknown citation style."]


def main():
    parser = argparse.ArgumentParser(
        description="Processes a research paper PDF to remove its 'Related Work' section and extract the citations from it.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("pdf_path", type=str, help="Path to the research paper PDF file.")
    args = parser.parse_args()

    if not os.path.exists(args.pdf_path):
        print(f"Error: The file '{args.pdf_path}' does not exist.")
        return

    print(f"Processing '{args.pdf_path}'...")
    full_text = extract_text_from_pdf(args.pdf_path)
    if not full_text: return

    related_work_keywords = ['Related Work', 'Literature Review', 'Previous Work']
    start_idx, end_idx = find_section_indices(full_text, related_work_keywords)

    base_filename = os.path.splitext(os.path.basename(args.pdf_path))[0]
    output_paper_path = f"{base_filename}_without_related_work.pdf"
    output_citations_path = f"{base_filename}_related_work_citations.txt"
    citations = []

    if start_idx is None:
        print("Warning: Could not find a 'Related Work' or similar section. No PDF will be generated.")
        citations = ["Section to analyze was not found."]
    else:
        redact_section_and_save_pdf(args.pdf_path, start_idx, end_idx, output_paper_path)
        print(f"\n✅ Success! New PDF saved to: {output_paper_path}")

        related_work_text = full_text[start_idx:end_idx]
        print("Finding citations in the section...")
        citation_info = parse_citations_from_section(related_work_text)

        if not citation_info['keys']:
            citations = ["No valid citations were found in the section."]
        else:
            print(f"Found {len(citation_info['keys'])} unique citations. Extracting from references...")
            citations = extract_references_by_key(full_text, citation_info)

    if citations:
        with open(output_citations_path, "w", encoding="utf-8") as f:
            if citations and not citations[0].startswith("Could not find") and not citations[0].startswith("No valid"):
                f.write(f"Found {len(citations)} papers cited in the 'Related Work' section:\n\n")

            for citation in citations:
                f.write(f"{citation}\n\n")
        print(f"✅ Success! Cited papers saved to: {output_citations_path}")

if __name__ == '__main__':
    main()