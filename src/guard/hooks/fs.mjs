// fs hook: intercept async file reads and route decisions through the
// supervisor. For the PoC we cover `fs.readFile` (callback form) and
// `fs.promises.readFile`. Sync variants will come after the SharedArrayBuffer
// plumbing lands.

import fs from "node:fs";
import path from "node:path";

function denyError(reason, target) {
  const err = new Error(`ARA_DENY: ${reason} (${target})`);
  err.code = reason || "ARA_DENY";
  err.path = target;
  err.syscall = "open";
  return err;
}

function absolutize(p) {
  if (typeof p !== "string") {
    // Buffer, URL, fd, etc. — pass through for PoC.
    try {
      return String(p);
    } catch {
      return "";
    }
  }
  return path.resolve(p);
}

async function checkOp(guard, op, targetPath) {
  const target = absolutize(targetPath);
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
  const approvalHit = guard?.localRules?.matchApprovalPath?.(target);
  if (approvalHit && guard?.ipc) {
    try {
      return await guard.ipc.request({
        type: "query", op, path: target, pid: process.pid,
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

const checkRead = (guard, p) => checkOp(guard, "fs.read", p);
const checkReaddir = (guard, p) => checkOp(guard, "fs.readdir", p);
const checkMkdir  = (guard, p) => checkOp(guard, "fs.mkdir", p);
const checkRmdir  = (guard, p) => checkOp(guard, "fs.rmdir", p);
const checkChdir  = (guard, p) => checkOp(guard, "fs.chdir", p);
const checkChmod  = (guard, p) => checkOp(guard, "fs.chmod", p);
const checkChown  = (guard, p) => checkOp(guard, "fs.chown", p);

function syncLocalDeny(guard, op, target) {
  const hit = guard.localRules?.matchPath(target);
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
    // best-effort
  }
  return hit;
}

export function installFsHook(guard) {
  const origReadFile = fs.readFile;
  const origPromiseReadFile = fs.promises.readFile;
  const origReaddir = fs.readdir;
  const origPromiseReaddir = fs.promises.readdir;
  const origReadFileSync = fs.readFileSync;
  const origReaddirSync = fs.readdirSync;
  const origOpenDir = fs.opendir;
  const origOpenDirSync = fs.opendirSync;

  fs.readFile = function patchedReadFile(pathArg, ...rest) {
    const cb = rest[rest.length - 1];
    const callback = typeof cb === "function" ? cb : null;
    const target = absolutize(pathArg);
    (async () => {
      const d = await checkRead(guard, target);
      if (d.verdict !== "allow") {
        const err = denyError(d.reason, target);
        if (callback) callback(err);
        return;
      }
      return origReadFile.call(fs, pathArg, ...rest);
    })();
  };

  fs.promises.readFile = async function patchedPromiseReadFile(pathArg, options) {
    const target = absolutize(pathArg);
    const d = await checkRead(guard, target);
    if (d.verdict !== "allow") {
      throw denyError(d.reason, target);
    }
    return origPromiseReadFile.call(fs.promises, pathArg, options);
  };

  fs.readdir = function patchedReaddir(pathArg, ...rest) {
    const cb = rest[rest.length - 1];
    const callback = typeof cb === "function" ? cb : null;
    const target = absolutize(pathArg);
    (async () => {
      const d = await checkReaddir(guard, target);
      if (d.verdict !== "allow") {
        const err = denyError(d.reason, target);
        if (callback) callback(err);
        return;
      }
      return origReaddir.call(fs, pathArg, ...rest);
    })();
  };

  fs.promises.readdir = async function patchedPromiseReaddir(pathArg, options) {
    const target = absolutize(pathArg);
    const d = await checkReaddir(guard, target);
    if (d.verdict !== "allow") {
      throw denyError(d.reason, target);
    }
    return origPromiseReaddir.call(fs.promises, pathArg, options);
  };

  // Sync variants — enforced via cached rules from the supervisor. Still
  // audited: every sync deny emits a policy_hit_sync event.

  fs.readFileSync = function patchedReadFileSync(pathArg, ...rest) {
    const target = absolutize(pathArg);
    const hit = syncLocalDeny(guard, "fs.read", target);
    if (hit) throw denyError("DENY_FILE_PATH_BLOCKED", target);
    return origReadFileSync.call(fs, pathArg, ...rest);
  };

  fs.readdirSync = function patchedReaddirSync(pathArg, ...rest) {
    const target = absolutize(pathArg);
    // Debug: announce every call so we can see which API OpenClaw uses.
    try {
      guard.ipc.send({
        type: "event",
        kind: "fs_trace",
        op: "fs.readdirSync",
        path: target,
        pid: process.pid,
      });
    } catch {
      /* ignore */
    }
    const hit = syncLocalDeny(guard, "fs.readdir", target);
    if (hit) throw denyError("DENY_FILE_PATH_BLOCKED", target);
    return origReaddirSync.call(fs, pathArg, ...rest);
  };

  fs.opendir = function patchedOpenDir(pathArg, ...rest) {
    const cb = rest[rest.length - 1];
    const callback = typeof cb === "function" ? cb : null;
    const target = absolutize(pathArg);
    (async () => {
      const d = await checkReaddir(guard, target);
      if (d.verdict !== "allow") {
        const err = denyError(d.reason, target);
        if (callback) callback(err);
        return;
      }
      return origOpenDir.call(fs, pathArg, ...rest);
    })();
  };

  fs.opendirSync = function patchedOpenDirSync(pathArg, ...rest) {
    const target = absolutize(pathArg);
    const hit = syncLocalDeny(guard, "fs.readdir", target);
    if (hit) throw denyError("DENY_FILE_PATH_BLOCKED", target);
    return origOpenDirSync.call(fs, pathArg, ...rest);
  };

  if (fs.promises.opendir) {
    const origPromiseOpenDir = fs.promises.opendir;
    fs.promises.opendir = async function patchedPromiseOpenDir(pathArg, options) {
      const target = absolutize(pathArg);
      const d = await checkReaddir(guard, target);
      if (d.verdict !== "allow") throw denyError(d.reason, target);
      return origPromiseOpenDir.call(fs.promises, pathArg, options);
    };
  }

  // ---- nav layer: mkdir / rmdir / chdir ----------------------------
  // These hit Bulwark's `nav` layer via the _OP_TO_LAYER mapping on
  // the supervisor side (fs.mkdir → nav, fs.rmdir → nav, fs.chdir →
  // nav). Each has callback + promise + sync variants.

  function wrapMethodWithCheck(container, name, checker) {
    const orig = container[name];
    if (typeof orig !== "function") return;
    container[name] = function patched(pathArg, ...rest) {
      const cb = rest[rest.length - 1];
      const callback = typeof cb === "function" ? cb : null;
      const target = absolutize(pathArg);
      (async () => {
        const d = await checker(guard, target);
        if (d.verdict !== "allow") {
          const err = denyError(d.reason, target);
          if (callback) callback(err);
          return;
        }
        return orig.call(container, pathArg, ...rest);
      })();
    };
  }

  function wrapPromiseWithCheck(container, name, checker) {
    const orig = container[name];
    if (typeof orig !== "function") return;
    container[name] = async function patched(pathArg, ...rest) {
      const target = absolutize(pathArg);
      const d = await checker(guard, target);
      if (d.verdict !== "allow") throw denyError(d.reason, target);
      return orig.call(container, pathArg, ...rest);
    };
  }

  function wrapSyncWithCheck(container, name, op) {
    const orig = container[name];
    if (typeof orig !== "function") return;
    container[name] = function patched(pathArg, ...rest) {
      const target = absolutize(pathArg);
      const hit = syncLocalDeny(guard, op, target);
      if (hit) throw denyError("DENY_FILE_PATH_BLOCKED", target);
      return orig.call(container, pathArg, ...rest);
    };
  }

  wrapMethodWithCheck(fs, "mkdir", checkMkdir);
  wrapMethodWithCheck(fs, "rmdir", checkRmdir);
  wrapMethodWithCheck(fs, "rm",    checkRmdir);
  wrapPromiseWithCheck(fs.promises, "mkdir", checkMkdir);
  wrapPromiseWithCheck(fs.promises, "rmdir", checkRmdir);
  wrapPromiseWithCheck(fs.promises, "rm",    checkRmdir);
  wrapSyncWithCheck(fs, "mkdirSync", "fs.mkdir");
  wrapSyncWithCheck(fs, "rmdirSync", "fs.rmdir");
  wrapSyncWithCheck(fs, "rmSync",    "fs.rmdir");

  // process.chdir — synchronous, no callback form.
  try {
    const origChdir = process.chdir.bind(process);
    process.chdir = function patchedChdir(pathArg) {
      const target = absolutize(pathArg);
      const hit = syncLocalDeny(guard, "fs.chdir", target);
      if (hit) throw denyError("DENY_FILE_PATH_BLOCKED", target);
      return origChdir(pathArg);
    };
  } catch { /* process.chdir may be frozen in some runtimes */ }

  // ---- perm layer: chmod / chown / {l,f}chmod / {l,f}chown ---------

  wrapMethodWithCheck(fs, "chmod",   checkChmod);
  wrapMethodWithCheck(fs, "lchmod",  checkChmod);
  wrapMethodWithCheck(fs, "chown",   checkChown);
  wrapMethodWithCheck(fs, "lchown",  checkChown);
  wrapPromiseWithCheck(fs.promises, "chmod",  checkChmod);
  wrapPromiseWithCheck(fs.promises, "lchmod", checkChmod);
  wrapPromiseWithCheck(fs.promises, "chown",  checkChown);
  wrapPromiseWithCheck(fs.promises, "lchown", checkChown);
  wrapSyncWithCheck(fs, "chmodSync",  "fs.chmod");
  wrapSyncWithCheck(fs, "lchmodSync", "fs.chmod");
  wrapSyncWithCheck(fs, "chownSync",  "fs.chown");
  wrapSyncWithCheck(fs, "lchownSync", "fs.chown");
}
