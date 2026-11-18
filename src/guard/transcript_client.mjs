// Node-side ARA transcript client. Mirrors src/ara/clients/transcript.py
// for use inside Node-based agents (OpenClaw). Speaks the same
// prompt_intercept IPC over the existing ARA_SOCKET unix socket.
//
// Fail-open: any IPC error returns {action: "allow"} so a hung supervisor
// never wedges the agent loop. The libc shim + Node fs/exec hooks still
// enforce policy at the syscall layer — this channel is for visibility
// and prompt-layer mediation only.

import { Socket } from "node:net";

const PROTO = "v1";

let _seq = 2000;
function nextSeq() { return ++_seq; }

let _conn = null;
let _conn_ready = null;
let _send_lock = Promise.resolve();
let _recv_buf = "";
let _pending = []; // [{seq, resolve}]

function enabled() {
  return process.env.ARA_TRANSCRIPT_PROTOCOL === PROTO
    && !!process.env.ARA_SOCKET
    && !!process.env.ARA_SESSION_TOKEN;
}

async function connect() {
  if (_conn) return _conn;
  if (_conn_ready) return _conn_ready;
  const sockPath = process.env.ARA_SOCKET;
  const token = process.env.ARA_SESSION_TOKEN;
  _conn_ready = new Promise((resolve, reject) => {
    const s = new Socket();
    s.setTimeout(10_000);
    s.once("error", reject);
    s.connect(sockPath, () => {
      s.removeListener("error", reject);
      // Hello handshake.
      const hello = JSON.stringify({
        type: "hello",
        token,
        session: process.env.ARA_SESSION_ID || "",
        pid: process.pid,
        ppid: process.ppid,
        runtime: "node-transcript-client",
        seq: nextSeq(),
      }) + "\n";
      s.write(hello);
      // Drain hello_ack (one line).
      let helloLine = "";
      const onData = (chunk) => {
        helloLine += chunk.toString("utf8");
        const nl = helloLine.indexOf("\n");
        if (nl !== -1) {
          s.removeListener("data", onData);
          // Push the rest of the buffer onto the recv state.
          _recv_buf = helloLine.slice(nl + 1);
          s.on("data", onMessage);
          _conn = s;
          resolve(s);
        }
      };
      s.on("data", onData);
    });
  }).catch(() => null);
  return _conn_ready;
}

function onMessage(chunk) {
  _recv_buf += chunk.toString("utf8");
  let nl;
  while ((nl = _recv_buf.indexOf("\n")) !== -1) {
    const line = _recv_buf.slice(0, nl);
    _recv_buf = _recv_buf.slice(nl + 1);
    if (!line.trim()) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    const seq = msg.seq;
    const idx = _pending.findIndex((p) => p.seq === seq);
    if (idx !== -1) {
      const { resolve } = _pending.splice(idx, 1)[0];
      resolve(msg);
    }
  }
}

export async function intercept(role, payload, { model } = {}) {
  if (!enabled()) return { action: "allow" };
  const sock = await connect();
  if (!sock) return { action: "allow" };
  const seq = nextSeq();
  const msg = JSON.stringify({
    type: "prompt_intercept",
    seq, role, payload, pid: process.pid, model: model || null,
  }) + "\n";
  // Serialize sends so two callers don't interleave on the socket.
  let waiter;
  const wait = new Promise((resolve) => { waiter = { seq, resolve }; });
  _pending.push(waiter);
  _send_lock = _send_lock.then(() => new Promise((res) => {
    sock.write(msg, () => res());
  }));
  try {
    await _send_lock;
    const reply = await Promise.race([
      wait,
      new Promise((r) => setTimeout(() => r({ action: "allow", _timeout: true }), 5_000)),
    ]);
    return reply;
  } catch {
    return { action: "allow" };
  }
}

export function emit(role, payload, opts) {
  // Fire-and-forget — supervisor still records the turn.
  intercept(role, payload, opts).catch(() => {});
}
