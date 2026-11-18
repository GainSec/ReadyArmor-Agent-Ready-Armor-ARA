"""Pack-Forge T9 step 6 — UI-path integration over the live armory HTTP
server. Drives the wired routes end-to-end: render the curate panel, POST
/forge/<id>/pack/curate with a Seal, and confirm a runtime pack.json lands
with the dropped item excluded; GET /forge/packs shows the promote button.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.parse
from pathlib import Path

from ara.armory.app import run_armory
from ara.armory.auth import ensure_token
from ara.forge import state as fstate
from ara.forge.alphabets import pack as pack_mod
from ara.forge.phases import synthesize
from ara.armory import forge_actions
from ara.forge.session import ForgeConfig, ForgeKind, ForgePhase, ForgeSession


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start(state: Path) -> int:
    port = _free_port()
    t = threading.Thread(
        target=run_armory,
        kwargs={
            "state_root": state, "walls_file": None,
            "bind": "127.0.0.1", "port": port,
            "tls_cert": None, "tls_key": None, "behind_proxy": False,
        },
        daemon=True,
    )
    t.start()
    for _ in range(30):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/healthz")
            conn.getresponse().read()
            conn.close()
            return port
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("armory did not start")


def _pack_session_ready(state: Path, name="dom") -> ForgeSession:
    """A pack session synthesized to a draft, ready for curate."""
    s = ForgeSession.new(state, weapon_target_id=name, operator_id="op",
                         config=ForgeConfig(kind=ForgeKind.PACK.value))
    s.phase = ForgePhase.REVIEW
    s._persist()
    path = fstate.forge_extractions_path(state, s.forge_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"channel": "propose.alphabet",
         "body": {"name": "category", "shape": "categorical",
                  "fields": [{"name": "id", "type": "str"}]}},
        {"channel": "extract.conc",
         "body": {"alphabet_name": "category", "entry": {"id": "sqli"}}},
        {"channel": "extract.conc",
         "body": {"alphabet_name": "category", "entry": {"id": "drop_me"}}},
        {"channel": "propose.binding",
         "body": {"field": "category", "rule": "FIELD_IN_ALPHABET",
                  "alphabet": "category"}},
    ]
    with path.open("w") as f:
        for i, ev in enumerate(events):
            f.write(json.dumps({"ts": 0.0, "manifest_element_id": "m1",
                                "channel": ev["channel"], "body": ev["body"],
                                "extraction_id": f"e{i}", "citation": {}}) + "\n")
    forge_actions.handle_forge_synthesize(state, s.forge_id, {})
    return s


def _post(port: int, path: str, body: dict) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST", path,
        body=urllib.parse.urlencode(body),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return conn.getresponse()


def test_pack_curate_and_seal_over_http(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    tok = ensure_token(state)
    s = _pack_session_ready(state, "dom")

    # sanity: two entries exist in the draft, one we will drop
    d = synthesize.load_pack_draft(state, s.forge_id)
    entries = d.items_for("entries")
    assert len(entries) == 2
    drop_idx = next(i for i, it in enumerate(entries)
                    if it["data"]["entry"]["id"] == "drop_me")

    port = _start(state)

    # the curate panel is rendered on the forge detail page
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/forge/{s.forge_id}?t={tok}")
    resp = conn.getresponse()
    html = resp.read().decode()
    assert resp.status == 200
    assert "pack/curate" in html
    conn.close()

    # keep everything EXCEPT the drop_me entry; press Seal
    body = {"seal": "1"}
    for g, n in [("alphabets", 1), ("entries", 2), ("bindings", 1),
                 ("chunks", 5), ("policy", 1)]:
        for i in range(n):
            if g == "entries" and i == drop_idx:
                continue  # omit → dropped
            body[f"keep_{g}_{i}"] = "on"
    resp = _post(port, f"/forge/{s.forge_id}/pack/curate?t={tok}", body)
    assert resp.status in (200, 303), resp.read()
    resp.read()

    # the runtime pack landed, and the dropped entry is not in it
    pk_path = pack_mod.runtime_packs_dir(state) / "dom.json"
    assert pk_path.exists()
    pk = pack_mod.load_pack(pk_path)
    cat = next(a for a in pk.alphabets if a.name == "category")
    ids = {e_["id"] for e_ in cat.starter}
    assert "sqli" in ids and "drop_me" not in ids


def test_packs_list_shows_promote_over_http(short_tmp: Path):
    state = short_tmp / "state"
    state.mkdir()
    tok = ensure_token(state)
    # drop a runtime pack straight into the store
    rd = pack_mod.runtime_packs_dir(state)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "dom.json").write_text(json.dumps({
        "name": "dom", "version": "0.1.0", "description": "forged",
        "alphabets": [], "validator_bindings": [], "chunk_templates": [],
        "policy_defaults": {}, "manifest_detection": {},
    }))
    port = _start(state)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/forge/packs?t={tok}")
    resp = conn.getresponse()
    html = resp.read().decode()
    assert resp.status == 200
    assert "/forge/packs/dom/promote" in html
    conn.close()
