"""
Prompt templates for Phase 2 — Related Works generation and evaluation.

Separate from pipeline/prompts.py (which handles Phase 1 extraction).
"""


def _build_length_constraint(target_word_count: int | None) -> str:
    """Return a length-guideline line for the generation prompt, or empty string."""
    if not target_word_count:
        return ""
    return (
        f"7. LENGTH GUIDELINE: The original Related Works section was approximately "
        f"{target_word_count} words. Write a section of similar length (within ±15%). "
        f"Do NOT pad with filler or cut important content to hit this target — "
        f"the primary goal is natural, comprehensive coverage at a comparable scale."
    )


def build_generation_prompt(
    paper_text: str,
    bib_content: str,
    enriched_entries: list[dict] | None = None,
    output_format: str = "both",
    target_word_count: int | None = None,
) -> str:
    """
    Build the Related Works generation prompt.
    
    Args:
        paper_text: Full paper text (markdown) with Related Works removed.
        bib_content: Raw .bib file content.
        enriched_entries: Optional enriched bib entries with abstracts.
        output_format: "latex", "markdown", or "both".
        target_word_count: Optional target word count for length matching.
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
            full_text = entry.get("full_text", "")
            
            part = f"### [{key}]\n"
            part += f"**Title:** {title}\n"
            part += f"**Authors:** {authors}\n"
            if year:
                part += f"**Year:** {year}\n"
            if venue:
                part += f"**Venue:** {venue}\n"
                
            if full_text:
                part += f"**Full Text:**\n<full_text>\n{full_text}\n</full_text>\n"
            elif abstract:
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
{_build_length_constraint(target_word_count)}

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
    target_word_count: int | None = None,
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

    # Build length instruction for manual prompt
    length_rule = ""
    if target_word_count:
        length_rule = (
            f"\n- LENGTH GUIDELINE: The original Related Works section was approximately "
            f"{target_word_count} words. Write a section of similar length (within ±15%). "
            f"Do NOT pad with filler or cut important content to hit this target — "
            f"the primary goal is natural, comprehensive coverage at a comparable scale."
        )

    prompt = f"""I need you to write a Related Works section for an academic paper. 

IMPORTANT RULES:
- Cite ONLY the papers listed below. Do NOT cite any other papers.
- Use author-date citation format, e.g., (Smith et al., 2021).
- Organize the section thematically with clear subsections.
- Be specific about what each paper contributes and how it relates to the current paper.{length_rule}

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


def build_argumentative_role_prompt(
    generated_text: str,
    ground_truth: str,
    paper_text: str,
    bib_content: str,
) -> str:
    """
    Build the Argumentative Role Analysis judge prompt (Tier 2B).

    This is a SEPARATE judge call from the 7-dimension quality scorer.
    It performs per-citation analysis across two dimensions:

      Metric 1 — Argumentative Role Classification:
        For each citation in both texts, classify the rhetorical role it serves
        (foundation, contrast, gap, support, method, context) and compare.

      Metric 2 — Selective Emphasis Accuracy:
        For each shared citation, compare which specific aspect of the cited
        paper is emphasized in the generated vs. original text.

    Returns a JSON schema with per-citation role mappings and emphasis
    comparisons that evaluator.py parses into aggregate scores.
    """
    # Truncate if needed to fit context
    gen_preview = generated_text[:20000]
    gt_preview = ground_truth[:20000]
    paper_preview = paper_text[:15000]

    return f"""You are an expert evaluator of academic argumentation. You will perform a detailed, per-citation analysis comparing a GENERATED Related Works section against the ORIGINAL from the same paper.

## CITATION ROLE TAXONOMY

Every citation in a Related Works section serves one of these rhetorical roles:

| Role Code | Role Name | Description | Example |
|-----------|-----------|-------------|---------|
| FND | Foundation | Work the current paper builds upon directly | "Building on Smith (2020), we extend..." |
| CTR | Contrast | Alternative approach cited to expose its limitations | "Unlike Jones (2019), which requires..." |
| GAP | Gap | Cited to demonstrate what the field is missing | "However, none of these address..." |
| SUP | Support | Corroborates a claim or validates the paper's premise | "This trend is well-documented (Chen, 2021)..." |
| MTD | Method | Cited for a specific technique, dataset, or tool adopted | "Following the protocol of Park (2020)..." |
| CTX | Context | Broad scene-setting with no specific argumentative function | "Machine learning has seen rapid advances..." |

## YOUR TASKS

### Task 1: Argumentative Role Classification
For every citation that appears in EITHER the generated or original text:
1. Identify the citation key
2. Extract the sentence(s) surrounding the citation in each text
3. Classify the rhetorical role using the taxonomy above
4. If the citation appears in both texts, determine if the roles match

### Task 2: Selective Emphasis Analysis
For every citation that appears in BOTH texts:
1. Identify what specific aspect of the cited paper the ORIGINAL text emphasizes
2. Identify what specific aspect the GENERATED text emphasizes
3. Rate the match as: "precise" (same aspect, same framing), "partial" (same area, different specific point), or "generic_or_different" (generic summary or entirely different aspect)

## INPUTS

### GENERATED Related Works:
---
{gen_preview}
---

### ORIGINAL Related Works (Ground Truth):
---
{gt_preview}
---

### Paper Context:
---
{paper_preview}
---

## OUTPUT FORMAT
Return ONLY valid JSON:
{{
    "citation_roles": {{
        "<citation_key>": {{
            "generated_role": "<FND|CTR|GAP|SUP|MTD|CTX|ABSENT>",
            "generated_context": "the sentence(s) from the generated text where this citation appears, or empty string if absent",
            "original_role": "<FND|CTR|GAP|SUP|MTD|CTX|ABSENT>",
            "original_context": "the sentence(s) from the original text where this citation appears, or empty string if absent",
            "role_match": "<exact|adjacent|mismatch|one_sided>"
        }}
    }},
    "emphasis_comparisons": {{
        "<citation_key>": {{
            "original_emphasis": "what specific aspect the original highlights about this work",
            "generated_emphasis": "what specific aspect the generated text highlights about this work",
            "match_level": "<precise|partial|generic_or_different>"
        }}
    }},
    "role_distribution": {{
        "generated": {{"FND": 0, "CTR": 0, "GAP": 0, "SUP": 0, "MTD": 0, "CTX": 0}},
        "original": {{"FND": 0, "CTR": 0, "GAP": 0, "SUP": 0, "MTD": 0, "CTX": 0}}
    }},
    "summary": "2-3 sentence summary of how well the generated text preserves the argumentative roles and emphasis of the original"
}}

Respond with JSON only."""


def build_coherence_prompt(
    generated_text: str,
    ground_truth: str,
    paper_text: str,
) -> str:
    """
    Build the Argumentative Coherence judge prompt (Tier 2C).

    This is a SEPARATE judge call that evaluates section-level
    argumentative properties across two dimensions:

      Metric 3 — Challenge-Citation Alignment:
        Extract the paper's core challenges/contributions, then check whether
        the citations in the generated RW serve those challenges.

      Metric 4 — Narrative Arc Scoring:
        Assess whether the section follows a logical argumentative progression
        from landscape → sub-problems → gaps → positioning.

    Returns a JSON schema with challenge mappings, alignment scores,
    and arc phase analysis.
    """
    # Truncate if needed to fit context
    gen_preview = generated_text[:20000]
    gt_preview = ground_truth[:20000]
    paper_preview = paper_text[:20000]

    return f"""You are an expert evaluator of academic argumentation. You will analyze the argumentative COHERENCE of a generated Related Works section — whether it builds a convincing case for the paper's contribution.

## YOUR TASKS

### Task 1: Challenge-Citation Alignment

**Step A — Extract Challenges:** Read the paper context below and identify the paper's 3-7 core research challenges, contributions, or motivating problems. These are typically stated in the abstract and introduction as the problems the paper addresses.

**Step B — Map Citations to Challenges:** For each citation in the GENERATED text, determine which challenge(s) it serves. A citation "serves" a challenge if it:
- Establishes prior work on that challenge (foundation/context)
- Shows limitations of existing approaches to that challenge (contrast/gap)
- Validates the importance of that challenge (support)
- Provides methodology relevant to addressing that challenge (method)

If a citation doesn't clearly serve ANY of the paper's challenges, mark it as "unconnected."

**Step C — Repeat for the ORIGINAL text.**

**Step D — Compute Alignment:**
- Challenge Coverage = fraction of challenges served by at least one citation
- Citation Relevance = fraction of citations connected to at least one challenge
- Alignment F1 = harmonic mean of coverage and relevance

### Task 2: Narrative Arc Analysis

A well-constructed Related Works follows an argumentative arc:

1. **LANDSCAPE** — Broad field overview, orienting the reader
2. **SUB-PROBLEMS** — Narrowing to specific relevant research areas
3. **GAPS & LIMITATIONS** — What existing work doesn't solve (builds tension)
4. **POSITIONING** — Where the current paper fits (resolves tension)

For the GENERATED text:
- Identify which paragraphs/passages correspond to each phase
- Rate whether the phases appear in a logical order
- Rate whether the balance across phases is appropriate

Repeat for the ORIGINAL text for comparison.

## INPUTS

### GENERATED Related Works:
---
{gen_preview}
---

### ORIGINAL Related Works (Ground Truth):
---
{gt_preview}
---

### Paper Context (for extracting challenges):
---
{paper_preview}
---

## OUTPUT FORMAT
Return ONLY valid JSON:
{{
    "challenges": [
        {{
            "id": "C1",
            "description": "brief description of the challenge/contribution"
        }}
    ],
    "challenge_citation_mapping": {{
        "generated": {{
            "<citation_key>": {{
                "challenges_served": ["C1", "C3"],
                "connection_explanation": "brief explanation of how this citation relates to the challenge(s)"
            }}
        }},
        "original": {{
            "<citation_key>": {{
                "challenges_served": ["C1"],
                "connection_explanation": "brief explanation"
            }}
        }}
    }},
    "alignment_scores": {{
        "generated": {{
            "challenge_coverage": 0.0,
            "citation_relevance": 0.0,
            "alignment_f1": 0.0,
            "unconnected_citations": ["list of citation keys not serving any challenge"]
        }},
        "original": {{
            "challenge_coverage": 0.0,
            "citation_relevance": 0.0,
            "alignment_f1": 0.0,
            "unconnected_citations": []
        }}
    }},
    "narrative_arc": {{
        "generated": {{
            "phases_present": {{
                "landscape": true,
                "sub_problems": true,
                "gaps_limitations": true,
                "positioning": true
            }},
            "phase_ordering_score": 0.0,
            "phase_balance_score": 0.0,
            "phase_details": {{
                "landscape": "which paragraph(s) / brief quote",
                "sub_problems": "which paragraph(s) / brief quote",
                "gaps_limitations": "which paragraph(s) / brief quote",
                "positioning": "which paragraph(s) / brief quote"
            }}
        }},
        "original": {{
            "phases_present": {{
                "landscape": true,
                "sub_problems": true,
                "gaps_limitations": true,
                "positioning": true
            }},
            "phase_ordering_score": 0.0,
            "phase_balance_score": 0.0,
            "phase_details": {{
                "landscape": "which paragraph(s) / brief quote",
                "sub_problems": "which paragraph(s) / brief quote",
                "gaps_limitations": "which paragraph(s) / brief quote",
                "positioning": "which paragraph(s) / brief quote"
            }}
        }}
    }},
    "narrative_arc_scores": {{
        "generated": {{
            "presence_score": 0.0,
            "ordering_score": 0.0,
            "balance_score": 0.0,
            "composite_arc_score": 0.0
        }},
        "original": {{
            "presence_score": 0.0,
            "ordering_score": 0.0,
            "balance_score": 0.0,
            "composite_arc_score": 0.0
        }}
    }},
    "coherence_summary": "2-3 sentence summary comparing the argumentative coherence of generated vs. original"
}}
"""


def build_evidence_support_prompt(claims_batch: list[dict]) -> str:
    """
    Build the Tier 4 evidence-support judge prompt.

    Unlike build_judge_prompt / build_argumentative_role_prompt (which
    both take a bib_content parameter and never interpolate it — the
    knowledge-leakage check asks whether claims go "beyond what's in
    the title/abstract" without ever showing the judge the abstracts),
    this prompt is the first to actually show the judge the cited
    paper's retrieved text and ask it to check the claim against it.

    Args:
        claims_batch: list of dicts, each with:
            citation_id, key, sentence, context, co_cited_keys,
            paper_title, evidence_text, evidence_kind ("full_text" or
            "abstract"). Callers must have already filtered out spans
            with no retrievable evidence — this prompt is only for
            citations where there is something to check against.

    Returns:
        A prompt whose response is a JSON object with one verdict per
        citation_id in the batch.
    """
    items_text = []
    for item in claims_batch:
        co_cited = item.get("co_cited_keys") or []
        co_cited_note = (
            f"\n**Also cited in this sentence:** {', '.join(co_cited)} "
            f"(attribute the claim to [{item['key']}] specifically, not to these)"
            if co_cited else ""
        )
        kind_label = "FULL TEXT (truncated)" if item.get("evidence_kind") == "full_text" else "ABSTRACT"
        items_text.append(f"""### Citation [{item['citation_id']}] — key: {item['key']}

**Sentence citing this paper:** "{item['sentence']}"
**Surrounding context:** "{item['context']}"{co_cited_note}

**Cited paper title:** {item.get('paper_title', 'Unknown')}
**Cited paper {kind_label}:**
<evidence>
{item.get('evidence_text', '')}
</evidence>
""")

    items_block = "\n".join(items_text)

    return f"""You are a rigorous fact-checker for academic writing. For each citation below, you are given the SENTENCE that cites a paper and the ACTUAL TEXT of that cited paper (its full text or abstract). Your job is NOT to judge writing quality — it is to check whether the cited paper actually supports what the sentence claims about it.

## YOUR TASK, PER CITATION
1. Identify the specific assertion the sentence makes about the cited paper — what is it claiming this paper did, found, showed, or argued? (`claim`)
2. Classify what kind of claim it is (`claim_type`):
   - "specific_finding" — a concrete result, number, or finding
   - "method_attribution" — a specific method/technique attributed to this paper
   - "general_topic" — a broad topical claim ("X studied Y")
   - "comparative" — a claim comparing this paper to something else
   - "other"
3. Check the claim against ONLY the evidence text provided. Do NOT use any outside knowledge of the paper, even if you recognize it. If the evidence doesn't mention something, treat it as unsupported by the evidence — not as false, just unconfirmed by what you were given.
4. Assign a verdict:
   - "supported" — the evidence text clearly backs the claim
   - "partially_supported" — the evidence is related but doesn't fully confirm the specific claim (e.g. confirms the general topic but not the specific number/finding cited)
   - "unsupported" — the evidence text has nothing relevant to the claim
   - "contradicted" — the evidence text says something that conflicts with the claim
   - "unverifiable" — the evidence text is present but too tangential/off-topic to judge the claim either way (this is different from "unsupported": use "unsupported" when the evidence is on-topic but silent on the specific claim, "unverifiable" when the evidence doesn't engage with the claim's subject at all)
5. If supported or partially supported, quote the specific span of evidence text that backs it (`evidence_quote`) — copy it verbatim, do not paraphrase.
6. Give a one-sentence justification (≤40 words).

## CITATIONS TO CHECK

{items_block}

## OUTPUT FORMAT
Return ONLY valid JSON, with exactly one entry per citation_id above:
{{
    "verdicts": [
        {{
            "citation_id": "<from the input>",
            "claim": "<the specific assertion made about this paper>",
            "claim_type": "specific_finding" | "method_attribution" | "general_topic" | "comparative" | "other",
            "verdict": "supported" | "partially_supported" | "unsupported" | "contradicted" | "unverifiable",
            "confidence": <0.0-1.0>,
            "evidence_quote": "<verbatim span from the evidence text, or empty string>",
            "justification": "<one sentence, <=40 words>"
        }}
    ]
}}

Respond with JSON only."""
