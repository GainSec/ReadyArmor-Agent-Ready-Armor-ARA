// ARA-GUARD entry point.
//
// Loaded into every worker Node process via NODE_OPTIONS=--import=<this>.
// Sequence on load:
//   1. Connect to supervisor over the unix socket.
//   2. Handshake (exchange session token; receive sync_rules).
//   3. Install the loader via module.register — subsequent imports of
//      node:fs, node:child_process, etc. go through our wrappers. This
//      closes the ESM named-import gap (monkey-patching on the namespace
//      alone doesn't catch `import { spawn } from "node:child_process"`).
//   4. Start heartbeat + process.kill block.

import { register } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

import { GuardIpc } from "./ipc.mjs";
import { LocalRules } from "./local_rules.mjs";
import { installProcessKillHook } from "./hooks/process.mjs";
import { installHeartbeat } from "./hooks/heartbeat.mjs";
import { installFsHook } from "./hooks/fs.mjs";
import { installChildProcessHook } from "./hooks/child_process.mjs";
import { installHttpHook } from "./hooks/net.mjs";
import { installFetchHook } from "./hooks/fetch.mjs";
import { installForgeChannels } from "./hooks/forge_channels.mjs";

const SOCKET = process.env.ARA_SOCKET;
const TOKEN = process.env.ARA_SESSION_TOKEN;
const SESSION = process.env.ARA_SESSION_ID;
const GUARD_PATH = fileURLToPath(import.meta.url);
const WRAPPERS_URL = new URL("./wrappers.mjs", import.meta.url).href;
const LOADER_URL = new URL("./loader.mjs", import.meta.url).href;

async function arm() {
  if (!SOCKET || !TOKEN) {
    process.stderr.write(
      "[ara-guard] refusing to arm: ARA_SOCKET and ARA_SESSION_TOKEN are required\n",
    );
    process.exit(97);
  }

  const ipc = new GuardIpc({ socketPath: SOCKET });
  try {
    await ipc.connect();
  } catch (err) {
    process.stderr.write(
      `[ara-guard] failed to connect to supervisor at ${SOCKET}: ${err.message}\n`,
    );
    process.exit(98);
  }

  const hello = await ipc.request({
    type: "hello",
    token: TOKEN,
    session: SESSION,
    pid: process.pid,
    ppid: process.ppid,
    argv: process.argv,
    runtime: `node ${process.version}`,
  });

  if (!hello || hello.verdict !== "accept") {
    process.stderr.write(
      `[ara-guard] supervisor rejected handshake: ${JSON.stringify(hello)}\n`,
    );
    process.exit(99);
  }

  const localRules = new LocalRules();
  localRules.load(hello.sync_rules);

  // Hot-reload: the supervisor broadcasts fresh rules when WALLS.md changes.
  ipc._onUnsolicited = (msg) => {
    if (msg && msg.type === "sync_rules_update" && msg.sync_rules) {
      localRules.load(msg.sync_rules);
      process.stderr.write(
        `[ara-guard] rules reloaded (paths=${localRules.pathRules.length}, ` +
          `commands=${localRules.commandRules.length}, ` +
          `approvals=${
            localRules.approvalPathRules.length +
              localRules.approvalCommandRules.length
          })\n`,
      );
    }
  };

  const guard = {
    ipc,
    session: SESSION,
    token: TOKEN,
    socket: SOCKET,
    guardPath: GUARD_PATH,
    localRules,
  };
  localRules.attachGuard(guard);
  globalThis.__ARA_GUARD__ = guard;

  // Register the ESM loader. Subsequent imports of node:fs,
  // node:child_process, node:fs/promises, node:net, node:http, node:https
  // are served from our generated wrappers. This covers ESM named imports
  // that property-mutation cannot reach.
  register(LOADER_URL, {
    parentURL: import.meta.url,
    data: { wrappersUrl: WRAPPERS_URL },
  });

  // Property-mutation hooks. Cover CJS `require("node:fs")` (loader is
  // ESM-only) and namespace / default-import access like `fs.readFile(...)`.
  // Together with the loader above this closes both import styles.
  installFsHook(guard);
  installChildProcessHook(guard);
  installHttpHook(guard);
  installFetchHook(guard);

  // Non-module hooks (process.kill, heartbeat). Kept outside the loader
  // because they patch process-level state, not module imports.
  installProcessKillHook(guard);
  installHeartbeat(guard);

  // Forge-v1 channel bridge: exposes globalThis.ara.channel.call(...)
  // backed by the existing IPC connection. Cooperating agents detect
  // ARA_FORGE_CHANNELS=v1 and use the global directly.
  installForgeChannels(ipc);

  process.stderr.write(
    `[ara-guard] armed session=${SESSION} pid=${process.pid}\n`,
  );
}

await arm();
