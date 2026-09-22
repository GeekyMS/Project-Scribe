// Port of pipeline/resolvers.py. Multi-provider paper resolution — DOI/
// arXiv-first, title-search last. Pure HTTP via fetch(); no caching here
// (that's enrichmentCache.js's job). Runs in background.js's service
// worker, which is the one context allowed to make these cross-origin
// calls (host_permissions covers api.semanticscholar.org/api.crossref.org/
// api.openalex.org) without page-context CORS restrictions.

(() => {
  const S2_API = "https://api.semanticscholar.org/graph/v1";
  const CROSSREF_API = "https://api.crossref.org/works";
  const OPENALEX_API = "https://api.openalex.org/works";
  const UNPAYWALL_API = "https://api.unpaywall.org/v2";

  const S2_FIELDS = "paperId,title,abstract,year,authors,externalIds";

  const SIM_THRESHOLD_ARXIV = 0.75;
  const SIM_THRESHOLD_TITLE = 0.85;
  const SIM_THRESHOLD_TITLE_YEAR_MATCH = 0.75;

  // ── Title similarity — replaces Python's difflib.SequenceMatcher ──
  //
  // difflib.SequenceMatcher.ratio() is 2*M/T where M is the total size of
  // matching blocks (found via a longest-common-subsequence-style greedy
  // match) and T is the combined length of both strings. This is a direct
  // port of that algorithm — not a different similarity metric — so
  // thresholds tuned against the Python resolver stay meaningful.
  function sequenceMatcherRatio(a, b) {
    if (!a && !b) return 1.0;
    if (!a || !b) return 0.0;

    function findLongestMatch(aLo, aHi, bLo, bHi) {
      let bestI = aLo, bestJ = bLo, bestSize = 0;
      const j2len = new Map();
      for (let i = aLo; i < aHi; i++) {
        const newJ2len = new Map();
        for (let j = bLo; j < bHi; j++) {
          if (a[i] === b[j]) {
            const k = (j2len.get(j - 1) || 0) + 1;
            newJ2len.set(j, k);
            if (k > bestSize) {
              bestI = i - k + 1;
              bestJ = j - k + 1;
              bestSize = k;
            }
          }
        }
        j2len.clear();
        for (const [k, v] of newJ2len) j2len.set(k, v);
      }
      return [bestI, bestJ, bestSize];
    }

    function matchBlocks(aLo, aHi, bLo, bHi) {
      const [i, j, size] = findLongestMatch(aLo, aHi, bLo, bHi);
      if (size === 0) return 0;
      let total = size;
      if (aLo < i && bLo < j) total += matchBlocks(aLo, i, bLo, j);
      if (i + size < aHi && j + size < bHi) total += matchBlocks(i + size, aHi, j + size, bHi);
      return total;
    }

    // Cap input length for the recursive matcher — titles are short, but
    // this guards against pathological input without changing behavior
    // for the realistic case.
    const aClamped = a.slice(0, 2000);
    const bClamped = b.slice(0, 2000);
    const matching = matchBlocks(0, aClamped.length, 0, bClamped.length);
    return (2.0 * matching) / (aClamped.length + bClamped.length);
  }

  const NORM_RE = /[^a-z0-9\s]/g;
  const LATEX_CMD_RE = /\\[a-zA-Z]+\{([^}]*)\}/g;

  function normTitle(s) {
    if (!s) return "";
    let out = s.replace(LATEX_CMD_RE, "$1");
    out = out.replace(/\{/g, "").replace(/\}/g, "");
    out = out.toLowerCase();
    out = out.replace(NORM_RE, " ");
    return out.split(/\s+/).filter(Boolean).join(" ");
  }

  function titleSim(a, b) {
    const na = normTitle(a), nb = normTitle(b);
    if (!na || !nb) return 0.0;
    const seqRatio = sequenceMatcherRatio(na, nb);
    const ta = new Set(na.split(" "));
    const tb = new Set(nb.split(" "));
    const union = new Set([...ta, ...tb]);
    let inter = 0;
    for (const t of ta) if (tb.has(t)) inter++;
    const jaccard = union.size ? inter / union.size : 0.0;
    return Math.max(seqRatio, jaccard);
  }

  // ── HTTP helper ──
  // Each fetch is bounded: a hung/stalled resolver response otherwise blocks
  // the sequential per-entry enrichment loop indefinitely, since there's no
  // other retry or timeout above this to notice.
  const REQUEST_TIMEOUT_MS = 20000;

  async function getJson(url, params, { headers } = {}) {
    const qs = new URLSearchParams(
      Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== "")
    ).toString();
    const fullUrl = qs ? `${url}?${qs}` : url;
    try {
      let resp = await fetch(fullUrl, { headers, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
      if (resp.status === 429) {
        await new Promise((r) => setTimeout(r, 10000));
        resp = await fetch(fullUrl, { headers, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
      }
      if (resp.status !== 200) return [null, resp.status, `HTTP ${resp.status}`];
      return [await resp.json(), resp.status, null];
    } catch (e) {
      return [null, null, String(e)];
    }
  }

  // ── Semantic Scholar ──
  async function s2ById(idtype, ident, { apiKey } = {}) {
    const headers = apiKey ? { "x-api-key": apiKey } : undefined;
    const [data] = await getJson(`${S2_API}/paper/${idtype}:${ident}`, { fields: S2_FIELDS }, { headers });
    if (data) data._source = "semantic_scholar";
    return data;
  }

  async function s2Search(title, { limit = 5, apiKey } = {}) {
    const headers = apiKey ? { "x-api-key": apiKey } : undefined;
    const [data] = await getJson(`${S2_API}/paper/search`, { query: title, limit, fields: S2_FIELDS }, { headers });
    const papers = (data || {}).data || [];
    for (const p of papers) p._source = "semantic_scholar";
    return papers;
  }

  // ── Crossref ──
  async function crossrefByDoi(doi, { mailto } = {}) {
    const [data] = await getJson(`${CROSSREF_API}/${encodeURIComponent(doi)}`, mailto ? { mailto } : {});
    const msg = (data || {}).message;
    if (msg) msg._source = "crossref";
    return msg || null;
  }

  async function crossrefSearch(title, { limit = 5, mailto } = {}) {
    const params = { "query.bibliographic": title, rows: limit };
    if (mailto) params.mailto = mailto;
    const [data] = await getJson(CROSSREF_API, params);
    const items = (((data || {}).message || {}).items) || [];
    for (const it of items) it._source = "crossref";
    return items;
  }

  function crossrefTitle(item) {
    const titles = item.title || [];
    return titles.length ? titles[0] : "";
  }

  function titleOf(item) {
    const source = item._source;
    if (source === "crossref") return crossrefTitle(item);
    const t = item.title;
    if (Array.isArray(t)) return t.length ? t[0] : "";
    return t || item.display_name || "";
  }

  // ── OpenAlex ──
  function invertAbstract(invertedIndex) {
    if (!invertedIndex) return "";
    const positions = new Map();
    for (const [word, idxs] of Object.entries(invertedIndex)) {
      for (const i of idxs) positions.set(i, word);
    }
    if (positions.size === 0) return "";
    const keys = [...positions.keys()].sort((a, b) => a - b);
    return keys.map((k) => positions.get(k)).join(" ");
  }

  async function openalexByDoi(doi, { mailto, apiKey } = {}) {
    const params = {};
    if (mailto) params.mailto = mailto;
    if (apiKey) params.api_key = apiKey;
    const [data] = await getJson(`${OPENALEX_API}/https://doi.org/${doi}`, params);
    if (data) data._source = "openalex";
    return data;
  }

  async function openalexSearch(title, { limit = 5, mailto, apiKey } = {}) {
    const params = { search: title, per_page: limit };
    if (mailto) params.mailto = mailto;
    if (apiKey) params.api_key = apiKey;
    const [data] = await getJson(OPENALEX_API, params);
    const results = (data || {}).results || [];
    for (const r of results) r._source = "openalex";
    return results;
  }

  // ── Resolution cascade ──
  function extractDoiOrArxivFromUrl(url) {
    if (!url) return [null, null];
    const doiMatch = url.match(/10\.\d{4,9}\/[^\s"'<>]+/);
    if (doiMatch) return [doiMatch[0].replace(/[.,;]+$/, ""), null];
    const arxivMatch = url.match(/arxiv\.org\/(?:abs|pdf)\/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$/);
    if (arxivMatch) return [null, arxivMatch[1]];
    return [null, null];
  }

  function safeInt(s, def) {
    const n = parseInt(String(s).slice(0, 4), 10);
    return Number.isNaN(n) ? def : n;
  }

  async function resolveEntry(entry, cfg) {
    const title = entry.title || "";
    let doi = (entry.doi || "").trim();
    let arxivId = (entry.eprint || "").trim();
    const url = entry.url || "";

    if (!doi && !arxivId && url) {
      const [urlDoi, urlArxiv] = extractDoiOrArxivFromUrl(url);
      doi = doi || urlDoi || "";
      arxivId = arxivId || urlArxiv || "";
    }

    const s2Key = cfg.s2ApiKey;
    const oaKey = cfg.openalexApiKey;
    const mailto = cfg.mailto;

    const attempts = [];
    const result = { ...entry };

    function accept(paper, { status, matchedBy, sim, yearMatched }) {
      const abstract = paper.abstract || invertAbstract(paper.abstract_inverted_index);
      result.abstract = abstract || "";
      let externalIds = paper.externalIds || {};
      if (paper._source === "openalex") {
        const ids = paper.ids || {};
        externalIds = {
          DOI: (ids.doi || "").replace("https://doi.org/", ""),
          OpenAlex: ids.openalex,
        };
      } else if (paper._source === "crossref") {
        externalIds = { DOI: paper.DOI };
      }

      // All not-already-tried candidates, in order — acceptAsync tries
      // each in turn until one yields an abstract, matching Python's
      // for-loop-with-continue-on-failure (not just the first candidate;
      // a bug here silently dropped the openalex fallback whenever the
      // first candidate resolved a paper but still had no abstract).
      let backfillCandidates = [];
      if (!result.abstract) {
        const backfillDoi = externalIds.DOI;
        if (backfillDoi) {
          backfillCandidates = [
            ["semantic_scholar", () => s2ById("DOI", backfillDoi, { apiKey: s2Key })],
            ["openalex", () => openalexByDoi(backfillDoi, { mailto, apiKey: oaKey })],
          ].filter(([src]) => src !== paper._source);
        }
      }

      const matchedTitle = titleOf(paper);
      result.evidence = {
        status,
        source: paper._source,
        matched_by: matchedBy,
        matched_title: matchedTitle,
        title_similarity: sim,
        year_matched: yearMatched,
        external_ids: externalIds,
        abstract_chars: result.abstract.length,
        abstract_backfilled_from: null,
        full_text: { status: "not_attempted", source_url: "", chars: 0, error: null },
        attempts,
        fetched_at: null,
        resolver_version: "1",
      };
      return { result, backfillCandidates };
    }

    async function acceptAsync(paper, opts) {
      const { result: r, backfillCandidates } = accept(paper, opts);
      for (const [src, fn] of backfillCandidates) {
        try {
          const alt = await fn();
          const altAbstract = alt ? (alt.abstract || invertAbstract(alt.abstract_inverted_index)) : "";
          attempts.push({ provider: src, method: "abstract_backfill", n_results: alt ? 1 : 0, found_abstract: !!altAbstract });
          if (altAbstract) {
            r.abstract = altAbstract;
            r.evidence.abstract_chars = altAbstract.length;
            r.evidence.abstract_backfilled_from = src;
            break;
          }
        } catch (e) {
          attempts.push({ provider: src, method: "abstract_backfill", error: String(e) });
        }
      }
      return r;
    }

    function rejectAll() {
      result.abstract = "";
      result.evidence = {
        status: "unresolved",
        source: null, matched_by: null, matched_title: "",
        title_similarity: null, year_matched: null,
        external_ids: {}, abstract_chars: 0,
        full_text: { status: "not_attempted", source_url: "", chars: 0, error: null },
        attempts, fetched_at: null, resolver_version: "1",
      };
      return result;
    }

    // 1. DOI
    if (doi) {
      const doiCandidates = [
        ["semantic_scholar", () => s2ById("DOI", doi, { apiKey: s2Key })],
        ["crossref", () => crossrefByDoi(doi, { mailto })],
        ["openalex", () => openalexByDoi(doi, { mailto, apiKey: oaKey })],
      ];
      for (const [source, fn] of doiCandidates) {
        let paper;
        try {
          paper = await fn();
        } catch (e) {
          attempts.push({ provider: source, method: "doi", error: String(e) });
          continue;
        }
        if (paper) {
          const sim = titleSim(title, titleOf(paper));
          attempts.push({ provider: source, method: "doi", http_status: 200, n_results: 1, best_similarity: sim });
          const status = sim >= 0.5 || !title ? "matched_doi" : "matched_low_confidence";
          return acceptAsync(paper, { status, matchedBy: "doi", sim, yearMatched: null });
        }
        attempts.push({ provider: source, method: "doi", n_results: 0 });
      }
    }

    // 2. arXiv eprint
    if (arxivId) {
      let paper = null;
      try {
        paper = await s2ById("ARXIV", arxivId, { apiKey: s2Key });
      } catch (e) {
        attempts.push({ provider: "semantic_scholar", method: "arxiv", error: String(e) });
      }
      if (paper) {
        const sim = titleSim(title, paper.title || "");
        attempts.push({ provider: "semantic_scholar", method: "arxiv", http_status: 200, n_results: 1, best_similarity: sim });
        if (sim >= SIM_THRESHOLD_ARXIV || !title) {
          return acceptAsync(paper, { status: "matched_arxiv", matchedBy: "arxiv", sim, yearMatched: null });
        }
      } else {
        attempts.push({ provider: "semantic_scholar", method: "arxiv", n_results: 0 });
      }
    }

    // 3. Title search
    if (title) {
      const year = String(entry.year || "").trim();
      let best = null, bestSim = 0.0;
      const searchCandidates = [
        ["semantic_scholar", () => s2Search(title, { apiKey: s2Key })],
        ["crossref", () => crossrefSearch(title, { mailto })],
        ["openalex", () => openalexSearch(title, { mailto, apiKey: oaKey })],
      ];
      for (const [source, fn] of searchCandidates) {
        let candidates;
        try {
          candidates = await fn();
        } catch (e) {
          attempts.push({ provider: source, method: "title_search", error: String(e) });
          continue;
        }
        let localBestSim = 0.0;
        for (const c of candidates) {
          const sim = titleSim(title, titleOf(c));
          localBestSim = Math.max(localBestSim, sim);
          if (sim > bestSim) {
            best = c;
            bestSim = sim;
          }
        }
        attempts.push({ provider: source, method: "title_search", n_results: candidates.length, best_similarity: localBestSim });
      }

      if (best !== null) {
        let yearMatched = null;
        if (year) {
          const cYear = best.year
            || (((best["published-print"] || {})["date-parts"] || [[null]])[0][0])
            || best.publication_year
            || "";
          yearMatched = !!cYear && Math.abs(safeInt(cYear, 0) - safeInt(year, 0)) <= 1;
        }
        if (bestSim >= SIM_THRESHOLD_TITLE || (bestSim >= SIM_THRESHOLD_TITLE_YEAR_MATCH && yearMatched)) {
          const status = bestSim >= SIM_THRESHOLD_TITLE ? "matched_title" : "matched_fuzzy";
          return acceptAsync(best, { status, matchedBy: "title_search", sim: bestSim, yearMatched });
        } else if (bestSim >= 0.5) {
          return acceptAsync(best, { status: "matched_low_confidence", matchedBy: "title_search", sim: bestSim, yearMatched });
        }
      }
    }

    return rejectAll();
  }

  self.ScribeResolvers = { titleSim, normTitle, resolveEntry, invertAbstract };
})();
