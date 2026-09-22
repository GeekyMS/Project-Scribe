// Per-provider request scheduler. Pure accounting — no fetch() here, which
// is what makes it possible to unit-test deterministically with an injected
// clock. lib/httpClient.js is the only caller; it composes this with fetch,
// retry, and singleflight.
//
// Providers are logical names ("s2", "crossref", "openalex", "arxiv_api",
// "arxiv_html", "europepmc"), not hosts — api.semanticscholar.org serves
// both /paper/batch and /paper/search from one shared quota, and
// arxiv.org/html has a materially different practical budget from
// export.arxiv.org/api, so keying by host would conflate quotas that are
// actually independent.
//
// State persists to chrome.storage.session (when available) so a service-
// worker restart doesn't reset every bucket to full burst and immediately
// re-flood a provider that was just throttling us — that reset-to-full-burst
// cycle is exactly how the old blanket sleep(3000) code kept re-triggering
// 429s after every job.

(() => {
  const DEFAULTS = { rps: 1, burst: 1, concurrency: 1, minIntervalMs: 0, floorRps: 0.1 };
  const STORAGE_KEY = "scribe_rate_limiter_v1";
  const PERSIST_FIELDS = ["rps", "degradedUntil", "consecutive429", "lastPenaltyReason"];

  const providers = new Map();
  let clockFn = () => Date.now();
  let persistTimer = null;

  function now() {
    return clockFn();
  }

  function getState(provider) {
    let s = providers.get(provider);
    if (!s) {
      s = {
        ...DEFAULTS,
        configuredRps: DEFAULTS.rps,
        tokens: DEFAULTS.burst,
        lastRefill: now(),
        lastRequestAt: 0,
        inFlight: 0,
        queue: [],
        consecutive429: 0,
        consecutiveOk: 0,
        degradedUntil: 0,
        lastPenaltyReason: null,
        wakeTimer: null,
      };
      providers.set(provider, s);
    }
    return s;
  }

  function configure(provider, opts = {}) {
    const s = getState(provider);
    if (opts.rps != null) { s.rps = opts.rps; s.configuredRps = opts.rps; }
    // configure() is expected to run once per provider at setup — reset to
    // a full burst rather than just clamping down, otherwise a provider
    // configured with e.g. burst:2 would start with only the DEFAULTS
    // burst (1) worth of tokens, silently serializing what should be a
    // 2-wide initial burst.
    if (opts.burst != null) { s.burst = opts.burst; s.tokens = opts.burst; }
    if (opts.concurrency != null) s.concurrency = opts.concurrency;
    if (opts.minIntervalMs != null) s.minIntervalMs = opts.minIntervalMs;
    if (opts.floorRps != null) s.floorRps = opts.floorRps;
  }

  function refill(s) {
    const t = now();
    const elapsedS = Math.max(0, (t - s.lastRefill) / 1000);
    s.tokens = Math.min(s.burst, s.tokens + elapsedS * s.rps);
    s.lastRefill = t;
  }

  function scheduleWake(provider, delayMs) {
    const s = getState(provider);
    const delay = Math.max(0, delayMs);
    if (s.wakeTimer) return; // a wake is already pending; pump() re-evaluates everything when it fires
    s.wakeTimer = setTimeout(() => {
      s.wakeTimer = null;
      pump(provider);
    }, delay);
    // Never block process exit / SW idle purely because of a scheduled wake.
    if (s.wakeTimer && typeof s.wakeTimer.unref === "function") s.wakeTimer.unref();
  }

  function pump(provider) {
    const s = getState(provider);
    refill(s);
    const t = now();

    while (s.queue.length > 0) {
      if (s.inFlight >= s.concurrency) return;

      if (t < s.degradedUntil) {
        scheduleWake(provider, s.degradedUntil - t);
        return;
      }

      const sinceLast = t - s.lastRequestAt;
      if (s.minIntervalMs > 0 && sinceLast < s.minIntervalMs) {
        scheduleWake(provider, s.minIntervalMs - sinceLast);
        return;
      }

      const head = s.queue[0];
      if (s.tokens < head.cost) {
        const deficit = head.cost - s.tokens;
        const waitMs = s.rps > 0 ? (deficit / s.rps) * 1000 : 1000;
        scheduleWake(provider, waitMs);
        return;
      }

      s.tokens -= head.cost;
      s.lastRequestAt = t;
      s.inFlight += 1;
      s.queue.shift();
      let released = false;
      head.resolve({
        release() {
          if (released) return;
          released = true;
          s.inFlight = Math.max(0, s.inFlight - 1);
          pump(provider);
        },
      });
    }
  }

  function acquire(provider, { cost = 1, signal } = {}) {
    const s = getState(provider);
    return new Promise((resolve, reject) => {
      const waiter = { cost, resolve, reject };
      if (signal) {
        if (signal.aborted) {
          reject(new DOMException("aborted", "AbortError"));
          return;
        }
        signal.addEventListener("abort", () => {
          const idx = s.queue.indexOf(waiter);
          if (idx !== -1) s.queue.splice(idx, 1);
          reject(new DOMException("aborted", "AbortError"));
        });
      }
      s.queue.push(waiter);
      pump(provider);
    });
  }

  // Parses Retry-After (delta-seconds or HTTP-date), OpenAlex's
  // x-ratelimit-remaining/-reset, and Crossref's x-rate-limit-limit/
  // -interval, in that priority order, and folds them into the provider's
  // schedule — this is the single highest-value adaptation available,
  // since it trusts the server's own stated budget rather than a constant
  // we guessed.
  function report(provider, { status, headers, elapsedMs } = {}) {
    const s = getState(provider);
    const isThrottle = status === 429 || (typeof status === "number" && status >= 500);

    let retryAfterMs = null;
    if (headers && typeof headers.get === "function") {
      const ra = headers.get("retry-after");
      if (ra) {
        const secs = Number(ra);
        if (!Number.isNaN(secs)) retryAfterMs = secs * 1000;
        else {
          const dateMs = Date.parse(ra);
          if (!Number.isNaN(dateMs)) retryAfterMs = Math.max(0, dateMs - now());
        }
      }
      if (retryAfterMs == null) {
        const remaining = headers.get("x-ratelimit-remaining");
        const reset = headers.get("x-ratelimit-reset");
        if (remaining != null && reset != null && Number(remaining) <= 0) {
          const resetS = Number(reset);
          if (!Number.isNaN(resetS)) retryAfterMs = resetS * 1000;
        }
      }
      const crLimit = headers.get("x-rate-limit-limit");
      const crInterval = headers.get("x-rate-limit-interval");
      if (crLimit != null && crInterval != null) {
        const limitN = Number(crLimit);
        const intervalMatch = String(crInterval).match(/(\d+)s/);
        const intervalS = intervalMatch ? Number(intervalMatch[1]) : 1;
        if (!Number.isNaN(limitN) && intervalS > 0) {
          const serverRps = limitN / intervalS;
          if (serverRps > 0 && serverRps < s.configuredRps) s.rps = Math.min(s.rps, serverRps);
        }
      }
    }

    if (isThrottle) {
      s.consecutive429 += 1;
      s.consecutiveOk = 0;
      if (s.consecutive429 >= 2) s.rps = Math.max(s.floorRps, s.rps / 2);
      const penaltyMs = retryAfterMs != null ? retryAfterMs : Math.min(30000, 1000 * 2 ** s.consecutive429);
      penalize(provider, { untilMs: now() + penaltyMs, reason: status === 429 ? "rate_limited" : "server_error" });
    } else {
      s.consecutive429 = 0;
      s.consecutiveOk += 1;
      if (s.consecutiveOk >= 20) {
        s.rps = Math.min(s.configuredRps, s.rps * 1.25);
        s.consecutiveOk = 0;
      }
    }
    schedulePersist();
  }

  function penalize(provider, { untilMs, reason } = {}) {
    const s = getState(provider);
    s.degradedUntil = Math.max(s.degradedUntil, untilMs || 0);
    if (reason) s.lastPenaltyReason = reason;
    if (s.queue.length > 0) scheduleWake(provider, s.degradedUntil - now());
    schedulePersist();
  }

  function reset(provider) {
    providers.delete(provider);
  }

  function snapshot(provider) {
    if (provider) {
      const s = getState(provider);
      return {
        rps: s.rps, configuredRps: s.configuredRps, tokens: s.tokens, queued: s.queue.length,
        inFlight: s.inFlight, degradedUntil: s.degradedUntil, consecutive429: s.consecutive429,
        lastPenaltyReason: s.lastPenaltyReason,
      };
    }
    const out = {};
    for (const [name] of providers) out[name] = snapshot(name);
    return out;
  }

  function hasChromeSession() {
    return typeof chrome !== "undefined" && chrome.storage && chrome.storage.session;
  }

  function schedulePersist() {
    if (!hasChromeSession()) return;
    if (persistTimer) return;
    persistTimer = setTimeout(() => {
      persistTimer = null;
      persist();
    }, 500);
    if (typeof persistTimer.unref === "function") persistTimer.unref();
  }

  async function persist() {
    if (!hasChromeSession()) return;
    const out = {};
    for (const [name, s] of providers) {
      out[name] = {};
      for (const f of PERSIST_FIELDS) out[name][f] = s[f];
    }
    try {
      await chrome.storage.session.set({ [STORAGE_KEY]: out });
    } catch (e) {
      // best-effort — losing persisted rate state degrades to full-burst
      // on the next restart, which is the pre-Phase-1 baseline, not a crash
    }
  }

  async function hydrate() {
    if (!hasChromeSession()) return;
    try {
      const stored = await chrome.storage.session.get(STORAGE_KEY);
      const data = stored && stored[STORAGE_KEY];
      if (!data) return;
      for (const [name, fields] of Object.entries(data)) {
        const s = getState(name);
        for (const f of PERSIST_FIELDS) if (fields[f] !== undefined) s[f] = fields[f];
      }
    } catch (e) {
      // no stored state — start cold, which is safe (just conservative)
    }
  }

  // Test-only hook: lets a node harness inject a fake clock so refill/
  // backoff math is deterministic instead of racing real wall-clock time.
  function _setClock(fn) {
    clockFn = fn || (() => Date.now());
  }

  self.ScribeRateLimiter = { configure, acquire, report, penalize, reset, snapshot, hydrate, persist, _setClock };
})();
