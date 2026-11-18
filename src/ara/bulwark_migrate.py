"""Migrate legacy policy files into a unified ``bulwark.md``.

Sources merged (any/all may be present):
  - ``<state_root>/armors/<name>/WALLS.md`` → scoped to ``armor: <name>``
  - ``<state_root>/armors/<name>/overrides.json`` → scoped to ``armor: <name>``
    (``overrides`` list becomes ``allow``, ``blackholes`` becomes ``deny``)
  - ``<state_root>/overrides.json`` (global) → scoped to ``global``
  - ``<state_root>/rhema.json`` (global) → scoped to ``global``
    (request→ask, response→reply, tool_call→oc, any→all three)

After writing ``<state_root>/bulwark.md`` the originals are renamed to
``<original>.bulwark-migrated.bak``. Idempotent: if ``bulwark.md`` already
exists the migration is a no-op unless ``force=True``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import walls as walls_mod


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class MigrationReport:
    wrote_bulwark: bool = False
    bulwark_path: Path | None = None
    bulwark_text: str = ""
    renamed: list[tuple[Path, Path]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    rule_count: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def migrate(
    state_root: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> MigrationReport:
    report = MigrationReport()
    bulwark_path = state_root / "bulwark.md"
    report.bulwark_path = bulwark_path

    if bulwark_path.exists() and not force:
        report.skipped = True
        report.skip_reason = f"{bulwark_path} already exists (use --force to overwrite)"
        return report

    sections: list[tuple[str, list[str]]] = []
    legacy_files: list[Path] = []

    # ---- global overrides.json ----
    global_overrides = state_root / "overrides.json"
    if global_overrides.exists():
        lines, warn = _render_overrides(global_overrides)
        report.warnings.extend(warn)
        if lines:
            sections.append(("global", lines))
        legacy_files.append(global_overrides)

    # ---- global rhema.json ----
    rhema_file = state_root / "rhema.json"
    if rhema_file.exists():
        lines, warn = _render_rhema(rhema_file)
        report.warnings.extend(warn)
        if lines:
            # Append to global section (or create it if none).
            _append_to_scope(sections, "global", lines)
        legacy_files.append(rhema_file)

    # ---- per-armor ----
    armors_dir = state_root / "armors"
    if armors_dir.is_dir():
        for armor_dir in sorted(p for p in armors_dir.iterdir() if p.is_dir()):
            name = armor_dir.name
            walls_file = armor_dir / "WALLS.md"
            ovr_file = armor_dir / "overrides.json"
            scope_lines: list[str] = []
            if walls_file.exists():
                wl, wn = _render_walls(walls_file)
                report.warnings.extend(wn)
                scope_lines.extend(wl)
                legacy_files.append(walls_file)
            if ovr_file.exists():
                ol, on = _render_overrides(ovr_file)
                report.warnings.extend(on)
                scope_lines.extend(ol)
                legacy_files.append(ovr_file)
            if scope_lines:
                sections.append((f"armor: {name}", scope_lines))

    text = _assemble(sections)
    report.bulwark_text = text
    report.rule_count = sum(1 for s in text.splitlines() if s and not s.startswith("#"))

    if dry_run:
        return report

    if not sections:
        # Nothing to migrate; don't write an empty file or rename originals.
        report.skipped = True
        report.skip_reason = "no legacy policy files found"
        return report

    state_root.mkdir(parents=True, exist_ok=True)
    bulwark_path.write_text(text)
    report.wrote_bulwark = True

    for lf in legacy_files:
        bak = lf.with_suffix(lf.suffix + ".bulwark-migrated.bak")
        try:
            lf.rename(bak)
            report.renamed.append((lf, bak))
        except OSError as exc:
            report.warnings.append(f"could not rename {lf} → {bak}: {exc}")

    return report


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_walls(path: Path) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    try:
        walls = walls_mod.parse_file(path)
    except Exception as exc:
        return [], [f"{path}: parse error: {exc}"]

    lines: list[str] = []

    for r in walls.path_rules:
        lines.append(_fmt("deny", "fs", r.pattern, is_regex=r.is_regex))
    for r in walls.command_rules:
        lines.append(_fmt("deny", "exec", r.pattern, is_regex=r.is_regex))
    for r in walls.net_rules:
        target = f"{r.host_pattern}:{r.port_pattern}" if r.port_pattern != "*" else r.host_pattern
        lines.append(_fmt("deny", "net", target))
    for r in walls.env_rules:
        lines.append(_fmt("deny", "env", r.pattern))

    for r in walls.approval_rules:
        if r.kind == "path":
            lines.append(_fmt("approval", "fs", r.pattern, is_regex=r.is_regex))
        elif r.kind == "command":
            lines.append(_fmt("approval", "exec", r.pattern, is_regex=r.is_regex))
        elif r.kind == "net":
            lines.append(_fmt("approval", "net", r.pattern))

    for r in walls.allow_exec_rules:
        lines.append(_fmt("allow", "exec", r.pattern, is_regex=r.is_regex))

    if walls.exec_posture == "allowlist":
        warnings.append(
            f"{path}: exec_posture=allowlist — Bulwark is deny-listing by "
            "default; add an explicit `deny exec *` at the end of the scope "
            "plus your allow rules to reproduce allowlist posture."
        )

    return lines, warnings


def _render_overrides(path: Path) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{path}: parse error: {exc}"]

    lines: list[str] = []
    for entry in raw.get("blackholes", []) or []:
        layer = {"path": "fs", "command": "exec"}.get(entry.get("kind") or "")
        if not layer:
            warnings.append(
                f"{path}: blackhole kind={entry.get('kind')!r} unsupported; skipped"
            )
            continue
        lines.append(_fmt("deny", layer, entry.get("pattern") or ""))

    for entry in raw.get("overrides", []) or []:
        layer = {"path": "fs", "command": "exec"}.get(entry.get("kind") or "")
        if not layer:
            warnings.append(
                f"{path}: override kind={entry.get('kind')!r} unsupported; skipped"
            )
            continue
        lines.append(_fmt("allow", layer, entry.get("pattern") or ""))
    return lines, warnings


def _render_rhema(path: Path) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"{path}: parse error: {exc}"]

    scope_to_layer = {
        "request": "ask",
        "response": "reply",
        "tool_call": "oc",
    }
    lines: list[str] = []
    for rule in raw.get("rules") or []:
        scope = (rule.get("scope") or "any").lower()
        action = (rule.get("action") or "deny").lower()
        if action in {"rewrite", "shadow_rewrite"}:
            warnings.append(
                f"{path}: rule id={rule.get('id')} action={action} has a "
                "`rewrite_with` payload that does not fit the Bulwark DSL "
                "directly. The rule is emitted as a DENY with the payload "
                "preserved in a comment so you can reinstate it manually."
            )
            # Emit as a comment so the data isn't lost silently.
            payload = rule.get("rewrite_with")
            pattern = rule.get("pattern") or ""
            lines.append(
                f"# MIGRATION: former {action} rule, payload: "
                f"{json.dumps(payload, separators=(',', ':'))}"
            )
            lines.append(
                _fmt(
                    "deny",
                    scope_to_layer.get(scope, "oc"),
                    pattern,
                    is_regex=(rule.get("pattern_kind") == "regex"),
                )
            )
            continue
        if action not in {"deny", "approval", "allow"}:
            warnings.append(
                f"{path}: rule id={rule.get('id')} has unsupported action "
                f"{action!r}; skipped"
            )
            continue
        pattern = rule.get("pattern") or ""
        is_regex = rule.get("pattern_kind") == "regex"
        if scope == "any":
            for layer in ("ask", "reply", "oc"):
                lines.append(_fmt(action, layer, pattern, is_regex=is_regex))
        else:
            layer = scope_to_layer.get(scope, "oc")
            lines.append(_fmt(action, layer, pattern, is_regex=is_regex))
    return lines, warnings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(action: str, layer: str, pattern: str, *, is_regex: bool = False) -> str:
    body = ("regex:" + pattern) if is_regex else pattern
    return f"{action:<8} {layer:<4} {body}"


def _append_to_scope(
    sections: list[tuple[str, list[str]]], scope: str, lines: Iterable[str]
) -> None:
    for i, (s, existing) in enumerate(sections):
        if s == scope:
            existing.extend(lines)
            return
    sections.append((scope, list(lines)))


def _assemble(sections: list[tuple[str, list[str]]]) -> str:
    out: list[str] = [
        "# bulwark.md — auto-generated by `ara bulwark migrate`.",
        "# Review the rules below, then edit freely; changes hot-reload.",
        "",
    ]
    for scope, lines in sections:
        if not lines:
            continue
        out.append(f"## {scope}")
        out.extend(lines)
        out.append("")
    return "\n".join(out).rstrip() + "\n"
