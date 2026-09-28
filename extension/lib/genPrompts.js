// Port of the two pipeline/gen_prompts.py functions the extension actually
// uses: build_generation_prompt and build_evidence_support_prompt. The
// other prompt builders there (judge/argumentative-role/coherence/
// synthetic-negative) are validation-campaign tooling, out of scope per
// the port plan.

(() => {
  function buildLengthConstraint(targetWordCount) {
    if (!targetWordCount) return "";
    return `7. LENGTH GUIDELINE: The original Related Works section was approximately ${targetWordCount} words. Write a section of similar length (within ±15%). Do NOT pad with filler or cut important content to hit this target — the primary goal is natural, comprehensive coverage at a comparable scale.`;
  }

  function buildGenerationPrompt({ paperText, bibContent, enrichedEntries = null, outputFormat = "both", targetWordCount = null }) {
    let referencesSection;
    if (enrichedEntries) {
      const parts = [];
      for (const entry of enrichedEntries) {
        const key = entry.key || "unknown";
        const title = entry.title || "Unknown Title";
        const authors = entry.author || "Unknown Authors";
        const year = entry.year || "";
        const venue = entry.booktitle || entry.journal || "";
        const abstract = entry.abstract || "";
        const fullText = entry.full_text || "";

        let part = `### [${key}]\n`;
        part += `**Title:** ${title}\n`;
        part += `**Authors:** ${authors}\n`;
        if (year) part += `**Year:** ${year}\n`;
        if (venue) part += `**Venue:** ${venue}\n`;
        if (fullText) {
          part += `**Full Text:**\n<full_text>\n${fullText}\n</full_text>\n`;
        } else if (abstract) {
          part += `**Abstract:** ${abstract}\n`;
        } else {
          part += `**Abstract:** Not available\n`;
        }
        parts.push(part);
      }
      referencesSection = parts.join("\n");
    } else {
      referencesSection = bibContent;
    }

    let formatInstructions;
    if (outputFormat === "latex") {
      formatInstructions = `Output the Related Works section in LaTeX format.
Use \\cite{key} commands for citations, where 'key' matches the BibTeX citation keys provided.
Example: \\cite{smith2021} or \\cite{smith2021, jones2022} for multiple citations.`;
    } else if (outputFormat === "markdown") {
      formatInstructions = `Output the Related Works section in Markdown format.
Use author-date citations in parentheses, e.g., (Smith et al., 2021) or (Smith & Jones, 2022).
At the end of your output, include a comment listing every citation key you used:
<!-- CITATION_KEYS: smith2021, jones2022, ... -->`;
    } else {
      formatInstructions = `Output the Related Works section in TWO formats, separated by the marker "---FORMAT_SEPARATOR---":

FORMAT 1 (LaTeX):
Use \\cite{key} commands for citations, where 'key' matches the BibTeX citation keys provided.

---FORMAT_SEPARATOR---

FORMAT 2 (Markdown):
Use author-date citations in parentheses, e.g., (Smith et al., 2021).
At the end of the markdown version, include a comment listing every citation key you used:
<!-- CITATION_KEYS: key1, key2, ... -->`;
    }

    return `You are an expert academic writer tasked with writing the Related Works section for a research paper.

## YOUR TASK
Write a comprehensive Related Works section for the paper described below. You must synthesize the relevant literature and position the paper's contributions within the broader research landscape.

## CRITICAL CONSTRAINTS
1. You must cite ONLY the papers provided in the reference list below. Do NOT cite any other papers.
2. Do NOT invent, hallucinate, or reference any papers not explicitly listed in the references.
3. Every claim you make about a cited paper must be supportable from the information provided (title, authors, venue, abstract) and the context of the main paper.
4. Organize the section thematically — group related papers into coherent subsections with clear transitions.
5. Don't just list papers — explain how they relate to each other and to the current paper's approach.
6. Be specific about what each cited work contributes and how it differs from or informs the current paper.
${buildLengthConstraint(targetWordCount)}

## OUTPUT FORMAT
${formatInstructions}

## THE PAPER (with Related Works section removed)
--- PAPER START ---
${paperText}
--- PAPER END ---

## AVAILABLE REFERENCES (you MUST use ONLY these)

### BibTeX Entries
\`\`\`bibtex
${bibContent}
\`\`\`

### Reference Details
${referencesSection}

## INSTRUCTIONS
Now write the Related Works section. Make it comprehensive, well-organized, and academically rigorous.
Cite papers using the exact citation keys from the BibTeX entries above.
Do NOT cite any paper not listed above. Do NOT make up citations.`;
  }

  // Groups a batch's items by paper key and emits each paper's evidence
  // text ONCE, followed by every citation of it — not once per citation.
  // Before this, a paper cited 5 times in one batch had its full text (up
  // to several thousand chars, more with Phase 3's structured full text)
  // repeated 5 times in the prompt. Grouping also gives Anthropic prompt
  // caching (see llmProviders.js's cache_control wiring) a stable,
  // non-duplicated block per paper to work with.
  function groupClaimsByPaper(claimsBatch) {
    const order = [];
    const byKey = new Map();
    for (const item of claimsBatch) {
      if (!byKey.has(item.key)) {
        byKey.set(item.key, { key: item.key, paper_title: item.paper_title, evidence_kind: item.evidence_kind, evidence_text: item.evidence_text, items: [] });
        order.push(item.key);
      }
      byKey.get(item.key).items.push(item);
    }
    return order.map((k) => byKey.get(k));
  }

  // Static instructions, identical across every batch of a job — split out
  // from the per-batch content specifically so it can be sent as an
  // Anthropic cache_control-marked system block (see llmProviders.js). A
  // job with N batches previously paid full price for these ~300 tokens N
  // times; this makes only the first pay full price and the rest read
  // from cache. It's a modest saving (the dominant cost is each paper's
  // evidence text, not this preamble) but a real and low-risk one — it
  // doesn't depend on solving how to keep one paper's evidence text
  // aligned to the same cache breakpoint across non-consecutive batches.
  function buildEvidenceSupportSystemPrompt() {
    return `You are a rigorous fact-checker for academic writing. For each citation given, you are given the SENTENCE that cites a paper and the ACTUAL TEXT of that cited paper (its full text or abstract). Your job is NOT to judge writing quality — it is to check whether the cited paper actually supports what the sentence claims about it.

## YOUR TASK, PER CITATION
1. Identify the specific assertion the sentence makes about the cited paper — what is it claiming this paper did, found, showed, or argued? (\`claim\`)
2. Classify what kind of claim it is (\`claim_type\`):
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
5. If supported or partially supported, quote the specific span of evidence text that backs it (\`evidence_quote\`) — copy it verbatim, do not paraphrase.
6. Give a one-sentence justification (≤40 words).

## OUTPUT FORMAT
Return ONLY valid JSON, with exactly one entry per citation_id given:
{
    "verdicts": [
        {
            "citation_id": "<from the input>",
            "claim": "<the specific assertion made about this paper>",
            "claim_type": "specific_finding" | "method_attribution" | "general_topic" | "comparative" | "other",
            "verdict": "supported" | "partially_supported" | "unsupported" | "contradicted" | "unverifiable",
            "confidence": <0.0-1.0>,
            "evidence_quote": "<verbatim span from the evidence text, or empty string>",
            "justification": "<one sentence, <=40 words>"
        }
    ]
}

Respond with JSON only.`;
  }

  function buildEvidenceSupportUserPrompt(claimsBatch) {
    const papers = groupClaimsByPaper(claimsBatch);
    const paperBlocks = papers.map((p) => {
      const kindLabel = p.evidence_kind === "full_text" ? "FULL TEXT (truncated)" : "ABSTRACT";
      const claimsText = p.items.map((item) => {
        const coCited = item.co_cited_keys || [];
        const coCitedNote = coCited.length
          ? ` (also cited alongside: ${coCited.join(", ")} — attribute the claim to [${p.key}] specifically, not to these)`
          : "";
        return `- Citation [${item.citation_id}] — sentence: "${item.sentence}"\n  Surrounding context: "${item.context}"${coCitedNote}`;
      }).join("\n");

      return `### Paper: ${p.paper_title || "Unknown"} (key: ${p.key})

**${kindLabel}:**
<evidence>
${p.evidence_text || ""}
</evidence>

**Sentences citing this paper, to check individually:**
${claimsText}
`;
    });

    return `## CITATIONS TO CHECK\n\n${paperBlocks.join("\n")}`;
  }

  // Kept for any external/debug caller that wants the whole thing as one
  // string (e.g. logging what was sent) — evaluator.js itself calls the
  // system/user builders separately so it can mark the system half
  // cacheable.
  function buildEvidenceSupportPrompt(claimsBatch) {
    return `${buildEvidenceSupportSystemPrompt()}\n\n${buildEvidenceSupportUserPrompt(claimsBatch)}`;
  }

  self.ScribeGenPrompts = {
    buildGenerationPrompt, buildEvidenceSupportPrompt,
    buildEvidenceSupportSystemPrompt, buildEvidenceSupportUserPrompt,
  };
})();
