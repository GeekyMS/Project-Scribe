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
      window.dispatchEvent(new CustomEvent("scribe:request", { detail: { requestId, type, payload } }));
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
    const nodes = document.querySelectorAll(".item-name, [class*='file-tree'] span, [class*='entity-name']");
    for (const el of nodes) {
      const text = (el.textContent || "").trim();
      // Guard against folder-container elements whose textContent
      // concatenates every descendant filename (seen in SCRIBE_PROBE_FILES
      // as e.g. "book_5refs.bib") — a real leaf entry's name is short.
      if (text.endsWith(".bib") && text.length < 60) return text;
    }
    return null;
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

  async function fetchFileByName(targetText, { timeoutMs = 4000, pollMs = 150 } = {}) {
    const before = await callInject("SCRIBE_PROBE");
    const hookCountBefore = before.result && before.result.pathA ? before.result.pathA.hookFireCount : null;
    const previouslySelected = findCurrentlySelectedEntryText();

    const targets = findLeafEntriesByExactText(targetText);
    if (targets.length === 0) {
      return { error: `no file-tree entry found with exact text "${targetText}"`, previouslySelected };
    }
    clickEntry(targets[0]);

    const deadline = Date.now() + timeoutMs;
    let hookFired = false;
    while (Date.now() < deadline) {
      await sleep(pollMs);
      const probe = await callInject("SCRIBE_PROBE");
      const count = probe.result && probe.result.pathA ? probe.result.pathA.hookFireCount : null;
      if (hookCountBefore !== null && count !== null && count > hookCountBefore) {
        hookFired = true;
        break;
      }
    }

    const read = await callInject("SCRIBE_READ");

    let restored = false;
    if (previouslySelected) {
      const backTargets = findLeafEntriesByExactText(previouslySelected);
      if (backTargets.length > 0) {
        clickEntry(backTargets[0]);
        restored = true;
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
    return { name, content: fetchResult.read.text };
  }

  async function checkServiceHealth() {
    const { result, error } = await sendToBackground("SCRIBE_HEALTH", {});
    if (error || !result) return { ok: false, has_key: false };
    return result;
  }

  async function pollJob(jobId, onProgress, { intervalMs = 1500, maxWaitMs = 15 * 60 * 1000 } = {}) {
    const deadline = Date.now() + maxWaitMs;
    while (Date.now() < deadline) {
      const { result, error } = await sendToBackground("SCRIBE_POLL_JOB", { jobId });
      if (error) throw new Error(error);
      if (onProgress) onProgress(result.progress);
      if (result.status === "done") return result.result;
      if (result.status === "error") throw new Error(result.error || "job failed");
      await sleep(intervalMs);
    }
    throw new Error("timed out waiting for job to finish");
  }

  async function startAndAwaitJob(path, body, onProgress) {
    const { result: started, error: startErr } = await sendToBackground("SCRIBE_START_JOB", { path, body });
    if (startErr) throw new Error(startErr);
    return pollJob(started.job_id, onProgress);
  }

  // Full server-side Tier1 + Tier4 check when the service is up; degrades
  // to the offline JS key check (lib/citations.js) when it isn't — this is
  // the hybrid split's actual point, not just an error message.
  async function runCheck(text, bibContent, onProgress) {
    const health = await checkServiceHealth();
    if (!health.ok) {
      const offline = window.ScribeCitations.checkCitations(text, bibContent);
      return { mode: "offline", tier1: offline };
    }
    const result = await startAndAwaitJob("/jobs/check", { text, bib_content: bibContent }, onProgress);
    return { mode: "full", tier1: result.tier_1_citation_metrics, tier4: result.tier_4_evidence_support };
  }

  // ---- Panel ----------------------------------------------------------

  const state = {
    bibFilename: "",
    bibContent: null,
    generatedText: null,
    checkResult: null,
    busy: false,
  };

  function buildPanel() {
    const panel = document.createElement("div");
    panel.id = "scribe-panel";
    panel.style.cssText = [
      "position:fixed", "bottom:12px", "right:12px", "z-index:2147483647",
      "width:460px", "max-height:80vh", "overflow:auto",
      "background:#1e1e1e", "color:#e0e0e0", "font:12px/1.4 -apple-system,sans-serif",
      "border:1px solid #555", "border-radius:6px", "padding:10px",
      "box-shadow:0 2px 12px rgba(0,0,0,.5)",
    ].join(";");

    const title = document.createElement("div");
    title.textContent = "Scribe";
    title.style.cssText = "font-weight:bold;margin-bottom:8px;font-size:13px;";
    panel.appendChild(title);

    // -- Bib filename row --
    const bibRow = document.createElement("div");
    bibRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;align-items:center;";
    const bibLabel = document.createElement("span");
    bibLabel.textContent = "Bib file:";
    bibLabel.style.cssText = "flex-shrink:0;color:#999;";
    const bibInput = document.createElement("input");
    bibInput.type = "text";
    bibInput.placeholder = "auto-detect";
    bibInput.style.cssText = "flex:1;font:11px monospace;padding:3px;background:#2a2a2a;color:#eee;border:1px solid #555;";
    bibRow.appendChild(bibLabel);
    bibRow.appendChild(bibInput);
    panel.appendChild(bibRow);

    // -- Primary actions --
    const actionRow = document.createElement("div");
    actionRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;";
    const draftBtn = makeButton("Draft Related Work", onDraft);
    const checkBtn = makeButton("Check citations", onCheck);
    actionRow.appendChild(draftBtn);
    actionRow.appendChild(checkBtn);
    panel.appendChild(actionRow);

    // -- Status line --
    const statusLine = document.createElement("div");
    statusLine.style.cssText = "color:#999;margin-bottom:8px;min-height:14px;";
    statusLine.textContent = "";
    panel.appendChild(statusLine);

    // -- Results area --
    const resultsArea = document.createElement("div");
    panel.appendChild(resultsArea);

    // -- Debug toggle + tools (collapsed by default) --
    const debugToggle = document.createElement("div");
    debugToggle.textContent = "▸ debug tools";
    debugToggle.style.cssText = "color:#777;cursor:pointer;margin-top:10px;font-size:11px;";
    const debugPanel = document.createElement("div");
    debugPanel.style.cssText = "display:none;margin-top:6px;border-top:1px solid #444;padding-top:6px;";
    debugToggle.addEventListener("click", () => {
      const showing = debugPanel.style.display !== "none";
      debugPanel.style.display = showing ? "none" : "block";
      debugToggle.textContent = showing ? "▸ debug tools" : "▾ debug tools";
    });
    buildDebugTools(debugPanel);
    panel.appendChild(debugToggle);
    panel.appendChild(debugPanel);

    document.documentElement.appendChild(panel);

    // ---- action handlers (closures over the DOM refs above) ----

    function setStatus(text) {
      statusLine.textContent = text;
    }

    function setBusy(busy) {
      state.busy = busy;
      draftBtn.disabled = busy;
      checkBtn.disabled = busy;
    }

    function renderResults() {
      resultsArea.innerHTML = "";

      if (state.generatedText) {
        const heading = document.createElement("div");
        heading.textContent = "Draft:";
        heading.style.cssText = "font-weight:bold;margin-top:6px;";
        resultsArea.appendChild(heading);

        const pre = document.createElement("pre");
        pre.textContent = state.generatedText;
        pre.style.cssText = "white-space:pre-wrap;word-break:break-word;max-height:180px;overflow:auto;background:#151515;padding:6px;border:1px solid #444;margin:4px 0;";
        resultsArea.appendChild(pre);

        const btnRow = document.createElement("div");
        btnRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;";
        btnRow.appendChild(makeButton("Accept (insert)", onAccept));
        btnRow.appendChild(makeButton("Regenerate", onDraft));
        btnRow.appendChild(makeButton("Discard", onDiscard));
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

        setStatus("checking service...");
        const health = await checkServiceHealth();
        if (!health.ok) {
          setStatus("service is offline — drafting needs the local Python service running on 127.0.0.1:8765.");
          setBusy(false);
          return;
        }

        setStatus("generating (this can take a bit)...");
        const genResult = await startAndAwaitJob(
          "/jobs/generate",
          { tex_source: tex, bib_content: bib },
          (progress) => setStatus(`generating: ${progress.stage} (${progress.done}/${progress.total})`)
        );
        state.generatedText = genResult.latex_output || genResult.markdown_output || genResult.raw_output;

        setStatus("checking citations in the draft...");
        state.checkResult = await runCheck(
          state.generatedText,
          bib,
          (progress) => setStatus(`checking: ${progress.stage} (${progress.done}/${progress.total})`)
        );

        setStatus("done.");
        renderResults();
      } catch (err) {
        setStatus(`error: ${err.message || err}`);
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
        renderResults();
      } catch (err) {
        setStatus(`error: ${err.message || err}`);
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
        return;
      }
      setStatus(`inserted (document now ${result.newLength} chars). Note: inserts at the cursor in whichever file is currently open.`);
    }

    function onDiscard() {
      state.generatedText = null;
      state.checkResult = null;
      setStatus("");
      renderResults();
    }

    function makeButton(label, onClick) {
      const b = document.createElement("button");
      b.textContent = label;
      b.style.cssText = "cursor:pointer;padding:4px 8px;background:#333;color:#eee;border:1px solid #555;border-radius:3px;";
      b.addEventListener("click", onClick);
      return b;
    }
  }

  function verdictColor(verdict) {
    switch (verdict) {
      case "supported": return "#2e7d32";
      case "partially_supported": return "#b8860b";
      case "unsupported": case "contradicted": return "#c62828";
      default: return "#666"; // unverifiable
    }
  }

  function badge(text, color) {
    const el = document.createElement("span");
    el.textContent = text;
    el.style.cssText = `display:inline-block;padding:1px 6px;border-radius:3px;background:${color};color:#fff;margin:2px 4px 2px 0;font-size:10px;`;
    return el;
  }

  function renderFindings(container, checkResult) {
    const heading = document.createElement("div");
    heading.textContent = checkResult.mode === "offline"
      ? "Citation keys (offline check — server not running):"
      : "Findings:";
    heading.style.cssText = "font-weight:bold;margin-top:6px;";
    container.appendChild(heading);

    const tier1 = checkResult.tier1;
    if (tier1.hallucinated_keys && tier1.hallucinated_keys.length > 0) {
      const row = document.createElement("div");
      row.appendChild(document.createTextNode("Cited but not in bib: "));
      for (const key of tier1.hallucinated_keys) row.appendChild(badge(key, "#c62828"));
      container.appendChild(row);
    } else {
      const row = document.createElement("div");
      row.style.cssText = "color:#2e7d32;";
      row.textContent = "No hallucinated citation keys.";
      container.appendChild(row);
    }

    if (tier1.missing_keys && tier1.missing_keys.length > 0) {
      const row = document.createElement("div");
      row.style.cssText = "color:#999;";
      row.appendChild(document.createTextNode(`In bib but never cited: ${tier1.missing_keys.join(", ")}`));
      container.appendChild(row);
    }

    if (checkResult.mode === "offline") {
      const note = document.createElement("div");
      note.style.cssText = "color:#999;margin-top:4px;font-style:italic;";
      note.textContent = "Evidence-support checking (does the cited paper actually support the claim?) needs the local service — start it and re-check.";
      container.appendChild(note);
      return;
    }

    const tier4 = checkResult.tier4;
    if (!tier4 || !tier4.per_citation || tier4.per_citation.length === 0) return;

    const summary = document.createElement("div");
    summary.style.cssText = "color:#999;margin-top:6px;";
    summary.textContent = `Evidence coverage: ${(tier4.evidence_coverage * 100).toFixed(0)}% · support rate (of checked): ${tier4.support_rate != null ? (tier4.support_rate * 100).toFixed(0) + "%" : "n/a"}`;
    container.appendChild(summary);

    const list = document.createElement("div");
    list.style.cssText = "max-height:160px;overflow:auto;margin-top:4px;";
    // Worst verdicts first, so contradicted/unsupported citations aren't
    // buried at the bottom of a long list.
    const order = { contradicted: 0, unsupported: 1, partially_supported: 2, unverifiable: 3, supported: 4 };
    const sorted = [...tier4.per_citation].sort(
      (a, b) => (order[a.verdict] ?? 5) - (order[b.verdict] ?? 5)
    );
    for (const c of sorted) {
      const row = document.createElement("div");
      row.style.cssText = "padding:3px 0;border-bottom:1px solid #333;";
      const head = document.createElement("div");
      head.appendChild(document.createTextNode(c.citation_key + " "));
      head.appendChild(badge(c.verdict, verdictColor(c.verdict)));
      row.appendChild(head);
      if (c.justification) {
        const just = document.createElement("div");
        just.style.cssText = "color:#999;font-size:11px;";
        just.textContent = c.justification;
        row.appendChild(just);
      }
      list.appendChild(row);
    }
    container.appendChild(list);
  }

  // ---- Debug tools (the risk-spike panel, collapsed by default) -----------

  function buildDebugTools(container) {
    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;";

    const out = document.createElement("pre");
    out.style.cssText = "white-space:pre-wrap;word-break:break-all;margin:0;font-size:11px;max-height:200px;overflow:auto;";
    out.textContent = "(no result yet)";

    function report(label, promise) {
      out.textContent = `${label}: running...`;
      promise.then((payload) => {
        out.textContent = `${label} @ ${new Date().toLocaleTimeString()}\n` + JSON.stringify(payload, null, 2);
      });
    }

    function makeButton(label, onClick) {
      const b = document.createElement("button");
      b.textContent = label;
      b.style.cssText = "cursor:pointer;padding:4px 8px;";
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

    container.appendChild(btnRow);

    const fetchRow = document.createElement("div");
    fetchRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;";
    const fetchInput = document.createElement("input");
    fetchInput.type = "text";
    fetchInput.value = "refs.bib";
    fetchInput.style.cssText = "flex:1;font:11px monospace;padding:3px;";
    const fetchBtn = document.createElement("button");
    fetchBtn.textContent = "Fetch file by name";
    fetchBtn.style.cssText = "cursor:pointer;padding:4px 8px;";
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
