// child_process hook.
//
// Two responsibilities:
//
// 1. HEREDITY. On every spawn, rewrite the `env` option so that ARA-GUARD is
//    reinstalled into the child. Overwrite (not merge): any sanitize-env in
//    the worker runs before this hook and is clobbered on the way out.
//
// 2. ENFORCEMENT. Every spawn is a policy question to the supervisor. The
//    guard never decides locally — it always queries.
//
// The second responsibility collides with `spawn`'s synchronous return. We
// resolve it by returning a deferred ChildProcess: a shim that exposes
// PassThrough streams and forwards events. While the supervisor is deciding,
// the shim is live but pre-spawn. On deny, the shim emits 'error' and 'exit'.
// On allow, the real process is spawned and its events/streams are piped
// into the shim.
//
// Synchronous spawn variants (`spawnSync`, `execSync`, `execFileSync`) would
// need SharedArrayBuffer + a worker thread to block the event loop until a
// decision arrives. Not built for PoC. They currently only get heredity and
// audit logging; enforcement on those is a follow-up.

import child_process from "node:child_process";
import { PassThrough } from "node:stream";
import { EventEmitter } from "node:events";
import { emit as emitTranscript } from "../transcript_client.mjs";


// Subprocess boundaries that carry LLM conversation over stdio JSON-RPC.
// When openclaw (or anything else we supervise) spawns one of these, we
// tee both directions of the pipe into the transcript channel — without
// parsing protocol details. JSON-per-line is the one structural assumption.
const STDIO_RPC_TAPS = [
  {
    // /usr/bin/codex → /usr/lib/node_modules/@openai/codex/.../codex
    // First arg 'app-server'. Talks JSON-RPC-lite over stdio to openclaw.
    match: (cmd, args) =>
      /(^|\/)codex$/.test(String(cmd || "")) && args[0] === "app-server",
    model: "codex",
  },
];

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
    // Best-effort.
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

function emitSyncTargetOutput(guard, command, args, result, threwError = null) {
  const url = firstTargetUrl(command, args);
  if (!url) return;
  const stdout = result && Object.prototype.hasOwnProperty.call(result, "stdout")
    ? result.stdout
    : null;
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


function maybeTapStdio(shim, real, command, args) {
  // Tap both: shim (what openclaw holds) and real (the actual child's
  // pipes). Whichever one data actually flows through wins — different
  // consumers call into different sides of the PassThrough/Socket pair.
  if (!shim && !real) return;
  const match = STDIO_RPC_TAPS.find((t) => t.match(command, args));
  if (process.env.ARA_HOOK_DEBUG) {
    process.stderr.write(
      `[ara-hook] tap? cmd=${command} args[0]=${args[0]} match=${!!match}\n`,
    );
  }
  if (!match) return;

  const splitLines = (stream, role, tag) => {
    if (!stream) return;
    let buf = "";
    stream.on("data", (chunk) => {
      try {
        if (process.env.ARA_HOOK_DEBUG) {
          process.stderr.write(`[ara-hook] ${tag}/${role} ${chunk.length}B\n`);
        }
        buf += Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
        let nl;
        while ((nl = buf.indexOf("\n")) !== -1) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try {
            emitTranscript(role, { message: JSON.parse(line) },
                           { model: match.model });
          } catch {
            emitTranscript(role, { raw: line }, { model: match.model });
          }
        }
      } catch { /* never break the pipe */ }
    });
  };

  // Shim side: PassThrough streams openclaw talks to.
  if (shim) {
    splitLines(shim.stdin, "rpc.out", "shim-in");
    splitLines(shim.stdout, "rpc.in", "shim-out");
  }
  // Real side: the actual child's stdin/stdout Sockets. Tapping stdin
  // here needs a write-wrapper because Sockets don't emit 'data' for
  // writes.
  if (real) {
    if (real.stdin && typeof real.stdin.write === "function") {
      const origW = real.stdin.write.bind(real.stdin);
      let rbuf = "";
      real.stdin.write = function tappedW(chunk, ...rest) {
        try {
          const str = typeof chunk === "string"
            ? chunk : (Buffer.isBuffer(chunk) ? chunk.toString("utf8") : "");
          if (process.env.ARA_HOOK_DEBUG) {
            process.stderr.write(`[ara-hook] real-in/rpc.out ${str.length}B\n`);
          }
          rbuf += str;
          let nl;
          while ((nl = rbuf.indexOf("\n")) !== -1) {
            const line = rbuf.slice(0, nl).trim();
            rbuf = rbuf.slice(nl + 1);
            if (!line) continue;
            try { emitTranscript("rpc.out", { message: JSON.parse(line) }, { model: match.model }); }
            catch { emitTranscript("rpc.out", { raw: line }, { model: match.model }); }
          }
        } catch {}
        return origW(chunk, ...rest);
      };
    }
    splitLines(real.stdout, "rpc.in", "real-out");
  }

  if (process.env.ARA_HOOK_DEBUG) {
    const shimIn = shim && shim.stdin ? shim.stdin.listenerCount("data") : -1;
    const shimOut = shim && shim.stdout ? shim.stdout.listenerCount("data") : -1;
    const realIn = real && real.stdin ? real.stdin.listenerCount("data") : -1;
    const realOut = real && real.stdout ? real.stdout.listenerCount("data") : -1;
    process.stderr.write(
      `[ara-hook] tap-attached cmd=${command} listeners shim.stdin=${shimIn}`
      + ` shim.stdout=${shimOut} real.stdin=${realIn} real.stdout=${realOut}\n`,
    );
  }
}

function ensureImportFlag(existing, guardPath) {
  const importFlag = `--import=${guardPath}`;
  if (!existing) return importFlag;
  if (existing.includes(importFlag)) return existing;
  return `${importFlag} ${existing}`;
}

function rewriteOptions(options, guard) {
  const opts = options && typeof options === "object" ? { ...options } : {};
  const srcEnv =
    opts.env && typeof opts.env === "object" ? opts.env : process.env;
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
    // Best-effort.
  }
}

async function querySpawnDecision(guard, command, args, options) {
  // Round-trip to supervisor — it has the static rule engine plus the
  // mvdan/sh preflight interpreter. Spawn rate is low enough that the
  // IPC cost is invisible next to any real command's own latency.
  if (!guard || !guard.ipc) return { verdict: "deny", reason: "ARA_NO_GUARD" };
  try {
    const cwd = (options && typeof options === "object" && options.cwd)
      ? String(options.cwd)
      : process.cwd();
    return await guard.ipc.request({
      type: "query",
      op: "exec.spawn",
      command: String(command ?? ""),
      args: Array.isArray(args) ? args.map((a) => String(a)) : [],
      cwd,
      pid: process.pid,
    });
  } catch {
    return { verdict: "deny", reason: "ARA_IPC_FAILURE" };
  }
}

/**
 * A stand-in ChildProcess returned synchronously from a patched spawn. We
 * attach PassThrough streams for stdout/stderr so callers can subscribe
 * before the supervisor's decision arrives. If the decision is allow, we
 * spawn the real child and pipe its streams/events into this shim. If deny,
 * we emit 'error' and 'exit' and close the streams.
 */
function createDeferredChild(command) {
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

    // Replay any stdin the caller wrote before the real process existed.
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
    // Only emit 'error' if someone is listening; otherwise the default
    // EventEmitter behavior crashes the process, which swallows callers
    // that rely on the exec/execFile callback to get the error.
    if (shim.listenerCount("error") > 0) {
      shim.emit("error", err);
    }
    shim.emit("exit", 1, null);
    shim.emit("close", 1, null);
    shim.stdout.end();
    shim.stderr.end();
  };

  return shim;
}

function enforcedSpawn(origSpawn, guard, callArgs, extract) {
  const { command, args, optionsIdx } = extract(callArgs);
  logSpawnEvent(guard, command, args);

  const options = optionsIdx !== -1 ? callArgs[optionsIdx] : undefined;
  const rewritten = rewriteOptions(options, guard);
  const newArgs = [...callArgs];
  if (optionsIdx !== -1) newArgs[optionsIdx] = rewritten;
  else newArgs.push(rewritten);

  const shim = createDeferredChild(String(command ?? ""));
  tapTargetOutput(guard, shim, command, args);

  (async () => {
    const decision = await querySpawnDecision(guard, command, args, options);
    if (decision.verdict !== "allow") {
      shim._emitDeny(decision, String(command ?? ""));
      return;
    }
    let real;
    try {
      real = origSpawn.apply(child_process, newArgs);
    } catch (err) {
      shim._emitDeny(
        { reason: err.code || "SPAWN_FAILED" },
        String(command ?? ""),
      );
      return;
    }
    shim._attachReal(real);
    maybeTapStdio(shim, real, command, args);
  })();

  return shim;
}

function enforcedExec(origExec, guard, callArgs, extract) {
  // exec(command[, options][, callback]) — we accept a callback and wrap it,
  // plus return a deferred ChildProcess for consumers that use it like spawn.
  const { command, args, optionsIdx, callbackIdx } = extract(callArgs);
  logSpawnEvent(guard, command, args);

  const options = optionsIdx !== -1 ? callArgs[optionsIdx] : undefined;
  const origCallback =
    callbackIdx !== -1 && typeof callArgs[callbackIdx] === "function"
      ? callArgs[callbackIdx]
      : null;

  const rewritten = rewriteOptions(options, guard);
  const newArgs = [...callArgs];
  if (optionsIdx !== -1) newArgs[optionsIdx] = rewritten;
  else newArgs.splice(callbackIdx === -1 ? newArgs.length : callbackIdx, 0, rewritten);

  const shim = createDeferredChild(String(command ?? ""));
  tapTargetOutput(guard, shim, command, args);

  (async () => {
    const decision = await querySpawnDecision(guard, command, args, options);
    if (decision.verdict !== "allow") {
      shim._emitDeny(decision, String(command ?? ""));
      if (origCallback) {
        const err = new Error(
          `ARA_DENY: ${decision.reason || "ARA_DENY"} (${command})`,
        );
        err.code = decision.reason || "ARA_DENY";
        origCallback(err, "", "");
      }
      return;
    }
    let real;
    try {
      real = origExec.apply(child_process, newArgs);
    } catch (err) {
      shim._emitDeny(
        { reason: err.code || "SPAWN_FAILED" },
        String(command ?? ""),
      );
      if (origCallback) origCallback(err, "", "");
      return;
    }
    shim._attachReal(real);
    maybeTapStdio(shim, real, command, args);
  })();

  return shim;
}

function wrapSyncAuditOnly(fn, guard, extract) {
  // Sync variants: can't enforce via async IPC without SharedArrayBuffer
  // plumbing. For now, keep heredity + audit only.
  return function patched(...callArgs) {
    const { command, args, optionsIdx } = extract(callArgs);
    logSpawnEvent(guard, command, args);
    const options = optionsIdx !== -1 ? callArgs[optionsIdx] : undefined;
    const rewritten = rewriteOptions(options, guard);
    if (optionsIdx !== -1) callArgs[optionsIdx] = rewritten;
    else callArgs.push(rewritten);
    try {
      const result = fn.apply(child_process, callArgs);
      emitSyncTargetOutput(guard, command, args, result);
      return result;
    } catch (err) {
      emitSyncTargetOutput(guard, command, args, null, err);
      throw err;
    }
  };
}

export function installChildProcessHook(guard) {
  const origSpawn = child_process.spawn;
  const origSpawnSync = child_process.spawnSync;
  const origExec = child_process.exec;
  const origExecSync = child_process.execSync;
  const origExecFile = child_process.execFile;
  const origExecFileSync = child_process.execFileSync;
  const origFork = child_process.fork;

  const spawnExtract = (a) => {
    const command = a[0];
    let args = [];
    let optionsIdx = -1;
    if (Array.isArray(a[1])) {
      args = a[1];
      if (a[2] && typeof a[2] === "object") optionsIdx = 2;
    } else if (a[1] && typeof a[1] === "object") {
      optionsIdx = 1;
    }
    return { command, args, optionsIdx };
  };

  const execExtract = (a) => {
    const raw = String(a[0] ?? "");
    const idx = raw.search(/\s/);
    const head = idx === -1 ? raw : raw.slice(0, idx);
    const tail = idx === -1 ? "" : raw.slice(idx + 1);
    const args = tail ? tail.split(/\s+/).filter(Boolean) : [];
    let optionsIdx = -1;
    let callbackIdx = -1;
    if (typeof a[1] === "function") {
      callbackIdx = 1;
    } else if (a[1] && typeof a[1] === "object") {
      optionsIdx = 1;
      if (typeof a[2] === "function") callbackIdx = 2;
    }
    return { command: head, args, optionsIdx, callbackIdx };
  };

  const execFileExtract = (a) => {
    const command = a[0];
    let args = [];
    let optionsIdx = -1;
    let callbackIdx = -1;
    let i = 1;
    if (Array.isArray(a[i])) {
      args = a[i];
      i += 1;
    }
    if (a[i] && typeof a[i] === "object" && !Array.isArray(a[i])) {
      optionsIdx = i;
      i += 1;
    }
    if (typeof a[i] === "function") callbackIdx = i;
    return { command, args, optionsIdx, callbackIdx };
  };

  child_process.spawn = function patched(...a) {
    return enforcedSpawn(origSpawn, guard, a, spawnExtract);
  };
  child_process.exec = function patched(...a) {
    return enforcedExec(origExec, guard, a, execExtract);
  };
  child_process.execFile = function patched(...a) {
    return enforcedExec(origExecFile, guard, a, execFileExtract);
  };
  child_process.fork = function patched(...a) {
    return enforcedSpawn(origFork, guard, a, spawnExtract);
  };

  // Sync variants: heredity + audit only (no enforcement).
  child_process.spawnSync = wrapSyncAuditOnly(origSpawnSync, guard, spawnExtract);
  child_process.execSync = wrapSyncAuditOnly(origExecSync, guard, (a) => {
    const command = a[0];
    let optionsIdx = -1;
    if (a[1] && typeof a[1] === "object") optionsIdx = 1;
    return { command, args: [], optionsIdx };
  });
  child_process.execFileSync = wrapSyncAuditOnly(origExecFileSync, guard, (a) => {
    const command = a[0];
    let args = [];
    let optionsIdx = -1;
    if (Array.isArray(a[1])) {
      args = a[1];
      if (a[2] && typeof a[2] === "object" && !Array.isArray(a[2])) optionsIdx = 2;
    } else if (a[1] && typeof a[1] === "object" && !Array.isArray(a[1])) {
      optionsIdx = 1;
    }
    return { command, args, optionsIdx };
  });
}
