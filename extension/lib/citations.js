// Citation-key validation — the browser half of the hybrid split (see the
// MVP plan). Deterministic, no network, no server dependency: this is what
// keeps the extension useful even when the local Python service is down.
//
// Deliberately a small subset of pipeline/citation_spans.py, not a port of
// it: no sentence anchoring, no author-date resolution, no evidence
// matching. Those stay server-side as the single source of truth (see the
// MVP plan's "what is deliberately not duplicated" section) — this file
// only answers "does every cited key exist in the bib?", the same question
// Tier 1's hallucinated_keys/missing_keys answer, computed independently
// so it still works with the server off.
//
// Loaded as a plain script alongside content.js (see manifest.json's
// content_scripts order) — no module system, so this attaches a single
// global, ScribeCitations, for content.js to call.

(() => {
  // Matches the \cite-family: \cite, \citep, \citet, \citealp, each
  // optionally starred (\citep*) and with an optional [pre][post] note,
  // capturing the (possibly multi-key, comma-separated) key list.
  const CITE_RE = /\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])?(?:\[[^\]]*\])?\{([^}]+)\}/g;

  function extractCitedKeys(text) {
    const keys = new Set();
    let match;
    CITE_RE.lastIndex = 0;
    while ((match = CITE_RE.exec(text)) !== null) {
      for (const rawKey of match[1].split(",")) {
        const key = rawKey.trim();
        if (key) keys.add(key);
      }
    }
    return keys;
  }

  // Matches @type{key, ... — same entry-opening pattern
  // pipeline/abstract_fetcher.py::parse_bib_content uses, but we only need
  // the key here, not the full field parse.
  const BIB_ENTRY_RE = /@(\w+)\s*\{([^,]+),/g;

  function extractBibKeys(bibContent) {
    const keys = new Set();
    let match;
    BIB_ENTRY_RE.lastIndex = 0;
    while ((match = BIB_ENTRY_RE.exec(bibContent)) !== null) {
      keys.add(match[2].trim());
    }
    return keys;
  }

  function checkCitations(text, bibContent) {
    const cited = extractCitedKeys(text);
    const bib = extractBibKeys(bibContent);

    const hallucinated = [...cited].filter((k) => !bib.has(k)).sort();
    const missing = [...bib].filter((k) => !cited.has(k)).sort();
    const validCited = [...cited].filter((k) => bib.has(k));

    return {
      cited_keys: [...cited].sort(),
      bib_keys: [...bib].sort(),
      hallucinated_keys: hallucinated,
      missing_keys: missing,
      precision: cited.size > 0 ? validCited.length / cited.size : 1.0,
      recall: bib.size > 0 ? validCited.length / bib.size : 1.0,
    };
  }

  window.ScribeCitations = { extractCitedKeys, extractBibKeys, checkCitations };
})();
