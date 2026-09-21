// Built-in provider presets. Each entry: { id, label, baseUrl, shape }.
// "shape" selects the request/response branch in llmProviders.js.
// "custom" has no fixed baseUrl — the user supplies one at settings time;
// it reuses the "openai" shape, since that's what self-hosted/alternate
// OpenAI-compatible endpoints (Groq, Together, local Ollama, etc.) speak.

(() => {
  const PROVIDERS = [
    {
      id: "openrouter",
      label: "OpenRouter",
      baseUrl: "https://openrouter.ai/api/v1",
      shape: "openai",
      keyHelpUrl: "https://openrouter.ai/settings/keys",
      presetModels: ["anthropic/claude-sonnet-4.6", "openai/gpt-5", "google/gemini-3-pro"],
    },
    {
      id: "openai",
      label: "OpenAI",
      baseUrl: "https://api.openai.com/v1",
      shape: "openai",
      keyHelpUrl: "https://platform.openai.com/api-keys",
      presetModels: ["gpt-5", "gpt-5-mini"],
    },
    {
      id: "anthropic",
      label: "Anthropic",
      baseUrl: "https://api.anthropic.com/v1",
      shape: "anthropic",
      keyHelpUrl: "https://console.anthropic.com/settings/keys",
      presetModels: ["claude-sonnet-4-6", "claude-opus-4-6"],
    },
    {
      id: "custom",
      label: "Custom (OpenAI-compatible)",
      baseUrl: "",
      shape: "openai",
      keyHelpUrl: "",
      presetModels: [],
    },
  ];

  function getProvider(id) {
    return PROVIDERS.find((p) => p.id === id) || null;
  }

  self.ScribeProviders = { PROVIDERS, getProvider };
})();
