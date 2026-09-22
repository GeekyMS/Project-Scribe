// Port of pipeline/generator.py's generation call + _parse_dual_output
// (including the two bugs fixed during the MVP build: markdown-header
// leak into LaTeX output, and the label-strip regex not anchoring
// correctly when the model prepends its own title).

(() => {
  const MAX_RETRIES = 2;
  const RETRY_DELAY_MS = 10000;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function parseDualOutput(raw) {
    const separator = "---FORMAT_SEPARATOR---";
    if (raw.includes(separator)) {
      const idx = raw.indexOf(separator);
      let latexPart = raw.slice(0, idx).trim();
      let mdPart = raw.slice(idx + separator.length).trim();

      latexPart = latexPart.replace(/^(?:FORMAT\s*1\s*\(?LaTeX\)?\s*:?\s*)/i, "").trim();
      mdPart = mdPart.replace(/^(?:FORMAT\s*2\s*\(?Markdown\)?\s*:?\s*)/i, "").trim();

      // Strip a leading block of markdown ATX-header lines (LaTeX output
      // should never start with one) — the model sometimes wraps its own
      // title above the requested format label, which the anchored
      // label-strip above never catches since it isn't at the very start.
      latexPart = latexPart.replace(/^(?:#+.*\n+)+/, "").trim();

      return [latexPart, mdPart];
    }
    if (raw.includes("\\cite{")) return [raw.trim(), ""];
    return ["", raw.trim()];
  }

  async function generateRelatedWork({
    provider, apiKey, baseUrl, model,
    paperText, bibContent, enrichedEntries, targetWordCount,
    onProgress,
  }) {
    const prompt = self.ScribeGenPrompts.buildGenerationPrompt({
      paperText, bibContent, enrichedEntries, outputFormat: "both", targetWordCount,
    });

    let raw = null;
    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      if (onProgress) onProgress({ stage: "generating", done: attempt - 1, total: MAX_RETRIES });
      try {
        raw = await self.ScribeLLM.chatComplete({
          provider, apiKey, baseUrl, model,
          messages: [{ role: "user", content: prompt }],
          timeoutMs: 300000,
        });
      } catch (e) {
        if (attempt < MAX_RETRIES) { await sleep(RETRY_DELAY_MS); continue; }
        throw e;
      }
      if (raw == null) {
        if (attempt < MAX_RETRIES) { await sleep(RETRY_DELAY_MS); continue; }
        throw new Error("generation produced an empty response");
      }
      break;
    }

    raw = raw.replace(/<think>[\s\S]*?<\/think>/g, "");
    const [latexOutput, markdownOutput] = parseDualOutput(raw);

    return {
      model,
      latex_output: latexOutput,
      markdown_output: markdownOutput,
      raw_output: raw,
    };
  }

  self.ScribeGenerate = { parseDualOutput, generateRelatedWork };
})();
