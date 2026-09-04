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

    panel.appendChild(btnRow);
    panel.appendChild(out);
    document.documentElement.appendChild(panel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildPanel);
  } else {
    buildPanel();
  }
})();
