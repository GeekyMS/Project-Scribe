// Provider-agnostic chat-completion adapter. One function, two shape
// branches — see ~/.claude/plans/zazzy-jumping-swan.md's "Provider-agnostic
// LLM layer" section for why this isn't hardcoded to one vendor.

(() => {
  async function callOpenAIShape({ baseUrl, apiKey, model, messages, maxTokens, jsonMode }) {
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
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`provider returned ${resp.status}: ${text.slice(0, 300)}`);
    }
    const data = await resp.json();
    const content = data?.choices?.[0]?.message?.content;
    if (content == null) throw new Error("provider response had no message content");
    return content;
  }

  async function callAnthropicShape({ baseUrl, apiKey, model, messages, maxTokens }) {
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
    if (system) body.system = system;

    const resp = await fetch(`${baseUrl.replace(/\/$/, "")}/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`provider returned ${resp.status}: ${text.slice(0, 300)}`);
    }
    const data = await resp.json();
    const block = (data?.content || []).find((c) => c.type === "text");
    if (!block) throw new Error("provider response had no text content block");
    return block.text;
  }

  // { provider: providerId, apiKey, baseUrl, model, messages, maxTokens,
  //   jsonMode } -> Promise<string> (the raw completion text).
  //
  // jsonMode requests response_format:{type:"json_object"} on OpenAI-shaped
  // providers only — Anthropic has no equivalent, so callers must extract
  // JSON from the raw text regardless (evaluator.js already does this,
  // mirroring how pipeline/evaluator.py's _call_judge always regexes
  // `\{.*\}` out of the response rather than trusting response_format
  // alone).
  async function chatComplete({ provider, apiKey, baseUrl, model, messages, maxTokens, jsonMode }) {
    if (!apiKey) throw new Error("no API key configured for this provider");
    if (!model) throw new Error("no model configured for this provider");
    const meta = self.ScribeProviders.getProvider(provider);
    const shape = (meta && meta.shape) || "openai";
    const url = baseUrl || (meta && meta.baseUrl);
    if (!url) throw new Error(`no base URL for provider "${provider}"`);

    if (shape === "anthropic") {
      return callAnthropicShape({ baseUrl: url, apiKey, model, messages, maxTokens });
    }
    return callOpenAIShape({ baseUrl: url, apiKey, model, messages, maxTokens, jsonMode });
  }

  self.ScribeLLM = { chatComplete };
})();
