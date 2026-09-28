// IndexedDB-backed cache, keyed by canonical paper identity rather than
// BibTeX key. Two stores:
//   papers  — keyPath "cid"; the actual enrichment record (evidence,
//             abstract, full text).
//   aliases — keyPath "alias"; maps every identifier a paper is known
//             by ("bibkey:foo2020", "doi:10.1/x", "arxiv:2401.00001")
//             to that paper's cid.
// This is what lets the same paper cited as `vaswani2017attention` in one
// project and `vaswani2017` in another resolve to one cached record
// instead of two — the whole point of moving off a BibTeX-keyed cache.
//
// v1 (a single "entries" store keyed directly by BibTeX key) is migrated
// into this schema in onupgradeneeded, and kept around afterward as
// rollback insurance rather than deleted.

(() => {
  const DB_NAME = "scribe-enrichment-cache";
  const DB_VERSION = 2;
  const LEGACY_STORE = "entries"; // v1
  const PAPERS_STORE = "papers";
  const ALIASES_STORE = "aliases";

  // "unresolved_incomplete" (added alongside the outcome-tracking rewrite of
  // resolvers.js) marks an entry where at least one provider attempt was
  // inconclusive — rate limited, network error, timeout — rather than a
  // confirmed "no such paper". Retrying it is exactly the fix for the bug
  // that made this set necessary in the first place: a transient failure
  // must never harden into a permanent non-result.
  const RETRYABLE_STATUSES = new Set(["unresolved", "unresolved_incomplete", "not_found", "fetch_error", "error"]);

  function normalizeDoi(doi) {
    return (doi || "").replace(/^https?:\/\/doi\.org\//i, "").trim().toLowerCase() || null;
  }

  function normalizeArxiv(id) {
    return (id || "").replace(/v\d+$/, "").trim().toLowerCase() || null;
  }

  // Not cryptographic — just needs to be stable and low-collision at
  // bibliography scale, as a last-resort identity for entries with
  // neither a DOI nor an arXiv id.
  function titleHash(title, year) {
    const s = `${(title || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()}|${year || ""}`;
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
    return h.toString(36);
  }

  // cid precedence: doi > arxiv > title+year hash > bibkey (degenerate —
  // only used when an entry has neither an identifier nor a title, which
  // in practice means a malformed BibTeX entry).
  function computeCid(entry) {
    const ext = (entry.evidence && entry.evidence.external_ids) || {};
    const doi = normalizeDoi(ext.DOI || entry.doi);
    if (doi) return `doi:${doi}`;
    const arxiv = normalizeArxiv(ext.ArXiv || ext.arxiv || entry.eprint);
    if (arxiv) return `arxiv:${arxiv}`;
    if (entry.title) return `title:${titleHash(entry.title, entry.year)}`;
    return `bibkey:${entry.key}`;
  }

  function idsOf(entry) {
    const ext = (entry.evidence && entry.evidence.external_ids) || {};
    return { doi: normalizeDoi(ext.DOI || entry.doi), arxiv: normalizeArxiv(ext.ArXiv || ext.arxiv || entry.eprint) };
  }

  let dbPromise = null;
  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);

      req.onupgradeneeded = (event) => {
        const db = req.result;
        const tx = req.transaction;

        if (!db.objectStoreNames.contains(LEGACY_STORE)) {
          db.createObjectStore(LEGACY_STORE, { keyPath: "key" });
        }

        let papersStore;
        if (!db.objectStoreNames.contains(PAPERS_STORE)) {
          papersStore = db.createObjectStore(PAPERS_STORE, { keyPath: "cid" });
          papersStore.createIndex("by_doi", "ids.doi");
          papersStore.createIndex("by_arxiv", "ids.arxiv");
        } else {
          papersStore = tx.objectStore(PAPERS_STORE);
        }

        let aliasesStore;
        if (!db.objectStoreNames.contains(ALIASES_STORE)) {
          aliasesStore = db.createObjectStore(ALIASES_STORE, { keyPath: "alias" });
          aliasesStore.createIndex("by_cid", "cid");
        } else {
          aliasesStore = tx.objectStore(ALIASES_STORE);
        }

        // Migrate v1 -> v2 with a cursor inside this same versionchange
        // transaction. No `await` between cursor steps: an IDB transaction
        // auto-commits on an empty microtask turn, so an async gap here
        // would silently truncate the migration. If this throws partway,
        // the whole versionchange transaction aborts and the DB cleanly
        // stays at its previous version — nothing is left half-migrated.
        if (event.oldVersion < 2 && db.objectStoreNames.contains(LEGACY_STORE)) {
          const legacyStore = tx.objectStore(LEGACY_STORE);
          const cursorReq = legacyStore.openCursor();
          cursorReq.onsuccess = () => {
            const cursor = cursorReq.result;
            if (!cursor) return;
            const oldEntry = cursor.value;
            const cid = computeCid(oldEntry);
            const ev = oldEntry.evidence || {};
            const migrated = { ...oldEntry, cid, ids: idsOf(oldEntry) };
            if (migrated.evidence) {
              // v1 records can't distinguish a 429 from genuine absence —
              // abstract_chars: 0 there means "unknown", never "confirmed
              // absent". Marking it unknown makes needsRefetch retry it,
              // which is the correct, cheap fix (a batched re-check, not
              // the per-entry cascade this used to cost).
              migrated.evidence.abstract_state = ev.abstract_chars > 0 ? "present" : "unknown";
            }
            papersStore.put(migrated);
            aliasesStore.put({ alias: `bibkey:${oldEntry.key}`, cid });
            if (migrated.ids.doi) aliasesStore.put({ alias: `doi:${migrated.ids.doi}`, cid });
            if (migrated.ids.arxiv) aliasesStore.put({ alias: `arxiv:${migrated.ids.arxiv}`, cid });
            cursor.continue();
          };
        }
      };

      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return dbPromise;
  }

  function reqToPromise(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function getPaperByCid(db, cid) {
    if (!cid) return null;
    const tx = db.transaction(PAPERS_STORE, "readonly");
    return (await reqToPromise(tx.objectStore(PAPERS_STORE).get(cid))) || null;
  }

  async function getAlias(db, alias) {
    if (!alias) return null;
    const tx = db.transaction(ALIASES_STORE, "readonly");
    return (await reqToPromise(tx.objectStore(ALIASES_STORE).get(alias))) || null;
  }

  // getEntry(bibKey, identifiers?) — looks up by the strongest identifier
  // available FIRST (doi, then arxiv), falling back to the BibTeX key.
  // This is what actually delivers cross-project reuse: a paper resolved
  // once under one project's BibTeX key is found by DOI/arXiv id the next
  // time it's cited under a different key, without a network request.
  // `identifiers` is optional — pass { doi, arxivId } when the raw BibTeX
  // entry already carries one (see ScribeResolvers.identifiersOf); when
  // omitted, only the BibTeX-key alias is tried.
  async function getEntry(bibKey, identifiers) {
    const db = await openDb();
    const candidates = [];
    if (identifiers && identifiers.doi) candidates.push(`doi:${normalizeDoi(identifiers.doi)}`);
    if (identifiers && identifiers.arxivId) candidates.push(`arxiv:${normalizeArxiv(identifiers.arxivId)}`);
    candidates.push(`bibkey:${bibKey}`);

    for (const alias of candidates) {
      const row = await getAlias(db, alias);
      if (row) {
        const paper = await getPaperByCid(db, row.cid);
        if (paper) return { ...paper, key: bibKey }; // report under THIS project's key, not whichever project last saved it
      }
    }
    return null;
  }

  async function saveEntry(entry) {
    const db = await openDb();
    const cid = computeCid(entry);
    const ids = idsOf(entry);
    const record = { ...entry, cid, ids };
    if (record.evidence) {
      const hasAbstract = (record.abstract || "").length > 0;
      // had_inconclusive_attempt means some provider attempt was rate
      // limited/errored rather than conclusively answering — that must
      // never be reported as "this paper confirmed has no abstract".
      record.evidence.abstract_state = hasAbstract
        ? "present"
        : record.evidence.had_inconclusive_attempt
          ? "unknown"
          : "absent_confirmed";
    }

    const tx = db.transaction([PAPERS_STORE, ALIASES_STORE], "readwrite");
    tx.objectStore(PAPERS_STORE).put(record);
    tx.objectStore(ALIASES_STORE).put({ alias: `bibkey:${entry.key}`, cid });
    if (ids.doi) tx.objectStore(ALIASES_STORE).put({ alias: `doi:${ids.doi}`, cid });
    if (ids.arxiv) tx.objectStore(ALIASES_STORE).put({ alias: `arxiv:${ids.arxiv}`, cid });
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async function getAll() {
    const db = await openDb();
    const tx = db.transaction(PAPERS_STORE, "readonly");
    return (await reqToPromise(tx.objectStore(PAPERS_STORE).getAll())) || [];
  }

  async function clearAll() {
    const db = await openDb();
    const storeNames = [PAPERS_STORE, ALIASES_STORE, LEGACY_STORE].filter((n) => db.objectStoreNames.contains(n));
    const tx = db.transaction(storeNames, "readwrite");
    for (const name of storeNames) tx.objectStore(name).clear();
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  // Decides whether a cached entry must be re-resolved. Only ever asked
  // about ABSTRACT enrichment — this deliberately does NOT consider
  // evidence.full_text.status, for the same reason it never did: full
  // text is fetched by a separate pass (hydrateFullText) with its own
  // status, and folding it in here would make the abstract cache miss on
  // every check the way it did before resolver_version tracking existed.
  function needsRefetch(cached, { force = false } = {}) {
    if (force || cached == null) return true;
    const evidence = cached.evidence || {};
    if (evidence.resolver_version !== "1") return true;
    if (RETRYABLE_STATUSES.has(evidence.status)) return true;
    // A matched paper can still have had_inconclusive_attempt === true —
    // e.g. it matched via one provider while another was rate-limited
    // partway through the abstract lookup — or, for a migrated v1
    // record, abstract_state === "unknown" (v1 couldn't tell a 429 from
    // genuine absence at all). Both mean "we don't actually know yet",
    // not "confirmed no abstract" — retry like any other inconclusive
    // result. This is exactly the failure mode that used to cache ~90
    // papers as permanently evidence-less after a transient 429.
    if (evidence.had_inconclusive_attempt) return true;
    if (evidence.abstract_state === "unknown") return true;
    return false;
  }

  self.ScribeEnrichmentCache = { getEntry, saveEntry, getAll, clearAll, needsRefetch };
})();
