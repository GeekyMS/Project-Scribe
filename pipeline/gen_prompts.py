"""
Prompt templates for Phase 2 — Related Works generation and evaluation.

Separate from pipeline/prompts.py (which handles Phase 1 extraction).
"""


def build_generation_prompt(
    paper_text: str,
    bib_content: str,
    enriched_entries: list[dict] | None = None,
    output_format: str = "both",
) -> str:
    """
    Build the Related Works generation prompt.
    
    Args:
        paper_text: Full paper text (markdown) with Related Works removed.
        bib_content: Raw .bib file content.
        enriched_entries: Optional enriched bib entries with abstracts.
        output_format: "latex", "markdown", or "both".
    """
    # Build the enriched references section if available
    references_section = ""
    if enriched_entries:
        ref_parts = []
        for entry in enriched_entries:
            key = entry.get("key", "unknown")
            title = entry.get("title", "Unknown Title")
            authors = entry.get("author", "Unknown Authors")
            year = entry.get("year", "")
            venue = entry.get("booktitle", "") or entry.get("journal", "") or ""
            abstract = entry.get("abstract", "")
            
            part = f"### [{key}]\n"
            part += f"**Title:** {title}\n"
            part += f"**Authors:** {authors}\n"
            if year:
                part += f"**Year:** {year}\n"
            if venue:
                part += f"**Venue:** {venue}\n"
            if abstract:
                part += f"**Abstract:** {abstract}\n"
            else:
                part += f"**Abstract:** Not available\n"
            
            ref_parts.append(part)
        
        references_section = "\n".join(ref_parts)
    else:
        references_section = bib_content

    # Format instructions
    if output_format == "latex":
        format_instructions = """Output the Related Works section in LaTeX format.
Use \\cite{key} commands for citations, where 'key' matches the BibTeX citation keys provided.
Example: \\cite{smith2021} or \\cite{smith2021, jones2022} for multiple citations."""
    elif output_format == "markdown":
        format_instructions = """Output the Related Works section in Markdown format.
Use author-date citations in parentheses, e.g., (Smith et al., 2021) or (Smith & Jones, 2022).
At the end of your output, include a comment listing every citation key you used:
<!-- CITATION_KEYS: smith2021, jones2022, ... -->"""
    else:  # both
        format_instructions = """Output the Related Works section in TWO formats, separated by the marker "---FORMAT_SEPARATOR---":

FORMAT 1 (LaTeX):
Use \\cite{key} commands for citations, where 'key' matches the BibTeX citation keys provided.

---FORMAT_SEPARATOR---

FORMAT 2 (Markdown):
Use author-date citations in parentheses, e.g., (Smith et al., 2021).
At the end of the markdown version, include a comment listing every citation key you used:
<!-- CITATION_KEYS: key1, key2, ... -->"""

    prompt = f"""You are an expert academic writer tasked with writing the Related Works section for a research paper.

## YOUR TASK
Write a comprehensive Related Works section for the paper described below. You must synthesize the relevant literature and position the paper's contributions within the broader research landscape.

## CRITICAL CONSTRAINTS
1. You must cite ONLY the papers provided in the reference list below. Do NOT cite any other papers.
2. Do NOT invent, hallucinate, or reference any papers not explicitly listed in the references.
3. Every claim you make about a cited paper must be supportable from the information provided (title, authors, venue, abstract) and the context of the main paper.
4. Organize the section thematically — group related papers into coherent subsections with clear transitions.
5. Don't just list papers — explain how they relate to each other and to the current paper's approach.
6. Be specific about what each cited work contributes and how it differs from or informs the current paper.

## OUTPUT FORMAT
{format_instructions}

## THE PAPER (with Related Works section removed)
--- PAPER START ---
{paper_text}
--- PAPER END ---

## AVAILABLE REFERENCES (you MUST use ONLY these)

### BibTeX Entries
```bibtex
{bib_content}
```

### Reference Details
{references_section}

## INSTRUCTIONS
Now write the Related Works section. Make it comprehensive, well-organized, and academically rigorous.
Cite papers using the exact citation keys from the BibTeX entries above.
Do NOT cite any paper not listed above. Do NOT make up citations."""

    return prompt


def build_manual_prompt(
    paper_text: str,
    bib_content: str,
    enriched_entries: list[dict] | None = None,
) -> str:
    """
    Build a prompt formatted for manual use in web-based LLMs.
    
    This version is self-contained (no JSON format requirements) and
    includes instructions for the user to paste into ChatGPT/Claude web.
    
    The web LLM may have search capabilities — this is intentional for
    testing whether allowing the model to look up papers improves quality.
    """
    # Build reference list
    ref_parts = []
    if enriched_entries:
        for entry in enriched_entries:
            key = entry.get("key", "unknown")
            title = entry.get("title", "Unknown Title")
            authors = entry.get("author", "Unknown Authors")
            year = entry.get("year", "")
            abstract = entry.get("abstract", "")
            
            part = f"- [{key}] {authors} ({year}). \"{title}\""
            if abstract:
                part += f"\n  Abstract: {abstract}"
            ref_parts.append(part)
        references_section = "\n\n".join(ref_parts)
    else:
        references_section = bib_content

    prompt = f"""I need you to write a Related Works section for an academic paper. 

IMPORTANT RULES:
- Cite ONLY the papers listed below. Do NOT cite any other papers.
- Use author-date citation format, e.g., (Smith et al., 2021).
- Organize the section thematically with clear subsections.
- Be specific about what each paper contributes and how it relates to the current paper.

THE PAPER (with Related Works removed):
---
{paper_text}
---

AVAILABLE REFERENCES (use ONLY these):
{references_section}

Please write the Related Works section now. At the end, list every citation key you used."""

    return prompt


def build_judge_prompt(
    generated_text: str,
    ground_truth: str,
    paper_text: str,
    bib_content: str,
) -> str:
    """
    Build the LLM-as-judge evaluation prompt.
    
    A different model evaluates the generated Related Works against
    the original on multiple quality dimensions.
    """
    # Truncate if needed to fit context
    gen_preview = generated_text[:20000]
    gt_preview = ground_truth[:20000]
    paper_preview = paper_text[:15000]
    
    return f"""You are an expert evaluator of academic writing. You will evaluate a GENERATED Related Works section by comparing it against the ORIGINAL Related Works section from the same paper.

## EVALUATION DIMENSIONS
Score each dimension on a 1-5 scale:

1. **Citation Accuracy** (1-5): Are the cited papers described correctly? Does the generated text accurately represent what each paper contributes?
   - 1 = Major factual errors about cited works
   - 3 = Mostly correct but some inaccuracies or vague descriptions  
   - 5 = All citations are accurately described

2. **Coverage** (1-5): Does the generated section cover the same research themes and topics as the original?
   - 1 = Misses most major themes
   - 3 = Covers main themes but misses some important ones
   - 5 = Comprehensive coverage matching the original

3. **Positioning** (1-5): Does it properly frame the paper's contribution relative to prior work?
   - 1 = Fails to explain how the paper differs from/builds on prior work
   - 3 = Some positioning but not as clear as the original
   - 5 = Clearly and accurately positions the paper's contributions

4. **Depth** (1-5): Does it engage substantively with cited work, or just name-drop?
   - 1 = Pure listing of papers with no discussion
   - 3 = Some discussion but superficial in places
   - 5 = Substantive engagement with each cited work's contributions and limitations

5. **Flow** (1-5): Does the narrative connect papers into a coherent story?
   - 1 = Disconnected listing with no transitions
   - 3 = Reasonable structure but some abrupt transitions
   - 5 = Smooth, logically connected narrative

6. **Organization** (1-5): Is the thematic grouping logical and well-structured?
   - 1 = No clear organization
   - 3 = Some structure but could be better organized
   - 5 = Clear, logical thematic grouping with appropriate subsections

7. **Overall Quality** (1-5): Holistic assessment — would this pass peer review as a Related Works section?
   - 1 = Unacceptable for publication
   - 3 = Adequate but needs significant revision
   - 5 = Publication-ready, comparable to human-written

## KNOWLEDGE LEAKAGE DETECTION
Also check for signs that the model used knowledge beyond what was provided:
- Does the generated text describe methodological details of cited papers that go far beyond what's in the title/abstract?
- Does it make claims about papers that could only come from reading the full paper?
- Note: Some knowledge from pre-training is expected and acceptable. Flag only egregious cases.

## INPUTS

### GENERATED Related Works:
---
{gen_preview}
---

### ORIGINAL Related Works (Ground Truth):
---
{gt_preview}
---

### Paper Context (for understanding what the paper is about):
---
{paper_preview}
---

## OUTPUT FORMAT
Return ONLY valid JSON:
{{
    "scores": {{
        "citation_accuracy": <1-5>,
        "coverage": <1-5>,
        "positioning": <1-5>,
        "depth": <1-5>,
        "flow": <1-5>,
        "organization": <1-5>,
        "overall_quality": <1-5>
    }},
    "score_justifications": {{
        "citation_accuracy": "explanation",
        "coverage": "explanation",
        "positioning": "explanation",
        "depth": "explanation",
        "flow": "explanation",
        "organization": "explanation",
        "overall_quality": "explanation"
    }},
    "knowledge_leakage": {{
        "detected": true or false,
        "evidence": ["list of specific examples where the model used knowledge beyond provided context"],
        "severity": "none" | "minor" | "major"
    }},
    "strengths": ["list of things the generated section does well"],
    "weaknesses": ["list of things the generated section does poorly"],
    "comparison_summary": "2-3 sentence summary of how the generated section compares to the original"
}}

Respond with JSON only."""
