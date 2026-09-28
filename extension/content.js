// ISOLATED world. Relay between inject.js (page/MAIN world) and background.js
// (service worker), plus the extension's UI panel. Owns its own DOM (per the
// convention that content.js, not inject.js, renders UI) — inject.js never
// touches anything but CodeMirror.
//
// Two layers of UI here:
//   1. The real feature: Draft / Check / Accept, backed by the local
//      service (background.js) when available, degrading to
//      lib/citations.js's offline key check when it isn't.
//   2. The risk-spike debug tools from the earlier investigation, kept
//      behind a collapsed toggle — they're how we re-diagnose things if
//      Overleaf ever changes its DOM/CodeMirror integration again.

(() => {
  const pending = new Map(); // requestId -> resolve function

  // Establishes the bridge nonce inject.js requires on every scribe:request
  // — see inject.js's comment for the threat this closes. Dispatched once,
  // as early as possible in this script's execution.
  const BRIDGE_NONCE = (crypto.randomUUID && crypto.randomUUID())
    || `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  window.dispatchEvent(new CustomEvent("scribe:init", { detail: { nonce: BRIDGE_NONCE } }));

  window.addEventListener("scribe:response", (event) => {
    const { requestId, result, error } = event.detail || {};
    const resolve = pending.get(requestId);
    if (!resolve) return;
    pending.delete(requestId);
    resolve({ result, error });
  });

  function callInject(type, payload) {
    const requestId = `${type}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    return new Promise((resolve) => {
      pending.set(requestId, resolve);
      window.dispatchEvent(new CustomEvent("scribe:request", { detail: { requestId, type, payload, nonce: BRIDGE_NONCE } }));
      setTimeout(() => {
        if (pending.has(requestId)) {
          pending.delete(requestId);
          resolve({ result: null, error: "timed out waiting for inject.js response" });
        }
      }, 5000);
    });
  }

  function sendToBackground(type, payload) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type, payload }, (ack) => resolve(ack || { result: null, error: "no response from background" }));
    });
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ---- Fetch another file by name (bib discovery — proven in FINDINGS.md §5) --

  function findLeafEntriesByExactText(targetText) {
    const matches = [];
    const nodes = document.querySelectorAll(".item-name, [class*='file-tree'] span, [class*='entity-name']");
    for (const el of nodes) {
      if ((el.textContent || "").trim() === targetText) matches.push(el);
    }
    return matches;
  }

  function findBibFilenameInFileTree() {
    // Same node set as findLeafEntriesByExactText, but looking for anything
    // ending in .bib rather than an exact match — this is how we auto-detect
    // a bib filename instead of requiring the user to type one.
    //
    // Folder-container elements' textContent concatenates every descendant
    // filename (seen in SCRIBE_PROBE_FILES as e.g. a folder "book_5"
    // containing "refs.bib" reporting textContent "book_5refs.bib") — the
    // same selector matches both the noisy container and the clean leaf
    // underneath it, so picking the FIRST match risked returning the noisy
    // one. Since concatenated noise is always longer than the real leaf
    // filename, take the shortest candidate instead of the first.
    const nodes = document.querySelectorAll(".item-name, [class*='file-tree'] span, [class*='entity-name']");
    let best = null;
    for (const el of nodes) {
      const text = (el.textContent || "").trim();
      if (text.endsWith(".bib") && text.length < 60) {
        if (!best || text.length < best.length) best = text;
      }
    }
    return best;
  }

  function findCurrentlySelectedEntryText() {
    const el = document.querySelector(
      "[class*='file-tree'] [class*='selected'], [class*='file-tree'] [aria-selected='true']"
    );
    if (!el) return null;
    const nameEl = el.querySelector(".item-name") || el;
    const text = (nameEl.textContent || "").trim();
    return text || null;
  }

  function clickEntry(el) {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  }

  // Waits for Path A's hook to fire again (i.e. Overleaf actually rebuilt
  // the editor for a newly-focused file) before trusting any read against
  // it. Shared between switching to the target file and switching back,
  // because both are real remounts and both need the same wait.
  async function waitForNextHookFire(hookCountBefore, { timeoutMs = 4000, pollMs = 150 } = {}) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      await sleep(pollMs);
      const probe = await callInject("SCRIBE_PROBE");
      const count = probe.result && probe.result.pathA ? probe.result.pathA.hookFireCount : null;
      if (hookCountBefore !== null && count !== null && count > hookCountBefore) return true;
    }
    return false;
  }

  async function fetchFileByName(targetText, { timeoutMs = 4000, pollMs = 150 } = {}) {
    const before = await callInject("SCRIBE_PROBE");
    const hookCountBefore = before.result && before.result.pathA ? before.result.pathA.hookFireCount : null;
    const previouslySelected = findCurrentlySelectedEntryText();

    // Snapshot the currently-open document BEFORE switching away, so we can
    // prove on return that Overleaf's remount actually caught back up to
    // the true state rather than leaving us reading/writing against a
    // stale local buffer. Added after a live test where switching away to
    // read a .bib and back silently lost ~20KB from the restored document
    // -- SCRIBE_READ/SCRIBE_INSERT correctly acting on whatever view
    // resolves is no protection if that view's own content is stale.
    let preSwitchSnapshot = null;
    if (previouslySelected) {
      const preRead = await callInject("SCRIBE_READ");
      if (preRead.result) {
        preSwitchSnapshot = { length: preRead.result.length, hash: preRead.result.hash };
      }
    }

    const targets = findLeafEntriesByExactText(targetText);
    if (targets.length === 0) {
      return { error: `no file-tree entry found with exact text "${targetText}"`, previouslySelected };
    }
    clickEntry(targets[0]);
    const hookFired = await waitForNextHookFire(hookCountBefore, { timeoutMs, pollMs });

    const read = await callInject("SCRIBE_READ");
    const hookCountAfterTargetRead = (await callInject("SCRIBE_PROBE")).result?.pathA?.hookFireCount ?? null;

    let restored = false;
    let integrityCheck = null;
    if (previouslySelected) {
      const backTargets = findLeafEntriesByExactText(previouslySelected);
      if (backTargets.length > 0) {
        clickEntry(backTargets[0]);
        restored = true;
        const restoreHookFired = await waitForNextHookFire(hookCountAfterTargetRead, { timeoutMs, pollMs });

        if (preSwitchSnapshot) {
          const postRead = await callInject("SCRIBE_READ");
          const postSnapshot = postRead.result
            ? { length: postRead.result.length, hash: postRead.result.hash }
            : null;
          const matches = !!postSnapshot
            && postSnapshot.length === preSwitchSnapshot.length
            && postSnapshot.hash === preSwitchSnapshot.hash;
          integrityCheck = { preSwitchSnapshot, postSnapshot, matches, restoreHookFired };
        }
      }
    }

    return {
      target: targetText,
      hookFired,
      hookCountBefore,
      previouslySelected,
      restored,
      read: read.result,
      readError: read.error,
      integrityCheck,
    };
  }

  // ---- The real feature: Draft / Check / Accept ----------------------------

  async function getCurrentDocText() {
    const { result, error } = await callInject("SCRIBE_READ");
    if (error || !result) throw new Error(error || "could not read the open document");
    return result.text;
  }

  async function fetchBibContent(preferredName) {
    const name = (preferredName || "").trim() || findBibFilenameInFileTree() || "refs.bib";
    const fetchResult = await fetchFileByName(name);
    if (fetchResult.error || !fetchResult.read) {
      throw new Error(fetchResult.error || `could not read "${name}"`);
    }
    // Refuse to proceed if the document we switched away from didn't come
    // back byte-identical -- see fetchFileByName's integrity check. Acting
    // on (reading, generating from, or inserting into) a document that may
    // have silently lost content is worse than stopping and asking the
    // user to check it, even though we can't tell from here whether this
    // is our bug or a legitimate concurrent edit by a collaborator.
    const ic = fetchResult.integrityCheck;
    if (ic && ic.matches === false) {
      throw new Error(
        `document changed while fetching "${name}" -- before: ${ic.preSwitchSnapshot.length} chars ` +
        `(hash ${ic.preSwitchSnapshot.hash}), after restoring: ${ic.postSnapshot ? ic.postSnapshot.length : "?"} chars ` +
        `(hash ${ic.postSnapshot ? ic.postSnapshot.hash : "?"}). Refusing to continue -- reload the page and check ` +
        `your document (Overleaf's history) before retrying.`
      );
    }
    return { name, content: fetchResult.read.text };
  }

  // Health of the OPTIONAL local Python service (dev/debug + cross-
  // validation tool only — see background.js's header comment). Not used
  // by the default Draft/Check path any more; kept for the debug panel.
  async function checkServiceHealth() {
    const { result, error } = await sendToBackground("SCRIBE_HEALTH", {});
    if (error || !result) return { ok: false, has_key: false };
    return result;
  }

  // Readiness of the in-extension JS pipeline — i.e. is an API key saved
  // for whichever provider Settings has configured for generation. This is
  // what decides offline-degrade vs. full Draft/Check now, replacing the
  // old "is the local service reachable" check.
  async function checkJsReady() {
    const { result, error } = await sendToBackground("SCRIBE_JS_READY", {});
    if (error || !result) return { ok: false, reason: error || "unknown" };
    return result;
  }

  // A large bibliography (many uncached references, many citation-span
  // batches) can legitimately take well past any fixed overall timeout --
  // the work is sequential and scales with citation count, not with wall
  // clock time. So instead of one fixed deadline for the whole job, this
  // times out on *stalls*: the clock only resets while progress.done (or
  // progress.stage) is still advancing, and fires if nothing moves for
  // stallMs. A stuck job still gets caught; a big-but-healthy one doesn't.
  // stallMs must comfortably exceed the longest gap between progress ticks
  // that a single *healthy* unit of work can produce: a judge batch retries
  // up to twice at 180s each plus a 10s delay (~370s) before its progress
  // callback fires, and a generation attempt can run up to 300s before its
  // retry re-fires progress. 8 minutes clears both with margin.
  // background.js reports this exact prefix when the service worker
  // restarted mid-job and lost its in-memory job store (jobs are kept in a
  // plain Map, not persisted — a restart is indistinguishable from the job
  // never having existed). That's a real possibility on a long-running
  // job even with the keepalive port, since Chrome can still recycle a
  // worker (extension update, browser resource pressure, manual reload).
  const UNKNOWN_JOB_PREFIX = "unknown job id:";

  async function pollJob(jobId, onProgress, { intervalMs = 1500, stallMs = 8 * 60 * 1000 } = {}) {
    let deadline = Date.now() + stallMs;
    let lastKey = null;
    while (Date.now() < deadline) {
      const { result, error } = await sendToBackground("SCRIBE_POLL_JOB", { jobId });
      if (error) {
        if (error.startsWith(UNKNOWN_JOB_PREFIX)) {
          // Same underlying situation as the stall-timeout below, just
          // discovered a different way — give the same reassuring,
          // accurate message instead of the raw internal error string.
          throw new Error(
            "the background worker restarted while this job was running. Enrichment caches each " +
            "reference as it resolves, so running Check again will pick up where this left off " +
            "instead of starting over."
          );
        }
        throw new Error(error);
      }
      if (onProgress) onProgress(result.progress);
      if (result.status === "done") return result.result;
      if (result.status === "error") throw new Error(result.error || "job failed");
      const p = result.progress || {};
      const key = `${p.stage || ""}:${p.done ?? ""}`;
      if (key !== lastKey) {
        lastKey = key;
        deadline = Date.now() + stallMs;
      }
      await sleep(intervalMs);
    }
    // The job itself is detached (background.js fires the runner and
    // doesn't await it), so it keeps going and keeps writing resolved
    // entries into the IndexedDB enrichment cache. Re-running therefore
    // resumes rather than restarting — worth saying, because the bare
    // message reads as though the work was lost.
    throw new Error(
      `no progress for ${Math.round(stallMs / 60000)} min -- the job appears stuck. Enrichment keeps ` +
      "running in the background and caches each reference as it resolves, so running " +
      "Check again will pick up where this left off instead of starting over."
    );
  }

  // Opens a runtime port for the job's duration and pings it periodically.
  // A connected port does NOT by itself stop Chrome idling the service
  // worker out mid-job — it's traffic ON the port that resets the idle
  // timer, so simply holding the connection open (as this used to do) was
  // a no-op after the initial connect event. Not a job payload channel;
  // SCRIBE_START_JOB/SCRIBE_POLL_JOB still carry the actual data over
  // sendMessage as before.
  const KEEPALIVE_PING_MS = 15000;

  async function startAndAwaitJob(path, body, onProgress) {
    const keepAlive = chrome.runtime.connect({ name: "scribe-job" });
    const pingTimer = setInterval(() => {
      try {
        keepAlive.postMessage({ type: "ping" });
      } catch {
        // Port already disconnected (worker restarted) — pollJob's own
        // unknown-job-id handling covers this; nothing to do here.
      }
    }, KEEPALIVE_PING_MS);
    try {
      const { result: started, error: startErr } = await sendToBackground("SCRIBE_START_JOB", { path, body });
      if (startErr) throw new Error(startErr);
      return await pollJob(started.job_id, onProgress);
    } finally {
      clearInterval(pingTimer);
      keepAlive.disconnect();
    }
  }

  // Full Tier1 + Tier4 check via the in-extension JS pipeline when a
  // provider key is configured; degrades to the offline JS key check
  // (lib/citations.js) when it isn't — this is the hybrid split's actual
  // point, not just an error message.
  // `source` records whether the checked text was the live document or a
  // not-yet-accepted draft, so click-to-reveal can explain itself rather
  // than reporting a bare "not found" for text that legitimately isn't in
  // the document yet.
  async function runCheck(text, bibContent, onProgress, source = "document") {
    const ready = await checkJsReady();
    if (!ready.ok) {
      const offline = window.ScribeCitations.checkCitations(text, bibContent);
      return { mode: "offline", tier1: offline, offlineReason: ready.reason, source };
    }
    const result = await startAndAwaitJob("/jobs/check", { text, bib_content: bibContent }, onProgress);
    return {
      mode: "full",
      tier1: result.tier_1_citation_metrics,
      tier4: result.tier_4_evidence_support,
      retrievalKeys: result.retrieval_keys,
      source,
    };
  }

  // ---- Panel ----------------------------------------------------------

  const state = {
    bibFilename: "",
    bibContent: null,
    generatedText: null,
    checkResult: null,
    busy: false,
  };

  // One-time visual design system: CSS variables + classes, injected as a
  // real <style> tag so buttons/inputs can have :hover and :disabled states
  // (impossible with inline style.cssText alone). Every element below uses
  // these classes instead of hand-rolled cssText so the panel reads as one
  // consistent surface rather than a pile of ad-hoc inline styles.
  function injectStyles() {
    if (document.getElementById("scribe-styles")) return;
    const style = document.createElement("style");
    style.id = "scribe-styles";
    style.textContent = `
      #scribe-panel {
        --scribe-bg: #1c1e22;
        --scribe-bg-raised: #24262b;
        --scribe-bg-sunken: #17181b;
        --scribe-border: #34363c;
        --scribe-text: #e8e9ec;
        --scribe-text-dim: #9a9da3;
        --scribe-text-faint: #6c6f76;
        --scribe-accent: #5b8cff;
        --scribe-accent-hover: #7aa0ff;
        --scribe-danger: #e5534b;
        --scribe-warn: #d4a72c;
        --scribe-success: #3fb950;
        --scribe-radius: 8px;
        position: fixed; bottom: 14px; right: 14px; z-index: 2147483647;
        width: 420px; max-height: 82vh;
        background: var(--scribe-bg); color: var(--scribe-text);
        font: 12px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        border: 1px solid var(--scribe-border); border-radius: var(--scribe-radius);
        box-shadow: 0 8px 30px rgba(0,0,0,.45), 0 1px 3px rgba(0,0,0,.3);
        display: flex; flex-direction: column; overflow: hidden;
      }
      #scribe-panel .scribe-header {
        display: flex; align-items: center; gap: 8px;
        padding: 10px 12px; cursor: grab; user-select: none;
        background: var(--scribe-bg-raised); border-bottom: 1px solid var(--scribe-border);
        flex-shrink: 0;
      }
      #scribe-panel .scribe-header:active { cursor: grabbing; }
      #scribe-panel .scribe-title { font-weight: 600; font-size: 13px; flex: 1; }
      #scribe-panel .scribe-dot {
        width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
        background: var(--scribe-text-faint); transition: background .15s;
      }
      #scribe-panel .scribe-dot.busy { background: var(--scribe-warn); animation: scribe-pulse 1s ease-in-out infinite; }
      #scribe-panel .scribe-dot.ok { background: var(--scribe-success); }
      #scribe-panel .scribe-dot.err { background: var(--scribe-danger); }
      @keyframes scribe-pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
      #scribe-panel .scribe-iconbtn {
        cursor: pointer; color: var(--scribe-text-dim); background: none; border: none;
        font-size: 13px; line-height: 1; padding: 4px 6px; border-radius: 4px;
      }
      #scribe-panel .scribe-iconbtn:hover { background: rgba(255,255,255,.08); color: var(--scribe-text); }
      #scribe-panel .scribe-iconbtn-lg { font-size: 18px; padding: 3px 7px; }
      #scribe-panel .scribe-info {
        display: inline-flex; align-items: center; justify-content: center;
        width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0;
        border: 1px solid var(--scribe-text-faint); color: var(--scribe-text-faint);
        font-size: 10px; font-style: italic; font-family: Georgia, serif;
        cursor: help; user-select: none;
      }
      #scribe-panel .scribe-info:hover { border-color: var(--scribe-text-dim); color: var(--scribe-text-dim); }
      #scribe-panel .scribe-tooltip-wrap { position: relative; display: inline-flex; }
      #scribe-panel .scribe-tooltip-box {
        display: none; position: absolute; z-index: 20; top: 20px; left: 0;
        width: 230px; background: var(--scribe-bg-raised); color: var(--scribe-text);
        border: 1px solid var(--scribe-border); border-radius: 6px; padding: 8px;
        font-size: 11px; line-height: 1.5; box-shadow: 0 6px 20px rgba(0,0,0,.45);
        cursor: default;
      }
      #scribe-panel .scribe-tooltip-box.open { display: block; }
      #scribe-panel .scribe-finding { cursor: pointer; transition: background .12s, border-color .12s; }
      #scribe-panel .scribe-finding:hover { background: #202227; }
      #scribe-panel .scribe-finding .scribe-jump { opacity: 0; transition: opacity .12s; }
      #scribe-panel .scribe-finding:hover .scribe-jump { opacity: 1; }
      #scribe-panel .scribe-body { padding: 12px; overflow: auto; }
      #scribe-panel .scribe-row { display: flex; gap: 6px; align-items: center; margin-bottom: 10px; }
      #scribe-panel .scribe-label { flex-shrink: 0; color: var(--scribe-text-dim); font-size: 11px; }
      #scribe-panel input.scribe-input {
        flex: 1; font: 11px/1.4 ui-monospace, monospace; padding: 6px 8px;
        background: var(--scribe-bg-sunken); color: var(--scribe-text);
        border: 1px solid var(--scribe-border); border-radius: 5px; outline: none;
      }
      #scribe-panel input.scribe-input:focus { border-color: var(--scribe-accent); }
      #scribe-panel .scribe-btn {
        cursor: pointer; padding: 7px 12px; font: 600 11.5px/1 -apple-system, sans-serif;
        border-radius: 6px; border: 1px solid var(--scribe-border);
        background: var(--scribe-bg-raised); color: var(--scribe-text);
        transition: background .12s, border-color .12s, opacity .12s;
      }
      #scribe-panel .scribe-btn:hover:not(:disabled) { background: #2c2f36; border-color: #45484f; }
      #scribe-panel .scribe-btn:disabled { opacity: .5; cursor: default; }
      #scribe-panel .scribe-btn-primary {
        background: var(--scribe-accent); border-color: var(--scribe-accent); color: #0d1117;
      }
      #scribe-panel .scribe-btn-primary:hover:not(:disabled) { background: var(--scribe-accent-hover); }
      #scribe-panel .scribe-btn-danger:hover:not(:disabled) { border-color: var(--scribe-danger); color: var(--scribe-danger); }
      #scribe-panel .scribe-actions { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
      #scribe-panel .scribe-status {
        color: var(--scribe-text-dim); margin-bottom: 4px; min-height: 14px;
        display: flex; align-items: center; gap: 6px;
      }
      #scribe-panel .scribe-spinner {
        width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
        border: 2px solid var(--scribe-border); border-top-color: var(--scribe-accent);
        animation: scribe-spin .7s linear infinite; display: none;
      }
      #scribe-panel .scribe-spinner.on { display: inline-block; }
      @keyframes scribe-spin { to { transform: rotate(360deg); } }
      #scribe-panel .scribe-heading { font-weight: 600; font-size: 11.5px; color: var(--scribe-text-dim); text-transform: uppercase; letter-spacing: .04em; margin: 4px 0 6px; }
      #scribe-panel .scribe-draft-box {
        white-space: pre-wrap; word-break: break-word; max-height: 180px; overflow: auto;
        background: var(--scribe-bg-sunken); border: 1px solid var(--scribe-border);
        border-radius: 6px; padding: 8px; margin: 4px 0 10px; font: 11px/1.5 ui-monospace, monospace;
      }
      #scribe-panel .scribe-empty {
        color: var(--scribe-text-faint); text-align: center; padding: 18px 8px; font-size: 11.5px;
      }
      #scribe-panel .scribe-toggle {
        color: var(--scribe-text-faint); cursor: pointer; font-size: 11px;
        padding: 8px 12px; border-top: 1px solid var(--scribe-border); flex-shrink: 0;
      }
      #scribe-panel .scribe-toggle:hover { color: var(--scribe-text-dim); }
    `;
    document.head.appendChild(style);
  }

  function makeDraggable(panel, handle) {
    let dragging = false, startX = 0, startY = 0, startRight = 0, startBottom = 0;
    handle.addEventListener("mousedown", (e) => {
      if (e.target.closest(".scribe-iconbtn")) return; // don't drag when clicking the collapse button
      dragging = true;
      startX = e.clientX;
      startY = e.clientY;
      const rect = panel.getBoundingClientRect();
      startRight = window.innerWidth - rect.right;
      startBottom = window.innerHeight - rect.bottom;
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      panel.style.right = `${startRight - (e.clientX - startX)}px`;
      panel.style.bottom = `${startBottom - (e.clientY - startY)}px`;
    });
    window.addEventListener("mouseup", () => { dragging = false; });
  }

  function buildPanel() {
    injectStyles();

    const panel = document.createElement("div");
    panel.id = "scribe-panel";

    // -- Header: title, connection dot, collapse toggle, drag handle --
    const header = document.createElement("div");
    header.className = "scribe-header";
    const statusDot = document.createElement("span");
    statusDot.className = "scribe-dot";
    const title = document.createElement("div");
    title.className = "scribe-title";
    title.textContent = "Scribe";
    const settingsBtn = document.createElement("button");
    settingsBtn.className = "scribe-iconbtn scribe-iconbtn-lg";
    settingsBtn.textContent = "⚙";
    settingsBtn.title = "Settings";
    const collapseBtn = document.createElement("button");
    collapseBtn.className = "scribe-iconbtn";
    collapseBtn.textContent = "–";
    collapseBtn.title = "Collapse";
    header.appendChild(statusDot);
    header.appendChild(title);
    header.appendChild(settingsBtn);
    header.appendChild(collapseBtn);
    panel.appendChild(header);
    makeDraggable(panel, header);

    // -- Body (everything collapse toggles) --
    const body = document.createElement("div");
    body.className = "scribe-body";

    // -- Bib filename row --
    const bibRow = document.createElement("div");
    bibRow.className = "scribe-row";
    const bibLabel = document.createElement("span");
    bibLabel.className = "scribe-label";
    bibLabel.textContent = "Bib file";
    const bibInput = document.createElement("input");
    bibInput.type = "text";
    bibInput.className = "scribe-input";
    bibInput.placeholder = "auto-detect";
    bibRow.appendChild(bibLabel);
    bibRow.appendChild(bibInput);
    body.appendChild(bibRow);

    // -- Target length row --
    // Python's batch pipeline computes this automatically from a
    // ground-truth Related Work section it deliberately stripped out
    // before generation (generate_related_work.py: target_word_count =
    // len(ground_truth.split())) — that's a benchmarking artifact with no
    // equivalent here (a live Overleaf doc isn't guaranteed to already
    // have a Related Work section to measure, and this isn't a strip-and-
    // replace tool). So instead of auto-computing it, the user just types
    // the number they want; buildGenerationPrompt's length-guideline
    // instruction (genPrompts.js) is already wired to use it, it was just
    // never being supplied from anywhere.
    const wordCountRow = document.createElement("div");
    wordCountRow.className = "scribe-row";
    const wordCountLabel = document.createElement("span");
    wordCountLabel.className = "scribe-label";
    wordCountLabel.textContent = "Target length (words)";
    const wordCountInput = document.createElement("input");
    wordCountInput.type = "number";
    wordCountInput.min = "1";
    wordCountInput.step = "1";
    wordCountInput.className = "scribe-input";
    wordCountInput.placeholder = "optional";
    wordCountRow.appendChild(wordCountLabel);
    wordCountRow.appendChild(wordCountInput);
    body.appendChild(wordCountRow);

    // -- Primary actions --
    const actionRow = document.createElement("div");
    actionRow.className = "scribe-actions";
    const draftBtn = makeButton("Draft Related Work", onDraft, { primary: true });
    const checkBtn = makeButton("Check citations", onCheck);
    actionRow.appendChild(draftBtn);
    actionRow.appendChild(checkBtn);
    body.appendChild(actionRow);

    // -- Status line --
    const statusLine = document.createElement("div");
    statusLine.className = "scribe-status";
    const spinner = document.createElement("span");
    spinner.className = "scribe-spinner";
    const statusText = document.createElement("span");
    statusLine.appendChild(spinner);
    statusLine.appendChild(statusText);
    body.appendChild(statusLine);

    // -- Settings panel (hidden until the gear icon is clicked) --
    const settingsPanel = document.createElement("div");
    settingsPanel.style.cssText = "display:none;margin-bottom:10px;";
    const settingsController = buildSettingsPanel(settingsPanel);
    body.appendChild(settingsPanel);
    settingsBtn.addEventListener("click", () => {
      const showing = settingsPanel.style.display !== "none";
      settingsPanel.style.display = showing ? "none" : "block";
      if (!showing) settingsController.refresh();
    });

    // -- Results area (starts with an empty-state hint) --
    const resultsArea = document.createElement("div");
    const emptyHint = document.createElement("div");
    emptyHint.className = "scribe-empty";
    emptyHint.textContent = "No draft yet — click “Draft Related Work” to generate one, or “Check citations” to verify what's already in the document.";
    resultsArea.appendChild(emptyHint);
    body.appendChild(resultsArea);

    // -- Debug toggle + tools (collapsed by default) --
    const debugToggle = document.createElement("div");
    debugToggle.className = "scribe-toggle";
    debugToggle.textContent = "▸ debug tools";
    const debugPanel = document.createElement("div");
    debugPanel.style.cssText = "display:none;margin-top:8px;";
    debugToggle.addEventListener("click", () => {
      const showing = debugPanel.style.display !== "none";
      debugPanel.style.display = showing ? "none" : "block";
      debugToggle.textContent = showing ? "▸ debug tools" : "▾ debug tools";
    });
    buildDebugTools(debugPanel);
    body.appendChild(debugToggle);
    body.appendChild(debugPanel);

    panel.appendChild(body);

    collapseBtn.addEventListener("click", () => {
      const collapsed = body.style.display === "none";
      body.style.display = collapsed ? "block" : "none";
      collapseBtn.textContent = collapsed ? "–" : "+";
      collapseBtn.title = collapsed ? "Collapse" : "Expand";
    });

    document.documentElement.appendChild(panel);

    // ---- action handlers (closures over the DOM refs above) ----

    panelSetStatus = setStatus;

    function setStatus(text) {
      statusText.textContent = window.ScribeSecrets.redactSecrets(text);
    }

    function setBusy(busy) {
      state.busy = busy;
      draftBtn.disabled = busy;
      checkBtn.disabled = busy;
      spinner.classList.toggle("on", busy);
      statusDot.classList.toggle("busy", busy);
      if (busy) statusDot.classList.remove("ok", "err");
    }

    function renderResults() {
      resultsArea.innerHTML = "";

      if (!state.generatedText && !state.checkResult) {
        const empty = document.createElement("div");
        empty.className = "scribe-empty";
        empty.textContent = "No draft yet — click “Draft Related Work” to generate one, or “Check citations” to verify what's already in the document.";
        resultsArea.appendChild(empty);
        return;
      }

      if (state.generatedText) {
        const heading = document.createElement("div");
        heading.className = "scribe-heading";
        heading.textContent = "Draft";
        resultsArea.appendChild(heading);

        const pre = document.createElement("pre");
        pre.textContent = state.generatedText;
        pre.className = "scribe-draft-box";
        resultsArea.appendChild(pre);

        const btnRow = document.createElement("div");
        btnRow.className = "scribe-actions";
        btnRow.appendChild(makeButton("Accept (insert)", onAccept, { primary: true }));
        btnRow.appendChild(makeButton("Regenerate", onDraft));
        btnRow.appendChild(makeButton("Discard", onDiscard, { danger: true }));
        resultsArea.appendChild(btnRow);
      }

      if (state.checkResult) renderFindings(resultsArea, state.checkResult);
    }

    async function ensureBib() {
      if (state.bibContent) return state.bibContent;
      setStatus("locating bibliography...");
      const { name, content } = await fetchBibContent(bibInput.value);
      bibInput.value = name;
      state.bibFilename = name;
      state.bibContent = content;
      return content;
    }

    async function onDraft() {
      if (state.busy) return;
      setBusy(true);
      try {
        setStatus("reading current document...");
        const tex = await getCurrentDocText();
        const bib = await ensureBib();

        setStatus("checking provider configuration...");
        const ready = await checkJsReady();
        if (!ready.ok) {
          setStatus(`drafting needs a provider key — ${ready.reason || "open Settings to add one"}.`);
          setBusy(false);
          return;
        }

        setStatus("generating (this can take a bit)...");
        const targetWordCount = parseInt(wordCountInput.value, 10);
        const genBody = { tex_source: tex, bib_content: bib };
        if (Number.isFinite(targetWordCount) && targetWordCount > 0) {
          genBody.target_word_count = targetWordCount;
        }
        const genResult = await startAndAwaitJob(
          "/jobs/generate",
          genBody,
          (progress) => setStatus(`generating: ${progress.stage} (${progress.done}/${progress.total})`)
        );
        state.generatedText = genResult.latex_output || genResult.markdown_output || genResult.raw_output;

        setStatus("checking citations in the draft...");
        state.checkResult = await runCheck(
          state.generatedText,
          bib,
          (progress) => setStatus(`checking: ${progress.stage} (${progress.done}/${progress.total})`),
          "draft"
        );

        setStatus("done.");
        statusDot.classList.add("ok");
        renderResults();
      } catch (err) {
        setStatus(`error: ${err.message || err}`);
        statusDot.classList.add("err");
      } finally {
        setBusy(false);
      }
    }

    async function onCheck() {
      if (state.busy) return;
      setBusy(true);
      try {
        setStatus("reading current document...");
        const tex = await getCurrentDocText();
        const bib = await ensureBib();

        setStatus("checking citations...");
        state.generatedText = null; // this is a check-only run, not a draft
        state.checkResult = await runCheck(
          tex,
          bib,
          (progress) => setStatus(`checking: ${progress.stage} (${progress.done}/${progress.total})`)
        );

        setStatus("done.");
        statusDot.classList.add("ok");
        renderResults();
      } catch (err) {
        setStatus(`error: ${err.message || err}`);
        statusDot.classList.add("err");
      } finally {
        setBusy(false);
      }
    }

    async function onAccept() {
      if (!state.generatedText) return;
      setStatus("inserting into document...");
      const { result, error } = await callInject("SCRIBE_INSERT", { text: state.generatedText });
      if (error) {
        setStatus(`insert failed: ${error}`);
        statusDot.classList.add("err");
        return;
      }
      statusDot.classList.remove("err");
      statusDot.classList.add("ok");
      setStatus(`inserted (document now ${result.newLength} chars). Note: inserts at the cursor in whichever file is currently open.`);
    }

    function onDiscard() {
      state.generatedText = null;
      state.checkResult = null;
      setStatus("");
      renderResults();
    }

    function makeButton(label, onClick, opts) {
      const b = document.createElement("button");
      b.textContent = label;
      b.className = "scribe-btn" + (opts && opts.primary ? " scribe-btn-primary" : "") + (opts && opts.danger ? " scribe-btn-danger" : "");
      b.addEventListener("click", onClick);
      return b;
    }
  }

  // Every finding — a hallucinated key or a Tier 4 verdict — renders through
  // this one shape so the list reads as one consistent thing instead of two
  // differently-laid-out sections. label/color come first (what kind of
  // problem, how severe), then the citation key, then whatever detail is
  // available (sentence, justification) in the same place every time.
  const VERDICT_META = {
    hallucinated: { label: "NOT IN BIB", color: "#c62828" },
    contradicted: { label: "CONTRADICTED", color: "#c62828" },
    unsupported: { label: "UNSUPPORTED", color: "#c62828" },
    partially_supported: { label: "PARTIAL", color: "#b8860b" },
    supported: { label: "SUPPORTED", color: "#2e7d32" },
    unverifiable: { label: "UNVERIFIABLE", color: "#666" },
  };

  // pipeline/evaluator.py's verdict_source for unverifiable rows — human
  // wording for each, so the expanded list says *why* rather than just
  // repeating "unverifiable" with no further detail.
  const UNVERIFIABLE_REASONS = {
    key_not_in_bib: "citation key not found in the bibliography",
    low_confidence_match: "matched a paper, but the match was too shaky to trust — couldn't confidently identify which paper this is",
    no_evidence: "no abstract or full text was retrieved for the cited paper",
    judge_omitted_from_response: "the judge's response silently skipped this citation",
  };
  // "no_evidence" (and low_confidence_match, if the match itself was
  // inconclusive) can mean either "this paper genuinely has nothing
  // retrievable" or "a provider request was rate-limited/errored and we
  // never got a real answer" — those read very differently, and only the
  // second is fixed by adding an API key or just re-running Check.
  function unverifiableReason(source, inconclusive) {
    if (inconclusive && (source === "no_evidence" || source === "low_confidence_match")) {
      return "a retrieval request was rate-limited or failed rather than confirming this paper has no evidence — try Check again, or add an API key in Settings";
    }
    return UNVERIFIABLE_REASONS[source] || "not enough retrieved evidence to check this claim";
  }

  function badge(text, color) {
    const el = document.createElement("span");
    el.textContent = text;
    el.style.cssText = `display:inline-block;padding:1px 6px;border-radius:3px;background:${color};color:#fff;font-size:10px;font-weight:600;letter-spacing:0.02em;flex-shrink:0;`;
    return el;
  }

  // Set by buildPanel so module-scope helpers (revealFinding) can report
  // into the panel's status line, which is already redaction-wrapped.
  let panelSetStatus = () => {};

  // ---- Click a finding -> select it in the editor -------------------------
  //
  // Locates by SEARCHING the live document rather than trusting the
  // char offsets the checker produced. Those offsets are relative to the
  // text that was checked, which is not the live document when: the check
  // ran against a generated draft that hasn't been accepted yet; the user
  // edited the file since checking; or the check ran through the Python
  // service (whose rows historically carried no offsets at all). Searching
  // handles all three uniformly. The offsets, when present, are used only
  // to disambiguate between repeated occurrences.

  function escapeRegExp(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // Whitespace-tolerant: the checked text and the live document can differ
  // in line wrapping, which would defeat a literal indexOf.
  function sentenceRegex(sentence) {
    const words = sentence.trim().split(/\s+/).filter(Boolean).map(escapeRegExp);
    if (words.length === 0) return null;
    return new RegExp(words.join("\\s+"), "g");
  }

  function allMatches(re, text) {
    const out = [];
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      out.push({ from: m.index, to: m.index + m[0].length });
      if (m[0].length === 0) re.lastIndex++;
    }
    return out;
  }

  function pickMatch(matches, hint) {
    if (matches.length === 1 || hint == null) return matches[0];
    let best = matches[0];
    let bestDist = Math.abs(matches[0].from - hint);
    for (const m of matches) {
      const d = Math.abs(m.from - hint);
      if (d < bestDist) { best = m; bestDist = d; }
    }
    return best;
  }

  // Fallback when the sentence can't be found: locate the \cite-family
  // command carrying this key. Mirrors lib/citations.js's extractCitedKeys
  // pattern, narrowed to a specific key.
  function citeCommandMatch(docText, key) {
    const re = new RegExp(
      "\\\\cite[a-zA-Z]*\\*?(?:\\[[^\\]]*\\])*\\{[^}]*\\b" + escapeRegExp(key) + "\\b[^}]*\\}",
      "g"
    );
    return allMatches(re, docText);
  }

  async function revealFinding(data) {
    const report = panelSetStatus;
    try {
      const docText = await getCurrentDocText();

      let matches = [];
      if (data.sentence) {
        const re = sentenceRegex(data.sentence);
        if (re) matches = allMatches(re, docText);
      }
      let how = "sentence";

      if (matches.length === 0 && data.key) {
        matches = citeCommandMatch(docText, data.key);
        how = "citation key";
      }

      if (matches.length === 0) {
        report(data.fromDraft
          ? "that sentence is from the draft — Accept it first, then click again."
          : "couldn't find that text in the open document (edited, or a different file is open).");
        return;
      }

      const target = pickMatch(matches, data.charStart);
      const { error } = await callInject("SCRIBE_REVEAL", { from: target.from, to: target.to });
      if (error) {
        report(`could not jump: ${error}`);
        return;
      }
      const extra = matches.length > 1 ? ` (${matches.length} occurrences, jumped to nearest)` : "";
      report(`jumped to ${data.key} via ${how}${extra}.`);
    } catch (err) {
      report(`could not jump: ${err.message || err}`);
    }
  }

  function findingRow(data) {
    const { verdict, key, extra, sentence, justification } = data;
    const meta = VERDICT_META[verdict] || { label: verdict, color: "#666" };
    const row = document.createElement("div");
    row.className = "scribe-finding";
    row.style.cssText = `border-left:3px solid ${meta.color};background:var(--scribe-bg-sunken);border-radius:5px;padding:7px 9px;margin-bottom:6px;`;
    row.title = "Click to jump to this citation in the editor";
    // Selecting the sentence text to copy it shouldn't also fire a jump.
    row.addEventListener("click", () => {
      if (String(window.getSelection())) return;
      revealFinding(data);
    });

    const head = document.createElement("div");
    head.style.cssText = "display:flex;align-items:center;gap:6px;";
    head.appendChild(badge(meta.label, meta.color));
    const keyEl = document.createElement("span");
    keyEl.textContent = key;
    keyEl.style.cssText = "font-weight:600;";
    head.appendChild(keyEl);
    if (extra) {
      const extraEl = document.createElement("span");
      extraEl.textContent = extra;
      extraEl.style.cssText = "color:#888;font-size:11px;";
      head.appendChild(extraEl);
    }
    const jump = document.createElement("span");
    jump.textContent = "↗";
    jump.className = "scribe-jump";
    jump.style.cssText = "margin-left:auto;color:var(--scribe-text-faint);font-size:12px;flex-shrink:0;";
    head.appendChild(jump);
    row.appendChild(head);

    if (sentence) {
      const s = document.createElement("div");
      s.style.cssText = "color:#bbb;font-size:11px;font-style:italic;margin-top:3px;";
      s.textContent = sentence.length > 220 ? sentence.slice(0, 220) + "…" : sentence;
      row.appendChild(s);
    }
    if (justification) {
      const j = document.createElement("div");
      j.style.cssText = "color:#999;font-size:11px;margin-top:2px;";
      j.textContent = justification;
      row.appendChild(j);
    }
    return row;
  }

  function renderFindings(container, checkResult) {
    const heading = document.createElement("div");
    heading.textContent = checkResult.mode === "offline"
      ? "Citation keys (offline check)"
      : "Findings";
    heading.className = "scribe-heading";
    container.appendChild(heading);

    const tier1 = checkResult.tier1;
    const tier4 = checkResult.mode !== "offline" ? checkResult.tier4 : null;
    // Findings from a Draft describe text that isn't in the document
    // until Accept, so click-to-reveal can say so instead of just
    // reporting "not found".
    const fromDraft = checkResult.source === "draft";

    // One combined, consistently-shaped list: hallucinated keys (a citation
    // that doesn't exist at all — the most severe possible finding) sort
    // first, then Tier 4 verdicts worst-first. "unverifiable" mentions carry
    // no judgment about the claim (see pipeline/evaluator.py's docstring:
    // it means retrieval couldn't find enough evidence, not that the claim
    // is false) — collapsed into one summary line instead of listed, so the
    // informative rows aren't buried under noise.
    const rows = [];
    const unverifiableRows = [];
    for (const key of tier1.hallucinated_keys || []) {
      rows.push({
        verdict: "hallucinated",
        key,
        justification: "Cited in the text but this key doesn't exist in the bibliography — likely a fabricated reference.",
        sortRank: 0,
        fromDraft,
      });
    }
    if (tier4 && tier4.per_citation) {
      const order = { contradicted: 1, unsupported: 2, partially_supported: 3, supported: 4 };
      for (const c of tier4.per_citation) {
        const ordinal = (c.citation_id || "").split("#")[1];
        if (c.verdict === "unverifiable") {
          unverifiableRows.push({
            verdict: "unverifiable",
            key: c.citation_key,
            extra: ordinal ? `mention ${ordinal}` : null,
            sentence: c.sentence,
            justification: `Not checked: ${unverifiableReason(c.verdict_source, c.evidence_inconclusive)}.`,
            charStart: c.char_start,
            fromDraft,
          });
          continue;
        }
        rows.push({
          verdict: c.verdict,
          key: c.citation_key,
          extra: ordinal ? `mention ${ordinal}` : null,
          sentence: c.sentence,
          justification: c.justification,
          sortRank: order[c.verdict] ?? 5,
          charStart: c.char_start,
          fromDraft,
        });
      }
    }
    rows.sort((a, b) => a.sortRank - b.sortRank);

    // The full list — every judged citation plus every unverifiable one —
    // is always reachable, whether that's because something's actually
    // wrong (rows non-empty, shown by default) or because everything
    // checked out clean (rows empty, collapsed behind a toggle so the
    // panel isn't cluttered when there's nothing to act on).
    const totalChecked = rows.length + unverifiableRows.length;

    // Without retrieval API keys, most evidence lookups are silently
    // rate-limited rather than genuinely absent — surface that plainly
    // rather than letting a degraded run look like clean "no evidence"
    // findings for papers that do have abstracts available.
    const missingKeys = checkResult.retrievalKeys
      ? Object.entries(checkResult.retrievalKeys).filter(([, present]) => !present).map(([id]) => id)
      : [];
    if (missingKeys.length > 0) {
      const labels = { s2: "Semantic Scholar", openalex: "OpenAlex" };
      const warning = document.createElement("div");
      warning.style.cssText = "color:var(--scribe-warning,#b45309);background:var(--scribe-warning-bg,#fef3c7);border-radius:4px;padding:6px 8px;margin:4px 0 8px;font-size:11px;";
      warning.textContent = `No API key set for ${missingKeys.map((k) => labels[k] || k).join(" or ")} — expect many citations to show "no evidence" due to rate limiting, not because the paper actually lacks one. Add a free key in Settings.`;
      container.appendChild(warning);
    }

    if (tier4) {
      const summary = document.createElement("div");
      summary.style.cssText = "color:var(--scribe-text-dim);margin:4px 0 8px;";
      summary.textContent = `Evidence coverage: ${(tier4.evidence_coverage * 100).toFixed(0)}% · support rate (of checked): ${tier4.support_rate != null ? (tier4.support_rate * 100).toFixed(0) + "%" : "n/a"}`;
      container.appendChild(summary);
    }

    if (rows.length === 0) {
      const empty = document.createElement("div");
      empty.style.cssText = "color:var(--scribe-success);margin-top:4px;";
      empty.textContent = unverifiableRows.length > 0
        ? `No hallucinated citations, no evidence problems found among the checkable citations.`
        : "No hallucinated citations, no evidence problems found.";
      container.appendChild(empty);
    } else {
      const list = document.createElement("div");
      list.style.cssText = "max-height:260px;overflow:auto;margin-top:4px;";
      for (const r of rows) list.appendChild(findingRow(r));
      container.appendChild(list);
    }

    if (tier1.missing_keys && tier1.missing_keys.length > 0) {
      const row = document.createElement("div");
      row.style.cssText = "color:var(--scribe-text-faint);margin-top:6px;font-size:11px;";
      row.textContent = `In bib but never cited: ${tier1.missing_keys.join(", ")}`;
      container.appendChild(row);
    }

    if (checkResult.mode === "offline") {
      const note = document.createElement("div");
      note.style.cssText = "color:var(--scribe-text-dim);margin-top:6px;font-style:italic;";
      const reason = checkResult.offlineReason ? ` (${checkResult.offlineReason})` : "";
      note.textContent = `Evidence-support checking (does the cited paper actually support the claim?) is unavailable right now${reason} — open Settings, fix that, and re-check.`;
      container.appendChild(note);
      return;
    }

    // One toggle covers both asks: seeing every citation even when nothing
    // is wrong (expands rows.length===0's clean state into the full list),
    // and seeing what each "unverifiable" mention actually is instead of
    // just a bare count.
    if (totalChecked > 0) {
      const toggleWrap = document.createElement("div");
      toggleWrap.style.cssText = "margin-top:6px;";
      const toggle = document.createElement("div");
      toggle.style.cssText = "color:var(--scribe-accent);cursor:pointer;font-size:11px;";
      const expandedList = document.createElement("div");
      expandedList.style.cssText = "display:none;max-height:260px;overflow:auto;margin-top:6px;";

      const label = unverifiableRows.length > 0
        ? `Show all ${totalChecked} citation${totalChecked === 1 ? "" : "s"} checked (including ${unverifiableRows.length} unverifiable)`
        : `Show all ${totalChecked} citation${totalChecked === 1 ? "" : "s"} checked`;
      toggle.textContent = `▸ ${label}`;

      let built = false;
      toggle.addEventListener("click", () => {
        const showing = expandedList.style.display !== "none";
        if (!showing && !built) {
          // Rebuild every checked citation, worst-first, unverifiable last —
          // this is the literal "show everything" view, independent of the
          // default (problems-only-by-default) list above.
          const all = [...rows, ...unverifiableRows.map((r) => ({ ...r, sortRank: 6 }))];
          all.sort((a, b) => (a.sortRank ?? 6) - (b.sortRank ?? 6));
          for (const r of all) expandedList.appendChild(findingRow(r));
          built = true;
        }
        expandedList.style.display = showing ? "none" : "block";
        toggle.textContent = `${showing ? "▸" : "▾"} ${label}`;
      });

      toggleWrap.appendChild(toggle);
      toggleWrap.appendChild(expandedList);
      container.appendChild(toggleWrap);
    }
  }

  // ---- Settings: provider/key/model, retrieval keys -----------------------
  //
  // Keys never live in this script's variables any longer than it takes to
  // send them to background.js — content.js only ever holds what the user
  // just typed, for the length of one SCRIBE_SAVE_LLM_KEY/SCRIBE_TEST_KEY
  // round trip. Storage itself (session for keys, local for everything
  // else) lives entirely in background.js/lib/settingsStore.js.

  // A click-toggle tooltip — native `title` only shows on hover after a
  // delay, which is easy to miss and does nothing on click/tap. Closes on
  // an outside click or Escape.
  // API keys are visually masked like a password field, but deliberately
  // NOT input[type=password] — that's exactly what triggers Chrome's (and
  // other password managers') "save this login?" prompt, offering to file
  // an API key next to the nearest text field it guesses is a "username"
  // (e.g. the model-id field). An API key isn't a site login and has no
  // business in that vault — especially since Chrome's saved passwords can
  // sync to the user's Google account, which is exactly the disk/cloud
  // persistence this extension's key storage was deliberately designed to
  // avoid (chrome.storage.session, never chrome.storage.sync — see
  // lib/settingsStore.js). `-webkit-text-security` gives the same dot-
  // masked look without being a password field at all.
  let maskInputCounter = 0;
  function maskInput(input) {
    input.type = "text";
    input.style.webkitTextSecurity = "disc";
    input.autocomplete = "off";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("data-lpignore", "true");   // LastPass
    input.setAttribute("data-1p-ignore", "true");  // 1Password
    input.setAttribute("data-bwignore", "true");   // Bitwarden
    input.name = `scribe-secret-${maskInputCounter++}`;
  }

  function makeInfoIcon(text) {
    const wrap = document.createElement("span");
    wrap.className = "scribe-tooltip-wrap";
    const icon = document.createElement("span");
    icon.className = "scribe-info";
    icon.textContent = "i";
    const box = document.createElement("div");
    box.className = "scribe-tooltip-box";
    box.textContent = text;
    wrap.appendChild(icon);
    wrap.appendChild(box);

    function close() { box.classList.remove("open"); }
    icon.addEventListener("click", (e) => {
      e.stopPropagation();
      box.classList.toggle("open");
    });
    document.addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

    return wrap;
  }

  // Keys are stored per-provider as a named list (lib/settingsStore.js),
  // so switching providers and back never loses anything — but the key
  // *picker* still needs to reload for whichever provider is now
  // selected, otherwise it keeps showing the previous provider's keys.
  // Returns the change handler so callers (buildSettingsPanel's refresh)
  // can await it directly instead of relying on a dispatched event, which
  // doesn't block for async work.
  function providerField(providerSelect, modelList, baseUrlRow, keySection) {
    async function onProviderChange() {
      const meta = window.ScribeProviders.getProvider(providerSelect.value);
      baseUrlRow.style.display = providerSelect.value === "custom" ? "flex" : "none";
      modelList.innerHTML = "";
      for (const m of (meta && meta.presetModels) || []) {
        const opt = document.createElement("option");
        opt.value = m;
        modelList.appendChild(opt);
      }
      await keySection.refresh(providerSelect.value);
    }
    providerSelect.addEventListener("change", onProviderChange);
    onProviderChange();
    return onProviderChange;
  }

  // A provider can have several named keys saved at once (e.g. two
  // different OpenRouter keys, "Personal" / "Lab account") — this builds
  // the picker: a <select> of saved keys plus "+ Add new key", an inline
  // add-key form, delete, and test. Raw key values never round-trip back
  // out of background.js once saved — only id/nickname/masked value do.
  function buildKeySection(baseUrlInput, modelInput) {
    const wrap = document.createElement("div");

    const selectRow = document.createElement("div");
    selectRow.className = "scribe-row";
    const selectLabel = document.createElement("span");
    selectLabel.className = "scribe-label";
    selectLabel.textContent = "API key";
    const keySelect = document.createElement("select");
    keySelect.className = "scribe-input";
    const deleteBtn = document.createElement("button");
    deleteBtn.className = "scribe-btn scribe-btn-danger";
    deleteBtn.textContent = "Delete";
    deleteBtn.style.cssText = "font-size:10.5px;padding:6px 9px;flex-shrink:0;";
    selectRow.appendChild(selectLabel);
    selectRow.appendChild(keySelect);
    selectRow.appendChild(deleteBtn);
    wrap.appendChild(selectRow);

    const newKeyForm = document.createElement("div");
    newKeyForm.style.cssText = "display:none;margin-top:4px;padding-top:6px;border-top:1px dashed var(--scribe-border);";

    const nickRow = document.createElement("div");
    nickRow.className = "scribe-row";
    const nickLabel = document.createElement("span");
    nickLabel.className = "scribe-label";
    nickLabel.textContent = "Nickname";
    const nickInput = document.createElement("input");
    nickInput.type = "text";
    nickInput.className = "scribe-input";
    nickInput.placeholder = "e.g. Personal, Lab account";
    nickRow.appendChild(nickLabel);
    nickRow.appendChild(nickInput);
    newKeyForm.appendChild(nickRow);

    const newKeyRow = document.createElement("div");
    newKeyRow.className = "scribe-row";
    const newKeyLabel = document.createElement("span");
    newKeyLabel.className = "scribe-label";
    newKeyLabel.textContent = "Key value";
    const newKeyInput = document.createElement("input");
    maskInput(newKeyInput);
    newKeyInput.className = "scribe-input";
    newKeyInput.placeholder = "paste key";
    newKeyRow.appendChild(newKeyLabel);
    newKeyRow.appendChild(newKeyInput);
    newKeyForm.appendChild(newKeyRow);

    const saveNewBtn = document.createElement("button");
    saveNewBtn.className = "scribe-btn scribe-btn-primary";
    saveNewBtn.textContent = "Save key";
    saveNewBtn.style.cssText = "font-size:10.5px;padding:5px 9px;";
    newKeyForm.appendChild(saveNewBtn);
    wrap.appendChild(newKeyForm);

    const testRow = document.createElement("div");
    testRow.style.cssText = "display:flex;gap:6px;align-items:center;margin-top:6px;";
    const testBtn = document.createElement("button");
    testBtn.className = "scribe-btn";
    testBtn.textContent = "Test key";
    testBtn.style.cssText = "font-size:10.5px;padding:5px 9px;";
    const testResult = document.createElement("span");
    testResult.style.cssText = "font-size:11px;";
    testRow.appendChild(testBtn);
    testRow.appendChild(testResult);
    wrap.appendChild(testRow);

    let currentProvider = null;

    function syncVisibility() {
      const isNew = keySelect.value === "__new__";
      newKeyForm.style.display = isNew ? "block" : "none";
      deleteBtn.style.display = !isNew && keySelect.value ? "inline-block" : "none";
    }

    async function refresh(providerId) {
      currentProvider = providerId;
      nickInput.value = "";
      newKeyInput.value = "";
      testResult.textContent = "";
      const { result } = await sendToBackground("SCRIBE_LIST_LLM_KEYS", { providerId });
      const keys = result || [];
      keySelect.innerHTML = "";
      for (const k of keys) {
        const opt = document.createElement("option");
        opt.value = k.id;
        opt.textContent = `${k.nickname} (${k.maskedKey})`;
        keySelect.appendChild(opt);
      }
      const addOpt = document.createElement("option");
      addOpt.value = "__new__";
      addOpt.textContent = "+ Add new key";
      keySelect.appendChild(addOpt);
      keySelect.value = keys.length ? keys[0].id : "__new__";
      syncVisibility();
      return keys;
    }

    keySelect.addEventListener("change", () => { testResult.textContent = ""; syncVisibility(); });

    saveNewBtn.addEventListener("click", async () => {
      if (!newKeyInput.value) return;
      const { result } = await sendToBackground("SCRIBE_SAVE_LLM_KEY", {
        providerId: currentProvider, nickname: nickInput.value, apiKey: newKeyInput.value,
      });
      await refresh(currentProvider);
      if (result && result.keyId) keySelect.value = result.keyId;
      syncVisibility();
    });

    deleteBtn.addEventListener("click", async () => {
      if (!keySelect.value || keySelect.value === "__new__") return;
      await sendToBackground("SCRIBE_DELETE_LLM_KEY", { providerId: currentProvider, keyId: keySelect.value });
      await refresh(currentProvider);
    });

    testBtn.addEventListener("click", async () => {
      testResult.textContent = "testing...";
      testResult.style.color = "var(--scribe-text-dim)";
      const payload = { providerId: currentProvider, baseUrl: baseUrlInput.value, model: modelInput.value };
      if (keySelect.value === "__new__") {
        if (!newKeyInput.value) { testResult.textContent = "enter a key first"; testResult.style.color = "var(--scribe-danger)"; return; }
        payload.apiKey = newKeyInput.value;
      } else {
        if (!keySelect.value) { testResult.textContent = "no key selected"; testResult.style.color = "var(--scribe-danger)"; return; }
        payload.keyId = keySelect.value;
      }
      const { result, error } = await sendToBackground("SCRIBE_TEST_KEY", payload);
      if (error || !result || !result.ok) {
        testResult.textContent = window.ScribeSecrets.redactSecrets((result && result.error) || error || "test failed");
        testResult.style.color = "var(--scribe-danger)";
      } else {
        testResult.textContent = "key works";
        testResult.style.color = "var(--scribe-success)";
      }
    });

    return {
      wrap, keySelect, refresh, syncVisibility,
      getSelectedKeyId: () => (keySelect.value === "__new__" ? "" : keySelect.value),
      setSelectedKeyId: (id) => { if (id && [...keySelect.options].some((o) => o.value === id)) { keySelect.value = id; syncVisibility(); } },
    };
  }

  function buildProviderBlock({ label }) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "border:1px solid var(--scribe-border);border-radius:6px;padding:8px;margin-bottom:8px;";

    const heading = document.createElement("div");
    heading.className = "scribe-heading";
    heading.style.marginTop = "0";
    heading.textContent = label;
    wrap.appendChild(heading);

    const providerRow = document.createElement("div");
    providerRow.className = "scribe-row";
    const providerLabel = document.createElement("span");
    providerLabel.className = "scribe-label";
    providerLabel.textContent = "Provider";
    const providerSelect = document.createElement("select");
    providerSelect.className = "scribe-input";
    for (const p of window.ScribeProviders.PROVIDERS) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label;
      providerSelect.appendChild(opt);
    }
    providerRow.appendChild(providerLabel);
    providerRow.appendChild(providerSelect);
    wrap.appendChild(providerRow);

    const baseUrlRow = document.createElement("div");
    baseUrlRow.className = "scribe-row";
    const baseUrlLabel = document.createElement("span");
    baseUrlLabel.className = "scribe-label";
    baseUrlLabel.textContent = "Base URL";
    const baseUrlInput = document.createElement("input");
    baseUrlInput.type = "text";
    baseUrlInput.className = "scribe-input";
    baseUrlInput.placeholder = "https://your-endpoint/v1";
    baseUrlRow.appendChild(baseUrlLabel);
    baseUrlRow.appendChild(baseUrlInput);
    wrap.appendChild(baseUrlRow);

    const modelListId = `scribe-models-${Math.random().toString(36).slice(2)}`;
    const modelRow = document.createElement("div");
    modelRow.className = "scribe-row";
    const modelLabel = document.createElement("span");
    modelLabel.className = "scribe-label";
    modelLabel.textContent = "Model";
    const modelInput = document.createElement("input");
    modelInput.type = "text";
    modelInput.className = "scribe-input";
    modelInput.setAttribute("list", modelListId);
    modelInput.placeholder = "model id";
    const modelList = document.createElement("datalist");
    modelList.id = modelListId;
    modelRow.appendChild(modelLabel);
    modelRow.appendChild(modelInput);
    wrap.appendChild(modelRow);
    wrap.appendChild(modelList);

    const keySection = buildKeySection(baseUrlInput, modelInput);
    wrap.appendChild(keySection.wrap);

    const onProviderChange = providerField(providerSelect, modelList, baseUrlRow, keySection);

    return { wrap, providerSelect, baseUrlInput, modelInput, keySection, refreshForProvider: onProviderChange };
  }

  function buildSettingsPanel(container) {
    // Local-server mode: use the Python service (127.0.0.1:8765) instead
    // of the in-extension JS pipeline below. An explicit switch, not an
    // automatic fallback — flips what Draft/Check actually talk to.
    const localWrap = document.createElement("div");
    localWrap.style.cssText = "border:1px solid var(--scribe-border);border-radius:6px;padding:8px;margin-bottom:8px;";
    const localRow = document.createElement("div");
    localRow.style.cssText = "display:flex;align-items:center;gap:6px;font-size:11.5px;";
    const localCheckbox = document.createElement("input");
    localCheckbox.type = "checkbox";
    localCheckbox.id = "scribe-use-local-service";
    const localLabel = document.createElement("label");
    localLabel.htmlFor = "scribe-use-local-service";
    localLabel.textContent = "Use local Python server instead (advanced)";
    localRow.appendChild(localCheckbox);
    localRow.appendChild(localLabel);
    localWrap.appendChild(localRow);
    const localHelp = document.createElement("div");
    localHelp.style.cssText = "font-size:11px;color:var(--scribe-text-faint);margin-top:6px;";
    localHelp.innerHTML = 'Skips provider/key setup below — start the server yourself in a terminal, then Draft/Check will use it instead:<br><code style="background:var(--scribe-bg-sunken);padding:2px 5px;border-radius:3px;display:inline-block;margin-top:4px;">./my_venv/bin/uvicorn service.app:app --port 8765</code>';
    localWrap.appendChild(localHelp);
    container.appendChild(localWrap);

    const providerFieldsWrap = document.createElement("div");
    localCheckbox.addEventListener("change", () => {
      providerFieldsWrap.style.opacity = localCheckbox.checked ? "0.45" : "1";
      providerFieldsWrap.style.pointerEvents = localCheckbox.checked ? "none" : "auto";
    });
    container.appendChild(providerFieldsWrap);

    const gen = buildProviderBlock({ label: "Generation" });
    providerFieldsWrap.appendChild(gen.wrap);

    const sameRow = document.createElement("div");
    sameRow.style.cssText = "display:flex;align-items:center;gap:6px;margin:2px 0 8px;font-size:11px;color:var(--scribe-text-dim);";
    const sameCheckbox = document.createElement("input");
    sameCheckbox.type = "checkbox";
    sameCheckbox.id = "scribe-same-provider";
    const sameLabel = document.createElement("label");
    sameLabel.htmlFor = "scribe-same-provider";
    sameLabel.textContent = "Use the same provider/model for judging citations";
    sameRow.appendChild(sameCheckbox);
    sameRow.appendChild(sameLabel);
    providerFieldsWrap.appendChild(sameRow);

    const judge = buildProviderBlock({ label: "Judging (Tier 4)" });
    providerFieldsWrap.appendChild(judge.wrap);
    sameCheckbox.addEventListener("change", () => {
      judge.wrap.style.display = sameCheckbox.checked ? "none" : "block";
    });

    // Retrieval provider keys — raise resolvers.js's rate limits, unrelated
    // to which LLM is answering Draft/Check.
    const retrievalWrap = document.createElement("div");
    retrievalWrap.style.cssText = "border:1px solid var(--scribe-border);border-radius:6px;padding:8px;margin-bottom:8px;";
    const retrievalHeading = document.createElement("div");
    retrievalHeading.className = "scribe-heading";
    retrievalHeading.style.marginTop = "0";
    retrievalHeading.textContent = "Reference lookup (optional)";
    retrievalWrap.appendChild(retrievalHeading);

    const mailtoRow = document.createElement("div");
    mailtoRow.className = "scribe-row";
    const mailtoLabelWrap = document.createElement("span");
    mailtoLabelWrap.style.cssText = "display:flex;align-items:center;gap:4px;flex-shrink:0;";
    const mailtoLabel = document.createElement("span");
    mailtoLabel.className = "scribe-label";
    mailtoLabel.textContent = "Contact email";
    const mailtoInfo = makeInfoIcon(
      "Not an account or credential — just identifies you to Crossref/OpenAlex's \"polite pool.\" They don't verify or email it; passing one just moves your requests off the slower anonymous rate limit onto the faster identified one. Leave blank and enrichment still works, just slower on a large bibliography."
    );
    mailtoLabelWrap.appendChild(mailtoLabel);
    mailtoLabelWrap.appendChild(mailtoInfo);
    const mailtoInput = document.createElement("input");
    mailtoInput.type = "text";
    mailtoInput.className = "scribe-input";
    mailtoInput.placeholder = "for Crossref/OpenAlex's polite pool";
    mailtoRow.appendChild(mailtoLabelWrap);
    mailtoRow.appendChild(mailtoInput);
    retrievalWrap.appendChild(mailtoRow);

    function retrievalKeyRow(label, id) {
      const row = document.createElement("div");
      row.className = "scribe-row";
      const l = document.createElement("span");
      l.className = "scribe-label";
      l.textContent = label;
      const input = document.createElement("input");
      maskInput(input);
      input.className = "scribe-input";
      input.placeholder = "not set this session";
      const saveBtn = document.createElement("button");
      saveBtn.className = "scribe-btn";
      saveBtn.textContent = "Save";
      saveBtn.style.cssText = "font-size:10.5px;padding:5px 9px;";
      saveBtn.addEventListener("click", async () => {
        if (!input.value) return;
        await sendToBackground("SCRIBE_SAVE_RETRIEVAL_KEY", { id, value: input.value });
        input.value = "";
        input.placeholder = "saved this session";
      });
      row.appendChild(l);
      row.appendChild(input);
      row.appendChild(saveBtn);
      retrievalWrap.appendChild(row);
      return input;
    }
    const s2Input = retrievalKeyRow("Semantic Scholar key", "s2");
    const openalexInput = retrievalKeyRow("OpenAlex key", "openalex");
    providerFieldsWrap.appendChild(retrievalWrap);

    const saveRow = document.createElement("div");
    saveRow.style.cssText = "display:flex;gap:6px;align-items:center;";
    const saveBtn = document.createElement("button");
    saveBtn.className = "scribe-btn scribe-btn-primary";
    saveBtn.textContent = "Save settings";
    const saveStatus = document.createElement("span");
    saveStatus.style.cssText = "font-size:11px;color:var(--scribe-text-dim);";
    saveRow.appendChild(saveBtn);
    saveRow.appendChild(saveStatus);
    container.appendChild(saveRow);

    saveBtn.addEventListener("click", async () => {
      const settings = {
        useLocalService: localCheckbox.checked,
        generation: {
          provider: gen.providerSelect.value, model: gen.modelInput.value, baseUrl: gen.baseUrlInput.value,
          keyId: gen.keySection.getSelectedKeyId(),
        },
        judge: {
          sameAsGeneration: sameCheckbox.checked,
          provider: judge.providerSelect.value, model: judge.modelInput.value, baseUrl: judge.baseUrlInput.value,
          keyId: judge.keySection.getSelectedKeyId(),
        },
        retrieval: { mailto: mailtoInput.value },
      };
      await sendToBackground("SCRIBE_SAVE_SETTINGS", { settings });
      saveStatus.textContent = "saved.";
      setTimeout(() => { saveStatus.textContent = ""; }, 2500);
    });

    async function refresh() {
      const { result } = await sendToBackground("SCRIBE_GET_SETTINGS_UI", {});
      if (!result) return;
      const { settings, retrievalKeyStatus } = result;

      localCheckbox.checked = !!settings.useLocalService;
      localCheckbox.dispatchEvent(new Event("change"));

      gen.providerSelect.value = settings.generation.provider;
      gen.baseUrlInput.value = settings.generation.baseUrl || "";
      gen.modelInput.value = settings.generation.model || "";
      await gen.refreshForProvider();
      gen.keySection.setSelectedKeyId(settings.generation.keyId);

      sameCheckbox.checked = !!settings.judge.sameAsGeneration;
      judge.wrap.style.display = sameCheckbox.checked ? "none" : "block";
      judge.providerSelect.value = settings.judge.provider;
      judge.baseUrlInput.value = settings.judge.baseUrl || "";
      judge.modelInput.value = settings.judge.model || "";
      await judge.refreshForProvider();
      judge.keySection.setSelectedKeyId(settings.judge.keyId);

      mailtoInput.value = settings.retrieval.mailto || "";
      s2Input.placeholder = retrievalKeyStatus.s2.hasKey ? `saved: ${retrievalKeyStatus.s2.maskedKey}` : "not set this session";
      openalexInput.placeholder = retrievalKeyStatus.openalex.hasKey ? `saved: ${retrievalKeyStatus.openalex.maskedKey}` : "not set this session";
    }

    return { refresh };
  }

  // ---- Debug tools (the risk-spike panel, collapsed by default) -----------

  function buildDebugTools(container) {
    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;";

    const out = document.createElement("pre");
    out.style.cssText = "white-space:pre-wrap;word-break:break-all;margin:0;font-size:11px;max-height:200px;overflow:auto;background:var(--scribe-bg-sunken);border:1px solid var(--scribe-border);border-radius:5px;padding:6px;color:var(--scribe-text-dim);";
    out.textContent = "(no result yet)";

    function report(label, promise) {
      out.textContent = `${label}: running...`;
      promise.then((payload) => {
        const dump = window.ScribeSecrets.redactSecrets(JSON.stringify(payload, null, 2));
        out.textContent = `${label} @ ${new Date().toLocaleTimeString()}\n` + dump;
      });
    }

    function makeButton(label, onClick) {
      const b = document.createElement("button");
      b.textContent = label;
      b.className = "scribe-btn";
      b.style.fontSize = "10.5px";
      b.style.padding = "5px 9px";
      b.addEventListener("click", onClick);
      btnRow.appendChild(b);
      return b;
    }

    makeButton("Probe", () => report("SCRIBE_PROBE", callInject("SCRIBE_PROBE")));
    makeButton("Read doc", () => report("SCRIBE_READ", callInject("SCRIBE_READ")));
    makeButton("Insert test text", () =>
      report("SCRIBE_INSERT", callInject("SCRIBE_INSERT", { text: "%SCRIBE-SPIKE-TEST-INSERT%" }))
    );
    makeButton("Ping background", () => report("background round-trip", sendToBackground("PING", {})));
    makeButton("Probe file access", () => report("SCRIBE_PROBE_FILES", callInject("SCRIBE_PROBE_FILES")));
    makeButton("Health check", () => report("SCRIBE_HEALTH", sendToBackground("SCRIBE_HEALTH", {})));
    makeButton("Retrieval stats", () => report("SCRIBE_RETRIEVAL_STATS", sendToBackground("SCRIBE_RETRIEVAL_STATS", {})));

    container.appendChild(btnRow);

    const fetchRow = document.createElement("div");
    fetchRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;";
    const fetchInput = document.createElement("input");
    fetchInput.type = "text";
    fetchInput.value = "refs.bib";
    fetchInput.className = "scribe-input";
    const fetchBtn = document.createElement("button");
    fetchBtn.textContent = "Fetch file by name";
    fetchBtn.className = "scribe-btn";
    fetchBtn.style.fontSize = "10.5px";
    fetchBtn.style.padding = "5px 9px";
    fetchBtn.addEventListener("click", () => report(`fetch "${fetchInput.value}"`, fetchFileByName(fetchInput.value)));
    fetchRow.appendChild(fetchInput);
    fetchRow.appendChild(fetchBtn);
    container.appendChild(fetchRow);
    container.appendChild(out);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildPanel);
  } else {
    buildPanel();
  }
})();
