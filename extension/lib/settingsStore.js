// Storage split by sensitivity — see the plan's "Storage — secrets vs.
// settings" section. Runs only in background.js: it's the one context
// that touches secrets, and chrome.storage.session defaults to
// TRUSTED_CONTEXTS access anyway, so content scripts couldn't read it
// even if they tried.
//
// API keys (LLM provider keys + retrieval provider keys) -> session
// storage: never written to disk, gone on browser restart, survives
// service-worker termination (unlike a plain module-level variable).
//
// LLM keys are stored as a *list* per provider, each entry carrying a
// nickname — e.g. two different OpenRouter keys ("Personal", "Lab
// account") can both be saved at once, and Settings picks which one is
// active per slot (generation/judge) by id, not by provider alone.
//
// Non-secret settings (which provider/model, base URLs, OpenAlex mailto,
// which saved key id is active) -> local storage: persists across
// restarts so only the keys themselves need re-entry.

(() => {
  const SESSION_KEY = "scribe_secrets_v1";
  const LOCAL_KEY = "scribe_settings_v1";

  const DEFAULT_SETTINGS = {
    // When true, Draft/Check run against the local Python service
    // (127.0.0.1:8765, `./my_venv/bin/uvicorn service.app:app --port
    // 8765`, started by the user in a terminal) instead of the in-
    // extension JS pipeline below. Not a fallback the extension picks
    // automatically — an explicit user choice, since it changes which API
    // key/provider setup is actually required.
    useLocalService: false,
    generation: { provider: "openrouter", model: "anthropic/claude-sonnet-4.6", baseUrl: "", keyId: "" },
    judge: { sameAsGeneration: true, provider: "openrouter", model: "anthropic/claude-sonnet-4.6", baseUrl: "", keyId: "" },
    retrieval: { mailto: "" },
  };

  async function getSettings() {
    const stored = await chrome.storage.local.get(LOCAL_KEY);
    const settings = stored[LOCAL_KEY];
    if (!settings) return JSON.parse(JSON.stringify(DEFAULT_SETTINGS));
    return {
      useLocalService: settings.useLocalService ?? DEFAULT_SETTINGS.useLocalService,
      generation: { ...DEFAULT_SETTINGS.generation, ...settings.generation },
      judge: { ...DEFAULT_SETTINGS.judge, ...settings.judge },
      retrieval: { ...DEFAULT_SETTINGS.retrieval, ...settings.retrieval },
    };
  }

  async function saveSettings(settings) {
    await chrome.storage.local.set({ [LOCAL_KEY]: settings });
  }

  async function getSecrets() {
    const stored = await chrome.storage.session.get(SESSION_KEY);
    return stored[SESSION_KEY] || { llmKeys: {}, retrievalKeys: {} };
  }

  async function saveSecrets(secrets) {
    await chrome.storage.session.set({ [SESSION_KEY]: secrets });
  }

  function maskKey(value) {
    if (!value) return "";
    if (value.length <= 8) return "*".repeat(value.length);
    return `${value.slice(0, 5)}...${value.slice(-4)}`;
  }

  function newId() {
    return (crypto.randomUUID && crypto.randomUUID())
      || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  // ── LLM keys: list per provider, each { id, nickname, apiKey } ──

  async function listLlmKeys(providerId) {
    const secrets = await getSecrets();
    const list = (secrets.llmKeys || {})[providerId] || [];
    return list.map(({ id, nickname, apiKey }) => ({ id, nickname, maskedKey: maskKey(apiKey) }));
  }

  async function saveLlmKey(providerId, { id, nickname, apiKey }) {
    const secrets = await getSecrets();
    const list = [...((secrets.llmKeys || {})[providerId] || [])];
    const keyId = id || newId();
    const idx = list.findIndex((k) => k.id === keyId);
    const entry = { id: keyId, nickname: (nickname || "").trim() || "Key", apiKey };
    if (idx >= 0) list[idx] = entry; else list.push(entry);
    secrets.llmKeys = { ...(secrets.llmKeys || {}), [providerId]: list };
    await saveSecrets(secrets);
    return keyId;
  }

  async function deleteLlmKey(providerId, keyId) {
    const secrets = await getSecrets();
    const list = ((secrets.llmKeys || {})[providerId] || []).filter((k) => k.id !== keyId);
    secrets.llmKeys = { ...(secrets.llmKeys || {}), [providerId]: list };
    await saveSecrets(secrets);
  }

  async function getLlmKeyValue(providerId, keyId) {
    if (!keyId) return "";
    const secrets = await getSecrets();
    const list = (secrets.llmKeys || {})[providerId] || [];
    const found = list.find((k) => k.id === keyId);
    return found ? found.apiKey : "";
  }

  // ── Retrieval keys: single value per id ("s2" | "openalex") — not a
  // list, since these raise a rate limit rather than being "which
  // credential answers this call" the way LLM keys are. ──

  async function getRetrievalKey(id) {
    const secrets = await getSecrets();
    return (secrets.retrievalKeys || {})[id] || "";
  }

  async function saveRetrievalKey(id, value) {
    const secrets = await getSecrets();
    secrets.retrievalKeys = { ...(secrets.retrievalKeys || {}), [id]: value };
    await saveSecrets(secrets);
  }

  // Returns settings plus, per provider, the list of saved keys (id +
  // nickname + masked value only — raw keys never reach content.js).
  async function getSettingsForUi() {
    const settings = await getSettings();
    const secrets = await getSecrets();
    const llmKeys = {};
    for (const p of self.ScribeProviders.PROVIDERS) {
      llmKeys[p.id] = await listLlmKeys(p.id);
    }
    const retrievalKeyStatus = {
      s2: { hasKey: !!(secrets.retrievalKeys || {}).s2, maskedKey: maskKey((secrets.retrievalKeys || {}).s2) },
      openalex: { hasKey: !!(secrets.retrievalKeys || {}).openalex, maskedKey: maskKey((secrets.retrievalKeys || {}).openalex) },
    };
    return { settings, llmKeys, retrievalKeyStatus };
  }

  self.ScribeSettingsStore = {
    getSettings, saveSettings,
    listLlmKeys, saveLlmKey, deleteLlmKey, getLlmKeyValue,
    getRetrievalKey, saveRetrievalKey,
    getSettingsForUi, maskKey,
  };
})();
