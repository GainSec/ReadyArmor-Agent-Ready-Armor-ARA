// Force every undici fetch in the host process to honor HTTPS_PROXY /
// HTTP_PROXY / NO_PROXY env vars by installing a global EnvHttpProxyAgent
// dispatcher. Node's built-in fetch (undici) does NOT honor proxy env
// vars by default — clients have to opt in by passing `dispatcher` per
// request. Setting it globally covers every code path, including
// nested deps like @anthropic-ai/sdk/pi-ai used by openclaw's litellm
// provider for chat completions.
//
// Loaded via `NODE_OPTIONS=--import=…` from ARA's launch.py when the
// contained worker is openclaw or any other Node-based agent runtime
// that uses undici for outbound HTTPS.
//
// Why we resolve undici from a configurable path: bare `import 'undici'`
// fails because plain Node only ships undici as an internal symbol, not
// a public package. ARA passes `ARA_UNDICI_MODULE_PATH` as the absolute
// path to the host runtime's bundled undici (e.g. openclaw's). When
// unset, we no-op — better silent than crash.
import process from "node:process";
import { createRequire } from "node:module";

function main() {
  const undiciPath = process.env.ARA_UNDICI_MODULE_PATH;
  if (!undiciPath) return;
  if (!process.env.HTTPS_PROXY && !process.env.HTTP_PROXY) return;
  try {
    // createRequire resolves CommonJS-style directory paths the way
    // ES `import` does not; openclaw's bundled undici only ships CJS.
    const require = createRequire(import.meta.url);
    const undici = require(undiciPath);
    if (undici.setGlobalDispatcher && undici.EnvHttpProxyAgent && undici.fetch) {
      undici.setGlobalDispatcher(new undici.EnvHttpProxyAgent());
      // Node 22's `globalThis.fetch` is wired to its INTERNAL undici
      // instance — separate from any user-installed `undici` package.
      // setGlobalDispatcher above only affects this loaded undici copy,
      // not Node's internal one. So any caller of `globalThis.fetch`
      // (Anthropic SDK, pi-ai, OpenAI SDK, etc.) bypasses our proxy.
      // Override globalThis.fetch with the proxy-aware fetch from the
      // undici copy whose dispatcher we just patched. Captures-by-ref
      // taken AFTER this preload (which runs first via NODE_OPTIONS
      // --import) pick up the override.
      globalThis.fetch = undici.fetch;
      process.stderr.write(
        `[ara] undici proxy preload: dispatcher=EnvHttpProxyAgent + ` +
        `globalThis.fetch override  ` +
        `https_proxy=${process.env.HTTPS_PROXY || "(unset)"}\n`,
      );
    }
  } catch (err) {
    process.stderr.write(
      `[ara] undici proxy preload failed: ${err && err.message || err}\n`,
    );
  }
}

main();
