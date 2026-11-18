// Forge-v1 channel bridge for Node-runtime agents (OpenClaw, generic Node LLM agents).
//
// Installs ``globalThis.ara.channel`` — a small Promise-based client over the
// guard's existing IPC connection. Cooperating agents detect availability via
// ``process.env.ARA_FORGE_CHANNELS === "v1"`` and call channels directly:
//
//   const result = await globalThis.ara.channel.call("fact.assert", {
//     predicate: "target.host", value: "example.com",
//     asserted_by_crumb: "c1", evidence_refs: ["e:abc..."],
//   });
//
// The Python supervisor's ``_on_channel_call`` handler dispatches the request
// against the active engagement's registry, enforces tool-surface, audit-logs
// the result, and returns the result dict (or rejection error).
//
// On rejection (validation failure, tool-not-in-surface, no-weapon-equipped,
// channel error), the call rejects with an Error whose ``code`` matches
// the supervisor's ``error_kind`` and ``channel`` carries the channel name.

const FORGE_CHANNELS_PROTOCOL = "v1";

export class ChannelCallError extends Error {
  constructor(code, message, { channel = null } = {}) {
    super(`${code}: ${message}`);
    this.name = "ChannelCallError";
    this.code = code;
    this.channel = channel;
  }
}

export function installForgeChannels(ipc, { timeoutMs = 30000 } = {}) {
  // Install the global API. Re-installing is a no-op (idempotent).
  if (globalThis.ara && globalThis.ara.channel) {
    return globalThis.ara.channel;
  }
  const channel = {
    /**
     * Call a typed forge channel by name. Resolves with the supervisor's
     * ``result`` dict; rejects with :class:`ChannelCallError` on
     * supervisor-side rejection or transport error.
     *
     * @param {string} name    channel name (e.g. ``"fact.assert"``)
     * @param {object} body    body matching the channel's schema
     * @param {{timeoutMs?:number}=} opts
     * @returns {Promise<object>}
     */
    async call(name, body, opts = {}) {
      if (typeof name !== "string" || !name) {
        throw new ChannelCallError("invalid_channel", "channel name required");
      }
      if (body !== undefined && body !== null && typeof body !== "object") {
        throw new ChannelCallError("invalid_body", "body must be an object");
      }
      let resp;
      try {
        resp = await ipc.request(
          { type: "channel_call", channel: name, body: body || {} },
          { timeoutMs: opts.timeoutMs ?? timeoutMs },
        );
      } catch (err) {
        const code = err?.message === "ARA_REQUEST_TIMEOUT" ? "timeout"
                   : err?.message === "ARA_SOCKET_CLOSED" ? "transport_closed"
                   : "transport_error";
        throw new ChannelCallError(code, err?.message ?? String(err), { channel: name });
      }
      if (!resp || resp.type !== "channel_call_response") {
        throw new ChannelCallError(
          "protocol_error",
          `unexpected response type: ${resp?.type}`,
          { channel: name },
        );
      }
      if (!resp.ok) {
        throw new ChannelCallError(
          resp.error_kind ?? "unknown",
          resp.error ?? "channel call failed",
          { channel: name },
        );
      }
      return resp.result ?? {};
    },

    /**
     * Convenience: emit a chunk-swap tag.
     * @param {string} tag e.g. ``"<go>"`` or ``"<pause>"``
     * @returns {Promise<object>}
     */
    async emitTag(tag) {
      return channel.call("chunks.emit_tag", { tag });
    },

    /**
     * Discovery — true when the bridge is installed and bound to a
     * supervisor. Cooperating agents check this before calling.
     */
    available: true,

    /** Protocol version pinned to ``ARA_FORGE_CHANNELS``. */
    protocol: FORGE_CHANNELS_PROTOCOL,
  };

  globalThis.ara = globalThis.ara || {};
  globalThis.ara.channel = channel;

  // Discovery flag for cooperating agents. Set on env so child processes
  // inherit it (they get a fresh guard via NODE_OPTIONS so they re-arm).
  process.env.ARA_FORGE_CHANNELS = FORGE_CHANNELS_PROTOCOL;

  return channel;
}

// For tests / advanced callers that want to install against a custom
// IPC-shaped object (one with ``.request({...}, {timeoutMs})``).
export function _installForTesting(ipcLike) {
  // Reset any prior install so test isolation works.
  if (globalThis.ara) delete globalThis.ara.channel;
  return installForgeChannels(ipcLike);
}
