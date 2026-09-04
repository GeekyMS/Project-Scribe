// Service worker. Does no real work in this spike — it exists to prove the
// full three-context round trip (inject.js <-> content.js <-> background.js)
// is wired correctly before it has to carry an LLM call and a fetch() to the
// future local Python service (Phase 3).

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  console.log("[scribe-spike] background received:", message);
  sendResponse({ echo: message, receivedAt: new Date().toISOString() });
  return true;
});
