// Composes lib/rateLimiter.js with fetch(), retry, outcome classification,
// and request coalescing. This is the ONE place in the codebase allowed to
// call fetch() against a literature-retrieval provider — resolvers.js,
// fullText.js etc. all go through here.
//
// The contract that matters most: request() never throws and never
// collapses a failure into "not found". Every caller gets back an
// `outcome` field and must branch on it, not on truthiness of `data` — a
// rate limit or network error must never again be indistinguishable from a
// provider conclusively saying "no such paper", which was the root cause
// of ~90 papers being cached forever with an empty abstract.

(() => {
  const DEFAULT_TIMEOUT_MS = 20000;
  const DEFAULT_MAX_ATTEMPTS = 4;
  const RETRYABLE_OUTCOMES = new Set(["rate_limited", "server_error", "network_error", "timeout"]);

  const inFlight = new Map(); // singleflight key -> Promise

  function classifyResponse(resp) {
    if (resp.status === 429) return "rate_limited";
    // Semantic Scholar throttles with 500 as often as 429 — a bare 5xx from
    // any provider is treated as rate limiting, never as a confirmed "no
    // record", since we have no way to tell those apart from the status
    // code alone and the safe assumption is "try again later".
    if (resp.status >= 500) return "server_error";
    if (resp.status === 401 || resp.status === 403) return "auth_error";
    if (resp.status === 404) return "ok_no_record";
    if (resp.status === 200) return "ok";
    return "parse_error";
  }

  function fullJitterDelay(attempt, { base = 1000, cap = 60000 } = {}) {
    return Math.random() * Math.min(cap, base * 2 ** attempt);
  }

  function sleep(ms) {
    return new Promise((resolve) => {
      const t = setTimeout(resolve, ms);
      if (typeof t.unref === "function") t.unref();
    });
  }

  // request() never throws. Returns:
  //   { ok, data, status, headers, outcome, error, retryAfterS, attempts, elapsedMs }
  async function request(provider, opts = {}) {
    const {
      url, params, method = "GET", headers, body,
      timeoutMs = DEFAULT_TIMEOUT_MS, maxAttempts = DEFAULT_MAX_ATTEMPTS,
      parse = "json", cost = 1, signal: externalSignal,
    } = opts;

    const qs = params
      ? new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString()
      : "";
    const fullUrl = qs ? `${url}?${qs}` : url;

    let attempts = 0;
    const startedAt = Date.now();

    for (;;) {
      attempts += 1;
      const lease = await self.ScribeRateLimiter.acquire(provider, { cost, signal: externalSignal });

      let resp, elapsedMs, networkOutcome = null, networkError = null;
      const reqStart = Date.now();
      try {
        const timeoutSignal = AbortSignal.timeout(timeoutMs);
        const signal = externalSignal ? AbortSignal.any([externalSignal, timeoutSignal]) : timeoutSignal;
        resp = await fetch(fullUrl, { method, headers, body, signal });
      } catch (e) {
        const isTimeout = e && (e.name === "TimeoutError" || e.name === "AbortError");
        networkOutcome = isTimeout ? "timeout" : "network_error";
        networkError = String(e);
      } finally {
        elapsedMs = Date.now() - reqStart;
      }

      if (networkOutcome) {
        self.ScribeRateLimiter.report(provider, { status: null, headers: null, elapsedMs });
        lease.release();
        if (attempts < maxAttempts && RETRYABLE_OUTCOMES.has(networkOutcome)) {
          await sleep(fullJitterDelay(attempts));
          continue;
        }
        return { ok: false, data: null, status: null, headers: null, outcome: networkOutcome, error: networkError, retryAfterS: null, attempts, elapsedMs: Date.now() - startedAt };
      }

      self.ScribeRateLimiter.report(provider, { status: resp.status, headers: resp.headers, elapsedMs });
      lease.release();

      const outcome = classifyResponse(resp);
      const retryAfterS = parseRetryAfterHeader(resp.headers);

      if (outcome === "rate_limited" || outcome === "server_error") {
        if (attempts < maxAttempts) {
          const delay = retryAfterS != null ? retryAfterS * 1000 : fullJitterDelay(attempts);
          await sleep(delay);
          continue;
        }
        return { ok: false, data: null, status: resp.status, headers: resp.headers, outcome, error: `HTTP ${resp.status}`, retryAfterS, attempts, elapsedMs: Date.now() - startedAt };
      }

      if (outcome === "auth_error" || outcome === "ok_no_record") {
        return { ok: outcome === "ok_no_record", data: null, status: resp.status, headers: resp.headers, outcome, error: outcome === "auth_error" ? `HTTP ${resp.status}` : null, retryAfterS, attempts, elapsedMs: Date.now() - startedAt };
      }

      if (outcome !== "ok") {
        return { ok: false, data: null, status: resp.status, headers: resp.headers, outcome: "parse_error", error: `HTTP ${resp.status}`, retryAfterS, attempts, elapsedMs: Date.now() - startedAt };
      }

      try {
        const data = parse === "text" ? await resp.text() : parse === "raw" ? resp : await resp.json();
        return { ok: true, data, status: 200, headers: resp.headers, outcome: "ok", error: null, retryAfterS, attempts, elapsedMs: Date.now() - startedAt };
      } catch (e) {
        return { ok: false, data: null, status: 200, headers: resp.headers, outcome: "parse_error", error: String(e), retryAfterS, attempts, elapsedMs: Date.now() - startedAt };
      }
    }
  }

  function parseRetryAfterHeader(headers) {
    if (!headers || typeof headers.get !== "function") return null;
    const v = headers.get("retry-after");
    if (!v) return null;
    const secs = Number(v);
    if (!Number.isNaN(secs)) return secs;
    const dateMs = Date.parse(v);
    if (!Number.isNaN(dateMs)) return Math.max(0, (dateMs - Date.now()) / 1000);
    return null;
  }

  function json(provider, url, params, opts = {}) {
    return request(provider, { url, params, parse: "json", ...opts });
  }

  function text(provider, url, opts = {}) {
    return request(provider, { url, parse: "text", ...opts });
  }

  // Coalesces concurrent callers asking for the same provider+id so a
  // bibliography with the same paper cited under two different BibTeX keys
  // (or two tabs checking at once) issues one request, not two. Callers
  // must normalize the key themselves (lowercased DOI with the
  // https://doi.org/ prefix stripped, arXiv id with version stripped, ...).
  function singleflight(key, fn) {
    const existing = inFlight.get(key);
    if (existing) return existing;
    const p = Promise.resolve().then(fn).finally(() => inFlight.delete(key));
    inFlight.set(key, p);
    return p;
  }

  function inFlightCount() {
    return inFlight.size;
  }

  self.ScribeHttp = { request, json, text, singleflight, inFlightCount };
})();
