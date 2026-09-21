// Port of pipeline/abstract_fetcher.py's parse_bib_content + enrich_entries.
// Runs in background.js's service worker; uses resolvers.js (HTTP) and
// enrichmentCache.js (IndexedDB) directly rather than through messages,
// since all three live in the same worker context.

(() => {
  const RATE_S2_UNAUTH = 3000; // ms — see abstract_fetcher.py's RATE_S2_UNAUTH
  const RATE_S2_AUTH = 1000;

  function parseBibContent(text) {
    const entries = [];
    const entryRe = /@(\w+)\s*\{([^,]+),\s*([\s\S]*?)\n\}/g;
    const fieldRe = /(\w+)\s*=\s*(?:\{((?:[^{}]|\{[^{}]*\})*)\}|"([^"]*)"|(\S+))/g;

    let m;
    while ((m = entryRe.exec(text)) !== null) {
      const entryType = m[1].toLowerCase();
      const entryKey = m[2].trim();
      const body = m[3];

      const entry = { key: entryKey, type: entryType };

      fieldRe.lastIndex = 0;
      let fm;
      while ((fm = fieldRe.exec(body)) !== null) {
        const fieldName = fm[1].toLowerCase();
        let fieldValue = fm[2] ?? fm[3] ?? fm[4] ?? "";
        fieldValue = fieldValue.replace(/\\&/g, "&").trim();
        entry[fieldName] = fieldValue;
      }

      entries.push(entry);
    }
    return entries;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // progress(done, total, key, status) — mirrors abstract_fetcher.py's
  // enrich_entries progress callback shape.
  async function enrichEntries(bibEntries, cfg = {}, { forceRefresh = false, progress } = {}) {
    const { mailto, s2ApiKey, openalexApiKey } = cfg;
    const rateDelay = s2ApiKey ? RATE_S2_AUTH : RATE_S2_UNAUTH;
    const resolveCfg = { mailto, s2ApiKey, openalexApiKey };

    const enriched = [];
    for (let i = 0; i < bibEntries.length; i++) {
      const entry = bibEntries[i];
      const key = entry.key || `entry_${i}`;
      const cached = await self.ScribeEnrichmentCache.getEntry(key);
      const needsFetch = self.ScribeEnrichmentCache.needsRefetch(cached, { force: forceRefresh });

      let result;
      if (!needsFetch && cached) {
        result = cached;
      } else {
        result = await self.ScribeResolvers.resolveEntry(entry, resolveCfg);
        result.key = key; // keyPath for IndexedDB
        result.evidence.fetched_at = new Date().toISOString();
        await self.ScribeEnrichmentCache.saveEntry(result);
        await sleep(result.evidence.attempts && result.evidence.attempts.length ? rateDelay : 50);
      }

      enriched.push(result);
      if (progress) progress(i + 1, bibEntries.length, key, result.evidence.status);
    }

    return enriched;
  }

  self.ScribeBibEnrichment = { parseBibContent, enrichEntries };
})();
