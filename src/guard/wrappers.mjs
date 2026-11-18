// Wrapper factories. Each `wrap_*` function takes the original module
// namespace and the guard instance, returns an object whose keys override
// the named exports the loader will expose.
//
// Runs on the main thread. `guard.ipc` is live; `guard.localRules` is the
// cache pushed by the supervisor on hello (used for sync-path decisions).

import { PassThrough } from "node:stream";
import { EventEmitter } from "node:events";
import path from "node:path";

function absolutize(p) {
  if (typeof p !== "string") {
    try {
      return String(p);
    } catch {
      return "";
    }
  }
  return path.resolve(p);
}

function denyError(reason, target) {
  const err = new Error(`ARA_DENY: ${reason} (${target})`);
  err.code = reason || "ARA_DENY";
  err.path = target;
  return err;
}

async function asyncQuery(guard, op, extra) {
  // Fast path: evaluate locally against the rule cache pushed by the
  // supervisor on hello. The supervisor is still the authority — it owns
  // the rules — but a round-trip per fs call is prohibitive for agents
  // that do hundreds of config reads per turn.
  //
  // Local path for path-op queries with an IPC escape hatch for approvals.
  if (op === "fs.read" || op === "fs.readdir") {
    const target = extra?.path ?? "";
    const denyHit = guard?.localRules?.matchPath(target);
    if (denyHit) {
      try {
        guard?.ipc?.send({
          type: "event", kind: "policy_hit", op, path: target,
          pid: process.pid, verdict: "deny",
          reason: "DENY_FILE_PATH_BLOCKED", rule_id: denyHit.rule_id,
        });
      } catch { /* best-effort */ }
      return { verdict: "deny", reason: "DENY_FILE_PATH_BLOCKED", rule_id: denyHit.rule_id };
    }
    // Approval match: round-trip so the supervisor can block on operator
    // response. Approvals are rare; the IPC cost is justified.
    const approvalHit = guard?.localRules?.matchApprovalPath?.(target);
    if (approvalHit) {
      try {
        return await guard.ipc.request({
          type: "query", op, pid: process.pid, ...extra,
        }, { timeoutMs: 150000 });
      } catch {
        return { verdict: "deny", reason: "ARA_IPC_FAILURE" };
      }
    }
    try {
      guard?.ipc?.send({
        type: "event", kind: "policy_hit", op, path: target,
        pid: process.pid, verdict: "allow",
        reason: null, rule_id: null,
      });
    } catch { /* best-effort */ }
    return { verdict: "allow" };
  }
  // Command-op queries always round-trip to the supervisor. Unlike fs
  // ops (hundreds per turn; local eval matters), spawn is infrequent and
  // the supervisor does extra work the guard can't: shell preflight via
  // mvdan/sh to catch runtime-expanded paths (cd && ls, variable
  // indirection, aliases, functions, eval, etc.).
  if (op === "exec.spawn") {
    if (!guard || !guard.ipc) return { verdict: "deny", reason: "ARA_NO_GUARD" };
    try {
      // cwd defaults to our process's cwd when the spawn options don't set one.
      const cwd = (extra && extra.cwd) || (typeof process !== "undefined" ? process.cwd() : null);
      return await guard.ipc.request({
        type: "query",
        op,
        pid: process.pid,
        ...extra,
        cwd,
      });
    } catch {
      return { verdict: "deny", reason: "ARA_IPC_FAILURE" };
    }
  }
  // Unknown ops: fall back to the supervisor for authoritative decisions
  // (deferred features like approval prompts etc. will live there).
  if (!guard || !guard.ipc) return { verdict: "deny", reason: "ARA_NO_GUARD" };
  try {
    return await guard.ipc.request({ type: "query", op, pid: process.pid, ...extra });
  } catch {
    return { verdict: "deny", reason: "ARA_IPC_FAILURE" };
  }
}

function syncPathDeny(guard, op, target) {
  const hit = guard?.localRules?.matchPath(target);
  if (!hit) return null;
  try {
    guard.ipc.send({
      type: "event",
      kind: "policy_hit_sync",
      op,
      path: target,
      verdict: "deny",
      reason: "DENY_FILE_PATH_BLOCKED",
      rule_id: hit.rule_id,
      pid: process.pid,
    });
  } catch {
    /* best-effort */
  }
  return hit;
}

function syncCommandDeny(guard, command, args) {
  const cmdArgs = (args || []).map((a) => String(a));
  const hit = guard?.localRules?.matchCommand(String(command), cmdArgs);
  if (!hit) return null;
  try {
    guard.ipc.send({
      type: "event",
      kind: "policy_hit_sync",
      op: "exec.spawn",
      command: String(command),
      args: cmdArgs,
      verdict: "deny",
      reason: "DENY_COMMAND_BLOCKED",
      rule_id: hit.rule_id,
      pid: process.pid,
    });
  } catch {
    /* best-effort */
  }
  return hit;
}

// ---------------- node:fs ----------------

export function wrap_node_fs(orig, guard) {
  const w = {};

  w.readFile = function patchedReadFile(pathArg, ...rest) {
    const cb = rest[rest.length - 1];
    const callback = typeof cb === "function" ? cb : null;
    const target = absolutize(pathArg);
    (async () => {
      const d = await asyncQuery(guard, "fs.read", { path: target });
      if (d.verdict !== "allow") {
        const err = denyError(d.reason, target);
        if (callback) callback(err);
        return;
      }
      return orig.readFile(pathArg, ...rest);
    })();
  };

  w.readFileSync = function patchedReadFileSync(pathArg, ...rest) {
    const target = absolutize(pathArg);
    if (syncPathDeny(guard, "fs.read", target)) {
      throw denyError("DENY_FILE_PATH_BLOCKED", target);
    }
    return orig.readFileSync(pathArg, ...rest);
  };

  w.readdir = function patchedReaddir(pathArg, ...rest) {
    const cb = rest[rest.length - 1];
    const callback = typeof cb === "function" ? cb : null;
    const target = absolutize(pathArg);
    (async () => {
      const d = await asyncQuery(guard, "fs.readdir", { path: target });
      if (d.verdict !== "allow") {
        const err = denyError(d.reason, target);
        if (callback) callback(err);
        return;
      }
      return orig.readdir(pathArg, ...rest);
    })();
  };

  w.readdirSync = function patchedReaddirSync(pathArg, ...rest) {
    const target = absolutize(pathArg);
    if (syncPathDeny(guard, "fs.readdir", target)) {
      throw denyError("DENY_FILE_PATH_BLOCKED", target);
    }
    return orig.readdirSync(pathArg, ...rest);
  };

  w.opendir = function patchedOpenDir(pathArg, ...rest) {
    const cb = rest[rest.length - 1];
    const callback = typeof cb === "function" ? cb : null;
    const target = absolutize(pathArg);
    (async () => {
      const d = await asyncQuery(guard, "fs.readdir", { path: target });
      if (d.verdict !== "allow") {
        const err = denyError(d.reason, target);
        if (callback) callback(err);
        return;
      }
      return orig.opendir(pathArg, ...rest);
    })();
  };

  w.opendirSync = function patchedOpenDirSync(pathArg, ...rest) {
    const target = absolutize(pathArg);
    if (syncPathDeny(guard, "fs.readdir", target)) {
      throw denyError("DENY_FILE_PATH_BLOCKED", target);
    }
    return orig.opendirSync(pathArg, ...rest);
  };

  // Carry forward the promises namespace, merging original + patches so
  // consumers accessing fs.promises.stat / fs.promises.writeFile etc. still
  // see the full API, with our wrappers overriding only where present.
  if (orig.promises) {
    w.promises = {
      ...orig.promises,
      ...wrap_node_fs_promises(orig.promises, guard),
    };
  }

  return w;
}

// ---------------- node:fs/promises ----------------

export function wrap_node_fs_promises(orig, guard) {
  const w = {};

  w.readFile = async function patchedPromiseReadFile(pathArg, options) {
    const target = absolutize(pathArg);
    const d = await asyncQuery(guard, "fs.read", { path: target });
    if (d.verdict !== "allow") throw denyError(d.reason, target);
    return orig.readFile(pathArg, options);
  };

  w.readdir = async function patchedPromiseReaddir(pathArg, options) {
    const target = absolutize(pathArg);
    const d = await asyncQuery(guard, "fs.readdir", { path: target });
    if (d.verdict !== "allow") throw denyError(d.reason, target);
    return orig.readdir(pathArg, options);
  };

  if (orig.opendir) {
    w.opendir = async function patchedPromiseOpenDir(pathArg, options) {
      const target = absolutize(pathArg);
      const d = await asyncQuery(guard, "fs.readdir", { path: target });
      if (d.verdict !== "allow") throw denyError(d.reason, target);
      return orig.opendir(pathArg, options);
    };
  }

  return w;
}

// ---------------- node:child_process ----------------

function ensureImportFlag(existing, guardPath) {
  const importFlag = `--import=${guardPath}`;
  if (!existing) return importFlag;
  if (existing.includes(importFlag)) return existing;
  return `${importFlag} ${existing}`;
}

function rewriteOptions(options, guard) {
  const opts = options && typeof options === "object" ? { ...options } : {};
  const srcEnv = opts.env && typeof opts.env === "object" ? opts.env : process.env;
  const env = { ...srcEnv };
  env.NODE_OPTIONS = ensureImportFlag(env.NODE_OPTIONS, guard.guardPath);
  env.ARA_SOCKET = guard.socket;
  env.ARA_SESSION_TOKEN = guard.token;
  env.ARA_SESSION_ID = guard.session;
  opts.env = env;
  return opts;
}

function logSpawnEvent(guard, command, args) {
  try {
    guard.ipc.send({
      type: "event",
      kind: "spawn",
      command: String(command ?? ""),
      args: Array.isArray(args) ? args.map((a) => String(a)) : [],
      pid: process.pid,
    });
  } catch {
    /* best-effort */
  }
}

const TARGET_URL_RE = /https?:\/\/[^\s'"<>]+/;
const TARGET_OUTPUT_MAX_BYTES = 256 * 1024;

function firstTargetUrl(command, args) {
  const parts = [String(command ?? "")];
  if (Array.isArray(args)) {
    for (const arg of args) parts.push(String(arg ?? ""));
  }
  const match = TARGET_URL_RE.exec(parts.join(" "));
  return match ? match[0].replace(/[),.;]+$/g, "") : "";
}

function appendBounded(chunks, state, chunk) {
  let buf;
  try {
    buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
  } catch {
    return;
  }
  if (state.bytes >= TARGET_OUTPUT_MAX_BYTES) {
    state.truncated = true;
    return;
  }
  const remaining = TARGET_OUTPUT_MAX_BYTES - state.bytes;
  if (buf.length > remaining) {
    chunks.push(buf.subarray(0, remaining));
    state.bytes += remaining;
    state.truncated = true;
    return;
  }
  chunks.push(buf);
  state.bytes += buf.length;
}

function bufferText(chunks) {
  try {
    return Buffer.concat(chunks).toString("utf8");
  } catch {
    return "";
  }
}

function emitTargetOutput(guard, payload) {
  try {
    guard.ipc.send(payload);
  } catch {
    /* best-effort */
  }
}

function tapTargetOutput(guard, shim, command, args) {
  const url = firstTargetUrl(command, args);
  if (!url || !shim) return;
  const stdoutChunks = [];
  const stderrChunks = [];
  const stdoutState = { bytes: 0, truncated: false };
  const stderrState = { bytes: 0, truncated: false };
  let emitted = false;
  if (shim.stdout) {
    shim.stdout.on("data", (chunk) => appendBounded(stdoutChunks, stdoutState, chunk));
  }
  if (shim.stderr) {
    shim.stderr.on("data", (chunk) => appendBounded(stderrChunks, stderrState, chunk));
  }
  const finalize = (code, signal) => {
    if (emitted) return;
    emitted = true;
    const stdoutText = bufferText(stdoutChunks);
    const stderrText = bufferText(stderrChunks);
    if (!stdoutText && !stderrText) return;
    emitTargetOutput(guard, {
      type: "event",
      kind: "spawn_output",
      source: "node-child-process",
      command: String(command ?? ""),
      args: Array.isArray(args) ? args.map((a) => String(a)) : [],
      pid: shim.pid,
      url,
      exit_code: code,
      signal,
      stdout_text: stdoutText,
      stderr_text: stderrText,
      stdout_truncated: stdoutState.truncated,
      stderr_truncated: stderrState.truncated,
    });
  };
  shim.on("exit", finalize);
  shim.on("close", finalize);
}

function emitSyncTargetOutput(guard, command, args, result, stdoutOverride = null, threwError = null) {
  const url = firstTargetUrl(command, args);
  if (!url) return;
  const stdout = stdoutOverride ?? (
    result && Object.prototype.hasOwnProperty.call(result, "stdout") ? result.stdout : null
  );
  const stderr = result && Object.prototype.hasOwnProperty.call(result, "stderr")
    ? result.stderr
    : (threwError?.stderr ?? null);
  const stdoutChunks = [];
  const stderrChunks = [];
  const stdoutState = { bytes: 0, truncated: false };
  const stderrState = { bytes: 0, truncated: false };
  if (stdout) appendBounded(stdoutChunks, stdoutState, stdout);
  if (stderr) appendBounded(stderrChunks, stderrState, stderr);
  const stdoutText = bufferText(stdoutChunks);
  const stderrText = bufferText(stderrChunks);
  if (!stdoutText && !stderrText) return;
  emitTargetOutput(guard, {
    type: "event",
    kind: "spawn_output",
    source: "node-child-process-sync",
    command: String(command ?? ""),
    args: Array.isArray(args) ? args.map((a) => String(a)) : [],
    pid: result?.pid,
    url,
    exit_code: result?.status ?? threwError?.status ?? null,
    signal: result?.signal ?? threwError?.signal ?? null,
    stdout_text: stdoutText,
    stderr_text: stderrText,
    stdout_truncated: stdoutState.truncated,
    stderr_truncated: stderrState.truncated,
  });
}

function createDeferredChild() {
  const shim = new EventEmitter();
  shim.pid = undefined;
  shim.exitCode = null;
  shim.signalCode = null;
  shim.killed = false;
  shim.stdin = new PassThrough();
  shim.stdout = new PassThrough();
  shim.stderr = new PassThrough();
  shim.stdio = [shim.stdin, shim.stdout, shim.stderr];

  let real = null;
  const pendingStdinChunks = [];
  let stdinEnded = false;
  shim.stdin.on("data", (c) => pendingStdinChunks.push(c));
  shim.stdin.on("end", () => {
    stdinEnded = true;
  });

  shim.kill = function kill(signal) {
    shim.killed = true;
    if (real && real.kill) return real.kill(signal);
    return true;
  };

  shim._attachReal = function attachReal(realChild) {
    real = realChild;
    shim.pid = realChild.pid;
    if (realChild.stdin) {
      for (const chunk of pendingStdinChunks) realChild.stdin.write(chunk);
      if (stdinEnded) realChild.stdin.end();
      shim.stdin.pipe(realChild.stdin);
    }
    if (realChild.stdout) realChild.stdout.pipe(shim.stdout);
    if (realChild.stderr) realChild.stderr.pipe(shim.stderr);
    realChild.on("error", (err) => shim.emit("error", err));
    realChild.on("exit", (code, signal) => {
      shim.exitCode = code;
      shim.signalCode = signal;
      shim.emit("exit", code, signal);
    });
    realChild.on("close", (code, signal) => shim.emit("close", code, signal));
    realChild.on("disconnect", () => shim.emit("disconnect"));
    realChild.on("message", (msg, handle) => shim.emit("message", msg, handle));
  };

  shim._emitDeny = function emitDeny(decision, cmdForMessage) {
    const reason = (decision && decision.reason) || "ARA_DENY";
    const err = new Error(`ARA_DENY: ${reason} (${cmdForMessage})`);
    err.code = reason;
    err.ruleId = decision?.rule_id;
    shim.exitCode = 1;
    if (shim.listenerCount("error") > 0) shim.emit("error", err);
    shim.emit("exit", 1, null);
    shim.emit("close", 1, null);
    shim.stdout.end();
    shim.stderr.end();
  };

  return shim;
}

export function wrap_node_child_process(orig, guard) {
  const w = {};

  function enforcedAsyncSpawn(origFn, command, args, options, callback) {
    logSpawnEvent(guard, command, args);
    const shim = createDeferredChild();
    tapTargetOutput(guard, shim, command, args);
    (async () => {
      const d = await asyncQuery(guard, "exec.spawn", {
        command: String(command ?? ""),
        args: Array.isArray(args) ? args.map((a) => String(a)) : [],
        cwd: (options && typeof options === "object" && options.cwd)
          ? String(options.cwd)
          : process.cwd(),
      });
      if (d.verdict !== "allow") {
        shim._emitDeny(d, String(command ?? ""));
        if (callback) {
          const err = new Error(
            `ARA_DENY: ${d.reason || "ARA_DENY"} (${command})`,
          );
          err.code = d.reason || "ARA_DENY";
          callback(err, "", "");
        }
        return;
      }
      const rewritten = rewriteOptions(options, guard);
      let real;
      try {
        real = origFn.call(orig, command, args, rewritten);
      } catch (err) {
        shim._emitDeny({ reason: err.code || "SPAWN_FAILED" }, String(command ?? ""));
        if (callback) callback(err, "", "");
        return;
      }
      shim._attachReal(real);
      _tapShimStdio(shim, command, args);
    })();
    return shim;
  }

  w.spawn = function patchedSpawn(command, ...rest) {
    let args = [];
    let options = undefined;
    if (Array.isArray(rest[0])) {
      args = rest[0];
      options = rest[1];
    } else {
      options = rest[0];
    }
    return enforcedAsyncSpawn(orig.spawn, command, args, options, null);
  };

  w.fork = function patchedFork(modulePath, ...rest) {
    let args = [];
    let options = undefined;
    if (Array.isArray(rest[0])) {
      args = rest[0];
      options = rest[1];
    } else {
      options = rest[0];
    }
    return enforcedAsyncSpawn(orig.fork, modulePath, args, options, null);
  };

  w.exec = function patchedExec(command, ...rest) {
    let options = undefined;
    let callback = null;
    if (typeof rest[0] === "function") {
      callback = rest[0];
    } else {
      options = rest[0];
      if (typeof rest[1] === "function") callback = rest[1];
    }
    // `exec(cmdString)` takes a full shell command. Split on first whitespace
    // so the policy engine sees the actual program + args, not one blob.
    const raw = String(command ?? "");
    const idx = raw.search(/\s/);
    const head = idx === -1 ? raw : raw.slice(0, idx);
    const tail = idx === -1 ? "" : raw.slice(idx + 1);
    const parsedArgs = tail ? tail.split(/\s+/).filter(Boolean) : [];
    return enforcedAsyncSpawn(
      (cmd, args, opts) => orig.exec(command, opts),
      head,
      parsedArgs,
      options,
      callback,
    );
  };

  w.execFile = function patchedExecFile(file, ...rest) {
    let args = [];
    let options = undefined;
    let callback = null;
    let i = 0;
    if (Array.isArray(rest[i])) {
      args = rest[i];
      i += 1;
    }
    if (rest[i] && typeof rest[i] === "object" && !Array.isArray(rest[i])) {
      options = rest[i];
      i += 1;
    }
    if (typeof rest[i] === "function") callback = rest[i];
    return enforcedAsyncSpawn(orig.execFile, file, args, options, callback);
  };

  // Sync variants: enforce via local cache, heredity in env.
  w.spawnSync = function patchedSpawnSync(command, ...rest) {
    let args = [];
    let options = undefined;
    if (Array.isArray(rest[0])) {
      args = rest[0];
      options = rest[1];
    } else {
      options = rest[0];
    }
    logSpawnEvent(guard, command, args);
    if (syncCommandDeny(guard, command, args)) {
      const err = denyError("DENY_COMMAND_BLOCKED", command);
      return { pid: -1, status: 1, signal: null, stdout: Buffer.alloc(0), stderr: Buffer.from(String(err.message)), output: [null, Buffer.alloc(0), Buffer.from(String(err.message))], error: err };
    }
    const rewritten = rewriteOptions(options, guard);
    const result = orig.spawnSync(command, args, rewritten);
    emitSyncTargetOutput(guard, command, args, result);
    return result;
  };

  w.execSync = function patchedExecSync(command, options) {
    const raw = String(command ?? "");
    const idx = raw.search(/\s/);
    const head = idx === -1 ? raw : raw.slice(0, idx);
    const tail = idx === -1 ? "" : raw.slice(idx + 1);
    const parsedArgs = tail ? tail.split(/\s+/).filter(Boolean) : [];
    logSpawnEvent(guard, head, parsedArgs);
    if (syncCommandDeny(guard, head, parsedArgs)) {
      throw denyError("DENY_COMMAND_BLOCKED", command);
    }
    const rewritten = rewriteOptions(options, guard);
    try {
      const output = orig.execSync(command, rewritten);
      emitSyncTargetOutput(guard, head, parsedArgs, null, output);
      return output;
    } catch (err) {
      emitSyncTargetOutput(guard, head, parsedArgs, null, null, err);
      throw err;
    }
  };

  w.execFileSync = function patchedExecFileSync(file, ...rest) {
    let args = [];
    let options = undefined;
    if (Array.isArray(rest[0])) {
      args = rest[0];
      options = rest[1];
    } else {
      options = rest[0];
    }
    logSpawnEvent(guard, file, args);
    if (syncCommandDeny(guard, file, args)) {
      throw denyError("DENY_COMMAND_BLOCKED", file);
    }
    const rewritten = rewriteOptions(options, guard);
    try {
      const output = orig.execFileSync(file, args, rewritten);
      emitSyncTargetOutput(guard, file, args, null, output);
      return output;
    } catch (err) {
      emitSyncTargetOutput(guard, file, args, null, null, err);
      throw err;
    }
  };

  return w;
}

// ---------------- node:net / http / https ----------------
//
// The enforcing logic lives in hooks/net.mjs and monkey-patches the REAL
// singletons via ara-bypass imports. That single mutation covers:
//   - CJS `require('node:http')` — same singleton
//   - Default ESM import `import http from 'node:http'` — we merge ...__orig
//     in loader.mjs, so the patched .request propagates
//   - Named ESM import `import { request } from 'node:http'` — bindings are
//     captured at module resolve, so we mirror the patched function here
// These wrap_* functions just pick up the already-patched method off the
// singleton and re-export it, so named imports see the enforcing version.

export function wrap_node_net(orig /* , guard */) {
  // Prototype patching already applied in hooks/net.mjs; nothing to re-export
  // separately because Socket.prototype is the same object both import
  // styles see. Return empty merge.
  return {};
}

function reexportRequest(orig) {
  if (typeof orig.request !== "function") return {};
  // orig.request IS the patched function (set by hooks/net.mjs on the same
  // singleton). Re-expose it so named imports pick up enforcement.
  return { request: orig.request };
}

export function wrap_node_http(orig /* , guard */) {
  return reexportRequest(orig);
}

export function wrap_node_https(orig /* , guard */) {
  return reexportRequest(orig);
}

// ---------------- undici (named fetch import) ----------------
//
// `import { fetch } from "undici"` — bundled by openclaw/pi-ai so their
// LLM calls don't flow through the globalThis.fetch singleton we patch
// in hooks/fetch.mjs. We wrap it here with the same logic so the named
// import path also lands in the transcript.

const _LLM_HOSTS = /(chatgpt\.com|api\.openai\.com|api\.anthropic\.com|api\.x\.ai|generativelanguage\.googleapis\.com)/i;
const _PROVIDER_BY_HOST = {
  "chatgpt.com": "codex",
  "api.openai.com": "openai",
  "api.anthropic.com": "anthropic",
};

function _providerForUrl(u) {
  try {
    const host = new URL(u).hostname;
    for (const [h, p] of Object.entries(_PROVIDER_BY_HOST)) if (host.endsWith(h)) return p;
  } catch {}
  return "unknown";
}

async function _captureReq(input, init) {
  const url = typeof input === "string" || input instanceof URL ? String(input) : input?.url;
  if (!url || !_LLM_HOSTS.test(url)) return null;
  let body = null;
  try {
    if (init?.body) {
      if (typeof init.body === "string") body = JSON.parse(init.body);
      else if (init.body && typeof init.body.text === "function") {
        body = JSON.parse(await init.body.text());
      }
    }
  } catch { /* non-JSON */ }
  return { url, method: (init?.method || "GET").toUpperCase(), body,
           provider: _providerForUrl(url) };
}

function _teeStream(originalBody) {
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
      if (total > 512 * 1024) break;
    }
    const buf = new Uint8Array(total);
    let off = 0;
    for (const c of chunks) { buf.set(c, off); off += c.byteLength; }
    return new TextDecoder("utf-8", { fatal: false }).decode(buf);
  })();
  return [forward, acc];
}

export function wrap_undici(orig /* , guard */) {
  const w = {};
  if (typeof orig.fetch !== "function") return w;
  const origFetch = orig.fetch;

  w.fetch = async function ara_undici_fetch(input, init) {
    let captured = null;
    try { captured = await _captureReq(input, init); } catch {}
    if (captured) {
      try {
        await _intercept("request", {
          url: captured.url, method: captured.method, body: captured.body,
        }, { model: captured.provider });
      } catch { /* drop */ }
    }
    const resp = await origFetch(input, init);
    if (captured && resp && resp.body) {
      try {
        const [forward, accPromise] = _teeStream(resp.body);
        const newResp = new Response(forward, {
          status: resp.status,
          statusText: resp.statusText,
          headers: resp.headers,
        });
        accPromise.then((text) => {
          if (text == null) return;
          _emit("response", {
            url: captured.url,
            status: resp.status,
            body_text: text.length > 64_000 ? text.slice(0, 64_000) + "…" : text,
          }, { model: captured.provider });
        }).catch(() => {});
        return newResp;
      } catch { /* fall through */ }
    }
    return resp;
  };
  return w;
}


// ---------------- OpenClaw codex transport (raw JSON-RPC) ----------------
//
// `client.js` exports the AppServerClient class whose private writeMessage
// method serializes outbound JSON-RPC frames. We monkey-patch the class
// prototype so every send is recorded, and we tap inbound frames via the
// internal line-reader's `line` event.

import { intercept as _intercept, emit as _emit } from "./transcript_client.mjs";

// Stdio-RPC tap list. Any spawned child whose command+args matches one of
// these is tapped: shim.stdin / shim.stdout data events mirror every
// newline-framed JSON message to the transcript channel.
const STDIO_RPC_TAPS = [
  {
    match: (cmd, args) =>
      /(^|\/)codex$/.test(String(cmd || "")) && (args || [])[0] === "app-server",
    model: "codex",
  },
];

function _tapShimStdio(shim, command, args) {
  if (!shim) return;
  const match = STDIO_RPC_TAPS.find((t) => t.match(command, args));
  if (process.env.ARA_HOOK_DEBUG) {
    process.stderr.write(
      `[ara-hook/wrap] tap? cmd=${command} args0=${(args||[])[0]} match=${!!match}\n`,
    );
  }
  if (!match) return;
  const splitLines = (stream, role) => {
    if (!stream) return;
    let buf = "";
    stream.on("data", (chunk) => {
      try {
        buf += Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
        let nl;
        while ((nl = buf.indexOf("\n")) !== -1) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try {
            _emit(role, { message: JSON.parse(line) }, { model: match.model });
          } catch {
            _emit(role, { raw: line }, { model: match.model });
          }
        }
      } catch { /* never break the pipe */ }
    });
  };
  const splitLinesDbg = (stream, role) => {
    if (!stream) return;
    let buf = "";
    stream.on("data", (chunk) => {
      try {
        if (process.env.ARA_HOOK_DEBUG) {
          process.stderr.write(`[ara-hook/wrap] ${role} ${chunk.length}B\n`);
        }
        buf += Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
        let nl;
        while ((nl = buf.indexOf("\n")) !== -1) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try { _emit(role, { message: JSON.parse(line) }, { model: match.model }); }
          catch { _emit(role, { raw: line }, { model: match.model }); }
        }
      } catch { /* never break the pipe */ }
    });
  };
  splitLinesDbg(shim.stdin, "rpc.out");
  splitLinesDbg(shim.stdout, "rpc.in");
}

function _wrapAppServerClient(ClientCls) {
  if (!ClientCls || ClientCls.__ara_wrapped) return;
  const proto = ClientCls.prototype;

  if (typeof proto.writeMessage === "function") {
    const orig = proto.writeMessage;
    proto.writeMessage = function patched(message) {
      try {
        _emit("rpc.out", { message }, { model: "codex" });
      } catch { /* never break the agent */ }
      return orig.call(this, message);
    };
  }

  // Tap inbound: client.ts uses readline.createInterface(child.stdout)
  // and assigns it to this.lines, then iterates with a for-await-of in
  // start(). Patch start() to also emit each line.
  if (typeof proto.start === "function") {
    const origStart = proto.start;
    proto.start = function patched(...args) {
      const ret = origStart.apply(this, args);
      // After start(), this.lines is the readline interface. Subscribe.
      if (this.lines && typeof this.lines.on === "function") {
        this.lines.on("line", (line) => {
          try {
            const parsed = JSON.parse(line);
            _emit("rpc.in", { message: parsed }, { model: "codex" });
          } catch { /* non-JSON noise */ }
        });
      }
      return ret;
    };
  }

  ClientCls.__ara_wrapped = true;
}

export function wrap_client_js(orig /* , guard */) {
  // The class is exported under a few possible names depending on
  // OpenClaw build. Probe and patch whichever exists.
  for (const k of Object.keys(orig)) {
    const v = orig[k];
    if (typeof v === "function" && v.prototype
        && (typeof v.prototype.writeMessage === "function"
            || typeof v.prototype.start === "function")) {
      _wrapAppServerClient(v);
    }
  }
  return {};
}

// ---------------- OpenClaw event projector (parsed conversation) ----------------
//
// CodexAppServerEventProjector turns raw codex events into structured
// thread items (assistant_text, tool_use, reasoning, etc.). Wrapping its
// projection method captures the highest-fidelity, already-parsed view.

function _wrapEventProjector(ProjectorCls) {
  if (!ProjectorCls || ProjectorCls.__ara_wrapped) return;
  const proto = ProjectorCls.prototype;
  // The class shape varies by version; tap any method whose name suggests
  // it ingests raw events. Common candidates: project, projectEvent,
  // handleEvent, ingest, onEvent.
  for (const m of ["project", "projectEvent", "handleEvent", "ingest", "onEvent"]) {
    if (typeof proto[m] === "function") {
      const orig = proto[m];
      proto[m] = function patched(event, ...rest) {
        try {
          _emit("codex.event", { method: m, event }, { model: "codex" });
        } catch { /* swallow */ }
        return orig.call(this, event, ...rest);
      };
    }
  }
  ProjectorCls.__ara_wrapped = true;
}

export function wrap_event_projector_js(orig /* , guard */) {
  for (const k of Object.keys(orig)) {
    const v = orig[k];
    if (typeof v === "function" && v.prototype) {
      // The class name starts with "CodexAppServerEventProjector" but we
      // match by shape, not name, to survive renames.
      const protoKeys = Object.getOwnPropertyNames(v.prototype);
      if (protoKeys.some((n) => /event|project|ingest/i.test(n))) {
        _wrapEventProjector(v);
      }
    }
  }
  return {};
}
