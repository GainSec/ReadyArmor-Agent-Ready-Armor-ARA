// Local rule cache for sync-path ops.
//
// The supervisor mirrors its compiled rules to the guard on hello. Sync
// hooks consult this cache because they can't do an async IPC round-trip.
// The supervisor remains authoritative — it decides which rules exist; the
// guard only applies them.

function globToRegex(pattern) {
  let out = "^";
  let i = 0;
  while (i < pattern.length) {
    const c = pattern[i];
    if (c === "*") {
      if (pattern[i + 1] === "*") {
        out += ".*";
        i += 2;
        if (pattern[i] === "/") i += 1;
      } else {
        out += "[^/]*";
        i += 1;
      }
    } else if (c === "?") {
      out += "[^/]";
      i += 1;
    } else {
      out += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
      i += 1;
    }
  }
  out += "$";
  return new RegExp(out);
}

export class LocalRules {
  constructor() {
    this.pathRules = [];
    this.commandRules = [];
    this.approvalPathRules = [];
    this.approvalCommandRules = [];
    this.netRules = [];
    this.allowNetRules = [];
    this.approvalNetRules = [];
    this.shadowMode = false;
  }

  load(rules) {
    this.shadowMode = Boolean(rules?.shadow_mode);
    this.pathRules = (rules?.deny_path || []).map((r) => ({
      rule_id: r.rule_id,
      pattern: r.pattern,
      re: globToRegex(r.pattern),
    }));
    this.commandRules = (rules?.deny_command || []).map((r) => ({
      rule_id: r.rule_id,
      pattern: r.pattern,
      re: globToRegex(r.pattern),
    }));
    this.approvalPathRules = (rules?.approval_path || []).map((r) => ({
      rule_id: r.rule_id,
      pattern: r.pattern,
      re: globToRegex(r.pattern),
    }));
    this.approvalCommandRules = (rules?.approval_command || []).map((r) => ({
      rule_id: r.rule_id,
      pattern: r.pattern,
      re: globToRegex(r.pattern),
    }));
    this.netRules = (rules?.deny_net || []).map((r) => ({
      rule_id: r.rule_id,
      pattern: r.pattern,
      host: r.host || splitNet(r.pattern).host,
      port: r.port || splitNet(r.pattern).port,
      hostRe: globToRegex((r.host || splitNet(r.pattern).host).toLowerCase()),
      portRe: globToRegex(r.port || splitNet(r.pattern).port),
    }));
    this.allowNetRules = (rules?.allow_net || []).map((r) => ({
      rule_id: r.rule_id,
      pattern: r.pattern,
      host: r.host || splitNet(r.pattern).host,
      port: r.port || splitNet(r.pattern).port,
      hostRe: globToRegex((r.host || splitNet(r.pattern).host).toLowerCase()),
      portRe: globToRegex(r.port || splitNet(r.pattern).port),
    }));
    this.approvalNetRules = (rules?.approval_net || []).map((r) => {
      const { host, port } = splitNet(r.pattern);
      return {
        rule_id: r.rule_id,
        pattern: r.pattern,
        host, port,
        hostRe: globToRegex(host.toLowerCase()),
        portRe: globToRegex(port),
      };
    });
  }

  matchNet(host, port) {
    const p = port == null ? "" : String(port);
    const h = (host || "").toLowerCase();
    for (const rule of this.allowNetRules) {
      if (rule.hostRe.test(h) && (rule.port === "*" || rule.portRe.test(p))) {
        return null;
      }
    }
    for (const rule of this.netRules) {
      if (rule.hostRe.test(h) && (rule.port === "*" || rule.portRe.test(p))) {
        return this._shadowed(rule, "net.connect",
                              { host, port, touched: `${host}:${port}` });
      }
    }
    return null;
  }

  matchApprovalNet(host, port) {
    const p = port == null ? "" : String(port);
    const h = (host || "").toLowerCase();
    for (const rule of this.approvalNetRules) {
      if (rule.hostRe.test(h) && (rule.port === "*" || rule.portRe.test(p))) {
        return rule;
      }
    }
    return null;
  }

  _shadowed(rule, kind, extras) {
    if (!this.shadowMode) return rule;
    try {
      this._guard?.ipc?.send({
        type: "event",
        kind: "policy_hit",
        verdict: "allow",
        reason: "SHADOW_WOULD_DENY",
        rule_id: rule.rule_id,
        source: "guard-shadow",
        shadow: true,
        op: kind,
        pid: process.pid,
        ...(extras || {}),
      });
    } catch { /* best-effort */ }
    return null;
  }

  attachGuard(g) { this._guard = g; }

  matchPath(candidate) {
    for (const rule of this.pathRules) {
      if (rule.re.test(candidate)) {
        return this._shadowed(rule, "fs.open", { path: candidate, touched: candidate });
      }
    }
    return null;
  }

  matchApprovalPath(candidate) {
    for (const rule of this.approvalPathRules) {
      if (rule.re.test(candidate)) return rule;
    }
    return null;
  }

  matchCommand(command, args = []) {
    if (!command) return null;
    const basename = command.includes("/")
      ? command.slice(command.lastIndexOf("/") + 1)
      : command;
    const joined = args && args.length
      ? `${command} ${args.map(String).join(" ")}`
      : command;
    const candidates = new Set([command, basename, joined]);

    const SHELLS = new Set(["sh", "bash", "dash", "zsh", "ksh", "ash"]);
    if (SHELLS.has(basename) && Array.isArray(args)) {
      const idx = args.indexOf("-c");
      if (idx !== -1 && idx + 1 < args.length) {
        const inner = String(args[idx + 1]);
        const innerTokens = inner.split(/\s+/).filter(Boolean);
        if (innerTokens.length) {
          const innerCmd = innerTokens[0];
          candidates.add(innerCmd);
          candidates.add(
            innerCmd.includes("/")
              ? innerCmd.slice(innerCmd.lastIndexOf("/") + 1)
              : innerCmd,
          );
          candidates.add(inner);
        }
      }
    }

    for (const rule of this.commandRules) {
      for (const c of candidates) {
        if (c && rule.re.test(c)) {
          return this._shadowed(rule, "exec.spawn", { command, touched: c });
        }
      }
    }
    return null;
  }

  /** Return [pathRule, touchedPath] if any arg resolves to a walled path. */
  matchSpawnPath(command, args = [], cwd = null, home = null) {
    const candidates = extractPathCandidates(command, args, { cwd, home });
    for (const p of candidates) {
      // matchPath already applies shadow filtering; non-null means real deny.
      const hit = this.matchPath(p);
      if (hit) return [hit, p];
    }
    return null;
  }
}

function splitNet(pattern) {
  if (!pattern.includes(":")) return { host: pattern, port: "*" };
  const idx = pattern.lastIndexOf(":");
  const host = pattern.slice(0, idx) || "*";
  const port = pattern.slice(idx + 1) || "*";
  return { host, port };
}

const SHELLS_SET = new Set(["sh", "bash", "dash", "zsh", "ksh", "ash"]);

function isFlag(s) {
  return s.startsWith("-") && !s.startsWith("/") && !s.startsWith("./");
}

function pathShape(s) {
  if (!s) return false;
  if (s.startsWith("/") || s.startsWith("~") || s.startsWith("./") || s.startsWith("../"))
    return true;
  if (isFlag(s)) return false;
  if (s.includes("/")) return true;
  return false;
}

function resolvePath(s, { cwd, home }) {
  if (s.startsWith("~")) {
    if (s === "~") s = home;
    else if (s.startsWith("~/")) s = home + s.slice(1);
  }
  // Minimal absolutization (Node's path module isn't available here).
  if (cwd && !s.startsWith("/")) s = `${cwd}/${s}`;
  return normalize(s);
}

function normalize(p) {
  const parts = p.split("/");
  const out = [];
  for (const part of parts) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (out.length) out.pop();
      continue;
    }
    out.push(part);
  }
  return (p.startsWith("/") ? "/" : "") + out.join("/");
}

// Very light shell-string tokenizer that respects single and double quotes
// plus backslash escapes. Not a full shell parser; covers the cases the
// naive split misses.
function shellTokenize(s) {
  const tokens = [];
  let buf = "";
  let i = 0;
  let quote = null;
  while (i < s.length) {
    const c = s[i];
    if (quote) {
      if (c === "\\" && quote === '"' && i + 1 < s.length) {
        buf += s[i + 1];
        i += 2;
        continue;
      }
      if (c === quote) {
        quote = null;
        i += 1;
        continue;
      }
      buf += c;
      i += 1;
      continue;
    }
    if (c === '"' || c === "'") {
      quote = c;
      i += 1;
      continue;
    }
    if (c === "\\" && i + 1 < s.length) {
      buf += s[i + 1];
      i += 2;
      continue;
    }
    if (/\s/.test(c)) {
      if (buf) {
        tokens.push(buf);
        buf = "";
      }
      i += 1;
      continue;
    }
    buf += c;
    i += 1;
  }
  if (buf) tokens.push(buf);
  return tokens;
}

function extractPathCandidates(command, args, { cwd, home }) {
  const resolvedHome = home || (typeof process !== "undefined" ? process.env.HOME : "/");
  const ctx = { cwd, home: resolvedHome };
  const out = [];
  const push = (s) => {
    if (s) out.push(resolvePath(s, ctx));
  };

  function scanArgs(argv) {
    for (const a of argv) {
      if (typeof a !== "string") continue;
      if (pathShape(a)) {
        push(a);
        continue;
      }
      if (a.startsWith("-") && a.includes("=")) {
        const val = a.slice(a.indexOf("=") + 1);
        if (pathShape(val)) push(val);
      }
    }
  }

  scanArgs(args || []);

  const basename = command
    ? command.includes("/")
      ? command.slice(command.lastIndexOf("/") + 1)
      : command
    : "";
  if (SHELLS_SET.has(basename) && Array.isArray(args)) {
    const idx = args.indexOf("-c");
    if (idx !== -1 && idx + 1 < args.length) {
      const inner = String(args[idx + 1]);
      const innerTokens = shellTokenize(inner);
      scanArgs(innerTokens);
      if (innerTokens.length) {
        const nested = extractPathCandidates(innerTokens[0], innerTokens.slice(1), {
          cwd,
          home: resolvedHome,
        });
        out.push(...nested);
      }
    }
  }

  // Dedup preserving order.
  const seen = new Set();
  const dedup = [];
  for (const p of out) {
    if (!seen.has(p)) {
      seen.add(p);
      dedup.push(p);
    }
  }
  return dedup;
}
