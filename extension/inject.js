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

  window.addEventListener("scribe:request", (event) => {
    const { requestId, type, payload } = event.detail || {};

    try {
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
          length: view.state.doc.length,
          lines: view.state.doc.lines,
          hash: hashString(text),
          head: text.slice(0, 200),
          tail: text.slice(-200),
          viewport: { from: view.viewport.from, to: view.viewport.to },
        });
        return;
      }

      if (type === "SCRIBE_INSERT") {
        const view = pathAView || probePathB().view;
        if (!view) {
          respond(requestId, type, null, "no view resolved by either path");
          return;
        }
        const insertText = (payload && payload.text) || "%SCRIBE-SPIKE-TEST-INSERT%";
        const from = view.state.selection.main.head;
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
