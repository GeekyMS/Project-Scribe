// Port of pipeline/evaluator.py's evaluate_citations (Tier 1) and
// evaluate_evidence_support (Tier 4). Tier 2/2B/2C (LLM-as-judge quality
// scoring, argumentative role/coherence analysis) are validation-campaign
// tooling, out of scope per the port plan — only citation-key checking and
// evidence-support judging are part of the live Draft/Check path.

(() => {
  const MAX_RETRIES = 2;
  const RETRY_DELAY_MS = 10000;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ── Tier 1 ──
  function evaluateCitations(generatedText, bibEntries) {
    // Not deduped — matches evaluate_citations()'s `sorted([e["key"] for e
    // in entries])`, which keeps duplicates if the bib itself has a
    // repeated key.
    const bibKeys = bibEntries.map((e) => e.key).sort();
    const bibKeySet = new Set(bibKeys);

    const spans = self.ScribeCitationSpans.findCitationSpans(generatedText, bibEntries);

    const ckMatch = generatedText.match(/<!--\s*CITATION_KEYS:\s*(.*?)\s*-->/);
    const ckKeys = new Set();
    if (ckMatch) {
      for (const k of ckMatch[1].split(",")) {
        const t = k.trim();
        if (t) ckKeys.add(t);
      }
    }

    const citedKeys = new Set([...spans.map((s) => s.key), ...ckKeys]);
    citedKeys.delete("");

    const citedKeysArr = [...citedKeys].sort();
    const hallucinated = citedKeysArr.filter((k) => !bibKeySet.has(k)).sort();
    const missing = bibKeys.filter((k) => !citedKeys.has(k)).sort();

    const totalCitations = spans.length ? spans.length : ckKeys.size;

    let precision = 1.0;
    if (citedKeys.size) {
      let validCited = 0;
      for (const k of citedKeys) if (bibKeySet.has(k)) validCited++;
      precision = validCited / citedKeys.size;
    }

    let recall = 1.0;
    if (bibKeySet.size) {
      let citedFromBib = 0;
      for (const k of bibKeySet) if (citedKeys.has(k)) citedFromBib++;
      recall = citedFromBib / bibKeySet.size;
    }

    return {
      bib_keys: bibKeys,
      cited_keys: citedKeysArr,
      hallucinated_keys: hallucinated,
      missing_keys: missing,
      recall,
      precision,
      total_citation_instances: totalCitations,
      unique_citations: citedKeysArr.length,
      bib_entry_count: bibKeys.length,
    };
  }

  // ── Judge call: retry + <think>-strip + JSON extraction ──
  // Mirrors pipeline/evaluator.py's _call_judge — always regexes `\{.*\}`
  // out of the raw response rather than trusting response_format alone
  // (Anthropic has no response_format equivalent, and even OpenAI-shaped
  // providers occasionally wrap JSON in prose despite the instruction).
  async function callJudge(prompt, { provider, apiKey, baseUrl, model, timeoutMs = 180000 }) {
    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      let raw;
      try {
        raw = await self.ScribeLLM.chatComplete({
          provider, apiKey, baseUrl, model,
          messages: [{ role: "user", content: prompt }],
          jsonMode: true,
        });
      } catch (e) {
        if (attempt < MAX_RETRIES) {
          await sleep(RETRY_DELAY_MS);
          continue;
        }
        return [null, String(e)];
      }
      if (raw == null) {
        if (attempt < MAX_RETRIES) { await sleep(RETRY_DELAY_MS); continue; }
        return [null, "empty response"];
      }
      raw = raw.replace(/<think>[\s\S]*?<\/think>/g, "");
      const match = raw.match(/\{[\s\S]*\}/);
      if (match) {
        try {
          return [JSON.parse(match[0]), null];
        } catch (e) {
          if (attempt < MAX_RETRIES) { await sleep(RETRY_DELAY_MS); continue; }
          return [null, `invalid JSON: ${e}`];
        }
      }
      if (attempt < MAX_RETRIES) await sleep(RETRY_DELAY_MS);
    }
    return [null, "no valid JSON returned"];
  }

  function entryEvidenceKind(entry, maxFullTextChars) {
    const fullText = entry.full_text || "";
    if (fullText) return ["full_text", fullText.slice(0, maxFullTextChars)];
    const abstract = entry.abstract || "";
    if (abstract) return ["abstract", abstract];
    return ["none", ""];
  }

  // ── Tier 4 ──
  async function evaluateEvidenceSupport(
    generatedText,
    bibEntries,
    enrichedEntries,
    { provider, apiKey, baseUrl, model, maxFullTextChars = 6000, batchSize = 8 } = {}
  ) {
    const result = {
      per_citation: [], support_rate: 0, verifiable_count: 0, unverifiable_count: 0,
      total_spans: 0, evidence_coverage: 0, verdict_counts: {}, contradicted_keys: [],
      unverifiable_reasons: {}, evidence_kind_counts: {}, judge_model: model, n_batches: 0, error: null,
    };

    const entrySource = enrichedEntries != null ? enrichedEntries : (bibEntries || []);
    const entryByKey = new Map(entrySource.map((e) => [e.key || "", e]));

    // Python's `bib_entries or (enriched_entries or [])` treats an empty
    // list as falsy and falls through; JS's `[] || x` does not (empty
    // arrays are truthy), so this checks .length explicitly to match.
    const spanBibSource = (bibEntries && bibEntries.length) ? bibEntries : ((enrichedEntries && enrichedEntries.length) ? enrichedEntries : []);
    const spans = self.ScribeCitationSpans.findCitationSpans(generatedText, spanBibSource);
    result.total_spans = spans.length;
    if (spans.length === 0) return result;

    const LOW_CONFIDENCE_STATUSES = new Set(["matched_low_confidence"]);
    const SCORE_WEIGHTS = { supported: 1.0, partially_supported: 0.5, unsupported: 0.0, contradicted: 0.0 };

    const verifiableItems = [];
    const rowByCitationId = new Map();

    for (const span of spans) {
      const row = {
        citation_id: span.citation_id,
        citation_key: span.key,
        sentence_index: span.sentence_index,
        sentence: span.sentence,
        // Offsets into the *checked text*, carried purely as a
        // disambiguation hint for the panel's click-to-reveal: when a
        // sentence occurs many times (long_test.tex is 280 near-identical
        // paragraphs) it picks which occurrence to jump to. Not load-
        // bearing — reveal locates by searching the live document, so it
        // still works when these are absent (the Python path historically
        // dropped them) or stale (the document was edited since).
        char_start: span.char_start,
        char_end: span.char_end,
        co_cited_keys: span.co_cited_keys,
        style: span.style,
        resolution: span.resolution,
        evidence_kind: "none",
        evidence_status: null,
        evidence_chars: 0,
        claim: null,
        claim_type: null,
        verdict: "unverifiable",
        verdict_source: null,
        score: null,
        confidence: null,
        evidence_quote: "",
        justification: "",
        batch_index: null,
      };
      rowByCitationId.set(span.citation_id, row);

      const entry = entryByKey.get(span.key);
      if (entry == null || span.resolution === "unmatched") {
        row.verdict_source = "key_not_in_bib";
        continue;
      }

      const evidenceStatus = (entry.evidence || {}).status;
      row.evidence_status = evidenceStatus;

      if (LOW_CONFIDENCE_STATUSES.has(evidenceStatus)) {
        row.verdict_source = "low_confidence_match";
        continue;
      }

      const [evidenceKind, evidenceText] = entryEvidenceKind(entry, maxFullTextChars);
      row.evidence_kind = evidenceKind;
      row.evidence_chars = evidenceText.length;

      if (evidenceKind === "none") {
        row.verdict_source = "no_evidence";
        continue;
      }

      verifiableItems.push({
        citation_id: span.citation_id,
        key: span.key,
        sentence: span.sentence,
        context: span.context,
        co_cited_keys: span.co_cited_keys,
        paper_title: entry.title || "Unknown",
        evidence_text: evidenceText,
        evidence_kind: evidenceKind,
      });
    }

    let nBatches = 0;
    for (let batchStart = 0; batchStart < verifiableItems.length; batchStart += batchSize) {
      const batch = verifiableItems.slice(batchStart, batchStart + batchSize);
      if (batch.length === 0) continue;
      nBatches++;
      const prompt = self.ScribeGenPrompts.buildEvidenceSupportPrompt(batch);
      const [data, error] = await callJudge(prompt, { provider, apiKey, baseUrl, model, timeoutMs: 180000 });

      if (!data) {
        for (const item of batch) {
          const row = rowByCitationId.get(item.citation_id);
          if (row) row.verdict_source = "judge_call_failed";
        }
        result.error = result.error || error || "no valid JSON returned";
        continue;
      }

      const returnedIds = new Set();
      for (const verdict of data.verdicts || []) {
        const citationId = verdict.citation_id || "";
        const row = rowByCitationId.get(citationId);
        if (!row) continue;
        returnedIds.add(citationId);
        row.claim = verdict.claim || "";
        row.claim_type = verdict.claim_type || "other";
        row.verdict = verdict.verdict || "unverifiable";
        row.verdict_source = "judge";
        row.confidence = verdict.confidence ?? null;
        row.evidence_quote = verdict.evidence_quote || "";
        row.justification = verdict.justification || "";
        row.score = Object.prototype.hasOwnProperty.call(SCORE_WEIGHTS, row.verdict) ? SCORE_WEIGHTS[row.verdict] : null;
        row.batch_index = nBatches;
      }

      for (const item of batch) {
        const row = rowByCitationId.get(item.citation_id);
        if (row && !returnedIds.has(item.citation_id)) row.verdict_source = "judge_omitted_from_response";
      }
    }
    result.n_batches = nBatches;

    const perCitation = [...rowByCitationId.values()];
    result.per_citation = perCitation;

    const verdictCounts = {};
    const unverifiableReasons = {};
    const evidenceKindCounts = {};
    const scores = [];
    const contradictedKeys = [];

    for (const row of perCitation) {
      verdictCounts[row.verdict] = (verdictCounts[row.verdict] || 0) + 1;
      evidenceKindCounts[row.evidence_kind] = (evidenceKindCounts[row.evidence_kind] || 0) + 1;

      if (row.verdict === "unverifiable") {
        const reason = row.verdict_source || "unknown";
        unverifiableReasons[reason] = (unverifiableReasons[reason] || 0) + 1;
      } else if (row.score != null) {
        scores.push(row.score);
        if (row.verdict === "contradicted") contradictedKeys.push(row.citation_key);
      }
    }

    result.verdict_counts = verdictCounts;
    result.unverifiable_reasons = unverifiableReasons;
    result.evidence_kind_counts = evidenceKindCounts;
    result.contradicted_keys = contradictedKeys;
    result.verifiable_count = scores.length;
    result.unverifiable_count = result.total_spans - result.verifiable_count;
    result.evidence_coverage = result.total_spans ? result.verifiable_count / result.total_spans : 0.0;
    result.support_rate = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : 0.0;

    return result;
  }

  self.ScribeEvaluator = { evaluateCitations, evaluateEvidenceSupport, callJudge };
})();
