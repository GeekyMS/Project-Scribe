// IndexedDB-backed port of pipeline/evidence_cache.py. One object store,
// keyed by bib entry key, entries written individually (not one big blob)
// so an interrupted enrichment run keeps whatever it already resolved —
// same repairability property the per-entry JSON-file cache had.
//
// Chosen over chrome.storage.local (which the extension also uses, for
// settings) because chrome.storage.local has a ~10MB soft quota; an
// enrichment cache that grows across many projects/bibs over time
// shouldn't be arbitrarily capped by that.

(() => {
  const DB_NAME = "scribe-enrichment-cache";
  const STORE_NAME = "entries";
  const DB_VERSION = 1;

  // "unresolved_incomplete" (added alongside the outcome-tracking rewrite of
  // resolvers.js) marks an entry where at least one provider attempt was
  // inconclusive — rate limited, network error, timeout — rather than a
  // confirmed "no such paper". Retrying it is exactly the fix for the bug
  // that made this set necessary in the first place: a transient failure
  // must never harden into a permanent non-result.
  const RETRYABLE_STATUSES = new Set(["unresolved", "unresolved_incomplete", "not_found", "fetch_error", "error"]);

  let dbPromise = null;
  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          db.createObjectStore(STORE_NAME, { keyPath: "key" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return dbPromise;
  }

  async function getEntry(key) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const req = tx.objectStore(STORE_NAME).get(key);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  }

  async function saveEntry(entry) {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).put(entry);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async function getAll() {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const req = tx.objectStore(STORE_NAME).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }

  async function clearAll() {
    const db = await openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  // Decides whether a cached entry must be re-resolved. Only ever asked
  // about ABSTRACT enrichment.
  //
  // This deliberately does NOT consider evidence.full_text.status. The
  // Python original retried whenever full text was "not_attempted",
  // which made sense there because a later full-text pass could fill it
  // in. The extension never fetches full text at all (out of scope —
  // it needs PDF download + Docling), so resolvers.js hardcodes
  // "not_attempted" forever. Keeping that clause made needsRefetch
  // return true for *every* entry, including perfectly resolved ones
  // with a real abstract — so the cache never once hit, every Check
  // re-fetched the whole bibliography, and that is what was driving the
  // provider rate-limiting and the 15-minute job timeouts.
  function needsRefetch(cached, { force = false } = {}) {
    if (force || cached == null) return true;
    const evidence = cached.evidence || {};
    if (evidence.resolver_version !== "1") return true;
    if (RETRYABLE_STATUSES.has(evidence.status)) return true;
    // A matched paper can still have had_inconclusive_attempt === true —
    // e.g. it matched via one provider while another was rate-limited
    // partway through the abstract lookup. That is a "we don't actually
    // know yet" result being reported as abstract_chars: 0, not a
    // confirmed "this paper has no abstract" — the exact failure mode
    // that used to cache ~90 papers as permanently evidence-less after a
    // transient 429. Retry it like any other inconclusive result.
    if (evidence.had_inconclusive_attempt) return true;
    return false;
  }

  self.ScribeEnrichmentCache = { getEntry, saveEntry, getAll, clearAll, needsRefetch };
})();
