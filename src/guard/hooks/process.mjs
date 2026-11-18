// process.kill hook: refuse signals aimed at the supervisor.
//
// Under the Gated constraint the worker runs as the same user, so
// POSIX-level `kill` succeeds by default. This hook catches calls routed
// through Node's `process.kill` / `process._kill`.

export function installProcessKillHook(guard) {
  const supPid = Number(process.env.ARA_SUPERVISOR_PID || "0");
  if (!supPid) return;

  const origKill = process.kill.bind(process);
  process.kill = function patchedKill(pid, signal) {
    const target = Number(pid);
    if (target === supPid || target === -supPid) {
      try {
        guard.ipc.send({
          type: "event",
          kind: "kill_blocked",
          target: target,
          signal: String(signal ?? "SIGTERM"),
          pid: process.pid,
        });
      } catch {
        // best-effort log
      }
      const err = new Error(
        `ARA_DENY: signals to supervisor pid ${supPid} are forbidden`,
      );
      err.code = "ARA_KILL_BLOCKED";
      throw err;
    }
    return origKill(target, signal);
  };
}
