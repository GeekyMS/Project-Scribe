// Port of pipeline/citation_spans.py. Deterministic, no LLM: locates every
// citation mention in generated text and anchors each to its sentence.
// Runs in background.js's service worker (loaded via importScripts), so
// this attaches to `self`, not `window`.
//
// Kept in lockstep with citation_spans.py — same regexes, same guards, same
// resolution logic. A drift here silently changes every downstream Tier 4
// verdict, since this decides which sentence a claim is anchored to.

(() => {
  const ABBREVIATIONS = new Set([
    "e.g.", "i.e.", "et al.", "etc.", "cf.", "vs.", "approx.",
    "fig.", "figs.", "eq.", "eqs.", "no.", "nos.", "vol.", "vols.",
    "pp.", "p.", "ch.", "sec.", "secs.", "dr.", "mr.", "mrs.", "ms.",
    "prof.", "st.",
  ]);

  const BOUNDARY_RE = /(?<=[.!?])\s+(?=[A-Z0-9"'(“])/g;
  const INITIAL_RE = /\b[A-Z]\.$/;

  function hasUnbalancedBraceOrParen(text) {
    const depthBrace = (text.match(/\{/g) || []).length - (text.match(/\}/g) || []).length;
    const depthParen = (text.match(/\(/g) || []).length - (text.match(/\)/g) || []).length;
    return depthBrace > 0 || depthParen > 0;
  }

  function splitSentences(text) {
    if (!text) return [];

    const boundaries = [];
    for (const m of text.matchAll(BOUNDARY_RE)) boundaries.push(m.index + 1);
    // +1 lands right after the punctuation character (before whitespace),
    // matching Python's `m.start() + 1`.

    const sentences = [];
    let start = 0;
    for (const b of boundaries) {
      const candidate = text.slice(start, b);
      const stripped = candidate.replace(/\s+$/, "");

      const words = stripped.split(/\s+/).filter(Boolean);
      const tailWord = words.length ? words[words.length - 1].toLowerCase() : "";
      if (ABBREVIATIONS.has(tailWord)) continue;
      if (INITIAL_RE.test(stripped)) continue;
      if (hasUnbalancedBraceOrParen(candidate)) continue;

      sentences.push([start, b]);
      start = b;
    }

    if (start < text.length) sentences.push([start, text.length]);

    const result = [];
    for (const [sStart, sEnd] of sentences) {
      const raw = text.slice(sStart, sEnd);
      const stripped = raw.trim();
      if (!stripped) continue;
      const leadWs = raw.length - raw.replace(/^\s+/, "").length;
      const trailWs = raw.length - raw.replace(/\s+$/, "").length;
      result.push([stripped, sStart + leadWs, sEnd - trailWs]);
    }
    return result;
  }

  const LATEX_CITE_RE = /\\(cite|citep|citet|citealp|citeyear|autocite)\*?(?:\[[^\]]*\])*\{([^}]+)\}/g;

  // Comma before the year is mandatory — see citation_spans.py's comment:
  // a bare "Capitalized word" + whitespace + 4 digits with no comma is
  // common in ordinary text (e.g. a \paragraph{Paragraph 0001} heading),
  // and matched "Paragraph"/"0001" as a fake citation before this was
  // tightened. This fallback only ever runs when zero \cite-family
  // commands are found in the text at all.
  const AUTHOR_DATE_RE = /\(?\b([A-Z][A-Za-z'\-]+)(?:\s+et al\.|\s+(?:&|and)\s+[A-Z][A-Za-z'\-]+)?,\s+(\d{4}[a-z]?)\)?/g;

  function buildAuthorYearIndex(bibEntries) {
    const index = new Map();
    for (const entry of bibEntries) {
      const authorField = entry.author || "";
      const year = String(entry.year || "").trim();
      if (!authorField || !year) continue;
      const firstAuthor = authorField.split(" and ")[0].trim();
      let surname;
      if (firstAuthor.includes(",")) {
        surname = firstAuthor.split(",")[0].trim();
      } else {
        const parts = firstAuthor.split(/\s+/).filter(Boolean);
        surname = parts.length ? parts[parts.length - 1].trim() : firstAuthor;
      }
      surname = surname.toLowerCase();
      if (surname) index.set(`${surname}|${year}`, entry.key || "");
    }
    return index;
  }

  function findCitationSpans(text, bibEntries) {
    const sentences = splitSentences(text);
    if (sentences.length === 0) return [];

    function sentenceForOffset(offset) {
      for (let idx = 0; idx < sentences.length; idx++) {
        const [sent, sStart, sEnd] = sentences[idx];
        if (sStart <= offset && offset < sEnd) return [idx, sent, sStart];
      }
      for (let idx = 0; idx < sentences.length; idx++) {
        const [sent, sStart, sEnd] = sentences[idx];
        if (offset < sEnd) return [idx, sent, sStart];
      }
      const idx = sentences.length - 1;
      const [sent, sStart] = sentences[idx];
      return [idx, sent, sStart];
    }

    function contextFor(idx) {
      const parts = [];
      if (idx > 0) parts.push(sentences[idx - 1][0]);
      parts.push(sentences[idx][0]);
      if (idx + 1 < sentences.length) parts.push(sentences[idx + 1][0]);
      return parts.join(" ");
    }

    const spans = [];
    const ordinalByKey = new Map();
    function nextOrdinal(key) {
      const n = (ordinalByKey.get(key) || 0) + 1;
      ordinalByKey.set(key, n);
      return n;
    }

    let seenLatexMention = false;
    LATEX_CITE_RE.lastIndex = 0;
    let match;
    while ((match = LATEX_CITE_RE.exec(text)) !== null) {
      seenLatexMention = true;
      const keys = match[2].split(",").map((k) => k.trim()).filter(Boolean);
      if (keys.length === 0) continue;
      const charStart = match.index;
      const charEnd = match.index + match[0].length;
      const [sentIdx, sentText, sentStart] = sentenceForOffset(charStart);
      const context = contextFor(sentIdx);

      for (const key of keys) {
        const coCited = keys.filter((k) => k !== key);
        spans.push({
          citation_id: `${key}#${nextOrdinal(key)}`,
          key,
          surface: match[0],
          style: "latex",
          char_start: charStart,
          char_end: charEnd,
          sentence: sentText,
          sentence_index: sentIdx,
          sentence_char_start: sentStart,
          co_cited_keys: coCited,
          context,
          resolution: "exact",
        });
      }
    }

    if (seenLatexMention) return spans;

    const authorYearIndex = buildAuthorYearIndex(bibEntries || []);
    AUTHOR_DATE_RE.lastIndex = 0;
    while ((match = AUTHOR_DATE_RE.exec(text)) !== null) {
      const surname = match[1].toLowerCase();
      const year = match[2];
      const charStart = match.index;
      const charEnd = match.index + match[0].length;
      const [sentIdx, sentText, sentStart] = sentenceForOffset(charStart);
      const context = contextFor(sentIdx);

      const key = authorYearIndex.get(`${surname}|${year}`);
      const resolution = key ? "author_year_matched" : "unmatched";
      const resolvedKey = key || `unresolved:${surname}${year}`;

      spans.push({
        citation_id: `${resolvedKey}#${nextOrdinal(resolvedKey)}`,
        key: resolvedKey,
        surface: match[0],
        style: "author_date",
        char_start: charStart,
        char_end: charEnd,
        sentence: sentText,
        sentence_index: sentIdx,
        sentence_char_start: sentStart,
        co_cited_keys: [],
        context,
        resolution,
      });
    }

    return spans;
  }

  self.ScribeCitationSpans = { splitSentences, findCitationSpans };
})();
