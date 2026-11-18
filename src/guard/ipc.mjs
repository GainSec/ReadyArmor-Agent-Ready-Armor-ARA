// Minimal IPC client for ARA-GUARD.
// Line-delimited JSON over a unix socket.
//
// Ref/unref policy: the socket is unref'd by default so it doesn't pin the
// worker's event loop. While a request is outstanding, we ref the socket so
// the loop stays alive until the reply arrives, then unref again.

import net from "node:net";

export class GuardIpc {
  constructor({ socketPath }) {
    this.socketPath = socketPath;
    this.socket = null;
    this._buffer = "";
    this._pending = new Map(); // seq -> {resolve, reject}
    this._seq = 0;
    this._onEvent = null;
    this._closed = false;
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.socket = net.createConnection(this.socketPath);
      this.socket.once("connect", resolve);
      this.socket.once("error", reject);
    });
    this.socket.setEncoding("utf8");
    this.socket.on("data", (chunk) => this._onData(chunk));
    this.socket.on("error", () => {}); // swallow; transport is best-effort
    this.socket.on("close", () => {
      this._closed = true;
      for (const [, p] of this._pending) p.reject(new Error("ARA_SOCKET_CLOSED"));
      this._pending.clear();
    });
    // Default: don't pin the loop just for this socket.
    this.socket.unref();
  }

  _refIfNeeded() {
    if (this._pending.size === 1 && this.socket && !this._closed) this.socket.ref();
  }

  _unrefIfIdle() {
    if (this._pending.size === 0 && this.socket && !this._closed) this.socket.unref();
  }

  _onData(chunk) {
    this._buffer += chunk;
    let idx;
    while ((idx = this._buffer.indexOf("\n")) >= 0) {
      const line = this._buffer.slice(0, idx);
      this._buffer = this._buffer.slice(idx + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        continue;
      }
      if (msg && msg.seq !== undefined && this._pending.has(msg.seq)) {
        const p = this._pending.get(msg.seq);
        this._pending.delete(msg.seq);
        this._unrefIfIdle();
        p.resolve(msg);
      } else if (msg && this._onUnsolicited) {
        this._onUnsolicited(msg);
      } else if (this._onEvent) {
        this._onEvent(msg);
      }
    }
  }

  send(obj) {
    if (!this.socket || this._closed) return;
    this.socket.write(JSON.stringify(obj) + "\n");
  }

  async request(obj, { timeoutMs = 2000 } = {}) {
    if (!this.socket || this._closed) throw new Error("ARA_SOCKET_CLOSED");
    const seq = ++this._seq;
    const payload = { ...obj, seq };
    const promise = new Promise((resolve, reject) => {
      this._pending.set(seq, { resolve, reject });
      this._refIfNeeded();
      const timer = setTimeout(() => {
        if (this._pending.has(seq)) {
          this._pending.delete(seq);
          this._unrefIfIdle();
          reject(new Error("ARA_REQUEST_TIMEOUT"));
        }
      }, timeoutMs);
      timer.unref?.();
    });
    this.socket.write(JSON.stringify(payload) + "\n");
    return promise;
  }
}
