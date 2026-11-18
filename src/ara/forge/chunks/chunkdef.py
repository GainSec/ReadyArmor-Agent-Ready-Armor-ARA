"""ChunkDef + ExitTarget + GemRef schemas.

Chunks are part of the weapon seal:
``<weapons>/<weapon_id>/chunks/<chunk_id>.json``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GemRef:
    layer: str       # "fs" | "exec" | "net" | "reply" | "ask" | "oc"
    rule_subset: str
    # Cognitive-inhibition model (ARA cog-sci foundations): a chunk's gems
    # gate the agent's capability surface, and that gate *breathes* by phase.
    # inhibition_level 1.0 = fully inhibited (strictest — every rule in the
    # subset enforced), 0.0 = fully disinhibited (relaxable rules dropped).
    # Probe lowers inhibition to explore/exploit; enum & writeup stay high.
    # Missing/old gems default to 1.0 → identical to today's strict behavior.
    inhibition_level: float = 1.0
    # The ROE boundary. "strict" scope rules are NEVER relaxed regardless of
    # inhibition_level — disinhibition widens *what* the agent may attempt,
    # never *where*. Scope holds even when inhibition is 0.
    scope: str = "strict"


@dataclass(frozen=True)
class ExitTarget:
    tag: str
    next_chunk_id: str
    requires_caller: str | None = None  # "agent" | "operator" | "system" | None
    description: str | None = None


@dataclass(frozen=True)
class ChunkDef:
    id: str
    title: str
    prompt_fragment: str
    gems: list[GemRef] = field(default_factory=list)
    tool_surface: list[str] = field(default_factory=list)
    exits: dict[str, ExitTarget] = field(default_factory=dict)
    citations: dict[str, list[str]] = field(default_factory=dict)
    can_emit_finding: bool = False
    is_terminal: bool = False
    is_entry: bool = False

    def to_jsonable(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "prompt_fragment": self.prompt_fragment,
            "gems": [asdict(g) for g in self.gems],
            "tool_surface": list(self.tool_surface),
            "exits": {tag: asdict(t) for tag, t in self.exits.items()},
            "citations": {k: list(v) for k, v in self.citations.items()},
            "can_emit_finding": self.can_emit_finding,
            "is_terminal": self.is_terminal,
            "is_entry": self.is_entry,
        }

    @classmethod
    def from_jsonable(cls, d: dict) -> "ChunkDef":
        return cls(
            id=d["id"], title=d["title"],
            prompt_fragment=d.get("prompt_fragment", ""),
            gems=[GemRef(**g) for g in (d.get("gems") or [])],
            tool_surface=list(d.get("tool_surface") or []),
            exits={tag: ExitTarget(**t) for tag, t in (d.get("exits") or {}).items()},
            citations={k: list(v) for k, v in (d.get("citations") or {}).items()},
            can_emit_finding=bool(d.get("can_emit_finding", False)),
            is_terminal=bool(d.get("is_terminal", False)),
            is_entry=bool(d.get("is_entry", False)),
        )


def write_chunkdef(path: Path, chunk: ChunkDef) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(chunk.to_jsonable(), indent=2))


def read_chunkdef(path: Path) -> ChunkDef:
    return ChunkDef.from_jsonable(json.loads(path.read_text()))


def read_chunks_dir(weapon_dir: Path) -> list[ChunkDef]:
    cdir = Path(weapon_dir) / "chunks"
    if not cdir.exists():
        return []
    out: list[ChunkDef] = []
    for p in sorted(cdir.glob("*.json")):
        out.append(read_chunkdef(p))
    return out
