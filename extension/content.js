// ISOLATED world. Relay between inject.js (page/MAIN world) and background.js
// (service worker), plus the dev panel used to drive the spike manually.
// Owns its own DOM (per the handoff's convention that content.js, not
// inject.js, renders UI) — inject.js never touches anything but CodeMirror.

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
      // Belt-and-suspenders timeout so a dropped response doesn't hang the panel forever.
      setTimeout(() => {
        if (pending.has(requestId)) {
          pending.delete(requestId);
          resolve({ result: null, error: "timed out waiting for inject.js response" });
        }
      }, 5000);
    });
  }

  // Round-trip everything through background.js too, even though it does
  // nothing useful yet — this proves the full three-context path that will
  // eventually carry the LLM call and the Python-service fetch.
  function relayThroughBackground(type, payload) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage({ type, payload }, (ack) => resolve(ack));
    });
  }

  // ---- Fetch another file by name (Part 1 of the MVP plan) ----------------
  //
  // Confirmed via SCRIBE_PROBE_FILES: window._ide-style globals don't exist
  // on this Overleaf version, but real leaf file-tree entries are findable
  // in the DOM with clean, exact filename text (e.g. a `.item-name` div and
  // a bare `<span>`, both containing exactly "refs.bib" — the other DOM
  // matches from that probe were folder containers whose textContent
  // concatenates all descendant filenames, not real targets).
  //
  // Strategy: find the leaf entry, remember whatever's currently marked
  // selected (best-effort, so we can click back afterward), click the
  // target, wait for inject.js's Path A hook to fire again (proven durable
  // in the risk spike), then read via the existing SCRIBE_READ path.

  function findLeafEntriesByExactText(targetText) {
    const matches = [];
    const nodes = document.querySelectorAll(".item-name, [class*='file-tree'] span, [class*='entity-name']");
    for (const el of nodes) {
      if ((el.textContent || "").trim() === targetText) matches.push(el);
    }
    return matches;
  }

  function findCurrentlySelectedEntryText() {
    // Best-effort only — class names for "selected" are not confirmed by
    // the probe. If this comes back empty, restoring the original file
    // after the fetch will have to be done manually.
    const el = document.querySelector(
      "[class*='file-tree'] [class*='selected'], [class*='file-tree'] [aria-selected='true']"
    );
    if (!el) return null;
    const nameEl = el.querySelector(".item-name") || el;
    const text = (nameEl.textContent || "").trim();
    return text || null;
  }

  function clickEntry(el) {
    // React apps generally respond to a real MouseEvent dispatch better
    // than to a bare .click() on a non-<button> element.
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
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

    // Wait for Path A's hook to re-fire (new editor instance for the newly
    // opened file), rather than a fixed guess-and-hope delay.
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

  // ---- Dev panel ------------------------------------------------------

  function buildPanel() {
    const panel = document.createElement("div");
    panel.id = "scribe-spike-panel";
    panel.style.cssText = [
      "position:fixed", "bottom:12px", "right:12px", "z-index:2147483647",
      "width:420px", "max-height:60vh", "overflow:auto",
      "background:#1e1e1e", "color:#e0e0e0", "font:12px/1.4 monospace",
      "border:1px solid #555", "border-radius:6px", "padding:10px",
      "box-shadow:0 2px 12px rgba(0,0,0,.5)",
    ].join(";");

    const title = document.createElement("div");
    title.textContent = "Scribe Overleaf Risk Spike";
    title.style.cssText = "font-weight:bold;margin-bottom:6px;";
    panel.appendChild(title);

    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;";

    const out = document.createElement("pre");
    out.style.cssText = "white-space:pre-wrap;word-break:break-all;margin:0;font-size:11px;";
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
    makeButton("Ping background", () => report("background round-trip", relayThroughBackground("PING", {})));
    makeButton("Probe file access", () => report("SCRIBE_PROBE_FILES", callInject("SCRIBE_PROBE_FILES")));

    panel.appendChild(btnRow);

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
    panel.appendChild(fetchRow);
    panel.appendChild(out);
    document.documentElement.appendChild(panel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildPanel);
  } else {
    buildPanel();
  }
})();
