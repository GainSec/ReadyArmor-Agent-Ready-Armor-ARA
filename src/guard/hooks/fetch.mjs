// Universal fetch tap. Patches globalThis.fetch so every outbound
// request from the worker is observable — covers pi-ai's codex provider
// (chatgpt.com/backend-api), Anthropic SDK, raw OpenAI calls, and anything
// else that uses the Node 18+ built-in fetch.
//
// Strategy:
//   1. On request: clone-and-read the body, emit "request" to the
//      transcript channel, forward original payload upstream.
//   2. On response: tee the response stream so the caller still gets a
//      live stream while we accumulate a copy and emit a "response"
//      transcript line when the stream closes.

import { intercept, emit } from "../transcript_client.mjs";

const LLM_HOST_RE = /(chatgpt\.com|api\.openai\.com|api\.anthropic\.com|api\.x\.ai|generativelanguage\.googleapis\.com)/i;
const PROVIDER_FROM_HOST = {
  "chatgpt.com": "codex",
  "api.openai.com": "openai",
  "api.anthropic.com": "anthropic",
  "api.x.ai": "xai",
  "generativelanguage.googleapis.com": "google",
};

function inferProvider(urlStr) {
  try {
    const u = new URL(urlStr);
    for (const [host, provider] of Object.entries(PROVIDER_FROM_HOST)) {
      if (u.hostname.endsWith(host)) return provider;
    }
  } catch { /* ignore */ }
  return "unknown";
}

async function captureRequest(input, init) {
  const url = typeof input === "string" || input instanceof URL
    ? String(input)
    : input?.url;
  if (!url || !LLM_HOST_RE.test(url)) return null;
  const provider = inferProvider(url);
  const method = (init?.method || (input?.method) || "GET").toUpperCase();
  let body = null;
  try {
    if (init?.body) {
      if (typeof init.body === "string") body = JSON.parse(init.body);
      else if (init.body && typeof init.body.text === "function") {
        body = JSON.parse(await init.body.text());
      }
    } else if (input?.body && typeof input.clone === "function") {
      body = await input.clone().json().catch(() => null);
    }
  } catch { /* non-JSON body, leave null */ }
  return { url, method, provider, body };
}

function _teeStream(originalBody) {
  // Returns [forwardedStream, accumulatedPromise] using ReadableStream.tee()
  // when available; falls back to passthrough when the body isn't a stream.
  if (!originalBody || typeof originalBody.tee !== "function") {
    return [originalBody, Promise.resolve(null)];
  }
  const [forward, mirror] = originalBody.tee();
  const acc = (async () => {
    const reader = mirror.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.byteLength;
      // Cap to keep memory bounded; ~512KB per request is plenty for a
      // single LLM turn even with reasoning blocks.
      if (total > 512 * 1024) break;
    }
    const buf = new Uint8Array(total);
    let off = 0;
    for (const c of chunks) { buf.set(c, off); off += c.byteLength; }
    return new TextDecoder("utf-8", { fatal: false }).decode(buf);
  })();
  return [forward, acc];
}

export function installFetchHook(guard) {
  // Bypass the loader-wrapped require by going through the global object;
  // globalThis.fetch is the singleton everyone uses.
  if (typeof globalThis.fetch !== "function") return;
  if (globalThis.fetch.__ara_wrapped) return;

  const origFetch = globalThis.fetch.bind(globalThis);

  const patched = async function ara_fetch(input, init) {
    let captured = null;
    try {
      captured = await captureRequest(input, init);
    } catch { /* never break the call */ }

    if (captured) {
      try {
        // Synchronous-ish intercept: send to supervisor, get a decision.
        // For now any non-allow decision falls back to allow because the
        // rule engine isn't wired to deny LLM bytes-on-wire yet — the
        // hook structure is here so we can layer it on without changes
        // to OpenClaw / pi-ai.
        await intercept("request", {
          url: captured.url,
          method: captured.method,
          body: captured.body,
        }, { model: captured.provider });
      } catch { /* drop */ }
    }

    const resp = await origFetch(input, init);

    if (captured && resp && resp.body) {
      try {
        const [forwardBody, accPromise] = _teeStream(resp.body);
        const newResp = new Response(forwardBody, {
          status: resp.status,
          statusText: resp.statusText,
          headers: resp.headers,
        });
        accPromise.then((text) => {
          if (text == null) return;
          emit("response", {
            url: captured.url,
            status: resp.status,
            body_text: text.length > 64_000 ? text.slice(0, 64_000) + "…" : text,
          }, { model: captured.provider });
        }).catch(() => {});
        return newResp;
      } catch {
        // If tee fails (rare; non-Node platforms), fall back to opaque.
      }
    }
    return resp;
  };
  patched.__ara_wrapped = true;
  globalThis.fetch = patched;
}
