// MAIN world. Runs at document_start — before Overleaf constructs its editor.
// The only script in this spike allowed to touch the live CodeMirror EditorView.
//
// Two access paths are probed independently, never falling back silently into
// each other, so a Path A failure can't be masked by a Path B success:
//
//   Path A — UNSTABLE_editor:extensions. Overleaf's own documented (if
//            unsupported) hook. Fires every time it builds an editor, so a
//            captured handle should survive remounts (file switches, pane
//            open/close). See: docs.overleaf.com .../providing-custom-vim-key-bindings
//   Path B — walking the DOM for a node carrying `.cmView.view`. A snapshot:
//            nothing re-supplies it if Overleaf swaps the CM instance out.
//
// Findings get written to extension/FINDINGS.md after a live run; this file
// only needs to measure, not conclude.

(() => {
  const LOG_PREFIX = "[scribe-spike]";

  // ---- Path A: capture via UNSTABLE_editor:extensions --------------------

  let pathAView = null;
  let pathAFireLog = []; // measurement #4 evidence: one entry per hook fire

  window.addEventListener("UNSTABLE_editor:extensions", (event) => {
    const { CodeMirror, extensions } = event.detail || {};
    pathAFireLog.push({ at: new Date().toISOString() });
    console.log(LOG_PREFIX, "UNSTABLE_editor:extensions fired", pathAFireLog.length, "time(s)");

    if (!CodeMirror || !extensions || !Array.isArray(extensions)) {
      console.warn(LOG_PREFIX, "Path A: event fired but detail shape is unexpected", event.detail);
      return;
    }

    try {
      // Use the ViewPlugin/updateListener form so we stash the view on
      // construction without altering editor behavior. Using the CM classes
      // handed to us in event.detail avoids instanceof mismatches against a
      // second bundled copy of CodeMirror.
      extensions.push(
        CodeMirror.EditorView.updateListener.of((update) => {
          pathAView = update.view;
        })
      );
    } catch (err) {
      console.error(LOG_PREFIX, "Path A: failed to register updateListener", err);
    }
  });

  // ---- Path B: DOM walk for a `.cmView.view`-bearing node -----------------

  function probePathB() {
    const candidates = [];

    const editorEl = document.querySelector(".cm-editor");
    if (editorEl) candidates.push([".cm-editor cmView.view", editorEl.cmView && editorEl.cmView.view]);

    const contentEl = document.querySelector(".cm-content");
    if (contentEl) {
      candidates.push([".cm-content cmView.view", contentEl.cmView && contentEl.cmView.view]);
      candidates.push([".cm-content cmView.editorView", contentEl.cmView && contentEl.cmView.editorView]);
    }

    // Short walk: any element anywhere carrying a `cmView` property at all.
    if (editorEl) {
      let node = editorEl;
      let hops = 0;
      while (node && hops < 5) {
        if (node.cmView) {
          candidates.push([`walk-up hop ${hops}: cmView.view`, node.cmView.view]);
          candidates.push([`walk-up hop ${hops}: cmView.editorView`, node.cmView.editorView]);
        }
        node = node.parentElement;
        hops++;
      }
    }

    for (const [path, candidate] of candidates) {
      if (candidate && candidate.state && candidate.state.doc && typeof candidate.dispatch === "function") {
        return { path, view: candidate };
      }
    }
    return { path: null, view: null };
  }

  // ---- Bridge: window CustomEvents <-> content.js --------------------------
  // Request:  scribe:request  { requestId, type, payload }
  // Response: scribe:response { requestId, type, result } | { requestId, type, error }

  function hashString(str) {
    // Cheap non-cryptographic hash, sufficient to detect "did the doc change".
    let h = 0;
    for (let i = 0; i < str.length; i++) {
      h = (Math.imul(31, h) + str.charCodeAt(i)) | 0;
    }
    return h.toString(16);
  }

  function respond(requestId, type, result, error) {
    window.dispatchEvent(
      new CustomEvent("scribe:response", {
        detail: { requestId, type, result: result ?? null, error: error ?? null },
      })
    );
  }

  // ---- Path C: probe for a way to read OTHER project files (not just the
  // open document) — needed for real .bib discovery. Tried in order of
  // preference; report all three so FINDINGS.md can record which worked
  // without re-running the probe multiple times.

  function probeIdeObject() {
    // Overleaf's Angular/React app historically exposed an `_ide` (or
    // similar) object on window carrying the file tree and doc-loading
    // machinery. This has moved around across Overleaf versions, so try a
    // few known/plausible globals rather than assuming one name.
    const candidateGlobalNames = ["_ide", "ide", "__OL_IDE__"];
    const found = {};
    for (const name of candidateGlobalNames) {
      const obj = window[name];
      if (obj) {
        found[name] = {
          type: typeof obj,
          keys: typeof obj === "object" ? Object.keys(obj).slice(0, 40) : null,
        };
      }
    }
    return { checked: candidateGlobalNames, found };
  }

  function probeFileTreeDom() {
    // Look for anything in the DOM that looks like a file-tree entry
    // referencing a .bib file — report selectors/text, not full HTML.
    const candidates = [];
    const all = document.querySelectorAll("[class*='file-tree'] *, [class*='entity']");
    for (const el of all) {
      const text = (el.textContent || "").trim();
      if (text.endsWith(".bib") && text.length < 120) {
        candidates.push({
          text,
          tag: el.tagName,
          className: typeof el.className === "string" ? el.className.slice(0, 100) : null,
          hasClickHandlerGuess: !!(el.onclick || el.getAttribute("role") === "button"),
        });
      }
    }
    return { matchCount: candidates.length, candidates: candidates.slice(0, 10) };
  }

  function probeSocketOrRest() {
    // Report whether common Overleaf real-time/collab globals are present.
    // We do NOT call anything here — just detect presence, since calling
    // blind into an internal API could have side effects we don't want in
    // a spike.
    const candidateGlobalNames = ["socket", "io", "__meta__"];
    const found = {};
    for (const name of candidateGlobalNames) {
      if (window[name]) found[name] = typeof window[name];
    }
    // Also report the project id if visible in the URL, since REST/socket
    // approaches would need it.
    const projectIdMatch = window.location.pathname.match(/\/project\/([a-f0-9]+)/);
    return {
      found,
      projectId: projectIdMatch ? projectIdMatch[1] : null,
    };
  }

  // ---- Bridge nonce ---------------------------------------------------
  // content.js (isolated world) generates a random nonce and sends it once
  // via scribe:init; every scribe:request must carry the matching nonce or
  // is silently ignored. Without this, any page script — Overleaf's own
  // or something it loads — could dispatch a fake scribe:request and use
  // SCRIBE_INSERT to write into the document or SCRIBE_READ to exfiltrate
  // it, since window events have no built-in sender authentication.
  //
  // This is a best-effort raise of the bar, not an airtight guarantee: it
  // stops incidental/opportunistic interference, but a page script
  // specifically engineered to race scribe:init before content.js's own
  // (document_start vs document_idle timing makes this hard but not
  // provably impossible) could still win. Locking onto the *first* value
  // received means it can't be silently overridden after the real
  // handshake completes, which is the more likely real-world case.
  let bridgeNonce = null;
  window.addEventListener("scribe:init", (event) => {
    if (bridgeNonce === null) {
      bridgeNonce = (event.detail && event.detail.nonce) || null;
      console.log(LOG_PREFIX, "bridge nonce established");
    }
  });

  window.addEventListener("scribe:request", (event) => {
    const { requestId, type, payload, nonce } = event.detail || {};
    if (bridgeNonce === null || nonce !== bridgeNonce) {
      console.warn(LOG_PREFIX, "rejected scribe:request with invalid/missing nonce", type);
      return;
    }

    try {
      if (type === "SCRIBE_PROBE_FILES") {
        respond(requestId, type, {
          ideObject: probeIdeObject(),
          fileTreeDom: probeFileTreeDom(),
          socketOrRest: probeSocketOrRest(),
        });
        return;
      }

      if (type === "SCRIBE_PROBE") {
        const b = probePathB();
        const a = { path: pathAView ? "UNSTABLE_editor:extensions" : null, view: pathAView };
        const sameObject = !!(a.view && b.view && a.view === b.view);
        respond(requestId, type, {
          pathA: { resolved: !!a.view, hookFireCount: pathAFireLog.length, fireLog: pathAFireLog },
          pathB: { resolved: !!b.view, winningPath: b.path },
          sameObject,
        });
        return;
      }

      if (type === "SCRIBE_READ") {
        // Prefer whichever path resolved; report which one we used.
        const source = pathAView ? "A" : probePathB().view ? "B" : null;
        const view = pathAView || probePathB().view;
        if (!view) {
          respond(requestId, type, null, "no view resolved by either path");
          return;
        }
        const text = view.state.doc.toString();
        respond(requestId, type, {
          source,
          text,
          length: view.state.doc.length,
          lines: view.state.doc.lines,
          hash: hashString(text),
          head: text.slice(0, 200),
          tail: text.slice(-200),
          viewport: { from: view.viewport.from, to: view.viewport.to },
        });
        return;
      }

      if (type === "SCRIBE_REVEAL") {
        // Select a range and scroll it into view, so clicking a finding in
        // the panel takes the user straight to the sentence instead of
        // making them hunt for it in a 40KB .tex.
        //
        // Selection + scrollIntoView deliberately, not a Decoration mark:
        // this needs only view.dispatch(), which is already proven working
        // (see FINDINGS.md §3), whereas a persistent highlight would depend
        // on StateField/Decoration being reachable through
        // UNSTABLE_editor:extensions — never verified, since only
        // EditorView has ever been used from that hook.
        const view = pathAView || probePathB().view;
        if (!view) {
          respond(requestId, type, null, "no view resolved by either path");
          return;
        }
        const len = view.state.doc.length;
        // Clamp: content.js searches the document it last read, which can
        // be shorter than the live one by the time this arrives. An
        // out-of-range position throws inside CodeMirror.
        const anchor = Math.max(0, Math.min(Number(payload && payload.from) || 0, len));
        const head = Math.max(0, Math.min(Number(payload && payload.to) || 0, len));
        view.dispatch({ selection: { anchor, head }, scrollIntoView: true });
        view.focus();
        respond(requestId, type, { from: anchor, to: head, docLength: len });
        return;
      }

      if (type === "SCRIBE_INSERT") {
        const view = pathAView || probePathB().view;
        if (!view) {
          respond(requestId, type, null, "no view resolved by either path");
          return;
        }
        const requestedText = (payload && payload.text) || "%SCRIBE-SPIKE-TEST-INSERT%";
        const from = view.state.selection.main.head;
        const doc = view.state.doc;

        // Insert on its own blank line rather than running into whatever
        // is already at the cursor — found via a live test where a draft
        // landed mid-sentence as "...screen.## Related Work..." with no
        // separation. Pads only what's missing (checks up to 2 characters
        // each side for existing newlines) so re-running near an already
        // blank line doesn't pile up extra blank lines.
        const countTrailingNewlines = (s) => {
          let n = 0;
          for (let i = s.length - 1; i >= 0 && s[i] === "\n"; i--) n++;
          return n;
        };
        const countLeadingNewlines = (s) => {
          let n = 0;
          for (let i = 0; i < s.length && s[i] === "\n"; i++) n++;
          return n;
        };
        const before = doc.sliceString(Math.max(0, from - 2), from);
        const after = doc.sliceString(from, Math.min(doc.length, from + 2));
        const prefix = from === 0 ? "" : "\n".repeat(Math.max(0, 2 - countTrailingNewlines(before)));
        const suffix = from === doc.length ? "" : "\n".repeat(Math.max(0, 2 - countLeadingNewlines(after)));
        const insertText = prefix + requestedText + suffix;

        view.dispatch({ changes: { from, insert: insertText } });
        respond(requestId, type, {
          insertedAt: from,
          insertText,
          newLength: view.state.doc.length,
        });
        return;
      }

      respond(requestId, type, null, `unknown request type: ${type}`);
    } catch (err) {
      respond(requestId, type, null, String(err && err.stack ? err.stack : err));
    }
  });

  console.log(LOG_PREFIX, "inject.js loaded at document_start; Path A listener registered.");
})();
