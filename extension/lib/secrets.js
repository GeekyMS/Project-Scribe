// Redaction helper — applied to anything rendered in the debug panel or
// status line, since those are plausible exposure points once API keys
// exist (screen-shared demos, the visible <pre> in buildDebugTools).
// content.js never holds a key itself (background.js owns all secrets),
// but error strings/response bodies passed back from background.js could
// still echo one, so this matches on shape rather than relying on the
// caller to know which strings are sensitive.

(() => {
  const PATTERNS = [
    /\bsk-or-v1-[A-Za-z0-9]{16,}\b/g,       // OpenRouter
    /\bsk-ant-[A-Za-z0-9\-_]{16,}\b/g,       // Anthropic
    /\bsk-[A-Za-z0-9]{16,}\b/g,              // OpenAI-style
    /\bBearer\s+[A-Za-z0-9\-_.]{16,}/gi,     // Authorization headers
    /"(api_?key|apiKey|authorization)"\s*:\s*"[^"]{6,}"/gi, // JSON fields
  ];

  function redactSecrets(text, extraSecrets = []) {
    if (text == null) return text;
    let out = String(text);
    for (const secret of extraSecrets) {
      if (!secret) continue;
      out = out.split(secret).join("[REDACTED]");
    }
    for (const re of PATTERNS) {
      out = out.replace(re, (match) => {
        if (match.includes(":")) {
          // JSON field match — keep the key name, redact the value.
          return match.replace(/:\s*"[^"]*"/, ': "[REDACTED]"');
        }
        return "[REDACTED]";
      });
    }
    return out;
  }

  const target = typeof window !== "undefined" ? window : self;
  target.ScribeSecrets = { redactSecrets };
})();
