// Provider-agnostic chat-completion adapter. One function, two shape
// branches — see ~/.claude/plans/zazzy-jumping-swan.md's "Provider-agnostic
// LLM layer" section for why this isn't hardcoded to one vendor.

(() => {
  async function callOpenAIShape({ baseUrl, apiKey, model, messages, maxTokens, jsonMode, timeoutMs }) {
    const body = { model, messages };
    if (maxTokens) body.max_tokens = maxTokens;
    if (jsonMode) body.response_format = { type: "json_object" };

    const resp = await fetch(`${baseUrl.replace(/\/$/, "")}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`provider returned ${resp.status}: ${text.slice(0, 300)}`);
    }
    const data = await resp.json();
    const content = data?.choices?.[0]?.message?.content;
    if (content == null) throw new Error("provider response had no message content");
    // OpenAI (and most OpenRouter backends) cache a stable prompt prefix
    // automatically — no request-side opt-in needed — and report it back
    // as usage.prompt_tokens_details.cached_tokens. Passed through
    // unchanged so a caller can log/verify it; nothing else to do here.
    return { text: content, usage: data?.usage || null };
  }

  // cacheSystem: true marks the system prompt as an Anthropic
  // cache_control breakpoint — sent as a content-block array with
  // { type: "ephemeral" } on the block, per Anthropic's prompt-caching
  // API, instead of a plain string. Anthropic (unlike OpenAI) requires
  // this explicit opt-in; there is no equivalent for the OpenAI shape.
  async function callAnthropicShape({ baseUrl, apiKey, model, messages, maxTokens, timeoutMs, cacheSystem }) {
    let system;
    const converted = [];
    for (const m of messages) {
      if (m.role === "system") {
        system = system ? `${system}\n${m.content}` : m.content;
      } else {
        converted.push({ role: m.role === "assistant" ? "assistant" : "user", content: m.content });
      }
    }
    const body = {
      model,
      max_tokens: maxTokens || 8192,
      messages: converted,
    };
    if (system) {
      body.system = cacheSystem
        ? [{ type: "text", text: system, cache_control: { type: "ephemeral" } }]
        : system;
    }

    const resp = await fetch(`${baseUrl.replace(/\/$/, "")}/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`provider returned ${resp.status}: ${text.slice(0, 300)}`);
    }
    const data = await resp.json();
    const block = (data?.content || []).find((c) => c.type === "text");
    if (!block) throw new Error("provider response had no text content block");
    // data.usage carries cache_creation_input_tokens / cache_read_input_tokens
    // when cacheSystem was set — a caller can check cache_read_input_tokens
    // > 0 on the second+ call in a job to confirm caching actually took
    // effect, rather than trusting that sending cache_control worked.
    return { text: block.text, usage: data?.usage || null };
  }

  // { provider: providerId, apiKey, baseUrl, model, messages, maxTokens,
  //   jsonMode, cacheSystem } -> Promise<{ text, usage }>.
  //
  // jsonMode requests response_format:{type:"json_object"} on OpenAI-shaped
  // providers only — Anthropic has no equivalent, so callers must extract
  // JSON from the raw text regardless (evaluator.js already does this,
  // mirroring how pipeline/evaluator.py's _call_judge always regexes
  // `\{.*\}` out of the response rather than trusting response_format
  // alone).
  async function chatComplete({ provider, apiKey, baseUrl, model, messages, maxTokens, jsonMode, cacheSystem, timeoutMs = 180000 }) {
    if (!apiKey) throw new Error("no API key configured for this provider");
    if (!model) throw new Error("no model configured for this provider");
    const meta = self.ScribeProviders.getProvider(provider);
    const shape = (meta && meta.shape) || "openai";
    const url = baseUrl || (meta && meta.baseUrl);
    if (!url) throw new Error(`no base URL for provider "${provider}"`);

    if (shape === "anthropic") {
      return callAnthropicShape({ baseUrl: url, apiKey, model, messages, maxTokens, timeoutMs, cacheSystem });
    }
    return callOpenAIShape({ baseUrl: url, apiKey, model, messages, maxTokens, jsonMode, timeoutMs });
  }

  self.ScribeLLM = { chatComplete };
})();
