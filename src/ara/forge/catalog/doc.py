"""Markdown rendering of WeaponChannelCatalog."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ara.forge.catalog.generator import WeaponChannelCatalog


def render_catalog_md(catalog: WeaponChannelCatalog) -> str:
    lines = [
        f"# Channel Catalog — Weapon {catalog.weapon_id}",
        f"",
        f"- Seal: `{catalog.seal_id}`",
        f"- Generated: {datetime.fromtimestamp(catalog.generated_at).isoformat()}",
        f"- Channels: {len(catalog.channels)}",
        f"- Edges: {len(catalog.edges)}",
        f"",
        f"## Channels",
        f"",
    ]
    for ch in catalog.channels:
        lines.append(f"### `{ch.name}`")
        lines.append(f"- Sink subsystem: `{ch.sink_subsystem}`")
        lines.append(f"- Schema: `{ch.schema_name}`")
        lines.append(f"- Allowed callers: {', '.join(f'`{c}`' for c in ch.callers)}")
        if ch.sources:
            lines.append(f"- Triggered by: {', '.join(f'`{s}`' for s in ch.sources)}")
        if ch.sinks:
            lines.append(f"- Triggers: {', '.join(f'`{s}`' for s in ch.sinks)}")
        lines.append("")
        if ch.schema_fields:
            lines.append("Fields:")
            for f in ch.schema_fields:
                req = " *(required)*" if f.required else ""
                lines.append(f"- `{f.name}: {f.type}`{req}")
            lines.append("")
    if catalog.edges:
        lines.append("## Cross-wiring graph")
        lines.append("")
        lines.append("```")
        for e in catalog.edges:
            lines.append(f"{e.from_channel} → {e.to_channel}  ({e.trigger_description})")
        lines.append("```")
    return "\n".join(lines) + "\n"


def write_catalog_md(path: Path, catalog: WeaponChannelCatalog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_catalog_md(catalog))
