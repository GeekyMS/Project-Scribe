// Service worker. Owns every secret (API keys never leave this context —
// content.js/inject.js never see them) and every cross-origin fetch, since
// a service worker isn't subject to the page-context CORS/CSP surface a
// content script would be even with host_permissions declared.
//
// Runs the whole JS-ported pipeline (lib/*.js, loaded via importScripts —
// classic scripts, not ES modules, so they share this worker's global
// scope via `self.Scribe*` namespaces) as in-process job handlers, replacing
// the earlier design where this file only proxied fetch() calls to a local
// Python service. That service (service/) still exists and is still
// reachable at SERVICE_BASE_URL below — kept as an optional dev/debug and
// cross-validation tool, not required for the default no-server-needed flow.
//
// Job/poll shape (SCRIBE_START_JOB / SCRIBE_POLL_JOB) is unchanged from the
// Python-service era so content.js's UI code didn't need to change — only
// what happens inside those handlers moved from "fetch a remote service"
// to "run the pipeline in this worker."

importScripts(
  "lib/providers.js",
  "lib/settingsStore.js",
  "lib/citationSpans.js",
  "lib/resolvers.js",
  "lib/enrichmentCache.js",
  "lib/bibEnrichment.js",
  "lib/genPrompts.js",
  "lib/llmProviders.js",
  "lib/evaluator.js",
  "lib/generate.js"
);

const SERVICE_BASE_URL = "http://127.0.0.1:8765";

// ---- Optional local Python service (dev/debug + cross-validation only) --

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

// ---- In-process job store (replaces the Python service's job store) -----

const jobs = new Map(); // jobId -> { status, progress, result, error }

function newJobId() {
  return `job-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function makeJobHandle(jobId) {
  const record = { status: "running", progress: { stage: "starting", done: 0, total: 1 }, result: null, error: null };
  jobs.set(jobId, record);
  return {
    set progress(p) { record.progress = p; },
    get progress() { return record.progress; },
    set result(r) { record.result = r; record.status = "done"; },
    finishWithError(err) { record.error = String((err && err.message) || err); record.status = "error"; },
  };
}

async function resolveActiveProviderConfig(slot, settings) {
  // slot: "generation" | "judge". "judge" falls back to generation's
  // provider/model/baseUrl when settings.judge.sameAsGeneration is set —
  // mirrors evaluate_evidence_support's judge_model defaulting to the
  // same model as generation unless overridden.
  const cfg = slot === "judge" && settings.judge.sameAsGeneration ? settings.generation : settings[slot];
  const apiKey = await self.ScribeSettingsStore.getLlmKeyValue(cfg.provider, cfg.keyId);
  if (!apiKey) {
    const label = self.ScribeProviders.getProvider(cfg.provider)?.label || cfg.provider;
    throw new Error(`no API key selected for ${label} — open Settings and pick or add one`);
  }
  return { provider: cfg.provider, apiKey, baseUrl: cfg.baseUrl || undefined, model: cfg.model };
}

async function retrievalConfig(settings) {
  return {
    mailto: settings.retrieval.mailto || undefined,
    s2ApiKey: (await self.ScribeSettingsStore.getRetrievalKey("s2")) || undefined,
    openalexApiKey: (await self.ScribeSettingsStore.getRetrievalKey("openalex")) || undefined,
  };
}

async function runGenerateJob(job, body) {
  const settings = await self.ScribeSettingsStore.getSettings();
  const genCfg = await resolveActiveProviderConfig("generation", settings);
  const retrievalCfg = await retrievalConfig(settings);

  job.progress = { stage: "parsing_bib", done: 0, total: 1 };
  const bibEntries = self.ScribeBibEnrichment.parseBibContent(body.bib_content);

  job.progress = { stage: "enriching_references", done: 0, total: bibEntries.length };
  const enriched = await self.ScribeBibEnrichment.enrichEntries(bibEntries, retrievalCfg, {
    progress: (done, total, key) => { job.progress = { stage: "enriching_references", done, total, current_key: key }; },
  });

  job.progress = { stage: "generating", done: 0, total: 2 };
  const genResult = await self.ScribeGenerate.generateRelatedWork({
    provider: genCfg.provider, apiKey: genCfg.apiKey, baseUrl: genCfg.baseUrl, model: genCfg.model,
    paperText: body.tex_source, bibContent: body.bib_content, enrichedEntries: enriched,
    targetWordCount: body.target_word_count,
    onProgress: (p) => { job.progress = p; },
  });

  job.progress = { stage: "done", done: 1, total: 1 };
  job.result = {
    model: genCfg.model,
    latex_output: genResult.latex_output,
    markdown_output: genResult.markdown_output,
    raw_output: genResult.raw_output,
    enriched_entries: enriched,
  };
}

async function runCheckJob(job, body) {
  const settings = await self.ScribeSettingsStore.getSettings();
  const retrievalCfg = await retrievalConfig(settings);

  job.progress = { stage: "parsing_bib", done: 0, total: 1 };
  const bibEntries = self.ScribeBibEnrichment.parseBibContent(body.bib_content);

  job.progress = { stage: "checking_citation_keys", done: 0, total: 1 };
  const tier1 = self.ScribeEvaluator.evaluateCitations(body.text, bibEntries);

  job.progress = { stage: "enriching_references", done: 0, total: bibEntries.length };
  const enriched = await self.ScribeBibEnrichment.enrichEntries(bibEntries, retrievalCfg, {
    progress: (done, total, key) => { job.progress = { stage: "enriching_references", done, total, current_key: key }; },
  });

  const judgeCfg = await resolveActiveProviderConfig("judge", settings);
  job.progress = { stage: "checking_evidence_support", done: 0, total: 1 };
  const tier4 = await self.ScribeEvaluator.evaluateEvidenceSupport(body.text, bibEntries, enriched, {
    provider: judgeCfg.provider, apiKey: judgeCfg.apiKey, baseUrl: judgeCfg.baseUrl, model: judgeCfg.model,
    progress: (done, total) => { job.progress = { stage: "checking_evidence_support", done, total }; },
  });

  job.progress = { stage: "done", done: 1, total: 1 };
  job.result = { tier_1_citation_metrics: tier1, tier_4_evidence_support: tier4 };
}

function runJsJob(path, body) {
  const jobId = newJobId();
  const job = makeJobHandle(jobId);
  const runner = path === "/jobs/generate" ? runGenerateJob : path === "/jobs/check" ? runCheckJob : null;
  if (!runner) {
    job.finishWithError(`unknown job path: ${path}`);
    return jobId;
  }
  runner(job, body).catch((err) => job.finishWithError(err));
  return jobId;
}

// ---- Readiness (replaces "is the local service reachable") --------------

async function jsReadiness() {
  const settings = await self.ScribeSettingsStore.getSettings();

  if (settings.useLocalService) {
    try {
      const health = await serviceHealth();
      if (!health.ok) return { ok: false, reason: "local Python service not reachable — start it with: ./my_venv/bin/uvicorn service.app:app --port 8765" };
      return { ok: true };
    } catch {
      return { ok: false, reason: "local Python service not reachable at 127.0.0.1:8765 — start it with: ./my_venv/bin/uvicorn service.app:app --port 8765" };
    }
  }

  const genKey = await self.ScribeSettingsStore.getLlmKeyValue(settings.generation.provider, settings.generation.keyId);
  if (!genKey) {
    const label = self.ScribeProviders.getProvider(settings.generation.provider)?.label || settings.generation.provider;
    return { ok: false, reason: `no API key selected for ${label} — open Settings and pick or add one` };
  }
  return { ok: true };
}

// Preset provider origins (openrouter.ai/api.openai.com/api.anthropic.com)
// are already in host_permissions — nothing to request. A "Custom" base
// URL wasn't known at install time, so its origin needs a runtime grant
// from optional_host_permissions. Chrome requires permissions.request()
// to run inside a user gesture; going through runtime.sendMessage from
// content.js's Save-settings click can lose that association depending on
// Chrome version, so this is best-effort — if the prompt doesn't appear,
// the user can grant "on this site"/all-sites access manually from this
// extension's details page in chrome://extensions.
async function ensureCustomOriginPermission(settings) {
  const urls = [settings.generation, settings.judge]
    .filter((s) => s && s.provider === "custom" && s.baseUrl)
    .map((s) => s.baseUrl);
  for (const url of urls) {
    let origin;
    try {
      origin = new URL(url).origin + "/*";
    } catch {
      continue;
    }
    try {
      const already = await chrome.permissions.contains({ origins: [origin] });
      if (!already) await chrome.permissions.request({ origins: [origin] });
    } catch (e) {
      // Non-fatal: saving proceeds either way; requests will just fail at
      // call time with a clear error if the origin was never granted.
      console.warn("could not request permission for", origin, e);
    }
  }
}

// Either a raw apiKey (testing a key before it's saved) or a keyId
// (re-testing one already saved) — content.js never needs to know a
// saved key's raw value to trigger a test of it.
async function testKey({ providerId, apiKey, keyId, baseUrl, model }) {
  if (!apiKey && keyId) apiKey = await self.ScribeSettingsStore.getLlmKeyValue(providerId, keyId);
  if (!apiKey) return { ok: false, error: "no key to test" };
  const meta = self.ScribeProviders.getProvider(providerId);
  const testModel = model || (meta && meta.presetModels && meta.presetModels[0]);
  if (!testModel) return { ok: false, error: "no model to test with — enter a model id first" };
  try {
    await self.ScribeLLM.chatComplete({
      provider: providerId, apiKey, baseUrl: baseUrl || undefined, model: testModel,
      messages: [{ role: "user", content: "Reply with the single word: ok" }],
      maxTokens: 8,
    });
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

// ---- Keep-alive: a connected port prevents Chrome from idling this ------
// worker out mid-job. content.js opens one for the duration of a job; this
// listener just needs to exist so the connection is accepted.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "scribe-job") return;
  port.onDisconnect.addListener(() => {});
});

// ---- Message relay --------------------------------------------------------

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
    return true;
  }

  if (type === "SCRIBE_JS_READY") {
    jsReadiness()
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  if (type === "SCRIBE_START_JOB") {
    // payload.path selects the endpoint shape ("/jobs/generate" |
    // "/jobs/check"). Which backend actually handles it — the in-extension
    // JS pipeline or the local Python service — is decided by the saved
    // useLocalService setting (Settings panel), not a per-call flag, so
    // content.js and background.js can't disagree about which mode is
    // active.
    (async () => {
      const settings = await self.ScribeSettingsStore.getSettings();
      if (settings.useLocalService) return serviceStartJob(payload.path, payload.body);
      return { job_id: runJsJob(payload.path, payload.body) };
    })()
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  if (type === "SCRIBE_POLL_JOB") {
    (async () => {
      const settings = await self.ScribeSettingsStore.getSettings();
      if (settings.useLocalService) return servicePollJob(payload.jobId);
      const job = jobs.get(payload.jobId);
      if (!job) throw new Error(`unknown job id: ${payload.jobId}`);
      return { status: job.status, progress: job.progress, result: job.result, error: job.error };
    })()
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  if (type === "SCRIBE_GET_SETTINGS_UI") {
    self.ScribeSettingsStore.getSettingsForUi()
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  if (type === "SCRIBE_SAVE_SETTINGS") {
    ensureCustomOriginPermission(payload.settings)
      .then(() => self.ScribeSettingsStore.saveSettings(payload.settings))
      .then(() => sendResponse({ result: { ok: true }, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  // Retrieval keys (Semantic Scholar / OpenAlex) — single value per id,
  // not a named list, since they raise a rate limit rather than being
  // "which credential answers this call."
  if (type === "SCRIBE_SAVE_RETRIEVAL_KEY") {
    self.ScribeSettingsStore.saveRetrievalKey(payload.id, payload.value)
      .then(() => sendResponse({ result: { ok: true }, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  // LLM keys — named, multiple per provider. list/save/delete operate on
  // one provider's key list; save returns the new/updated key's id so the
  // settings UI can select it immediately.
  if (type === "SCRIBE_LIST_LLM_KEYS") {
    self.ScribeSettingsStore.listLlmKeys(payload.providerId)
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  if (type === "SCRIBE_SAVE_LLM_KEY") {
    self.ScribeSettingsStore.saveLlmKey(payload.providerId, { id: payload.id, nickname: payload.nickname, apiKey: payload.apiKey })
      .then((keyId) => sendResponse({ result: { keyId }, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  if (type === "SCRIBE_DELETE_LLM_KEY") {
    self.ScribeSettingsStore.deleteLlmKey(payload.providerId, payload.keyId)
      .then(() => sendResponse({ result: { ok: true }, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  if (type === "SCRIBE_TEST_KEY") {
    (async () => {
      if (payload.providerId === "custom" && payload.baseUrl) {
        await ensureCustomOriginPermission({ generation: { provider: "custom", baseUrl: payload.baseUrl }, judge: null });
      }
      return testKey(payload);
    })()
      .then((result) => sendResponse({ result, error: null }))
      .catch((err) => sendResponse({ result: null, error: String(err) }));
    return true;
  }

  sendResponse({ result: null, error: `unknown message type: ${type}` });
  return false;
});
