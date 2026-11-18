// Network hooks.
//
// Covers http.request / https.request (above TLS, so we see the request body
// in plaintext) and net.Socket.prototype.connect (raw TCP). On a deny match
// against local net rules we fail the connect synchronously — net.connect
// returns a socket shim that emits 'error' on the next tick.

// Go through CJS require to get the REAL http/net/https singletons — an
// ESM `import http from "node:http"` would hit our own loader wrapper and
// we'd end up mutating the wrapper instead of the shared module everyone
// else (including CJS require('http')) uses.
import { createRequire } from "node:module";
import { EventEmitter } from "node:events";
const require = createRequire(import.meta.url);
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");

function targetFromHttpArgs(args) {
  const first = args[0];
  let host = "";
  let port = null;
  let path = "/";
  let method = "GET";
  if (typeof first === "string") {
    try {
      const u = new URL(first);
      host = u.hostname;
      port = u.port ? Number(u.port) : (u.protocol === "https:" ? 443 : 80);
      path = u.pathname + u.search;
    } catch {
      host = first;
    }
  } else if (first && typeof first === "object") {
    host = first.hostname || first.host || "";
    port = first.port || null;
    path = first.path || "/";
    method = first.method || "GET";
  }
  if (port == null) port = 80;
  return { host, port, path, method };
}

function targetFromConnectArgs(args) {
  let first = args[0];
  // Node's createConnection() calls socket.connect(normalizedArray)
  // where the first arg is [options, cb]. Unwrap it.
  if (Array.isArray(first)) first = first[0];
  let host = "";
  let port = null;
  if (first && typeof first === "object") {
    if (typeof first.path === "string" && first.path.startsWith("/")) {
      return { unix: first.path };
    }
    host = first.host || first.hostname || "";
    port = first.port != null ? Number(first.port) : null;
  } else if (typeof first === "number") {
    host = args[1] && typeof args[1] === "string" ? args[1] : "localhost";
    port = first;
  } else if (typeof first === "string" && first.startsWith("/")) {
    return { unix: first };
  }
  if (!host) return null;
  return { host, port };
}

function redirectAfUnix(args, newPath) {
  const first = args[0];
  if (Array.isArray(first)) {
    // [options, cb] shape — rebuild options with new path.
    const opts = first[0];
    if (opts && typeof opts === "object") {
      first[0] = { ...opts, path: newPath };
    } else if (typeof opts === "string") {
      first[0] = newPath;
    }
  } else if (typeof first === "string") {
    args[0] = newPath;
  } else if (first && typeof first === "object") {
    first.path = newPath;
  }
  return args;
}

function redirectAfInet(args, newPort) {
  const first = args[0];
  if (Array.isArray(first)) {
    const opts = first[0];
    if (opts && typeof opts === "object") {
      first[0] = { ...opts, port: newPort };
    } else if (typeof opts === "number") {
      first[0] = newPort;
    }
  } else if (first && typeof first === "object") {
    args[0] = { ...first, port: newPort };
  } else if (typeof first === "number") {
    args[0] = newPort;
  }
  return args;
}

function emitRedirect(guard, op, reason, from, to) {
  try {
    guard.ipc.send({
      type: "event",
      kind: "policy_hit",
      op,
      verdict: "redirect",
      reason,
      from,
      to,
      pid: process.pid,
    });
  } catch { /* best-effort */ }
}

function denyTarget(guard, target, rule, op) {
  try {
    guard.ipc.send({
      type: "event",
      kind: "policy_hit",
      op,
      verdict: "deny",
      reason: "DENY_NET_BLOCKED",
      rule_id: rule.rule_id,
      host: target.host,
      port: target.port,
      touched: `${target.host}:${target.port}`,
      pid: process.pid,
    });
  } catch { /* best-effort */ }
}

function auditAllow(guard, target, op, extras) {
  try {
    guard.ipc.send({
      type: "event",
      kind: op === "http.request" ? "http_request" : "net_connect",
      verdict: "allow",
      host: target.host,
      port: target.port,
      pid: process.pid,
      ...(extras || {}),
    });
  } catch { /* best-effort */ }
}

export function installHttpHook(guard) {
  const localRules = guard.localRules;
  const origHttpReq = http.request;
  const origHttpsReq = https.request;

  function wrap(proto, orig, defaultPort) {
    return function patched(...args) {
      const tgt = targetFromHttpArgs(args);
      const rule = localRules.matchNet(tgt.host, tgt.port || defaultPort);
      if (rule) {
        denyTarget(guard, { host: tgt.host, port: tgt.port || defaultPort },
                   rule, "http.request");
        // Return a faux ClientRequest-ish emitter that emits 'error' async.
        const fake = new EventEmitter();
        fake.end = () => fake;
        fake.write = () => true;
        fake.destroy = () => {};
        fake.setHeader = () => {};
        fake.getHeader = () => undefined;
        fake.removeHeader = () => {};
        fake.abort = () => {};
        setImmediate(() => {
          const err = new Error(
            `ARA-GUARD: ${proto} request to ${tgt.host}:${tgt.port || defaultPort} denied by ${rule.rule_id}`,
          );
          err.code = "EACCES";
          err.ara = { rule_id: rule.rule_id, pattern: rule.pattern };
          fake.emit("error", err);
        });
        return fake;
      }
      auditAllow(guard, { host: tgt.host, port: tgt.port || defaultPort },
                 "http.request", { proto, path: tgt.path, method: tgt.method });
      return orig.apply(proto === "https" ? https : http, args);
    };
  }

  http.request = wrap("http", origHttpReq, 80);
  https.request = wrap("https", origHttpsReq, 443);

  // Node's http.get / https.get closure-capture the module-local `request`,
  // so mutating `http.request` alone doesn't affect them. Re-implement get()
  // as request()+end() using our patched request.
  http.get = function patchedGet(...args) {
    const req = http.request(...args);
    if (typeof req.end === "function") req.end();
    return req;
  };
  https.get = function patchedGet(...args) {
    const req = https.request(...args);
    if (typeof req.end === "function") req.end();
    return req;
  };

  const origConnect = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function patchedConnect(...args) {
    const tgt = targetFromConnectArgs(args);
    if (!tgt) return origConnect.apply(this, args);

    // AF_UNIX exec-approvals redirect: defense-in-depth over config rewrite.
    if (tgt.unix) {
      const realP = process.env.ARA_EXEC_APPROVALS_REAL_PATH;
      const tapP = process.env.ARA_EXEC_APPROVALS_TAP_PATH;
      if (realP && tapP && tgt.unix === realP) {
        emitRedirect(guard, "net.connect",
                     "AF_UNIX_REDIRECT_EXEC_APPROVALS", realP, tapP);
        args = redirectAfUnix(args, tapP);
      }
      return origConnect.apply(this, args);
    }

    // AF_INET gateway port redirect for loopback targets.
    const gwReal = process.env.ARA_GATEWAY_REAL_PORT;
    const gwTap = process.env.ARA_GATEWAY_TAP_PORT;
    if (gwReal && gwTap && tgt.port === Number(gwReal)
        && (tgt.host === "127.0.0.1" || tgt.host === "::1"
            || tgt.host === "localhost")) {
      emitRedirect(guard, "net.connect",
                   "AF_INET_REDIRECT_GATEWAY",
                   `${tgt.host}:${gwReal}`, `${tgt.host}:${gwTap}`);
      args = redirectAfInet(args, Number(gwTap));
      tgt.port = Number(gwTap);
    }

    const rule = localRules.matchNet(tgt.host, tgt.port);
    if (rule) {
      denyTarget(guard, tgt, rule, "net.connect");
      setImmediate(() => {
        const err = new Error(
          `ARA-GUARD: connect to ${tgt.host}:${tgt.port} denied by ${rule.rule_id}`,
        );
        err.code = "EACCES";
        err.ara = { rule_id: rule.rule_id, pattern: rule.pattern };
        this.emit("error", err);
        this.destroy(err);
      });
      return this;
    }
    auditAllow(guard, tgt, "net.connect", {});
    return origConnect.apply(this, args);
  };
}
