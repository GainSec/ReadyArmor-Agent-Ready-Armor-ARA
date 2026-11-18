"""Named chunk-graph workflows the operator picks when starting a pack forge.

A pack's chunk graph is the *investigation workflow* (enumerate → probe → …),
orthogonal to its vocabulary. The forge agent rarely proposes its own, so the
draft seeds a baseline. Rather than always seeding the offsec (web/recon) loop,
the operator selects a workflow at forge-create; ``synthesize_pack`` seeds that
one. Workflows live as JSON under ``seed/chunk-workflows/`` — each
``{name, description, chunks:[node,...]}`` where a node has the same shape as a
pack ``chunk_templates`` entry (id, title, prompt_fragment, tool_surface,
exits, gems, is_entry/is_terminal/can_emit_finding).
"""
from __future__ import annotations

import json
from pathlib import Path

_WORKFLOWS_DIR = Path(__file__).parent.parent / "seed" / "chunk-workflows"

DEFAULT = "general"


def workflows_dir() -> Path:
    return _WORKFLOWS_DIR


def forged_workflows_dir(state_root) -> Path:
    """Where FORGED workflows (derived from mats by the agent) live — separate
    from the seed backups so the two never clash and derived ones can be
    re-derived/overwritten freely."""
    return Path(state_root) / "chunk-workflows"


def _search_dirs(state_root=None) -> list[Path]:
    dirs = [_WORKFLOWS_DIR]
    if state_root is not None:
        dirs.insert(0, forged_workflows_dir(state_root))  # forged wins on name
    return dirs


def _find(name: str, state_root=None) -> Path | None:
    for d in _search_dirs(state_root):
        p = d / f"{name}.json"
        if p.exists():
            return p
    return None


def list_workflows(state_root=None) -> list[dict]:
    """Available workflows as ``[{name, description, forged}]``, default first.
    Merges seed backups with any forged (state-dir) workflows; forged wins on
    a name clash."""
    seen: dict = {}
    for d in reversed(_search_dirs(state_root)):   # seed first, forged overrides
        if not d.is_dir():
            continue
        forged = (state_root is not None and d == forged_workflows_dir(state_root))
        for p in sorted(d.glob("*.json")):
            try:
                data = json.loads(p.read_text())
            except (ValueError, OSError):
                continue
            nm = data.get("name", p.stem)
            seen[nm] = {"name": nm, "description": data.get("description", ""),
                        "forged": forged}
    out = list(seen.values())
    out.sort(key=lambda w: (w["name"] != DEFAULT, w["name"]))
    return out


def workflow_names(state_root=None) -> list[str]:
    return [w["name"] for w in list_workflows(state_root)]


def load_workflow(name: str | None, state_root=None) -> list[dict]:
    """Return the chunk-node list for ``name`` (falls back to DEFAULT, then to
    an empty list). Never raises — a missing workflow just yields no baseline.
    A forged (state-dir) workflow shadows a seed one of the same name."""
    name = (name or DEFAULT).strip() or DEFAULT
    path = _find(name, state_root) or _find(DEFAULT, state_root)
    if path is None:
        return []
    try:
        return list(json.loads(path.read_text()).get("chunks") or [])
    except (ValueError, OSError):
        return []


def validate_chunk_graph(chunks: list) -> list[str]:
    """Same structural rules the seal gate applies: >=1 node, exactly one
    entry, every exit resolving to a real node. Returns failure strings."""
    fails: list[str] = []
    if not chunks:
        return ["workflow has no chunk nodes"]
    ids = {c.get("id") for c in chunks}
    entries = sum(1 for c in chunks if c.get("is_entry"))
    if entries != 1:
        fails.append(f"needs exactly one entry node (got {entries})")
    for c in chunks:
        for tag, tgt in (c.get("exits") or {}).items():
            nxt = tgt.get("next_chunk_id") if isinstance(tgt, dict) else tgt
            if nxt and nxt not in ids:
                fails.append(f"chunk {c.get('id')!r} exit {tag} -> unknown {nxt!r}")
    return fails


def save_workflow(state_root, name: str, description: str,
                  chunks: list) -> Path:
    """Persist a forged workflow to the state-dir registry. Returns the path."""
    d = forged_workflows_dir(state_root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    path.write_text(json.dumps(
        {"name": name, "description": description, "chunks": chunks},
        indent=2))
    return path
