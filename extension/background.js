// Service worker. Owns every network call to the local Python service —
// not content.js, which runs in the page's isolated world and would be
// subject to the page's CSP/CORS surface for a cross-origin fetch even
// with host_permissions declared. A service worker has none of that.
//
// Kept as simple request/response messages (one message in, one response
// out) rather than a single long-lived "run this whole job" call, because
// a service worker Chrome may terminate after ~30s of no activity — and a
// multi-minute job with sleeps in between polls looks idle from Chrome's
// perspective. content.js owns the poll loop and calls back in short,
// independently-alive request/response round trips instead.

const SERVICE_BASE_URL = "http://127.0.0.1:8765";

async function serviceHealth() {
  const resp = await fetch(`${SERVICE_BASE_URL}/health`, { signal: AbortSignal.timeout(3000) });
  return resp.json();
}

async function serviceStartJob(path, body) {
  const resp = await fetch(`${SERVICE_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`service returned ${resp.status}`);
  return resp.json();
}

async function servicePollJob(jobId) {
  const resp = await fetch(`${SERVICE_BASE_URL}/jobs/${jobId}`);
  if (!resp.ok) throw new Error(`service returned ${resp.status}`);
  return resp.json();
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const { type, payload } = message || {};

  if (type === "PING") {
    sendResponse({ echo: message, receivedAt: new Date().toISOString() });
    return false;
  }

  if (type === "SCRIBE_HEALTH") {
    serviceHealth()
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true; // keep the message channel open for the async response
  }

  if (type === "SCRIBE_START_JOB") {
    serviceStartJob(payload.path, payload.body)
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  if (type === "SCRIBE_POLL_JOB") {
    servicePollJob(payload.jobId)
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  sendResponse({ result: null, error: `unknown message type: ${type}` });
  return false;
});
