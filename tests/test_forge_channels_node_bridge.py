"""Node-side tests for the forge_channels guard hook.

Drives ``installForgeChannels`` against a stub IPC inside a Node child
process and asserts the resulting ``globalThis.ara.channel.call(...)``
behaves correctly: success, channel rejection, transport timeout,
discovery flag, idempotent install.

Plus one full-stack live-socket test: spins up a real supervisor, has
Node connect over the unix socket via the actual ``GuardIpc``, and
makes a channel call end-to-end.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "src" / "guard" / "hooks" / "forge_channels.mjs"
GUARD_IPC_PATH = REPO_ROOT / "src" / "guard" / "ipc.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(not _have_node(), reason="node not installed")


def _run_node(script: str, *, timeout: float = 10.0,
              env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        env=full_env, capture_output=True, text=True, timeout=timeout,
    )


# ---------- pure unit tests with stub IPC ----------

def test_install_exposes_global_and_protocol_flag() -> None:
    script = f"""
import {{ installForgeChannels }} from {json.dumps(str(HOOK_PATH))};

const ipc = {{ request: async () => ({{ type: "channel_call_response",
                                          ok: true, result: {{ x: 1 }} }}) }};
installForgeChannels(ipc);

if (!globalThis.ara || !globalThis.ara.channel) {{
    console.error("FAIL: globalThis.ara.channel not installed");
    process.exit(2);
}}
if (globalThis.ara.channel.protocol !== "v1") {{
    console.error("FAIL: protocol not v1");
    process.exit(3);
}}
if (process.env.ARA_FORGE_CHANNELS !== "v1") {{
    console.error("FAIL: ARA_FORGE_CHANNELS env not set");
    process.exit(4);
}}
if (!globalThis.ara.channel.available) {{
    console.error("FAIL: channel.available not true");
    process.exit(5);
}}
console.log("ok");
"""
    proc = _run_node(script)
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_call_returns_result_dict_on_ok() -> None:
    script = f"""
import {{ installForgeChannels }} from {json.dumps(str(HOOK_PATH))};

let captured;
const ipc = {{
    request: async (msg) => {{
        captured = msg;
        return {{ type: "channel_call_response", ok: true,
                  channel: msg.channel, result: {{ fact_id: "f:abc" }} }};
    }},
}};
installForgeChannels(ipc);

const r = await globalThis.ara.channel.call("fact.assert", {{ predicate: "x" }});
if (r.fact_id !== "f:abc") {{
    console.error("FAIL: result not propagated:", JSON.stringify(r));
    process.exit(2);
}}
if (captured.type !== "channel_call" || captured.channel !== "fact.assert") {{
    console.error("FAIL: outbound msg shape:", JSON.stringify(captured));
    process.exit(3);
}}
console.log("ok");
"""
    proc = _run_node(script)
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_call_rejects_with_channel_call_error_on_supervisor_rejection() -> None:
    script = f"""
import {{ installForgeChannels, ChannelCallError }} from {json.dumps(str(HOOK_PATH))};

const ipc = {{
    request: async () => ({{
        type: "channel_call_response", ok: false,
        error_kind: "channel_rejection",
        error: "missing required field",
    }}),
}};
installForgeChannels(ipc);

let caught;
try {{
    await globalThis.ara.channel.call("fact.assert", {{}});
}} catch (e) {{
    caught = e;
}}
if (!caught || caught.code !== "channel_rejection") {{
    console.error("FAIL: wrong error", caught);
    process.exit(2);
}}
if (!(caught instanceof ChannelCallError)) {{
    console.error("FAIL: not ChannelCallError instance");
    process.exit(3);
}}
if (caught.channel !== "fact.assert") {{
    console.error("FAIL: channel not propagated");
    process.exit(4);
}}
console.log("ok");
"""
    proc = _run_node(script)
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_call_translates_transport_errors() -> None:
    script = f"""
import {{ installForgeChannels }} from {json.dumps(str(HOOK_PATH))};

const ipc = {{
    request: async () => {{ throw new Error("ARA_REQUEST_TIMEOUT"); }},
}};
installForgeChannels(ipc);

let caught;
try {{
    await globalThis.ara.channel.call("fact.assert", {{}});
}} catch (e) {{
    caught = e;
}}
if (caught.code !== "timeout") {{
    console.error("FAIL: expected timeout, got", caught.code);
    process.exit(2);
}}

const ipc2 = {{
    request: async () => {{ throw new Error("ARA_SOCKET_CLOSED"); }},
}};
delete globalThis.ara.channel;
installForgeChannels(ipc2);
let caught2;
try {{
    await globalThis.ara.channel.call("fact.assert", {{}});
}} catch (e) {{
    caught2 = e;
}}
if (caught2.code !== "transport_closed") {{
    console.error("FAIL: expected transport_closed, got", caught2.code);
    process.exit(3);
}}
console.log("ok");
"""
    proc = _run_node(script)
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_call_rejects_on_invalid_arguments() -> None:
    script = f"""
import {{ installForgeChannels }} from {json.dumps(str(HOOK_PATH))};

const ipc = {{ request: async () => ({{}}) }};
installForgeChannels(ipc);

let e1;
try {{ await globalThis.ara.channel.call("", {{}}); }} catch (e) {{ e1 = e; }}
if (e1?.code !== "invalid_channel") {{ console.error("FAIL e1", e1); process.exit(2); }}

let e2;
try {{ await globalThis.ara.channel.call("x.y", "not an object"); }} catch (e) {{ e2 = e; }}
if (e2?.code !== "invalid_body") {{ console.error("FAIL e2", e2); process.exit(3); }}
console.log("ok");
"""
    proc = _run_node(script)
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_emit_tag_helper_dispatches_chunks_emit_tag() -> None:
    script = f"""
import {{ installForgeChannels }} from {json.dumps(str(HOOK_PATH))};

let captured;
const ipc = {{
    request: async (msg) => {{
        captured = msg;
        return {{ type: "channel_call_response", ok: true,
                  result: {{ to_chunk: "probe" }} }};
    }},
}};
installForgeChannels(ipc);

const r = await globalThis.ara.channel.emitTag("<go>");
if (captured.channel !== "chunks.emit_tag") {{
    console.error("FAIL: wrong channel", captured.channel); process.exit(2);
}}
if (captured.body.tag !== "<go>") {{
    console.error("FAIL: wrong tag", captured.body); process.exit(3);
}}
if (r.to_chunk !== "probe") {{ console.error("FAIL: result", r); process.exit(4); }}
console.log("ok");
"""
    proc = _run_node(script)
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_install_is_idempotent_for_same_globalthis() -> None:
    script = f"""
import {{ installForgeChannels }} from {json.dumps(str(HOOK_PATH))};

const ipc = {{ request: async () => ({{ type: "channel_call_response",
                                          ok: true, result: {{}} }}) }};
const a = installForgeChannels(ipc);
const b = installForgeChannels(ipc);
if (a !== b) {{ console.error("FAIL: not same instance"); process.exit(2); }}
if (globalThis.ara.channel !== a) {{ console.error("FAIL: global drifted"); process.exit(3); }}
console.log("ok");
"""
    proc = _run_node(script)
    assert proc.returncode == 0, f"stderr={proc.stderr}"


# ---------- live-socket integration (full stack) ----------

@pytest.mark.asyncio
async def test_node_bridge_calls_real_supervisor_over_socket(short_tmp: Path) -> None:
    """Spin up the real Python supervisor + the real GuardIpc + the
    forge_channels hook in a Node child. Verify a channel call routes
    through the bridge end-to-end."""
    from ara import weapons as weapons_mod
    from ara.forge.chunks.chunkdef import ChunkDef, write_chunkdef
    from ara.runtime import audit as raudit
    from ara.runtime.binder.api import EvidenceBinder
    from ara.runtime.channels import registry
    from ara.supervisor import Supervisor

    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()

    # Seal a minimal weapon
    wid = "w-bridge"
    wdir = short_tmp / "weapons" / wid
    (wdir / "chunks").mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({
        "weapon_target_id": wid, "crumbs": [], "shapes": [],
    }))
    (wdir / "seal.json").write_text(json.dumps({"weapon_id": wid}))
    write_chunkdef(wdir / "chunks" / "enum.json", ChunkDef(
        id="enum", title="x", prompt_fragment="x",
        tool_surface=["fact.assert", "facts.query"],
        is_entry=True, is_terminal=True,
    ))
    weapons_mod.set_equipped_weapon(short_tmp, wid)

    sup = await Supervisor.start(state_root=short_tmp)
    try:
        # Drop a piece of evidence so fact.assert has something to cite
        eng_dir = short_tmp / "sessions" / sup.session.id
        binder = EvidenceBinder(eng_dir)
        rec = binder.put(
            b"x", source="tool:curl", source_metadata={}, summary="s",
            activation_id=sup.engagement.activation_manager.current(),
        )

        socket_path = sup.session.socket_path
        token = sup.session.token
        events_file = short_tmp / "node_events.jsonl"

        script = f"""
import {{ GuardIpc }} from {json.dumps(str(GUARD_IPC_PATH))};
import {{ installForgeChannels }} from {json.dumps(str(HOOK_PATH))};
import fs from "node:fs";

const ipc = new GuardIpc({{ socketPath: {json.dumps(str(socket_path))} }});
await ipc.connect();

// Handshake (matches what real guards do; supervisor-side validates token).
const hello = await ipc.request({{
    type: "hello", token: {json.dumps(token)},
    pid: process.pid, ppid: process.ppid,
    runtime: "node-bridge-test", argv: process.argv,
}});
if (hello.verdict !== "accept") {{
    console.error("FAIL hello:", JSON.stringify(hello));
    process.exit(2);
}}

installForgeChannels(ipc);

// Call fact.assert via the global API.
const result = await globalThis.ara.channel.call("fact.assert", {{
    predicate: "target.host", value: "example.com",
    asserted_by_crumb: "c1", evidence_refs: [{json.dumps(rec.id)}],
}});

fs.writeFileSync(
    {json.dumps(str(events_file))},
    JSON.stringify(result) + "\\n",
);

// Clean shutdown
ipc.socket?.end();
process.exit(0);
"""
        loop = asyncio.get_event_loop()
        proc = await loop.run_in_executor(None, lambda: subprocess.run(
            ["node", "--input-type=module", "-e", script],
            env={**os.environ}, capture_output=True, text=True, timeout=15,
        ))
        assert proc.returncode == 0, (
            f"node exited {proc.returncode}\nstderr={proc.stderr}\nstdout={proc.stdout}"
        )
        result = json.loads(events_file.read_text().splitlines()[0])
        assert result["fact"]["predicate"] == "target.host"
    finally:
        if sup._reload_task: sup._reload_task.cancel()
        if sup._watchdog_task: sup._watchdog_task.cancel()
        if sup._bulwark_task: sup._bulwark_task.cancel()
        await sup.server.stop()


@pytest.mark.asyncio
async def test_node_bridge_surfaces_supervisor_rejection(short_tmp: Path) -> None:
    """Tool-not-in-surface from the supervisor surfaces as a
    ChannelCallError on the Node side."""
    from ara import weapons as weapons_mod
    from ara.forge.chunks.chunkdef import ChunkDef, write_chunkdef
    from ara.runtime import audit as raudit
    from ara.runtime.channels import registry
    from ara.supervisor import Supervisor

    raudit.reset_registry_for_tests()
    registry.unregister_for_tests()

    wid = "w-bridge2"
    wdir = short_tmp / "weapons" / wid
    (wdir / "chunks").mkdir(parents=True)
    (wdir / "weapon.json").write_text(json.dumps({"weapon_target_id": wid}))
    (wdir / "seal.json").write_text(json.dumps({"weapon_id": wid}))
    write_chunkdef(wdir / "chunks" / "enum.json", ChunkDef(
        id="enum", title="x", prompt_fragment="x",
        tool_surface=["facts.query"],   # finding.file deliberately absent
        is_entry=True, is_terminal=True,
    ))
    weapons_mod.set_equipped_weapon(short_tmp, wid)

    sup = await Supervisor.start(state_root=short_tmp)
    try:
        socket_path = sup.session.socket_path
        token = sup.session.token

        script = f"""
import {{ GuardIpc }} from {json.dumps(str(GUARD_IPC_PATH))};
import {{ installForgeChannels }} from {json.dumps(str(HOOK_PATH))};

const ipc = new GuardIpc({{ socketPath: {json.dumps(str(socket_path))} }});
await ipc.connect();
await ipc.request({{ type: "hello", token: {json.dumps(token)},
                     pid: process.pid, ppid: process.ppid,
                     runtime: "test", argv: [] }});
installForgeChannels(ipc);

let caught;
try {{
    await globalThis.ara.channel.call("finding.file", {{}});
}} catch (e) {{
    caught = e;
}}
if (!caught || caught.code !== "tool_not_in_surface") {{
    console.error("FAIL: expected tool_not_in_surface, got", caught?.code);
    process.exit(2);
}}
console.log("ok");
ipc.socket?.end();
process.exit(0);
"""
        loop = asyncio.get_event_loop()
        proc = await loop.run_in_executor(None, lambda: subprocess.run(
            ["node", "--input-type=module", "-e", script],
            env={**os.environ}, capture_output=True, text=True, timeout=15,
        ))
        assert proc.returncode == 0, (
            f"node exited {proc.returncode}\nstderr={proc.stderr}\nstdout={proc.stdout}"
        )
    finally:
        if sup._reload_task: sup._reload_task.cancel()
        if sup._watchdog_task: sup._watchdog_task.cancel()
        if sup._bulwark_task: sup._bulwark_task.cancel()
        await sup.server.stop()
