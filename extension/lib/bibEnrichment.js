// Port of pipeline/abstract_fetcher.py's parse_bib_content + enrich_entries.
// Runs in background.js's service worker; uses resolvers.js (HTTP, via
// lib/rateLimiter.js + lib/httpClient.js) and enrichmentCache.js
// (IndexedDB) directly rather than through messages, since all three live
// in the same worker context.

(() => {
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

  // progress(done, total, key, status) — mirrors abstract_fetcher.py's
  // enrich_entries progress callback shape. `done`/`total` count cache
  // hits too (they're counted up front) so the caller's progress bar
  // reflects the whole bibliography, not just the entries that needed a
  // network round trip.
  //
  // Resolution itself goes through ScribeResolvers.resolveBatch, which
  // partitions entries by identifier and issues a handful of batched
  // provider requests instead of one request per entry — there is no
  // per-entry sleep() here any more; pacing belongs entirely to
  // lib/rateLimiter.js, which a blanket delay in this loop can't see or
  // adapt (it doesn't know which entries actually cost a request, and it
  // can't be told to back off after a 429 the way the limiter can).
  async function enrichEntries(bibEntries, cfg = {}, { forceRefresh = false, progress } = {}) {
    const resolveCfg = { mailto: cfg.mailto, s2ApiKey: cfg.s2ApiKey, openalexApiKey: cfg.openalexApiKey };
    const total = bibEntries.length;

    const cachedByKey = new Map();
    const toResolve = []; // { entry, key }
    for (let i = 0; i < bibEntries.length; i++) {
      const entry = bibEntries[i];
      const key = entry.key || `entry_${i}`;
      const cached = await self.ScribeEnrichmentCache.getEntry(key);
      if (!forceRefresh && cached && !self.ScribeEnrichmentCache.needsRefetch(cached, { force: forceRefresh })) {
        cachedByKey.set(key, cached);
      } else {
        toResolve.push({ entry, key });
      }
    }

    let done = 0;
    for (const [key, cached] of cachedByKey) {
      done += 1;
      if (progress) progress(done, total, key, cached.evidence.status);
    }

    if (toResolve.length > 0) {
      const resolvedList = await self.ScribeResolvers.resolveBatch(
        toResolve.map((t) => t.entry),
        resolveCfg,
        {
          onProgress: (_n, _batchTotal, key, status) => {
            done += 1;
            if (progress) progress(done, total, key, status);
          },
        }
      );

      for (let i = 0; i < toResolve.length; i++) {
        const { key } = toResolve[i];
        const result = resolvedList[i];
        result.key = key; // keyPath for IndexedDB
        result.evidence.fetched_at = new Date().toISOString();
        await self.ScribeEnrichmentCache.saveEntry(result);
        cachedByKey.set(key, result);
      }
    }

    return bibEntries.map((entry, i) => cachedByKey.get(entry.key || `entry_${i}`));
  }

  self.ScribeBibEnrichment = { parseBibContent, enrichEntries };
})();
