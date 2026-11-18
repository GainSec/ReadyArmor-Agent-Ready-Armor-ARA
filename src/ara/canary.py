"""Canary (honeytoken) management.

Seed realistic-looking but fake credentials into filesystem paths an
agent might read. When the agent echoes one back through an LLM reply
(indicating exfiltration intent), a Bulwark reply-layer rule fires and
blocks the response.

Model: one ``canaries.json`` per state root, each entry is a (path,
value, kind) triple. ``ara canary add`` creates the file + registers;
``ara canary list/remove/verify`` manages them; the MITM consults the
registered values on every reply body.

Canary values are intentionally unique and high-entropy so collisions
with real content are negligible. Format:

  ARA-CANARY-<kind>-<12-hex>-<12-hex>

where ``<kind>`` is a short tag (``aws``, ``stripe``, ``github``, …)
so a hit tells operators what the agent thought it was stealing.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Canary:
    id: str
    kind: str                      # "aws" | "stripe" | "github" | "generic" | ...
    path: str                      # absolute file path where the canary lives
    value: str                     # the exact string seeded + watched
    created_at: float
    note: Optional[str] = None
    last_hit_at: Optional[float] = None
    hit_count: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Store:
    canaries: list[Canary] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"canaries": [c.to_dict() for c in self.canaries]}


# ---- templates --------------------------------------------------------

_TEMPLATES: dict[str, tuple[str, str]] = {
    # kind -> (value_template, file_body_template)
    # {TOKEN} = full canary value, {UID} = short unique id
    "aws": (
        "AKIAARACANARY{UID}",
        "[default]\naws_access_key_id = {TOKEN}\n"
        "aws_secret_access_key = ARACANARY/{UID}/{RAND}\n",
    ),
    "stripe": (
        "sk_live_ARACANARY{UID}",
        "STRIPE_SECRET_KEY={TOKEN}\n",
    ),
    "github": (
        "ghp_ARACANARY{UID}{RAND}",
        "GITHUB_TOKEN={TOKEN}\n",
    ),
    "openai": (
        "sk-ARACANARY{UID}{RAND}",
        "OPENAI_API_KEY={TOKEN}\n",
    ),
    # OpenClaw gateway pairing token: bare 32-hex-chars in a JSON
    # entry shaped like ~/.openclaw/devices/paired.json. Chosen so
    # that if an agent exfiltrates an operator.admin token the value
    # looks indistinguishable from real paired-device tokens until
    # it's echoed back in a reply.
    "openclaw_pair": (
        # 32 lower-hex, with ARACANR7 nested inside to keep the
        # false-positive rate negligible against arbitrary UUIDs.
        "{UID16}aracan{RAND10}",
        '{{\n'
        '  "__ara_canary": "{TOKEN}",\n'
        '  "deviceId": "ARACAN-{UID}",\n'
        '  "role": "operator",\n'
        '  "scopes": ["operator.admin","operator.approvals",'
        '"operator.pairing","operator.read","operator.write"],\n'
        '  "tokens": {{\n'
        '    "operator": {{\n'
        '      "token": "{TOKEN}",\n'
        '      "role": "operator",\n'
        '      "scopes": ["operator.admin","operator.approvals",'
        '"operator.pairing"],\n'
        '      "createdAtMs": 1769557417403\n'
        '    }}\n'
        '  }},\n'
        '  "displayName": "gateway:ara-canary"\n'
        '}}\n',
    ),
    "generic": (
        "ARA-CANARY-{UID}-{RAND}",
        "# canary seeded by ARA — do not use\nCANARY_TOKEN={TOKEN}\n",
    ),
}


def _render_templates(kind: str) -> tuple[str, str]:
    tmpl = _TEMPLATES.get(kind, _TEMPLATES["generic"])
    uid = uuid.uuid4().hex[:12].upper()
    rand = secrets.token_hex(8).upper()
    uid16 = uuid.uuid4().hex[:16]
    rand10 = secrets.token_hex(5)
    value = tmpl[0].format(UID=uid, RAND=rand, UID16=uid16, RAND10=rand10)
    body = tmpl[1].format(
        TOKEN=value, UID=uid, RAND=rand,
        UID16=uid16, RAND10=rand10,
    )
    return value, body


def seed_defaults(state_root: Path) -> list[Canary]:
    """Seed the recommended default canary set if none exist.

    Runs on armory startup. Today: one ``openclaw_pair`` canary
    planted at ``~/.openclaw-ara-canaries/paired-stub.json`` — the
    file looks like a real paired-device entry, so any agent that
    reads it and echoes the value into a reply fires a
    ``canary_hit``. The path is distinct from the real
    ``~/.openclaw/devices/paired.json`` so we don't collide with
    live openclaw state, but sits under the openclaw parent dir so
    opportunistic filesystem scans still find it.

    Idempotent: if any canary already exists in the store, this is
    a no-op (operators who have deliberately curated canaries see
    no surprise additions).
    """
    existing = load(state_root)
    if existing.canaries:
        return []
    home = Path.home()
    canary_dir = home / ".openclaw-ara-canaries"
    stub_path = canary_dir / "paired-stub.json"
    if stub_path.exists():
        return []
    try:
        c = add(
            state_root, stub_path, kind="openclaw_pair",
            note="default ARA canary: operator.admin-token shape, "
                 "fires if echoed back in any LLM reply",
        )
        return [c]
    except (FileExistsError, OSError):
        return []


# ---- store load / save -----------------------------------------------

def path_for(state_root: Path) -> Path:
    return state_root / "canaries.json"


def load(state_root: Path) -> Store:
    p = path_for(state_root)
    if not p.exists():
        return Store()
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return Store()
    return Store(canaries=[Canary(**c) for c in raw.get("canaries") or []])


def save(state_root: Path, store: Store) -> None:
    p = path_for(state_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store.to_dict(), indent=2))


# ---- operations ------------------------------------------------------

def add(
    state_root: Path, target_path: Path, *,
    kind: str = "generic", note: str | None = None,
) -> Canary:
    """Seed a canary at ``target_path`` and register it.

    Writes the template body to the path (parent dirs created as needed).
    Idempotent on path collision — raises FileExistsError so operators
    deliberately pick unique locations.
    """
    target_path = Path(target_path).expanduser().resolve()
    if target_path.exists():
        raise FileExistsError(f"{target_path} already exists — pick a fresh path")
    value, body = _render_templates(kind)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(body)
    try:
        # Restrict so only the current user can read it — makes the
        # canary look like a real protected secret.
        os.chmod(target_path, 0o600)
    except OSError:
        pass
    c = Canary(
        id="can_" + uuid.uuid4().hex[:10],
        kind=kind,
        path=str(target_path),
        value=value,
        created_at=time.time(),
        note=note,
    )
    store = load(state_root)
    store.canaries.append(c)
    save(state_root, store)
    return c


def remove(state_root: Path, canary_id: str, *, delete_file: bool = False) -> bool:
    store = load(state_root)
    keep: list[Canary] = []
    removed: Canary | None = None
    for c in store.canaries:
        if c.id == canary_id:
            removed = c
        else:
            keep.append(c)
    if removed is None:
        return False
    if delete_file:
        try:
            Path(removed.path).unlink()
        except OSError:
            pass
    store.canaries = keep
    save(state_root, store)
    return True


def check_content(state_root: Path, content: str) -> list[Canary]:
    """Return every canary whose ``value`` appears in ``content``.

    Called by the MITM on every reply body. Case-sensitive exact
    substring — canary values are high-entropy, so false positives are
    negligible.
    """
    if not content:
        return []
    store = load(state_root)
    hits: list[Canary] = []
    for c in store.canaries:
        if c.value and c.value in content:
            hits.append(c)
    return hits


def record_hit(state_root: Path, canary_id: str) -> None:
    store = load(state_root)
    for c in store.canaries:
        if c.id == canary_id:
            c.hit_count += 1
            c.last_hit_at = time.time()
            break
    save(state_root, store)


def verify_all(state_root: Path) -> dict:
    """Confirm every canary's seed file still exists and contains its value."""
    store = load(state_root)
    ok: list[str] = []
    missing: list[str] = []
    tampered: list[str] = []
    for c in store.canaries:
        p = Path(c.path)
        if not p.exists():
            missing.append(c.id)
            continue
        try:
            if c.value in p.read_text():
                ok.append(c.id)
            else:
                tampered.append(c.id)
        except OSError:
            missing.append(c.id)
    return {"total": len(store.canaries), "ok": ok,
            "missing": missing, "tampered": tampered}
