// Heartbeat: send a periodic event so the supervisor can detect a silenced guard.

export function installHeartbeat(guard, { intervalMs = 2000 } = {}) {
  const send = () => {
    try {
      guard.ipc.send({
        type: "event",
        kind: "heartbeat",
        pid: process.pid,
        at: Date.now(),
      });
    } catch {
      // best-effort
    }
  };
  send();
  const timer = setInterval(send, intervalMs);
  timer.unref?.();
}
