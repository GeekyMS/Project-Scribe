// Structured full-text retrieval: arXiv HTML -> ar5iv -> Europe PMC JATS
// XML -> none. No PDF parsing — see the retrieval-rebuild plan for why
// (2-column text order mangles exactly the tables numeric claims live in).
//
// Design note / deviation from the original plan: the plan called for an
// offscreen document + DOMParser, since a service worker has neither DOM.
// This module does regex-based extraction directly in the service worker
// instead. LaTeXML's output (ltx_section/ltx_title/ltx_table/figcaption)
// and JATS (sec/title/table-wrap/caption) are both regular enough that
// anchored regexes extract section/table boundaries reliably, and it
// means this whole module is testable in plain Node against live APIs —
// an offscreen document is not (it needs a real Chrome renderer). If
// real-world PDFs/HTML turn out to need true DOM semantics, upgrading to
// an offscreen DOMParser is a contained follow-up, not a rewrite: only
// parseArxivHtml/parseJats below would move.

(() => {
  const ARXIV_HTML_BASE = "https://arxiv.org/html";
  const AR5IV_BASE = "https://ar5iv.labs.arxiv.org/html";
  const EUROPEPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest";

  const SECTION_MAX_CHARS = 8000;
  const TABLE_MAX_CHARS = 3000;
  // A page can come back 200 OK with real bytes and still yield nothing
  // section-targeting recognizes — observed live on an older ar5iv
  // conversion whose markup didn't match the expected ltx_section
  // structure closely enough. That must not be reported as "ok" with
  // usable evidence; it's the same "we don't actually have this" case as
  // no_html, just discovered one step later.
  const MIN_USEFUL_CHARS = 200;
  const KEEP_KINDS = new Set(["abstract", "results", "result", "experiments", "experiment", "evaluation", "findings", "conclusion", "discussion"]);

  function stripTags(html) {
    return (html || "")
      .replace(/<[^>]+>/g, " ")
      .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
      .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
      .replace(/\s+/g, " ")
      .trim();
  }

  function kindOf(title) {
    const first = (title || "").toLowerCase().replace(/^[\d.]+\s*/, "").split(/\s+/)[0] || "";
    return KEEP_KINDS.has(first) ? first : "other";
  }

  // ── arXiv HTML / ar5iv (LaTeXML output) ──
  function parseArxivHtml(html) {
    const sections = [];
    const tables = [];

    const abstractMatch = html.match(/<(?:div|blockquote)[^>]*class="[^"]*ltx_abstract[^"]*"[^>]*>([\s\S]*?)<\/(?:div|blockquote)>/);
    if (abstractMatch) {
      sections.push({ title: "Abstract", kind: "abstract", text: stripTags(abstractMatch[1]).slice(0, SECTION_MAX_CHARS) });
    }

    // Split on ltx_section boundaries rather than matching each one
    // greedily to a specific close tag — nested divs make a precise
    // open/close match impractical with regex, and a boundary split is
    // robust to that nesting.
    const parts = html.split(/<section[^>]*class="[^"]*\bltx_section\b[^"]*"[^>]*>/);
    for (let i = 1; i < parts.length; i++) {
      const block = parts[i];
      const titleMatch = block.match(/<h\d[^>]*class="[^"]*ltx_title[^"]*"[^>]*>([\s\S]*?)<\/h\d>/);
      const title = titleMatch ? stripTags(titleMatch[1]).replace(/^\d+(\.\d+)*\s*/, "") : "";
      const kind = kindOf(title);
      const withoutTables = block.replace(/<figure[^>]*class="[^"]*ltx_table[^"]*"[^>]*>[\s\S]*?<\/figure>/g, "");
      const bodyText = stripTags(withoutTables).slice(0, SECTION_MAX_CHARS);
      sections.push({ title, kind, text: bodyText });
    }

    const tableRe = /<figure[^>]*class="[^"]*ltx_table[^"]*"[^>]*>([\s\S]*?)<\/figure>/g;
    let m;
    while ((m = tableRe.exec(html)) !== null) {
      const block = m[1];
      const capMatch = block.match(/<figcaption[^>]*>([\s\S]*?)<\/figcaption>/);
      tables.push({ caption: capMatch ? stripTags(capMatch[1]) : "", text: stripTags(block).slice(0, TABLE_MAX_CHARS) });
    }

    return { sections, tables };
  }

  // ── Europe PMC JATS XML ──
  function parseJats(xml) {
    const sections = [];
    const tables = [];

    const abstractMatch = xml.match(/<abstract[^>]*>([\s\S]*?)<\/abstract>/);
    if (abstractMatch) sections.push({ title: "Abstract", kind: "abstract", text: stripTags(abstractMatch[1]).slice(0, SECTION_MAX_CHARS) });

    const secRe = /<sec[^>]*>([\s\S]*?)<\/sec>/g;
    let m;
    while ((m = secRe.exec(xml)) !== null) {
      const block = m[1];
      const titleMatch = block.match(/<title>([\s\S]*?)<\/title>/);
      const title = titleMatch ? stripTags(titleMatch[1]) : "";
      const kind = kindOf(title);
      const withoutTables = block.replace(/<table-wrap[^>]*>[\s\S]*?<\/table-wrap>/g, "");
      sections.push({ title, kind, text: stripTags(withoutTables).slice(0, SECTION_MAX_CHARS) });
    }

    const twRe = /<table-wrap[^>]*>([\s\S]*?)<\/table-wrap>/g;
    while ((m = twRe.exec(xml)) !== null) {
      const block = m[1];
      const capMatch = block.match(/<caption>([\s\S]*?)<\/caption>/);
      tables.push({ caption: capMatch ? stripTags(capMatch[1]) : "", text: stripTags(block).slice(0, TABLE_MAX_CHARS) });
    }

    return { sections, tables };
  }

  // Keep Abstract + Results/Experiments/Evaluation + Conclusion/Discussion
  // + every table with its caption. This is the whole point of using
  // structured HTML/XML instead of PDFs: naive chunking severs a numeric
  // claim from the table headers that give it meaning, and this keeps
  // both together while cutting a ~60k-char paper to roughly a third.
  function sectionTarget(parsed) {
    const keep = parsed.sections.filter((s) => KEEP_KINDS.has(s.kind));
    const chosen = keep.length ? keep : parsed.sections.slice(0, 3);
    let text = chosen.map((s) => `## ${s.title || "(untitled section)"}\n${s.text}`).join("\n\n");
    if (parsed.tables.length) {
      text += "\n\n## Tables\n" + parsed.tables.map((t) => `[Table: ${t.caption || "(no caption)"}]\n${t.text}`).join("\n\n");
    }
    return text;
  }

  async function fetchArxivHtml(arxivId) {
    const url = `${ARXIV_HTML_BASE}/${arxivId}`;
    const r = await self.ScribeHttp.text("arxiv_html", url, { timeoutMs: 20000, maxAttempts: 2 });
    if (r.ok && typeof r.data === "string" && r.data.length > 500) return { ok: true, html: r.data, source: "arxiv_html", url };
    return { ok: false, outcome: r.outcome, status: r.status };
  }

  async function fetchAr5iv(arxivId) {
    const url = `${AR5IV_BASE}/${arxivId}`;
    const r = await self.ScribeHttp.text("arxiv_html", url, { timeoutMs: 20000, maxAttempts: 2 });
    if (r.ok && typeof r.data === "string" && r.data.length > 500) return { ok: true, html: r.data, source: "ar5iv", url };
    return { ok: false, outcome: r.outcome, status: r.status };
  }

  async function resolvePmcid(doi) {
    const r = await self.ScribeHttp.json("europepmc", `${EUROPEPMC_API}/search`, { query: `DOI:"${doi}"`, format: "json", resultType: "core" }, { timeoutMs: 20000, maxAttempts: 2 });
    if (!r.ok) return null;
    const items = (r.data && r.data.resultList && r.data.resultList.result) || [];
    const withPmc = items.find((it) => it.pmcid);
    return withPmc ? withPmc.pmcid : null;
  }

  async function fetchEuropePmcFullText(pmcid) {
    const url = `${EUROPEPMC_API}/${pmcid}/fullTextXML`;
    const r = await self.ScribeHttp.text("europepmc", url, { timeoutMs: 20000, maxAttempts: 2 });
    if (r.ok && typeof r.data === "string" && r.data.length > 500) return { ok: true, xml: r.data, source: "europepmc_jats", url };
    return { ok: false, outcome: r.outcome, status: r.status };
  }

  // fetchFullText(entry) — entry is an enriched bib entry (has
  // evidence.external_ids from resolveBatch/resolveEntry). Returns
  // { status, source, source_url, chars, sections, tables, targeted_text, error }.
  // status: "ok" | "no_html" | "fetch_error" | "not_available".
  async function fetchFullText(entry) {
    const ext = (entry.evidence && entry.evidence.external_ids) || {};
    const arxivId = ext.ArXiv || ext.arxiv;

    if (arxivId) {
      let res = await fetchArxivHtml(arxivId);
      if (!res.ok) res = await fetchAr5iv(arxivId);
      if (res.ok) {
        const parsed = parseArxivHtml(res.html);
        const targeted = sectionTarget(parsed);
        if (targeted.length >= MIN_USEFUL_CHARS) {
          return { status: "ok", source: res.source, source_url: res.url, chars: targeted.length, sections: parsed.sections.length, tables: parsed.tables.length, targeted_text: targeted, error: null };
        }
        return { status: "no_html", source: null, source_url: null, chars: 0, sections: 0, tables: 0, targeted_text: "", error: "parsed_empty" };
      }
      return { status: res.outcome === "ok_no_record" ? "no_html" : "fetch_error", source: null, source_url: null, chars: 0, sections: 0, tables: 0, targeted_text: "", error: res.outcome || null };
    }

    const doi = ext.DOI;
    if (doi) {
      const pmcid = await resolvePmcid(doi);
      if (pmcid) {
        const res = await fetchEuropePmcFullText(pmcid);
        if (res.ok) {
          const parsed = parseJats(res.xml);
          const targeted = sectionTarget(parsed);
          if (targeted.length >= MIN_USEFUL_CHARS) {
            return { status: "ok", source: res.source, source_url: res.url, chars: targeted.length, sections: parsed.sections.length, tables: parsed.tables.length, targeted_text: targeted, error: null };
          }
          return { status: "not_available", source: null, source_url: null, chars: 0, sections: 0, tables: 0, targeted_text: "", error: "parsed_empty" };
        }
        return { status: "fetch_error", source: null, source_url: null, chars: 0, sections: 0, tables: 0, targeted_text: "", error: res.outcome || null };
      }
    }

    return { status: "not_available", source: null, source_url: null, chars: 0, sections: 0, tables: 0, targeted_text: "", error: null };
  }

  // arXiv HTML has no published rate-limit terms distinct from the query
  // API's 1-per-3s (that restriction is specifically about export.arxiv.org
  // API requests) — conservative default since it's still arXiv's
  // infrastructure. europepmc's config is set once in resolvers.js and
  // shared here since both hit the same host/budget.
  if (self.ScribeRateLimiter) {
    self.ScribeRateLimiter.configure("arxiv_html", { rps: 1, burst: 2, concurrency: 2, minIntervalMs: 500, floorRps: 0.2 });
  }

  self.ScribeFullText = { fetchFullText, parseArxivHtml, parseJats, sectionTarget };
})();
