// ESM loader: intercepts resolve() / load() so that named imports of
// node:child_process, node:fs, node:fs/promises, and node:net are
// transparently replaced with wrapped versions.
//
// This closes the ESM named-import gap — reassigning `fs.readFile = ...` in
// the main thread does NOT affect modules that hold a live binding from
// `import { readFile } from "node:fs"`. A loader intercepts resolution
// itself and returns a generated wrapper module so every consumer, however
// they import, gets the wrapped symbols.
//
// Runs in the loader thread; generated source is evaluated on the main
// thread where globalThis.__ARA_GUARD__ is available.

const BYPASS_PREFIX = "ara-bypass:";
const WRAP_PREFIX = "ara-wrap:";

const WRAP_TARGETS = new Set([
  "node:child_process",
  "node:fs",
  "node:fs/promises",
  "node:net",
  "node:http",
  "node:https",
]);

// Path-suffix wraps. After a specifier resolves to a file URL, if the
// path ends with one of these we wrap it. Used for OpenClaw (and similar
// agents) where the modules we want to tap aren't bare specifiers.
const WRAP_SUFFIXES = [
  // OpenClaw codex client: every outbound JSON-RPC message to the codex
  // subprocess passes through writeMessage() here. Raw frames in / out.
  "/extensions/codex/src/app-server/client.js",
  // Higher-level: parsed conversation events (assistant text, tool_use,
  // reasoning blocks). Cleaner data, same hook style.
  "/extensions/codex/src/app-server/event-projector.js",
];

// Pass this as context via import attributes when registering so the loader
// can locate the wrappers module URL on the main thread.
let WRAPPERS_URL = null;

export async function initialize(data) {
  if (data && typeof data === "object" && data.wrappersUrl) {
    WRAPPERS_URL = data.wrappersUrl;
  }
}

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith(BYPASS_PREFIX)) {
    return nextResolve(specifier.slice(BYPASS_PREFIX.length), context);
  }
  if (WRAP_TARGETS.has(specifier)) {
    return {
      url: `${WRAP_PREFIX}${specifier}`,
      shortCircuit: true,
      format: "module",
    };
  }
  // Path-suffix wraps: resolve through the host first, then check the URL.
  // Only bother probing when the specifier *might* match a suffix — cheap
  // pre-filter so we don't turn every resolve into an extra round-trip and
  // don't swallow the host's own ERR_MODULE_NOT_FOUND for genuinely missing
  // imports.
  if (!WRAP_SUFFIXES.length) return nextResolve(specifier, context);
  const hint = WRAP_SUFFIXES.some((s) => specifier.endsWith(s));
  if (!hint) return nextResolve(specifier, context);

  let next;
  try {
    next = await nextResolve(specifier, context);
  } catch (e) {
    throw e;
  }
  if (next?.url && WRAP_SUFFIXES.some((s) => next.url.endsWith(s))) {
    return {
      url: `${WRAP_PREFIX}${next.url}`,
      shortCircuit: true,
      format: next.format || "module",
    };
  }
  return next;
}

export async function load(url, context, nextLoad) {
  if (url.startsWith(WRAP_PREFIX)) {
    const spec = url.slice(WRAP_PREFIX.length);
    const source = await buildWrapperSource(spec);
    return { format: "module", source, shortCircuit: true };
  }
  return nextLoad(url, context);
}

async function buildWrapperSource(spec) {
  // Enumerate the original module's keys by importing via the bypass URL.
  // The dynamic import goes back through this same loader; resolve() sees
  // the bypass prefix and delegates to nextResolve, so we get the real
  // module object without re-intercepting.
  const original = await import(`${BYPASS_PREFIX}${spec}`);
  const keys = Object.keys(original).filter((k) => k !== "default" && isValidIdent(k));
  const wrapFn = wrapperFunctionName(spec);
  const wrappersUrl = WRAPPERS_URL;
  if (!wrappersUrl) {
    throw new Error("ARA loader: wrappersUrl not initialized");
  }

  let src = "";
  src += `import * as __orig from ${JSON.stringify(`${BYPASS_PREFIX}${spec}`)};\n`;
  src += `import { ${wrapFn} as __wrap } from ${JSON.stringify(wrappersUrl)};\n`;
  src += `const __guard = globalThis.__ARA_GUARD__;\n`;
  src += `const __w = __wrap(__orig, __guard);\n`;
  for (const key of keys) {
    // Prefer wrapped value when present; fall back to the original.
    src += `export const ${key} = (__w && Object.hasOwn(__w, ${JSON.stringify(
      key,
    )})) ? __w[${JSON.stringify(key)}] : __orig[${JSON.stringify(key)}];\n`;
  }
  // Default export: merge the original namespace with our wrapper so
  // `import fs from "node:fs"` yields the full module with overrides,
  // not just the handful of keys we patch.
  src += `const __default = { ...__orig, ...(__w || {}) };\n`;
  src += `export default (__w && Object.hasOwn(__w, "default")) ? __w.default : __default;\n`;

  return src;
}

function isValidIdent(name) {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name);
}

function wrapperFunctionName(spec) {
  // For named modules: "node:fs/promises" -> "wrap_node_fs_promises".
  // For path-suffix wraps (file URLs), derive the name from the matching
  // suffix so it stays stable across host paths and OpenClaw versions.
  if (spec.startsWith("file://")) {
    for (const sfx of WRAP_SUFFIXES) {
      if (spec.endsWith(sfx)) {
        return "wrap_" + sfx.replace(/.*\//, "").replace(/[^A-Za-z0-9]/g, "_");
      }
    }
  }
  return "wrap_" + spec.replace(/[^A-Za-z0-9]/g, "_");
}
