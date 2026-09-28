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
  // Used only by the batch DOI lookup (s2Batch): tldr is a usable weak
  // fallback when abstract is null (a machine summary, still better than
  // nothing for the judge), and openAccessPdf/externalIds are what route
  // an entry into the arXiv/Europe PMC backfill pass when S2 itself has no
  // abstract.
  const S2_BATCH_FIELDS = "paperId,title,abstract,year,authors,externalIds,tldr,openAccessPdf";

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

  // getResult never throws and never collapses a failure into "not found".
  // Every caller gets { data, status, outcome, error, retryAfterS } and must
  // branch on `outcome` — NOT on truthiness of `data` — so a rate limit or
  // network error can never be silently treated as "no record". This is a
  // thin adapter over lib/httpClient.js, which is what actually owns
  // per-provider rate limiting, retry/backoff, and singleflight coalescing
  // — resolvers.js itself no longer talks to fetch() directly.
  //
  // maxAttempts is deliberately lower here than httpClient's default: this
  // helper backs the single-entry cascade in resolveEntry(), which already
  // gets its resilience from trying up to three independent providers in
  // sequence. Letting each one separately retry 4x with backoff up to 60s
  // means one throttled provider can burn minutes before the cascade ever
  // reaches the next provider that might answer immediately — measured
  // live: a handful of residue entries took 2-9 minutes apiece under
  // sustained S2 throttling, entirely spent retrying S2 before falling
  // through to Crossref/OpenAlex. Batch primitives (s2Batch etc.) don't go
  // through this helper and keep the full retry budget, since a batch
  // covers the whole list in one shot with no equivalent fallback.
  const SINGLE_ITEM_MAX_ATTEMPTS = 2;
  function getResult(provider, url, params, { headers } = {}) {
    return self.ScribeHttp.json(provider, url, params, { headers, timeoutMs: REQUEST_TIMEOUT_MS, maxAttempts: SINGLE_ITEM_MAX_ATTEMPTS });
  }

  // ── Semantic Scholar ──
  async function s2ById(idtype, ident, { apiKey } = {}) {
    const headers = apiKey ? { "x-api-key": apiKey } : undefined;
    const r = await getResult("s2", `${S2_API}/paper/${idtype}:${ident}`, { fields: S2_FIELDS }, { headers });
    if (r.outcome === "ok" && r.data) {
      r.data._source = "semantic_scholar";
      r.outcome = r.data.abstract ? "ok_record_with_abstract" : "ok_record_no_abstract";
    } else if (r.outcome === "ok") {
      r.outcome = "ok_no_record";
    }
    return r;
  }

  async function s2Search(title, { limit = 5, apiKey } = {}) {
    const headers = apiKey ? { "x-api-key": apiKey } : undefined;
    const r = await getResult("s2", `${S2_API}/paper/search`, { query: title, limit, fields: S2_FIELDS }, { headers });
    const papers = (r.data || {}).data || [];
    for (const p of papers) p._source = "semantic_scholar";
    return { ...r, papers, outcome: r.outcome === "ok" ? "ok_search_complete" : r.outcome };
  }

  // ── Crossref ──
  async function crossrefByDoi(doi, { mailto } = {}) {
    const r = await getResult("crossref", `${CROSSREF_API}/${encodeURIComponent(doi)}`, mailto ? { mailto } : {});
    const msg = (r.data || {}).message;
    if (r.outcome === "ok" && msg) {
      msg._source = "crossref";
      r.data = msg;
      r.outcome = msg.abstract ? "ok_record_with_abstract" : "ok_record_no_abstract";
    } else if (r.outcome === "ok") {
      r.data = null;
      r.outcome = "ok_no_record";
    }
    return r;
  }

  async function crossrefSearch(title, { limit = 5, mailto } = {}) {
    const params = { "query.bibliographic": title, rows: limit };
    if (mailto) params.mailto = mailto;
    const r = await getResult("crossref", CROSSREF_API, params);
    const items = (((r.data || {}).message || {}).items) || [];
    for (const it of items) it._source = "crossref";
    return { ...r, papers: items, outcome: r.outcome === "ok" ? "ok_search_complete" : r.outcome };
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
    const r = await getResult("openalex", `${OPENALEX_API}/https://doi.org/${doi}`, params);
    if (r.outcome === "ok" && r.data) {
      r.data._source = "openalex";
      const hasAbstract = !!(r.data.abstract || invertAbstract(r.data.abstract_inverted_index));
      r.outcome = hasAbstract ? "ok_record_with_abstract" : "ok_record_no_abstract";
    } else if (r.outcome === "ok") {
      r.outcome = "ok_no_record";
    }
    return r;
  }

  async function openalexSearch(title, { limit = 5, mailto, apiKey } = {}) {
    const params = { search: title, per_page: limit };
    if (mailto) params.mailto = mailto;
    if (apiKey) params.api_key = apiKey;
    const r = await getResult("openalex", OPENALEX_API, params);
    const results = (r.data || {}).results || [];
    for (const item of results) item._source = "openalex";
    return { ...r, papers: results, outcome: r.outcome === "ok" ? "ok_search_complete" : r.outcome };
  }

  // ── Batch primitives ──
  // One request instead of one-per-paper. A 300-entry bibliography that
  // used to need ~900 sequential single-item requests (and the blanket
  // sleep(3000) between every one of them) needs on the order of 10-15
  // requests through these.

  const ARXIV_API = "https://export.arxiv.org/api/query";

  // POST /paper/batch — verified live: max 500 ids per request, `fields`
  // is a query-string param (not part of the JSON body), and the response
  // is a positional array aligned to the input `ids` array with `null` at
  // any index S2 doesn't have. Index into it; never match by returned DOI.
  const S2_BATCH_MAX = 500;
  async function s2Batch(ids, { apiKey, fields = S2_BATCH_FIELDS } = {}) {
    if (!ids.length) return { ok: true, papers: [], outcome: "ok_search_complete", chunkResults: [] };
    const headers = { "Content-Type": "application/json", ...(apiKey ? { "x-api-key": apiKey } : {}) };
    const chunks = [];
    for (let i = 0; i < ids.length; i += S2_BATCH_MAX) chunks.push(ids.slice(i, i + S2_BATCH_MAX));

    const papers = new Array(ids.length).fill(null);
    const chunkResults = [];
    for (let c = 0; c < chunks.length; c++) {
      const chunk = chunks[c];
      const offset = c * S2_BATCH_MAX;
      const r = await self.ScribeHttp.request("s2", {
        url: `${S2_API}/paper/batch`, method: "POST", headers, body: JSON.stringify({ ids: chunk }),
        params: { fields }, parse: "json", timeoutMs: REQUEST_TIMEOUT_MS,
      });
      chunkResults.push({ outcome: r.outcome === "ok" ? "ok_search_complete" : r.outcome, status: r.status, error: r.error, n: chunk.length });
      if (r.ok && Array.isArray(r.data)) {
        for (let i = 0; i < chunk.length; i++) {
          const paper = r.data[i];
          if (paper) {
            paper._source = "semantic_scholar";
            papers[offset + i] = paper;
          }
        }
      }
    }
    const allConclusive = chunkResults.every((cr) => cr.outcome === "ok_search_complete" || CONCLUSIVE_SET.has(cr.outcome));
    return { ok: true, papers, outcome: allConclusive ? "ok_search_complete" : "partial", chunkResults };
  }

  // filter=doi:A|B|C — verified live: max 100 values per filter.
  const OPENALEX_BATCH_MAX = 100;
  async function openalexBatchByDoi(dois, { mailto, apiKey } = {}) {
    if (!dois.length) return { ok: true, papers: [], outcome: "ok_search_complete", chunkResults: [] };
    const byDoi = new Map();
    const chunkResults = [];
    for (let i = 0; i < dois.length; i += OPENALEX_BATCH_MAX) {
      const chunk = dois.slice(i, i + OPENALEX_BATCH_MAX);
      const filterVal = chunk.map((d) => `https://doi.org/${d}`).join("|");
      const params = { filter: `doi:${filterVal}`, select: "id,doi,title,abstract_inverted_index,ids", "per-page": 100 };
      if (mailto) params.mailto = mailto;
      if (apiKey) params.api_key = apiKey;
      const r = await getResult("openalex", OPENALEX_API, params);
      chunkResults.push({ outcome: r.outcome === "ok" ? "ok_search_complete" : r.outcome, status: r.status, error: r.error, n: chunk.length });
      const results = (r.data || {}).results || [];
      for (const item of results) {
        item._source = "openalex";
        const doi = (item.doi || "").replace(/^https:\/\/doi\.org\//i, "").toLowerCase();
        if (doi) byDoi.set(doi, item);
      }
    }
    const papers = dois.map((d) => byDoi.get(d.toLowerCase()) || null);
    const allConclusive = chunkResults.every((cr) => cr.outcome === "ok_search_complete" || CONCLUSIVE_SET.has(cr.outcome));
    return { ok: true, papers, outcome: allConclusive ? "ok_search_complete" : "partial", chunkResults };
  }

  // filter=doi:A,doi:B,... — verified live with 100 DOIs in one request.
  const CROSSREF_BATCH_MAX = 100;
  async function crossrefBatchByDoi(dois, { mailto } = {}) {
    if (!dois.length) return { ok: true, papers: [], outcome: "ok_search_complete", chunkResults: [] };
    const byDoi = new Map();
    const chunkResults = [];
    for (let i = 0; i < dois.length; i += CROSSREF_BATCH_MAX) {
      const chunk = dois.slice(i, i + CROSSREF_BATCH_MAX);
      const params = { filter: chunk.map((d) => `doi:${d}`).join(","), rows: chunk.length };
      if (mailto) params.mailto = mailto;
      const r = await getResult("crossref", CROSSREF_API, params);
      chunkResults.push({ outcome: r.outcome === "ok" ? "ok_search_complete" : r.outcome, status: r.status, error: r.error, n: chunk.length });
      const items = (((r.data || {}).message || {}).items) || [];
      for (const item of items) {
        item._source = "crossref";
        const doi = (item.DOI || "").toLowerCase();
        if (doi) byDoi.set(doi, item);
      }
    }
    const papers = dois.map((d) => byDoi.get(d.toLowerCase()) || null);
    const allConclusive = chunkResults.every((cr) => cr.outcome === "ok_search_complete" || CONCLUSIVE_SET.has(cr.outcome));
    return { ok: true, papers, outcome: allConclusive ? "ok_search_complete" : "partial", chunkResults };
  }

  // arXiv id_list — verified live: several ids resolve to full Atom
  // <entry> blocks (title + abstract) in a single request. arXiv has no
  // JSON API; a service worker also has no DOMParser (that's why full HTML
  // parsing in Phase 3 needs an offscreen document), but Atom's structure
  // is simple enough to pull apart with anchored regexes rather than a
  // real XML parser for just title/summary/id.
  const ARXIV_BATCH_MAX = 100;
  function parseArxivAtom(xml) {
    const entries = [];
    const entryRe = /<entry>([\s\S]*?)<\/entry>/g;
    let m;
    while ((m = entryRe.exec(xml)) !== null) {
      const block = m[1];
      const idMatch = block.match(/<id>http:\/\/arxiv\.org\/abs\/([^<]+)<\/id>/);
      const titleMatch = block.match(/<title>([\s\S]*?)<\/title>/);
      const summaryMatch = block.match(/<summary>([\s\S]*?)<\/summary>/);
      const decode = (s) => (s || "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
      entries.push({
        arxivId: idMatch ? idMatch[1].replace(/v\d+$/, "") : null,
        title: decode(titleMatch && titleMatch[1]),
        abstract: decode(summaryMatch && summaryMatch[1]),
      });
    }
    return entries;
  }

  async function arxivBatch(ids) {
    if (!ids.length) return { ok: true, papers: [], outcome: "ok_search_complete", chunkResults: [] };
    const byId = new Map();
    const chunkResults = [];
    for (let i = 0; i < ids.length; i += ARXIV_BATCH_MAX) {
      const chunk = ids.slice(i, i + ARXIV_BATCH_MAX);
      const r = await self.ScribeHttp.text("arxiv_api", ARXIV_API, {
        params: { id_list: chunk.join(","), max_results: chunk.length }, timeoutMs: REQUEST_TIMEOUT_MS,
      });
      chunkResults.push({ outcome: r.outcome === "ok" ? "ok_search_complete" : r.outcome, status: r.status, error: r.error, n: chunk.length });
      if (r.ok && typeof r.data === "string") {
        for (const parsed of parseArxivAtom(r.data)) {
          if (parsed.arxivId) {
            byId.set(parsed.arxivId, { title: parsed.title, abstract: parsed.abstract, _source: "arxiv", externalIds: { ArXiv: parsed.arxivId } });
          }
        }
      }
    }
    const papers = ids.map((id) => byId.get(id.replace(/v\d+$/, "")) || null);
    const allConclusive = chunkResults.every((cr) => cr.outcome === "ok_search_complete" || CONCLUSIVE_SET.has(cr.outcome));
    return { ok: true, papers, outcome: allConclusive ? "ok_search_complete" : "partial", chunkResults };
  }

  // Europe PMC — near-total abstract coverage for biomedical/life-science
  // DOIs (including many preprints), essentially no CS/ML coverage. Used
  // only as a backfill pass, not in the main DOI cascade, since it isn't a
  // general-purpose scholarly index the way S2/OpenAlex/Crossref are.
  const EUROPEPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search";
  const EUROPEPMC_BATCH_MAX = 25;
  async function europepmcBatchByDoi(dois) {
    if (!dois.length) return { ok: true, papers: [], outcome: "ok_search_complete", chunkResults: [] };
    const byDoi = new Map();
    const chunkResults = [];
    for (let i = 0; i < dois.length; i += EUROPEPMC_BATCH_MAX) {
      const chunk = dois.slice(i, i + EUROPEPMC_BATCH_MAX);
      const query = chunk.map((d) => `DOI:"${d}"`).join(" OR ");
      const r = await getResult("europepmc", EUROPEPMC_API, { query, format: "json", resultType: "core", pageSize: 100 });
      chunkResults.push({ outcome: r.outcome === "ok" ? "ok_search_complete" : r.outcome, status: r.status, error: r.error, n: chunk.length });
      const items = (r.data && r.data.resultList && r.data.resultList.result) || [];
      for (const item of items) {
        const doi = (item.doi || "").toLowerCase();
        if (doi) byDoi.set(doi, { title: item.title || "", abstract: item.abstractText || "", _source: "europepmc" });
      }
    }
    const papers = dois.map((d) => byDoi.get((d || "").toLowerCase()) || null);
    const allConclusive = chunkResults.every((cr) => CONCLUSIVE_SET.has(cr.outcome));
    return { ok: true, papers, outcome: allConclusive ? "ok_search_complete" : "partial", chunkResults };
  }

  const CONCLUSIVE_SET = new Set(["ok_record_with_abstract", "ok_record_no_abstract", "ok_no_record", "ok_search_complete"]);

  // A single inconclusive attempt (rate limit, network error, timeout)
  // must not by itself block a confident "this paper doesn't exist" / "no
  // abstract exists" conclusion when OTHER independent providers already
  // answered conclusively — e.g. a title search tries S2, Crossref, and
  // OpenAlex; if S2 is rate-limited but Crossref and OpenAlex both
  // genuinely searched and found nothing, that IS a confident answer.
  // Requiring only "any single failure" (the original version of this
  // check) meant one flaky provider could mark an entry inconclusive even
  // when every other provider tried had already settled it — observed
  // live: OpenAlex succeeding didn't stop entries from being reported as
  // "rate-limited or failed" because S2 alone had failed. Requiring a
  // majority of attempted providers to be conclusive fixes that while
  // still correctly flagging genuinely under-answered entries (e.g. only
  // 1 of 3 providers ever responded).
  function isInconclusive(attempts) {
    if (!attempts || attempts.length === 0) return true;
    const conclusiveCount = attempts.filter((a) => CONCLUSIVE_SET.has(a.outcome)).length;
    return conclusiveCount / attempts.length < 0.5;
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

  // Pulls whatever DOI/arXiv identifier a raw BibTeX entry carries — in
  // doi/eprint fields directly, or mined out of a url field — before any
  // network request is made. Shared by resolveEntry/resolveBatch's own
  // partitioning AND by the cache lookup (enrichmentCache.js), so a paper
  // already known by DOI/arXiv id from an earlier project can be found by
  // that identifier instead of only by this project's BibTeX key.
  function identifiersOf(entry) {
    let doi = (entry.doi || "").trim();
    let arxivId = (entry.eprint || "").trim();
    const url = entry.url || "";
    if (!doi && !arxivId && url) {
      const [urlDoi, urlArxiv] = extractDoiOrArxivFromUrl(url);
      doi = doi || urlDoi || "";
      arxivId = arxivId || urlArxiv || "";
    }
    return { doi: doi || null, arxivId: arxivId || null };
  }

  async function resolveEntry(entry, cfg) {
    const title = entry.title || "";
    const { doi: doi0, arxivId: arxivId0 } = identifiersOf(entry);
    let doi = doi0 || "";
    let arxivId = arxivId0 || "";

    const s2Key = cfg.s2ApiKey;
    const oaKey = cfg.openalexApiKey;
    const mailto = cfg.mailto;

    const attempts = [];
    const result = { ...entry };

    function recordAttempt(base, r, extra) {
      attempts.push({
        ...base,
        outcome: r ? r.outcome : "network_error",
        http_status: r ? r.status : null,
        error: r ? r.error : null,
        retry_after_s: r ? r.retryAfterS : null,
        ...extra,
      });
    }

    // Single-shot accept: matches a paper and records whatever abstract
    // came back with it, with no per-entry serial backfill. Filling in a
    // still-missing abstract is now Phase 2's job, done once, in a batch,
    // across every entry resolveBatch touched (DOI-matched, arXiv-matched,
    // and title-matched alike) — see backfillMissingAbstracts() below. The
    // old version of this function looped through S2/OpenAlex one at a
    // time per entry to find an abstract; that's exactly the
    // one-request-per-paper pattern batching exists to eliminate, and it
    // had the same swallowed-429 blindness recordAttempt() now prevents
    // everywhere else.
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
        had_inconclusive_attempt: isInconclusive(attempts),
      };
      return result;
    }

    function rejectAll() {
      const inconclusive = isInconclusive(attempts);
      result.abstract = "";
      result.evidence = {
        status: inconclusive ? "unresolved_incomplete" : "unresolved",
        source: null, matched_by: null, matched_title: "",
        title_similarity: null, year_matched: null,
        external_ids: {}, abstract_chars: 0,
        full_text: { status: "not_attempted", source_url: "", chars: 0, error: null },
        attempts, fetched_at: null, resolver_version: "1",
        had_inconclusive_attempt: inconclusive,
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
        let r;
        try {
          r = await fn();
        } catch (e) {
          recordAttempt({ provider: source, method: "doi" }, null, { error: String(e) });
          continue;
        }
        const paper = r.data;
        if (paper) {
          const sim = titleSim(title, titleOf(paper));
          recordAttempt({ provider: source, method: "doi" }, r, { n_results: 1, best_similarity: sim });
          const status = sim >= 0.5 || !title ? "matched_doi" : "matched_low_confidence";
          return accept(paper, { status, matchedBy: "doi", sim, yearMatched: null });
        }
        // outcome is either ok_no_record (provider conclusively has no
        // record for this DOI) or an inconclusive outcome (rate limited,
        // network error, ...) — isInconclusive() classifies the whole
        // attempts array afterward, by majority, so this one failing
        // doesn't by itself discard what other providers conclusively found.
        recordAttempt({ provider: source, method: "doi" }, r, { n_results: 0 });
      }
    }

    // 2. arXiv eprint
    if (arxivId) {
      let r = null;
      try {
        r = await s2ById("ARXIV", arxivId, { apiKey: s2Key });
      } catch (e) {
        recordAttempt({ provider: "semantic_scholar", method: "arxiv" }, null, { error: String(e) });
      }
      const paper = r && r.data;
      if (paper) {
        const sim = titleSim(title, paper.title || "");
        recordAttempt({ provider: "semantic_scholar", method: "arxiv" }, r, { n_results: 1, best_similarity: sim });
        if (sim >= SIM_THRESHOLD_ARXIV || !title) {
          return accept(paper, { status: "matched_arxiv", matchedBy: "arxiv", sim, yearMatched: null });
        }
      } else if (r) {
        recordAttempt({ provider: "semantic_scholar", method: "arxiv" }, r, { n_results: 0 });
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
        let r;
        try {
          r = await fn();
        } catch (e) {
          recordAttempt({ provider: source, method: "title_search" }, null, { error: String(e) });
          continue;
        }
        const candidates = r.papers || [];
        let localBestSim = 0.0;
        for (const c of candidates) {
          const sim = titleSim(title, titleOf(c));
          localBestSim = Math.max(localBestSim, sim);
          if (sim > bestSim) {
            best = c;
            bestSim = sim;
          }
        }
        recordAttempt({ provider: source, method: "title_search" }, r, { n_results: candidates.length, best_similarity: localBestSim });
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
          return accept(best, { status, matchedBy: "title_search", sim: bestSim, yearMatched });
        } else if (bestSim >= 0.5) {
          return accept(best, { status: "matched_low_confidence", matchedBy: "title_search", sim: bestSim, yearMatched });
        }
      }
    }

    return rejectAll();
  }

  // ── Batch resolution ──
  // Primary entry point for enriching a bibliography. Partitions entries by
  // the strongest identifier available, resolves each partition with one
  // (or a handful of, for >100/>500-sized lists) request per provider, and
  // only falls back to the per-entry resolveEntry() cascade — with its
  // normal rate limiting — for whatever's left: title-only entries, and
  // arXiv ids whose title didn't clear the similarity threshold.

  function buildResolvedEntry(entry, paper, { status, matchedBy, sim, yearMatched, attempts }) {
    const abstract = paper.abstract || invertAbstract(paper.abstract_inverted_index) || "";
    let externalIds = paper.externalIds || {};
    if (paper._source === "openalex") {
      const ids = paper.ids || {};
      externalIds = { DOI: (ids.doi || "").replace("https://doi.org/", ""), OpenAlex: ids.openalex };
    } else if (paper._source === "crossref") {
      externalIds = { DOI: paper.DOI };
    }
    const hadInconclusive = isInconclusive(attempts);
    return {
      ...entry,
      abstract,
      evidence: {
        status, source: paper._source, matched_by: matchedBy, matched_title: titleOf(paper),
        title_similarity: sim, year_matched: yearMatched, external_ids: externalIds,
        abstract_chars: abstract.length, abstract_backfilled_from: null,
        full_text: { status: "not_attempted", source_url: "", chars: 0, error: null },
        attempts, fetched_at: null, resolver_version: "1", had_inconclusive_attempt: hadInconclusive,
      },
    };
  }

  function buildUnresolvedEntry(entry, attempts) {
    const hadInconclusive = isInconclusive(attempts);
    return {
      ...entry,
      abstract: "",
      evidence: {
        status: hadInconclusive ? "unresolved_incomplete" : "unresolved",
        source: null, matched_by: null, matched_title: "", title_similarity: null, year_matched: null,
        external_ids: {}, abstract_chars: 0,
        full_text: { status: "not_attempted", source_url: "", chars: 0, error: null },
        attempts, fetched_at: null, resolver_version: "1", had_inconclusive_attempt: hadInconclusive,
      },
    };
  }

  function batchOutcomeFor(paper, batchOutcome, hasAbstract) {
    if (paper) return hasAbstract ? "ok_record_with_abstract" : "ok_record_no_abstract";
    return batchOutcome === "ok_search_complete" ? "ok_no_record" : batchOutcome;
  }

  async function resolveBatch(entries, cfg = {}, { onProgress } = {}) {
    const { mailto, s2ApiKey, openalexApiKey } = cfg;
    const total = entries.length;
    const results = new Array(total).fill(null);
    let done = 0;
    function tick(key, status) {
      done += 1;
      if (onProgress) onProgress(done, total, key, status);
    }

    const doiIdx = [], arxivIdx = [], residueIdx = [];
    const doiOf = new Map(), arxivOf = new Map();
    entries.forEach((entry, i) => {
      const { doi, arxivId } = identifiersOf(entry);
      if (doi) { doiIdx.push(i); doiOf.set(i, doi); }
      else if (arxivId) { arxivIdx.push(i); arxivOf.set(i, arxivId); }
      else residueIdx.push(i);
    });

    // DOI batch — query all three providers for the whole DOI set
    // concurrently. Batching already amortizes the request cost to ~1-3
    // requests per provider regardless of list size, so there's no need
    // for the single-entry cascade's serial fallback-on-miss here.
    if (doiIdx.length) {
      const doiList = doiIdx.map((i) => doiOf.get(i));
      const [s2Res, oaRes, crRes] = await Promise.all([
        s2Batch(doiList.map((d) => `DOI:${d}`), { apiKey: s2ApiKey }),
        openalexBatchByDoi(doiList, { mailto, apiKey: openalexApiKey }),
        crossrefBatchByDoi(doiList, { mailto }),
      ]);
      doiIdx.forEach((idx, j) => {
        const entry = entries[idx];
        const s2Paper = s2Res.papers[j], oaPaper = oaRes.papers[j], crPaper = crRes.papers[j];
        const attempts = [
          { provider: "semantic_scholar", method: "doi_batch", outcome: batchOutcomeFor(s2Paper, s2Res.outcome, !!(s2Paper && s2Paper.abstract)), n_results: s2Paper ? 1 : 0 },
          { provider: "openalex", method: "doi_batch", outcome: batchOutcomeFor(oaPaper, oaRes.outcome, !!(oaPaper && invertAbstract(oaPaper.abstract_inverted_index))), n_results: oaPaper ? 1 : 0 },
          { provider: "crossref", method: "doi_batch", outcome: batchOutcomeFor(crPaper, crRes.outcome, !!(crPaper && crPaper.abstract)), n_results: crPaper ? 1 : 0 },
        ];
        const candidates = [["semantic_scholar", s2Paper], ["openalex", oaPaper], ["crossref", crPaper]].filter(([, p]) => p);
        // Prefer whichever candidate actually carries an abstract; fall
        // back to any matched identity so the paper is still identified
        // even when nothing in this batch had evidence text.
        let chosen = candidates.find(([, p]) => p.abstract || invertAbstract(p.abstract_inverted_index));
        if (!chosen) chosen = candidates[0];
        if (chosen) {
          const [, paper] = chosen;
          const sim = titleSim(entry.title || "", titleOf(paper));
          const status = sim >= 0.5 || !entry.title ? "matched_doi" : "matched_low_confidence";
          results[idx] = buildResolvedEntry(entry, paper, { status, matchedBy: "doi", sim, yearMatched: null, attempts });
          // Stash S2's machine-generated tldr (if any) as a last-resort
          // fallback for backfillMissingAbstracts() below — weaker than a
          // real abstract, only used if arXiv/Europe PMC also come up empty.
          const tldrText = s2Paper && s2Paper.tldr && s2Paper.tldr.text;
          if (tldrText) results[idx].evidence.tldr = tldrText;
        } else {
          results[idx] = buildUnresolvedEntry(entry, attempts);
        }
        tick(entry.key, results[idx].evidence.status);
      });
    }

    // arXiv batch — arXiv always carries an abstract when the id resolves,
    // so a hit here is close to free evidence for CS/ML-heavy bibliographies.
    if (arxivIdx.length) {
      const arxivList = arxivIdx.map((i) => arxivOf.get(i));
      const axRes = await arxivBatch(arxivList);
      arxivIdx.forEach((idx, j) => {
        const entry = entries[idx];
        const paper = axRes.papers[j];
        const attempts = [{ provider: "arxiv", method: "arxiv_batch", outcome: batchOutcomeFor(paper, axRes.outcome, true), n_results: paper ? 1 : 0 }];
        if (paper) {
          const sim = titleSim(entry.title || "", paper.title || "");
          if (sim >= SIM_THRESHOLD_ARXIV || !entry.title) {
            results[idx] = buildResolvedEntry(entry, paper, { status: "matched_arxiv", matchedBy: "arxiv", sim, yearMatched: null, attempts });
            tick(entry.key, results[idx].evidence.status);
            return;
          }
          attempts.push({ provider: "arxiv", method: "arxiv_batch_title_mismatch", outcome: "ok_no_record", n_results: 1, best_similarity: sim });
        }
        // Didn't clear the batch threshold — give the single-entry cascade
        // a shot; it also tries S2/Crossref/OpenAlex by title.
        residueIdx.push(idx);
        results[idx] = { __pendingAttempts: attempts };
      });
    }

    // Residue: title-only entries, or ones a batch pass didn't confidently
    // resolve. The only place the per-entry, individually-rate-limited
    // cascade still runs — expected to be a small fraction of a typical
    // bibliography once DOI/arXiv batching has done its work.
    for (const idx of residueIdx) {
      const entry = entries[idx];
      const prior = results[idx] && results[idx].__pendingAttempts;
      const resolved = await resolveEntry(entry, cfg);
      if (prior) resolved.evidence.attempts = [...prior, ...resolved.evidence.attempts];
      results[idx] = resolved;
      tick(entry.key, resolved.evidence.status);
    }

    await backfillMissingAbstracts(results);

    return results;
  }

  // One batched pass, run once across every matched-but-abstract-less
  // entry regardless of which path found it (DOI batch, arXiv batch, or
  // the title-search residue) — the direct replacement for the deleted
  // per-entry serial backfill loop. Ordered by measured abstract-coverage
  // value for a CS/ML-heavy bibliography: arXiv (always has an abstract
  // when the id is real) first, then Europe PMC (biomedical/preprint
  // coverage), then S2's machine-generated tldr as an absolute last resort.
  async function backfillMissingAbstracts(results) {
    const arxivCandidates = []; // { idx, arxivId }
    const doiCandidates = []; // { idx, doi }
    results.forEach((r, idx) => {
      if (!r || !r.evidence || r.evidence.abstract_chars > 0) return;
      const status = r.evidence.status;
      if (!status || status === "unresolved" || status === "unresolved_incomplete") return; // nothing matched — nothing to backfill
      const ext = r.evidence.external_ids || {};
      const arxivId = ext.ArXiv || ext.arxiv;
      if (arxivId && r.evidence.source !== "arxiv") arxivCandidates.push({ idx, arxivId });
      else if (ext.DOI) doiCandidates.push({ idx, doi: ext.DOI });
    });

    if (arxivCandidates.length) {
      const axRes = await arxivBatch(arxivCandidates.map((c) => c.arxivId));
      arxivCandidates.forEach((c, j) => {
        const r = results[c.idx];
        const paper = axRes.papers[j];
        r.evidence.attempts.push({
          provider: "arxiv", method: "abstract_backfill_batch",
          outcome: batchOutcomeFor(paper, axRes.outcome, true), n_results: paper ? 1 : 0,
        });
        if (paper && paper.abstract) {
          r.abstract = paper.abstract;
          r.evidence.abstract_chars = paper.abstract.length;
          r.evidence.abstract_backfilled_from = "arxiv";
        } else if (!CONCLUSIVE_SET.has(axRes.outcome === "ok" ? "ok_search_complete" : axRes.outcome)) {
          r.evidence.had_inconclusive_attempt = true;
        } else {
          doiCandidates.push(r.evidence.external_ids.DOI ? { idx: c.idx, doi: r.evidence.external_ids.DOI } : null);
        }
      });
    }

    const dedupedDoiCandidates = doiCandidates.filter((c) => c && results[c.idx].evidence.abstract_chars === 0);
    if (dedupedDoiCandidates.length) {
      const epmcRes = await europepmcBatchByDoi(dedupedDoiCandidates.map((c) => c.doi));
      dedupedDoiCandidates.forEach((c, j) => {
        const r = results[c.idx];
        if (r.evidence.abstract_chars > 0) return; // filled by the arXiv pass above
        const paper = epmcRes.papers[j];
        r.evidence.attempts.push({
          provider: "europepmc", method: "abstract_backfill_batch",
          outcome: paper ? "ok_record_with_abstract" : (epmcRes.outcome === "ok" ? "ok_no_record" : epmcRes.outcome),
          n_results: paper ? 1 : 0,
        });
        if (paper && paper.abstract) {
          r.abstract = paper.abstract;
          r.evidence.abstract_chars = paper.abstract.length;
          r.evidence.abstract_backfilled_from = "europepmc";
        } else if (!CONCLUSIVE_SET.has(epmcRes.outcome)) {
          r.evidence.had_inconclusive_attempt = true;
        }
      });
    }

    // Last resort: S2's machine-generated tldr, if one was stashed during
    // the DOI phase and nothing above filled the abstract in. Weaker than
    // real evidence, so it's marked distinctly rather than presented as
    // abstract_backfilled_from a real provider.
    for (const r of results) {
      if (r && r.evidence && r.evidence.abstract_chars === 0 && r.evidence.tldr) {
        r.abstract = r.evidence.tldr;
        r.evidence.abstract_chars = r.evidence.tldr.length;
        r.evidence.abstract_backfilled_from = "tldr";
      }
    }
  }

  // Conservative starting budgets — AIMD in rateLimiter.js adapts these up
  // on sustained success and down on repeated 429s/5xxs, so these are
  // floors to start safely from, not fixed throughput.
  if (self.ScribeRateLimiter) {
    self.ScribeRateLimiter.configure("s2", { rps: 1, burst: 2, concurrency: 2, floorRps: 0.2 });
    self.ScribeRateLimiter.configure("crossref", { rps: 3, burst: 3, concurrency: 3, floorRps: 0.5 });
    self.ScribeRateLimiter.configure("openalex", { rps: 8, burst: 8, concurrency: 5, floorRps: 1 });
    // arXiv's terms require ~1 request per 3 seconds on a single
    // connection — minIntervalMs is the floor that enforces that spacing
    // regardless of how the token bucket is otherwise configured.
    self.ScribeRateLimiter.configure("arxiv_api", { rps: 0.333, burst: 1, concurrency: 1, minIntervalMs: 3000, floorRps: 0.1 });
    // No official published SLA — the observed soft limit is well under
    // 10 req/s with aggressive throttling on bursts; conservative starting
    // point since this is a backfill-only source, not on the critical path.
    self.ScribeRateLimiter.configure("europepmc", { rps: 4, burst: 2, concurrency: 2, floorRps: 0.5 });
  }

  self.ScribeResolvers = {
    titleSim, normTitle, resolveEntry, resolveBatch, invertAbstract, identifiersOf,
    s2Batch, openalexBatchByDoi, crossrefBatchByDoi, arxivBatch, europepmcBatchByDoi,
  };
})();
