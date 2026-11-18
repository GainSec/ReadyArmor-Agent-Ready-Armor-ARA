"""Supervisor: ties together Session, IPC server, policy, and audit."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import approvals as approvals_mod
from . import armors as armors_mod
from . import debug_log
from . import overrides as overrides_mod
from . import task_action_gate as task_action_gate_mod
from . import weapons as weapons_mod
from .audit import JsonlWriter
from .ipc import Server
from .policy import Decision
from .session import Session, create_session
from .task_channel_adapt import (
    adapt_task_channel_args,
    drop_unresolved_evidence_refs,
)
from .walls import Walls, parse_file
from . import bulwark as bulwark_mod
from . import danger_class as danger_mod
from . import danger_registry as danger_registry_mod


@dataclass
class GuardConnection:
    pid: int
    ppid: int
    runtime: str
    argv: list[str]


from . import pause_and_ask_state as _paas

# Backwards-compat alias for tests + external callers.
_PendingClassification = _paas.PendingClassification


def _tool_choice_for_filtered_tools(tools: list[dict]) -> str | dict:
    if len(tools) == 1 and isinstance(tools[0], dict):
        fn = tools[0].get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if name == "mcp_ara_forge_chunks_emit_tag":
            return {"type": "function", "function": {"name": name}}
    return "required"


@dataclass
class Supervisor:
    session: Session
    walls: Walls
    server: Server
    events: JsonlWriter
    policy_hits: JsonlWriter
    state_root: Path
    guards: dict[int, GuardConnection] = field(default_factory=dict)

    walls_file: Path | None = None
    _walls_mtime: float | None = None

    # Bulwark — unified policy layer. Loaded from <state_root>/bulwark.md
    # and consulted in `_on_query` before walls/policy/overrides. A bulwark
    # `deny` or `approval` decision short-circuits the legacy path; `allow`
    # without a matching rule falls through so today's WALLS rules still
    # apply. Hot-reloaded by `_watch_bulwark`.
    bulwark: "bulwark_mod.Bulwark" = field(default_factory=lambda: bulwark_mod.Bulwark())
    bulwark_state: "bulwark_mod.RuntimeState" = field(default_factory=lambda: bulwark_mod.RuntimeState())
    _bulwark_mtime: float | None = None
    _bulwark_task: asyncio.Task | None = None
    # The path the watcher is currently following — armor's WALLS when
    # equipped, else the launch-time ``walls_file``. Tracked separately
    # so the watcher can detect a switch from one source to the other
    # and re-parse even when mtimes happen to match.
    _walls_active_path: Path | None = None
    _reload_task: asyncio.Task | None = None

    # Heartbeat watchdog: last-seen-at per guard pid. If a guard stops
    # sending heartbeats we assume it's been silenced (crashed, stalled,
    # attacker-killed) and SIGTERM the pid so the agent can't run
    # unmonitored. Tuned by ARA_HEARTBEAT_TIMEOUT_S (default 10s).
    _heartbeat_at: dict[int, float] = field(default_factory=dict)
    _watchdog_task: asyncio.Task | None = None

    # Forge-v1 engagement wiring (channels, crumb tracker, autofirer,
    # chunk runtime, activation manager). Populated when a weapon is
    # equipped at supervisor start (see ``_maybe_install_engagement``).
    # Stays None when no weapon is equipped — channel_call IPC then
    # returns ``no_weapon_equipped``.
    engagement: "object | None" = None

    # v0.2 pause-and-ask classification queue lives in the shared
    # ``pause_and_ask_state`` module keyed by ``state_root`` so the
    # MITM proxy (in the same process) and the supervisor share state.
    # See ``_paas`` import.

    @property
    def _pending_classifications(self) -> dict:
        """Per-instance view onto the shared pause-and-ask store. Kept
        as an instance accessor so existing tests / inspection points
        continue to work; the canonical store is ``_paas._store``."""
        return _paas._store(self.state_root)

    @classmethod
    async def start(
        cls,
        state_root: Path | None = None,
        walls: Walls | None = None,
        walls_file: Path | None = None,
        resume_id: str | None = None,
    ) -> "Supervisor":
        session = create_session(state_root=state_root, resume_id=resume_id)
        resolved_walls = walls if walls is not None else Walls(path_rules=())
        resolved_state_root = state_root if state_root is not None else session.root.parent.parent
        sup = cls(
            session=session,
            walls=resolved_walls,
            server=None,  # type: ignore[arg-type]
            events=JsonlWriter(session.events_path),
            policy_hits=JsonlWriter(session.policy_hits_path, chain=True),
            state_root=resolved_state_root,
        )
        sup.walls_file = walls_file
        # Don't pre-stamp _walls_mtime — let the first poll of
        # _watch_walls discover and parse, regardless of whether the
        # active source ends up being the launch file or an equipped
        # armor's WALLS. This also handles the start-with-no-walls case
        # cleanly: an armor equipped post-launch will be picked up.
        sup.server = Server(session.socket_path, sup._dispatch)
        path = resolved_state_root / "bulwark.md"
        if path.exists():
            try:
                sup.bulwark = bulwark_mod.parse_file(path)
                sup.bulwark_state = bulwark_mod.RuntimeState()
                sup._bulwark_mtime = path.stat().st_mtime
            except Exception:  # noqa: BLE001
                pass
        await sup.server.start()
        # Always start the watcher — even with no launch walls, it can
        # follow an armor equipped later.
        sup._reload_task = asyncio.create_task(sup._watch_walls())
        sup._watchdog_task = asyncio.create_task(sup._watch_heartbeats())
        sup._bulwark_task = asyncio.create_task(sup._watch_bulwark())
        # Forge-v1 engagement wiring (Q3-A): if a weapon is equipped,
        # install the channel registry + chunk runtime + auto-firer
        # against the active session.
        sup._maybe_install_engagement()
        # Forge-authoring channels (mats.search/get + extract.*) — these
        # are independent of the engagement (they read from forge/<id>/
        # not sessions/<id>/) so register them whenever the supervisor
        # is up. Cheap; no side effects until the agent calls them.
        try:
            from .forge.channels import install_forge_channels
            install_forge_channels(resolved_state_root)
        except Exception:  # noqa: BLE001
            pass
        return sup

    async def _watch_heartbeats(self, poll_interval: float = 1.0) -> None:
        """Kill guards that stop heartbeating. A silenced guard means we
        can't see what the agent is doing — treat as compromise, fail closed."""
        import os as _os
        import signal as _signal
        import time as _time
        try:
            timeout_s = float(_os.environ.get("ARA_HEARTBEAT_TIMEOUT_S") or 10.0)
        except ValueError:
            timeout_s = 10.0
        grace_until: dict[int, float] = {}
        while True:
            try:
                await asyncio.sleep(poll_interval)
                now = _time.time()
                # Dead pids we've already signaled — purge after a moment.
                for pid in list(grace_until):
                    if grace_until[pid] < now:
                        self.guards.pop(pid, None)
                        self._heartbeat_at.pop(pid, None)
                        grace_until.pop(pid, None)
                for pid in list(self.guards):
                    last = self._heartbeat_at.get(pid)
                    if last is None:
                        # Only Node guards currently emit heartbeat events.
                        # Libc-only processes still register as guards so the
                        # shim can consult rules and write policy hits, but
                        # they do not have a heartbeat loop. Do not kill those
                        # processes for a contract they never advertised.
                        continue
                    if now - last <= timeout_s:
                        continue
                    if pid in grace_until:
                        continue
                    # Short-lived helpers (e.g. ``ip neigh show`` spawned by
                    # a Node runtime) inherit the libc shim, register as
                    # guards, then exit in <500 ms. 60 s later the watchdog
                    # logs a spurious ``heartbeat_miss`` against the already-
                    # dead pid. Probe liveness first; if the kernel doesn't
                    # know the pid, silently drop it from tracking instead
                    # of treating a clean exit as a compromise event.
                    try:
                        _os.kill(pid, 0)
                        pid_alive = True
                    except OSError:
                        pid_alive = False
                    if not pid_alive:
                        self.guards.pop(pid, None)
                        self._heartbeat_at.pop(pid, None)
                        continue
                    await self.events.append({
                        "kind": "heartbeat_miss",
                        "pid": pid,
                        "last_seen_at": last,
                        "silent_for_s": round(now - last, 2),
                        "timeout_s": timeout_s,
                    })
                    try:
                        _os.kill(pid, _signal.SIGTERM)
                    except OSError:
                        pass
                    grace_until[pid] = now + 3.0
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                continue

    async def _watch_bulwark(self, poll_interval: float = 1.0) -> None:
        """Stat-poll ``<state_root>/bulwark.md`` and re-parse on mtime change.

        No guard broadcast — bulwark enforcement runs in the supervisor on
        every query, so no sync_rules need to be pushed down to the guards.
        """
        path = self.state_root / "bulwark.md"
        while True:
            try:
                await asyncio.sleep(poll_interval)
                if not path.exists():
                    if self.bulwark.rules:
                        self.bulwark = bulwark_mod.Bulwark()
                        self.bulwark_state = bulwark_mod.RuntimeState()
                        self._bulwark_mtime = None
                    continue
                mtime = path.stat().st_mtime
                if self._bulwark_mtime is not None and mtime == self._bulwark_mtime:
                    continue
                self._bulwark_mtime = mtime
                try:
                    new_bw = bulwark_mod.parse_file(path)
                except Exception as exc:  # noqa: BLE001
                    await self.events.append({
                        "kind": "bulwark_reload_error",
                        "path": str(path), "error": str(exc),
                    })
                    continue
                if new_bw.parse_errors:
                    await self.events.append({
                        "kind": "bulwark_reload_error",
                        "path": str(path),
                        "errors": list(new_bw.parse_errors)[:50],
                    })
                self.bulwark = new_bw
                # Fresh state on each reload — previous counters/ttls reset
                # intentionally, since rule IDs are regenerated on parse.
                self.bulwark_state = bulwark_mod.RuntimeState()
                # Re-broadcast sync_rules so guards pick up the new
                # nav/perm deny sets at the libc shim layer.
                try:
                    await self.server.broadcast({
                        "type": "sync_rules_update",
                        "sync_rules": self._sync_rules_payload(),
                    })
                except Exception:  # noqa: BLE001
                    pass
                await self.events.append({
                    "kind": "bulwark_reload",
                    "path": str(path),
                    "rule_count": len(new_bw.rules),
                })
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                continue

    async def _watch_walls(self, poll_interval: float = 1.0) -> None:
        """Stat-poll WALLS file; on mtime change re-parse and broadcast
        updated sync_rules to every live guard.

        The watcher follows the **equipped armor's WALLS** when one is
        equipped, falling back to the supervisor's launch-time
        ``--walls`` flag (``self.walls_file``) when no armor is active.
        Equipping a different armor triggers an immediate re-parse and
        broadcast — operators don't have to restart the supervisor for
        WALLS changes to take effect.
        """
        while True:
            try:
                await asyncio.sleep(poll_interval)
                # Resolve the file we should currently be watching.
                preferred = armors_mod.active_walls_file(self.state_root)
                effective = preferred if preferred and preferred.exists() else self.walls_file
                if effective is None or not effective.exists():
                    continue
                mtime = effective.stat().st_mtime
                source_changed = (
                    self._walls_active_path is None
                    or str(self._walls_active_path) != str(effective)
                )
                if (
                    not source_changed
                    and self._walls_mtime is not None
                    and mtime == self._walls_mtime
                ):
                    continue
                self._walls_mtime = mtime
                self._walls_active_path = effective
                try:
                    new_walls = parse_file(effective)
                except Exception as exc:  # noqa: BLE001
                    await self.events.append(
                        {
                            "kind": "walls_reload_error",
                            "error": str(exc),
                            "path": str(effective),
                        }
                    )
                    continue
                self.walls = new_walls
                payload = {
                    "type": "sync_rules_update",
                    "sync_rules": self._sync_rules_payload(),
                }
                sent = await self.server.broadcast(payload)
                await self.events.append(
                    {
                        "kind": "walls_reloaded",
                        "path": str(effective),
                        "source": "armor" if effective == preferred else "launch",
                        "pushed_to": sent,
                        "rules": {
                            "deny_path": len(new_walls.path_rules),
                            "deny_command": len(new_walls.command_rules),
                            "approval": len(new_walls.approval_rules),
                        },
                    }
                )
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                continue

    def _load_effective_overrides(self) -> overrides_mod.Store:
        """Merge global overrides with the currently-equipped armor's.
        Global wins on entry-id collision (see Store.merge)."""
        store = overrides_mod.load(self.state_root)
        active = armors_mod.active_armor_name(self.state_root)
        if active:
            armor_path = (
                armors_mod.armors_root(self.state_root)
                / active / armors_mod.OVERRIDES_FILENAME
            )
            if armor_path.exists():
                store = store.merge(overrides_mod.load_at(armor_path))
        return store

    def _sync_rules_payload(self) -> dict:
        shadow = armors_mod.active_shadow_mode(self.state_root)
        payload: dict = {
            "shadow_mode": shadow,
            "deny_path": [
                {"pattern": r.pattern, "rule_id": r.rule_id}
                for r in self.walls.path_rules
            ] + self._bulwark_path_rules("fs"),
            "deny_command": [
                {"pattern": r.pattern, "rule_id": r.rule_id}
                for r in self.walls.command_rules
            ] + self._bulwark_exec_rules("deny"),
            "approval_path": [
                {"pattern": r.pattern, "rule_id": r.rule_id}
                for r in self.walls.approval_rules if r.kind == "path"
            ],
            "approval_command": [
                {"pattern": r.pattern, "rule_id": r.rule_id}
                for r in self.walls.approval_rules if r.kind == "command"
            ],
            "deny_net": [
                {
                    "pattern": r.pattern,
                    "rule_id": r.rule_id,
                    "host": r.host_pattern,
                    "port": r.port_pattern,
                }
                for r in self.walls.net_rules
            ] + self._bulwark_net_rules("deny"),
            "allow_net": self._bulwark_net_rules("allow"),
            "approval_net": [
                {"pattern": r.pattern, "rule_id": r.rule_id}
                for r in self.walls.approval_rules if r.kind == "net"
            ],
            "deny_env": [
                {"pattern": r.pattern, "rule_id": r.rule_id}
                for r in self.walls.env_rules
            ],
            "allow_exec": [
                {"pattern": r.pattern, "rule_id": r.rule_id}
                for r in self.walls.allow_exec_rules
            ],
            "exec_posture": self.walls.exec_posture,
            # Bulwark nav + perm rules for the current effective scope
            # (global + equipped armor). Guards parse these and use them
            # to enforce mkdir/rmdir/chdir (nav) and chmod/chown (perm)
            # at the libc shim boundary.
            "deny_nav": self._bulwark_path_rules("nav"),
            "deny_perm": self._bulwark_path_rules("perm"),
        }
        # Cognitive-inhibition (chunk gems): attach the active chunk's profile
        # for observability, and — only when ARA_CHUNK_INHIBITION_ENFORCE is
        # set — relax relaxable, non-scope rules on disinhibited layers. This
        # is fail-strict and inert until rules are marked ``relaxable``; the
        # ROE/scope boundary is never dropped. See runtime/chunks/inhibition.py.
        payload["chunk_inhibition"] = self._active_inhibition_profile()
        return self._apply_inhibition_to_payload(payload)

    def _active_inhibition_profile(self) -> dict:
        """Derive the active chunk's inhibition profile; fail-strict."""
        import os as _os
        from .runtime.chunks.inhibition import (
            LAYERS as _LAYERS,
            active_inhibition_profile as _profile,
        )
        try:
            weapon_id = (_os.environ.get("ARA_TASK_WEAPON_ID") or "").strip()
            if not weapon_id:
                p = self.state_root / "equipped_weapon"
                if p.is_file():
                    weapon_id = p.read_text(errors="ignore").strip()
            return _profile(self.state_root, self.session.id, weapon_id)
        except Exception:  # noqa: BLE001
            return {
                layer: {"inhibition_level": 1.0, "scope": "strict",
                        "rule_subset": None}
                for layer in _LAYERS
            }

    def _apply_inhibition_to_payload(self, payload: dict) -> dict:
        """Relax relaxable, non-scope rules on disinhibited layers.

        Gated on ``ARA_CHUNK_INHIBITION_ENFORCE``; without it the payload is
        returned unchanged (strict). A rule is only ever dropped when it is
        explicitly marked ``relaxable`` and NOT marked ``scope`` — the ROE
        boundary holds regardless of inhibition. Fail-strict: any error leaves
        the strict payload intact.
        """
        import os as _os
        if not _os.environ.get("ARA_CHUNK_INHIBITION_ENFORCE"):
            return payload
        profile = payload.get("chunk_inhibition") or {}
        # payload deny-list key -> inhibition layer it answers to
        layer_of = {
            "deny_path": "fs", "deny_nav": "fs", "deny_perm": "fs",
            "deny_command": "exec", "deny_net": "net",
        }
        try:
            for key, layer in layer_of.items():
                rules = payload.get(key)
                if not isinstance(rules, list):
                    continue
                inh = float((profile.get(layer) or {}).get(
                    "inhibition_level", 1.0))
                if inh >= 1.0:
                    continue  # fully inhibited → keep every rule
                kept = []
                for r in rules:
                    if not isinstance(r, dict):
                        kept.append(r)
                        continue
                    # never relax scope/ROE rules or unmarked rules
                    if r.get("scope") or not r.get("relaxable"):
                        kept.append(r)
                        continue
                    # drop only when disinhibited past the rule's threshold
                    if inh >= float(r.get("relax_below", 1.0)):
                        kept.append(r)
                payload[key] = kept
        except Exception:  # noqa: BLE001
            return payload
        return payload

    def _bulwark_path_rules(self, layer: str) -> list[dict]:
        """Collect applicable path-glob rules for a layer from Bulwark.

        Only ``deny`` rules are pushed to the shim — allow/approval go
        through the Python supervisor path in ``_on_query``.
        """
        out: list[dict] = []
        try:
            armor_id = armors_mod.active_armor_name(self.state_root)
        except Exception:  # noqa: BLE001
            armor_id = None
        for r in self.bulwark.rules:
            if r.layer != layer or r.action != "deny":
                continue
            if not bulwark_mod.scope_matches(r.scope, armor_id):
                continue
            if r.match.is_regex or not r.match.path:
                continue
            out.append({"rule_id": r.rule_id, "pattern": r.match.path})
        return out

    def _bulwark_net_rules(self, action: str) -> list[dict]:
        """Collect applicable net rules from Bulwark for sync guards.

        The libc shim enforces net rules synchronously at connect() time.
        It needs both deny and allow net rules so forge-only default-deny
        can still reach the configured LLM/local control-plane endpoints.
        """
        out: list[dict] = []
        try:
            armor_id = armors_mod.active_armor_name(self.state_root)
        except Exception:  # noqa: BLE001
            armor_id = None
        for r in self.bulwark.rules:
            if r.layer != "net" or r.action != action:
                continue
            if not bulwark_mod.scope_matches(r.scope, armor_id):
                continue
            if r.match.is_regex:
                continue
            host = r.match.host or "*"
            port = r.match.port or "*"
            out.append({
                "rule_id": r.rule_id,
                "pattern": r.match.primary,
                "host": host,
                "port": port,
            })
        return out

    def _bulwark_exec_rules(self, action: str) -> list[dict]:
        """Collect applicable exec rules from Bulwark for sync guards.

        The libc shim command matcher only supports glob-style patterns, so
        regex exec rules stay on the supervisor query path.
        """
        out: list[dict] = []
        try:
            armor_id = armors_mod.active_armor_name(self.state_root)
        except Exception:  # noqa: BLE001
            armor_id = None
        for r in self.bulwark.rules:
            if r.layer != "exec" or r.action != action:
                continue
            if not bulwark_mod.scope_matches(r.scope, armor_id):
                continue
            if r.match.is_regex or not r.match.path:
                continue
            out.append({"rule_id": r.rule_id, "pattern": r.match.path})
        return out

    @classmethod
    async def start_with_walls_file(
        cls, walls_file: Path, state_root: Path | None = None
    ) -> "Supervisor":
        return await cls.start(state_root=state_root, walls=parse_file(walls_file))

    async def stop(self) -> None:
        import contextlib
        if self._reload_task is not None:
            self._reload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reload_task
            self._reload_task = None
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watchdog_task
            self._watchdog_task = None
        if self._bulwark_task is not None:
            self._bulwark_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._bulwark_task
            self._bulwark_task = None
        await self.server.stop()

    async def _dispatch(self, msg: dict) -> dict | None:
        kind = msg.get("type")
        if kind == "hello":
            return await self._on_hello(msg)
        if kind == "query":
            return await self._on_query(msg)
        if kind == "event":
            await self._on_event(msg)
            return None
        if kind == "prompt_intercept":
            return await self._on_prompt_intercept(msg)
        if kind == "channel_call":
            return await self._on_channel_call(msg)
        if kind == "flush_audit":
            # Caller wants a durability barrier before returning a user-visible
            # deny. policy_hits writes already fsync per-line, so this is a
            # no-op if no writes are in flight; otherwise the Lock serializes
            # us behind them. Return ACK to unblock the caller.
            async with self.policy_hits._get_lock():
                pass
            return {"type": "flush_audit_ack", "seq": msg.get("seq")}
        return {"type": "error", "error": f"unknown message type: {kind!r}"}

    async def _on_prompt_intercept(self, msg: dict) -> dict:
        """LLM-call mediation. Records every turn to ``transcript.jsonl``,
        then evaluates Rhema rules and returns a decision.

        On outbound requests, auto-injects a forge-v1 prompt brief
        teaching the LLM about ``globalThis.ara.channel`` and the active
        chunk's tool_surface + exits. Idempotent (sentinel-marked).

        Decision shape: ``{action, [reason, rule_id, redirect,
        injected_response, new_payload]}``. Default is ``allow``."""
        from . import rhema as rhema_mod

        seq = msg.get("seq")
        role = str(msg.get("role") or "request")
        payload = msg.get("payload") or {}

        if not hasattr(self, "_transcript"):
            from .transcript import TranscriptWriter
            self._transcript = TranscriptWriter(
                self.session.root / "transcript.jsonl",
            )
        self._transcript.append({
            "kind": "transcript",
            "role": role,
            "payload": payload,
            "pid": msg.get("pid"),
            "model": msg.get("model"),
        })

        # Forge-v1 inline tag bridge (2.3 §7.1): when the assistant turn
        # contains <tag> markers matching the active chunk's exits, fire
        # chunks.emit_tag synthetically. Lets OpenClaw / generic LLM
        # agents participate in chunk swaps without learning the
        # channel_call protocol — they just emit tags in prose.
        if role in ("response", "assistant", "completion"):
            self._scan_inline_tags(payload)

        # Forge-v1 auto-brief: on outbound requests, inject a system
        # prompt fragment teaching the LLM about globalThis.ara.channel
        # plus the active chunk's tool_surface + exits. Idempotent via
        # sentinel; re-mediated requests don't stack briefs.
        auto_brief_payload: dict | None = None
        if role == "request":
            briefed = self._maybe_inject_forge_brief(payload)
            if briefed is not None and briefed is not payload:
                auto_brief_payload = briefed
                payload = briefed   # downstream rules see the augmented form
            debt_payload = self._maybe_filter_task_channel_debt_tools(payload)
            if debt_payload is not None and debt_payload is not payload:
                auto_brief_payload = debt_payload
                payload = debt_payload

        # v0.2 pause-and-ask classification: on outbound requests with a
        # pending classification queued, inject a system message asking
        # the agent to classify; on responses with a pending "asked"
        # entry, parse the classification JSON and write the registry.
        classify_payload: dict | None = None
        if role == "request":
            classify_payload = self._maybe_inject_classification_request(payload)
            if classify_payload is not None and classify_payload is not payload:
                payload = classify_payload
        elif role in ("response", "assistant", "completion"):
            self._maybe_consume_classification_response(payload)

        rule = rhema_mod.evaluate(self.state_root, role, payload)
        if rule is None:
            if auto_brief_payload is not None:
                return {
                    "type": "decision", "seq": seq, "action": "rewrite",
                    "reason": "FORGE_BRIEF_INJECTED",
                    "new_payload": auto_brief_payload,
                }
            return {"type": "decision", "seq": seq, "action": "allow"}

        if rule.action == "deny":
            injected = rhema_mod.synthesize_deny_response(rule)
            # Tag the deny in the transcript so the dashboard can show it.
            self._transcript.append({
                "kind": "transcript",
                "role": "rhema_deny",
                "payload": {
                    "rule_id": rule.id, "pattern": rule.pattern,
                    "scope": rule.scope, "redirect": rule.redirect,
                },
                "pid": msg.get("pid"),
                "model": msg.get("model"),
            })
            return {
                "type": "decision", "seq": seq, "action": "deny",
                "reason": rule.reason or "RHEMA_RULE_FIRED",
                "rule_id": rule.id,
                "injected_response": injected,
            }
        if rule.action == "rewrite" and rule.rewrite_with:
            return {
                "type": "decision", "seq": seq, "action": "rewrite",
                "reason": rule.reason or "RHEMA_RULE_FIRED",
                "rule_id": rule.id,
                "new_payload": rule.rewrite_with,
            }
        # approval/unknown actions are no-ops for the alpha — fall through.
        return {"type": "decision", "seq": seq, "action": "allow"}

    async def _on_hello(self, msg: dict) -> dict:
        token = msg.get("token")
        seq = msg.get("seq")
        if token != self.session.token:
            debug_log.emit(
                "supervisor", "hello.reject",
                state_root=self.state_root,
                session_id=self.session.id,
                claimed_pid=msg.get("pid"),
                runtime=msg.get("runtime"),
                reason="BAD_TOKEN",
            )
            await self.events.append(
                {
                    "kind": "hello_reject",
                    "reason": "BAD_TOKEN",
                    "claimed_pid": msg.get("pid"),
                }
            )
            return {
                "type": "hello_ack",
                "seq": seq,
                "verdict": "reject",
                "reason": "BAD_TOKEN",
            }
        pid = int(msg.get("pid", 0))
        conn = GuardConnection(
            pid=pid,
            ppid=int(msg.get("ppid", 0)),
            runtime=str(msg.get("runtime", "")),
            argv=list(msg.get("argv", [])),
        )
        self.guards[pid] = conn
        debug_log.emit(
            "supervisor", "hello.accept",
            state_root=self.state_root,
            session_id=self.session.id,
            pid=pid,
            ppid=conn.ppid,
            runtime=conn.runtime,
            argv=conn.argv,
        )
        await self.events.append(
            {
                "kind": "hello",
                "pid": pid,
                "ppid": conn.ppid,
                "runtime": conn.runtime,
                "argv": conn.argv,
            }
        )
        return {
            "type": "hello_ack",
            "seq": seq,
            "verdict": "accept",
            "session": self.session.id,
            # Rules mirrored to the guard for sync ops where an async IPC
            # round-trip isn't viable, plus for async fast-paths. Supervisor
            # is still authoritative — it defines the rules; guard applies
            # them, and round-trips on approval matches.
            "sync_rules": self._sync_rules_payload(),
        }

    async def _on_query(self, msg: dict) -> dict:
        seq = msg.get("seq")
        debug_log.emit(
            "supervisor", "query.received",
            state_root=self.state_root,
            session_id=self.session.id,
            seq=seq,
            op=msg.get("op"),
            command=msg.get("command"),
            args=msg.get("args"),
            path=msg.get("path"),
            host=msg.get("host"),
            port=msg.get("port"),
            url=msg.get("url"),
            pid=msg.get("pid"),
        )

        # Bulwark is the only policy source post-migration. If no rule
        # matches, fall through to default-allow (matching the
        # post-migration empty-walls behaviour without needing the
        # legacy walls+overrides+policy code path).
        bw_decision = self._evaluate_bulwark(msg)
        if bw_decision is not None:
            decision = bw_decision
            debug_log.emit(
                "supervisor", "query.bulwark_decision",
                state_root=self.state_root,
                session_id=self.session.id,
                verdict=decision.verdict,
                reason=decision.reason,
                rule_id=decision.rule_id,
                touched=decision.touched,
            )
        else:
            decision = Decision(
                verdict="allow", reason="DEFAULT_ALLOW",
                rule_id=None, touched=None,
            )

        # ── v0.1: trim-gate composition ───────────────────────────────
        # Bulwark ruled. Now consult the engagement contract: TOD,
        # rate-limit, stop-watcher, tool classification. Trim gate is
        # *stricter* — it can promote allow→approval (token needed,
        # classification needed) or allow→deny (out-of-window,
        # stop-condition tripped, classified-active on passive
        # engagement). It cannot soften a bulwark deny.
        trim_decision = self._evaluate_trim_gate(msg, decision)
        if trim_decision is not None:
            decision = trim_decision
            debug_log.emit(
                "supervisor", "query.trim_decision",
                state_root=self.state_root,
                session_id=self.session.id,
                verdict=decision.verdict,
                reason=decision.reason,
                rule_id=decision.rule_id,
                touched=decision.touched,
            )

        debug_log.emit(
            "supervisor", "query.task_gate_consider",
            state_root=self.state_root,
            session_id=self.session.id,
            verdict=decision.verdict,
            reason=decision.reason,
            task_id=os.environ.get("ARA_TASK_RUN_ID"),
            op=msg.get("op"),
            command=msg.get("command"),
        )
        if decision.verdict == "allow":
            task_decision = task_action_gate_mod.evaluate_query(
                self.state_root, msg,
            )
            if task_decision is not None:
                decision = task_decision
                debug_log.emit(
                    "supervisor", "query.task_decision",
                    state_root=self.state_root,
                    session_id=self.session.id,
                    verdict=decision.verdict,
                    reason=decision.reason,
                    rule_id=decision.rule_id,
                    touched=decision.touched,
                )

        # Mechanical danger-class enforcement, ahead of the approval path.
        # Irreversible target actions (e.g. a `mkfs`/`wipefs` spawn) are
        # hard-denied and auto-added to the armor bulwark; provable ones are
        # recorded for the operator and never auto-granted (see eligibility).
        _dop = str(msg.get("op") or "")
        _dflag = danger_mod.classify_event(_dop, msg)
        if _dflag.is_danger:
            _dtarget = str(
                msg.get("url") or msg.get("path")
                or msg.get("command") or _dflag.target)
            try:
                danger_registry_mod.flag_and_maybe_block(
                    self.state_root, op=_dop, flag=_dflag, target=_dtarget,
                    armor_id=armors_mod.active_armor_name(self.state_root),
                    session_id=self.session.id,
                )
            except Exception as _dexc:  # noqa: BLE001
                debug_log.emit_exception(
                    "supervisor", "danger.record_failed", _dexc,
                    state_root=self.state_root, session_id=self.session.id,
                    op=_dop, target=_dtarget,
                )
            if _dflag.tier == danger_mod.IRREVERSIBLE:
                debug_log.emit(
                    "supervisor", "danger.irreversible_denied",
                    state_root=self.state_root, session_id=self.session.id,
                    op=_dop, target=_dtarget, pattern=_dflag.pattern,
                )
                decision = Decision(
                    verdict="deny", reason="DANGER_IRREVERSIBLE",
                    rule_id="danger.irreversible", touched=_dtarget,
                )

        shadow_mode = armors_mod.active_shadow_mode(self.state_root)

        # Approval verdict: write pending record, notify, block until operator
        # responds (or timeout). Response can promote to override/blackhole.
        if decision.verdict == "approval":
            debug_log.emit(
                "supervisor", "query.approval_required",
                state_root=self.state_root,
                session_id=self.session.id,
                verdict=decision.verdict,
                reason=decision.reason,
                rule_id=decision.rule_id,
                touched=decision.touched,
            )
            record = approvals_mod.ApprovalRecord(
                id=approvals_mod.new_approval_id(),
                session_id=self.session.id,
                created_at=__import__("time").time(),
                op=str(msg.get("op", "")),
                path=msg.get("path"),
                command=str(msg.get("command") or "") or None,
                args=list(msg.get("args") or []),
                cwd=msg.get("cwd"),
                pid=msg.get("pid"),
                rule_id=decision.rule_id,
                rule_pattern=decision.approval_pattern,
            )
            approvals_mod.write_pending(self.session.root, record)
            await self.events.append(
                {
                    "kind": "approval_pending",
                    "approval_id": record.id,
                    "op": record.op,
                    "path": record.path,
                    "command": record.command,
                    "rule_id": record.rule_id,
                }
            )
            # Notify operator via stderr — CLI surfaces the prompt.
            import sys as _sys
            target = record.path or record.command or ""
            _sys.stderr.write(
                f"[ara] APPROVAL {record.id} op={record.op} target={target}\n"
                f"[ara] respond: ara approval respond {record.id} [allow|deny|allow-always|deny-always]\n"
            )
            # Routine (non-danger) ops may auto-grant after a short timeout
            # when the operator has the feature enabled, so an IPC/approval
            # blip can't stall an autonomous run. Danger-classified ops keep
            # the full human-in-the-loop timeout and deny-on-timeout.
            _ag_eligible = (
                approvals_mod.autogrant_enabled(self.state_root)
                and danger_mod.is_auto_grant_eligible(
                    str(msg.get("op") or ""), msg)
            )
            if _ag_eligible:
                resolved = await approvals_mod.wait_for_resolution(
                    self.session.root, record.id,
                    timeout=approvals_mod.autogrant_timeout_s(self.state_root),
                    on_timeout="allow",
                )
            elif os.environ.get("ARA_TASK_RUN_ID"):
                # Unattended task run: rather than hang forever waiting for a
                # human, self-attest via a shadow model call — ask whether the
                # flagged operation ACTUALLY performs the destructive action or
                # is a false positive (a benign read/probe that merely contains
                # the keyword, e.g. reading /etc/passwd). Allow false positives,
                # deny confirmed-danger. INTERIM — replace with context-aware
                # rules (tracked on the workbench).
                _act, _rzn = self._shadow_attest_danger(msg, record)
                approvals_mod.resolve(
                    self.session.root, record.id, _act, reason=_rzn)
                debug_log.emit(
                    "approval-attest", "resolved",
                    state_root=self.state_root, session_id=self.session.id,
                    approval_id=record.id, rule_id=record.rule_id,
                    action=_act, reason=_rzn)
                resolved = await approvals_mod.wait_for_resolution(
                    self.session.root, record.id
                )
            else:
                resolved = await approvals_mod.wait_for_resolution(
                    self.session.root, record.id
                )
            await self.events.append(
                {
                    "kind": "approval_resolved",
                    "approval_id": record.id,
                    "status": resolved.status,
                    "action": resolved.action,
                    "reason": resolved.reason,
                    "auto_granted": resolved.action == "allow-timeout",
                }
            )
            # Translate resolution into a final Decision. "allow-always" /
            # "deny-always" also mutate the overrides store.
            act = resolved.action or "deny"
            expires_at = None
            if resolved.ttl_s and resolved.ttl_s > 0:
                expires_at = __import__("time").time() + resolved.ttl_s
            if act.startswith("allow"):
                if act == "allow-always":
                    _bulwark_append_from_approval(
                        self.state_root, record, action="allow",
                        expires_at=expires_at,
                    )
                decision = Decision(
                    verdict="allow",
                    reason=("APPROVAL_AUTO_GRANTED"
                            if act == "allow-timeout" else "APPROVAL_ALLOWED"),
                    rule_id=record.rule_id, touched=target,
                )
            else:
                if act == "deny-always":
                    _bulwark_append_from_approval(
                        self.state_root, record, action="deny",
                        expires_at=expires_at,
                    )
                decision = Decision(
                    verdict="deny",
                    reason=("APPROVAL_TIMEOUT" if act == "timeout" else "APPROVAL_DENIED"),
                    rule_id=record.rule_id, touched=target,
                )

        # Shadow mode: if the equipped armor is in dry-run, turn deny into
        # allow but keep the audit entry tagged so operators can see what
        # would have been denied. Approval-verdict resolutions are honored
        # as-is (operator decided already).
        shadowed = False
        if (
            shadow_mode and decision.verdict == "deny"
            and decision.rule_id != "danger.irreversible"
        ):
            shadowed = True
            original = decision
            decision = Decision(
                verdict="allow",
                reason=f"SHADOW_WOULD_DENY:{original.reason or ''}",
                rule_id=original.rule_id,
                touched=original.touched,
            )

        await self.policy_hits.append(
            {
                "kind": "query",
                "op": msg.get("op"),
                "path": msg.get("path"),
                "command": msg.get("command"),
                "args": msg.get("args"),
                "pid": msg.get("pid"),
                "verdict": decision.verdict,
                "reason": decision.reason,
                "rule_id": decision.rule_id,
                "touched": decision.touched,
                "shadow": shadowed or None,
            }
        )
        # Exec-deny feedback: when the guard hard-blocks an exec, queue a
        # one-shot notice so the MITM proxy can tell the agent (on its next
        # model turn) what was blocked and to skip it, instead of the agent
        # re-issuing the same silently-denied command. This covers denials
        # decided at the supervisor (kind=query). Shim-enforced static command
        # blocks (kind=policy_hit) are covered in ``_on_event``. Shadowed
        # denies became verdict=allow above, so they don't nag.
        if msg.get("op") == "exec.spawn" and decision.verdict == "deny":
            self._queue_exec_deny_notice(
                msg.get("command"), msg.get("args"), decision.rule_id,
            )
        debug_log.emit(
            "supervisor", "query.final",
            state_root=self.state_root,
            session_id=self.session.id,
            seq=seq,
            verdict=decision.verdict,
            reason=decision.reason,
            rule_id=decision.rule_id,
            touched=decision.touched,
            shadow=shadowed,
        )
        return {"type": "decision", "seq": seq, **decision.to_dict()}

    # ── seccomp-notif egress enforcement (kernel-level, unbypassable) ────
    # A seccomp filter in the contained agent traps every connect() and hands
    # it here. We decide with the SAME bulwark policy the shim/MITM use, so
    # this is ARA's existing engine enforcing at a new, un-dodgeable layer.
    def attach_seccomp_notif(self, notif_fd: int) -> None:
        import threading
        self._seccomp_stop = False
        t = threading.Thread(
            target=self._seccomp_notif_loop, args=(notif_fd,),
            name="ara-seccomp-notif", daemon=True)
        self._seccomp_thread = t
        t.start()
        debug_log.emit(
            "seccomp-egress", "attached", state_root=self.state_root,
            session_id=self.session.id, notif_fd=notif_fd)

    def _shadow_attest_danger(self, msg: dict, record) -> "tuple[str, str]":
        """Interim: ask the model whether a danger-flagged op is real or a false
        positive (a benign read/probe that merely matches the rule keyword).
        Returns ("allow", reason) for a false positive, else ("deny", reason).
        Fail-closed to deny on any error. INTERIM — replace with context-aware
        rules (tracked on the workbench)."""
        rid = getattr(record, "rule_id", "") or ""
        try:
            from .temper_driver import _default_model_invoke
            cmd = " ".join(
                [str(msg.get("path") or "")]
                + [str(a) for a in (msg.get("args") or [])]
            ).strip() or str(
                msg.get("command") or msg.get("argv")
                or msg.get("touched") or "")
            pattern = ""
            for r in self.bulwark.rules:
                if r.rule_id == rid:
                    pattern = r.match.primary or ""
                    break
            system = (
                "You are a security guard classifier for an AUTHORIZED pentest. "
                "An operation was auto-flagged because it matched a rule meant to "
                "catch a genuinely destructive or irreversible action (changing "
                "credentials, adding/removing users, flashing firmware, factory "
                "reset, wiping disks). Decide whether the operation ACTUALLY "
                "performs that destructive action, or is a benign read / probe / "
                "recon that merely contains the keyword (e.g. reading "
                "/etc/passwd, or requesting a URL whose path contains 'passwd' "
                "or 'reset'). Answer with ONE word only: 'yes' (it genuinely "
                "performs the destructive action) or 'no' (false positive)."
            )
            user = (
                f"Flag rule pattern: {pattern}\n"
                f"Operation: {cmd}\n"
                "Does this actually perform the destructive action? "
                "Answer yes or no only."
            )
            reply = str(_default_model_invoke(
                system, user, timeout_s=25.0) or "").strip().lower()
            token0 = reply.split()[0].strip(".,!:'\"") if reply else ""
            if token0.startswith("no"):
                return "allow", f"SHADOW_ATTEST_FALSE_POSITIVE:{rid}"
            return "deny", f"SHADOW_ATTEST_CONFIRMED_DANGER:{rid}"
        except Exception as exc:  # noqa: BLE001
            return "deny", ("SHADOW_ATTEST_ERROR:" + repr(exc))[:90]

    def _seccomp_failopen(self) -> bool:
        return str(os.environ.get("ARA_SECCOMP_FAILOPEN") or "").strip().lower(
        ) in ("1", "true", "on", "yes")

    @staticmethod
    def _net_rule_match(rule: dict, host: str, port: str) -> bool:
        import fnmatch
        rh = str(rule.get("host") or "*")
        rp = str(rule.get("port") or "*")
        return fnmatch.fnmatch(host, rh) and (
            rp == "*" or fnmatch.fnmatch(port, rp))

    def _seccomp_decide(self, fam, host, port) -> "tuple[str, str]":
        # Net decision uses ALLOW-FIRST semantics on the same bulwark net
        # rules the shim gets (allowlist beats deny) — NOT _evaluate_bulwark,
        # which is deny-over-allow and would wrongly deny loopback/provider.
        from . import seccomp_notif as _sn
        if fam is None:  # couldn't read the destination
            return ("allow" if self._seccomp_failopen() else "deny", "READ_FAIL")
        if fam in _sn.LOOPBACK_ALLOW_FAMILIES:
            return ("allow", "AF_UNIX")  # local IPC — never egress, no deadlock
        if host is None:
            return ("allow", f"FAMILY_{fam}")  # non-inet/unix, rare
        ps = str(port)
        for r in self._bulwark_net_rules("allow"):
            if self._net_rule_match(r, host, ps):
                return ("allow", f"ALLOW_NET:{r.get('rule_id') or ''}")
        for r in self._bulwark_net_rules("deny"):
            if self._net_rule_match(r, host, ps):
                return ("deny", f"DENY_NET:{r.get('rule_id') or ''}")
        return ("allow" if self._seccomp_failopen() else "deny", "NO_RULE")

    def _seccomp_notif_loop(self, notif_fd: int) -> None:
        from . import seccomp_notif as _sn
        h = _sn.NotifHandler(notif_fd)
        while not getattr(self, "_seccomp_stop", False):
            try:
                n = h.recv()
            except OSError as exc:
                debug_log.emit(
                    "seccomp-egress", "recv_error", state_root=self.state_root,
                    session_id=self.session.id, error=repr(exc))
                break
            if n is None:
                continue
            if not h.id_valid(n.id):
                continue
            t0 = time.time()
            if n.nr == _sn.SYS_connect:
                ptr, alen, op = n.args[1], n.args[2], "connect"
            elif n.nr == _sn.SYS_sendto:
                ptr, alen, op = n.args[4], n.args[5], "sendto"
            else:  # unexpected trap — let it proceed
                try:
                    h.allow(n.id)
                except OSError:
                    pass
                continue
            fam, host, port = h.read_dest(n.pid, ptr, alen)
            verdict, reason = self._seccomp_decide(fam, host, port)
            try:
                (h.allow if verdict == "allow" else h.deny)(n.id)
            except OSError as exc:
                debug_log.emit(
                    "seccomp-egress", "respond_error",
                    state_root=self.state_root, session_id=self.session.id,
                    error=repr(exc))
                continue
            debug_log.emit(
                "seccomp-egress", "decision", state_root=self.state_root,
                session_id=self.session.id, pid=n.pid, op=op, family=fam,
                host=host, port=port, verdict=verdict, reason=reason,
                latency_ms=round((time.time() - t0) * 1000, 2),
                enforcer="seccomp")
        debug_log.emit(
            "seccomp-egress", "loop_exit", state_root=self.state_root,
            session_id=self.session.id)

    def _evaluate_bulwark(self, msg: dict) -> "Decision | None":
        """Map an incoming guard query to a Bulwark event and evaluate.
        Returns a legacy-shaped ``Decision`` when a rule fires (or None
        to fall through to walls/overrides)."""
        if not self.bulwark.rules:
            return None
        event = _bulwark_event_from_msg(msg)
        if event is None:
            return None
        try:
            armor_id = armors_mod.active_armor_name(self.state_root)
        except Exception:  # noqa: BLE001
            armor_id = None
        d = bulwark_mod.evaluate(
            self.bulwark, event,
            armor_id=armor_id, state=self.bulwark_state,
        )
        if d.rule is None:
            debug_log.emit(
                "bulwark", "no_match",
                state_root=self.state_root,
                session_id=self.session.id,
                op=msg.get("op"),
                bulwark_event=event,
                armor_id=armor_id,
            )
            return None
        touched = (
            event.get("path") or event.get("host") or event.get("target") or ""
        )
        action = d.action
        if action == "allow":
            verdict = "allow"
            reason = f"BULWARK_ALLOW:{d.rule.rule_id}"
        elif action == "approval":
            verdict = "approval"
            reason = f"BULWARK_APPROVAL:{d.rule.rule_id}"
        elif action == "deny":
            verdict = "deny"
            reason = f"BULWARK_DENY:{d.rule.rule_id}"
        else:
            # rewrite/shadow_rewrite aren't meaningful at the syscall
            # layer; treat as fall-through.
            debug_log.emit(
                "bulwark", "non_syscall_action_fallthrough",
                state_root=self.state_root,
                session_id=self.session.id,
                op=msg.get("op"),
                bulwark_event=event,
                armor_id=armor_id,
                rule_id=d.rule.rule_id,
                action=action,
            )
            return None
        debug_log.emit(
            "bulwark", "rule_fired",
            state_root=self.state_root,
            session_id=self.session.id,
            op=msg.get("op"),
            bulwark_event=event,
            armor_id=armor_id,
            rule_id=d.rule.rule_id,
            action=action,
            verdict=verdict,
            touched=str(touched),
        )
        return Decision(
            verdict=verdict, reason=reason,
            rule_id=d.rule.rule_id, touched=str(touched),
        )

    # ── v0.2 pause-and-ask classification helpers ───────────────────
    def _maybe_inject_classification_request(
        self, payload: dict,
    ) -> dict | None:
        return _paas.maybe_inject_classification_request(self.state_root, payload)

    def _maybe_consume_classification_response(self, payload: dict) -> None:
        _paas.maybe_consume_classification_response(self.state_root, payload)

    _inject_system_message = staticmethod(_paas.inject_system_message)
    _extract_response_text = staticmethod(_paas.extract_response_text)

    # ── v0.1 trim-gate composition ──────────────────────────────────
    def _evaluate_trim_gate(
        self, msg: dict, prior: Decision,
    ) -> "Decision | None":
        """Consult the v0.1 trim gate. Returns a *stricter* Decision when
        the engagement contract demands one, else None (keep prior).

        Per spec ``armor-trim-v0.1-amendment.md`` §11 + §7-10. Lazy
        imports keep cold-start fast for sessions with no active trim
        — most ops fall through with a single dict lookup.
        """
        if prior.verdict == "deny":
            return None
        if task_action_gate_mod.is_task_control_plane_action(msg):
            return None
        if (
            os.environ.get("ARA_TASK_SUPERVISOR") == "armory-hermes-loop"
            and task_action_gate_mod.is_task_shell_transport_action(msg)
        ):
            return None
        armor_name = armors_mod.active_armor_name(self.state_root)
        if not armor_name:
            return None
        try:
            from .runtime import trim_gate as _tg
        except Exception:
            return None
        view = _tg.view_for_active_trim(self.state_root, armor_name)
        if view is None:
            return None

        tool, args = _extract_tool_for_classification(msg)
        host = str(msg.get("host") or msg.get("target") or "")
        op_str = str(msg.get("op", ""))
        op_kind = "read" if op_str.startswith(("fs.read", "fs.stat")) else "active"

        try:
            v = _tg.evaluate_call(
                self.state_root, trim=view,
                op_kind=op_kind, host=host,
                tool=tool, args=args,
                now_epoch=time.time(),
            )
        except Exception as exc:
            # Best-effort: trim gate must never break policy. Failure
            # falls through to bulwark's prior decision.
            return None  # noqa: F841 — exc available for future logging

        if v.allow and not v.requires_token and not v.requires_classification:
            debug_log.emit(
                "trim", "allow",
                state_root=self.state_root,
                session_id=self.session.id,
                op=msg.get("op"),
                op_kind=op_kind,
                host=host,
                tool=tool,
                gate=v.gate,
                reason=v.reason,
            )
            return None

        if not v.allow:
            decision = Decision(
                verdict="deny", reason=f"TRIM_GATE: {v.reason}",
                rule_id=v.gate, touched=host or tool or None,
            )
            debug_log.emit(
                "trim", "deny",
                state_root=self.state_root,
                session_id=self.session.id,
                op=msg.get("op"),
                op_kind=op_kind,
                host=host,
                tool=tool,
                gate=v.gate,
                reason=v.reason,
                verdict=decision.verdict,
            )
            return decision
        if v.requires_classification:
            # v0.2 pause-and-ask: register the pending classification
            # so the next outbound LLM turn injects a system message
            # asking the agent to classify, and the subsequent response
            # is parsed for the JSON reply. See _on_prompt_intercept.
            # The original tool call is denied for THIS attempt — the
            # agent re-tries after seeing the verdict, and on retry the
            # registry has the entry so the gate concurs/denies based
            # on the now-known classification.
            if v.classification_key and v.classification_prompt:
                _paas.queue(
                    self.state_root,
                    _paas.PendingClassification(
                        tool=tool, flag_pattern=tool,
                        classification_key=v.classification_key,
                        classification_prompt=v.classification_prompt,
                        engagement_env=view.env, posture=view.posture,
                        queued_at=time.time(),
                    ),
                )
            decision = Decision(
                verdict="approval",
                reason=f"TRIM_GATE: classify `{tool}` first",
                rule_id="tool_registry:first_use",
                touched=tool, approval_pattern=v.classification_key,
            )
            debug_log.emit(
                "trim", "classification_required",
                state_root=self.state_root,
                session_id=self.session.id,
                op=msg.get("op"),
                op_kind=op_kind,
                host=host,
                tool=tool,
                gate=v.gate,
                reason=v.reason,
                classification_key=v.classification_key,
            )
            return decision
        if v.requires_token:
            decision = Decision(
                verdict="approval",
                reason=f"TRIM_GATE: needs {v.requires_token}",
                rule_id="tool_registry:token_required",
                touched=tool, approval_pattern=v.requires_token,
            )
            debug_log.emit(
                "trim", "token_required",
                state_root=self.state_root,
                session_id=self.session.id,
                op=msg.get("op"),
                op_kind=op_kind,
                host=host,
                tool=tool,
                gate=v.gate,
                reason=v.reason,
                token=v.requires_token,
            )
            return decision
        return None

    def _queue_exec_deny_notice(self, command, args, rule_id) -> None:
        """Queue a one-shot exec-deny notice for the MITM to inject on the
        agent's next turn. Best-effort and additive — the deny is already
        recorded. Called from both the supervisor-query path (``_on_query``)
        and the shim-enforced static-block path (``_on_event``)."""
        try:
            from . import exec_deny_notices as _edn
            _cmd = str(command or "")
            if isinstance(args, list):
                _cmd = (_cmd + " " + " ".join(str(a) for a in args)).strip()
            _rule = next(
                (r for r in getattr(getattr(self, "bulwark", None), "rules", [])
                 if getattr(r, "rule_id", None) == rule_id),
                None,
            )
            _tok = ""
            if _rule is not None:
                _m = getattr(_rule, "match", None)
                _tok = _edn.extract_matched_token(
                    getattr(_m, "primary", "") or "",
                    bool(getattr(_m, "is_regex", False)),
                    _cmd,
                )
            _edn.append(
                self.state_root,
                rule_id=rule_id or "",
                matched_token=_tok,
                cmd_summary=_cmd[:200],
                ts=time.time(),
            )
        except Exception:  # noqa: BLE001
            pass

    async def _on_event(self, msg: dict) -> None:
        payload = {k: v for k, v in msg.items() if k != "type"}
        payload.setdefault("kind", "event")
        kind = payload.get("kind")
        # Record heartbeats so the watchdog can tell silenced guards from
        # live ones. Still written to events.jsonl for the timeline view.
        if kind == "heartbeat":
            pid = msg.get("pid")
            if isinstance(pid, int) and pid > 0:
                import time as _time
                self._heartbeat_at[pid] = _time.time()
        # Guard emits "policy_hit" events after local rule evaluation (the
        # guard decides against supervisor-pushed rules, but the audit trail
        # still lands here authoritatively). Route those to policy_hits.jsonl
        # so the on-disk shape matches the legacy query-driven audit.
        if kind in ("policy_hit", "policy_hit_sync"):
            await self.policy_hits.append(payload)
            # Shim-enforced static command blocks (rm/chmod/curl/…) are
            # decided locally by the guard and never become a supervisor
            # query, so the ``_on_query`` notice hook misses them. Cover them
            # here so the agent also gets the skip-and-continue feedback.
            if (payload.get("op") == "exec.spawn"
                    and payload.get("verdict") == "deny"):
                self._queue_exec_deny_notice(
                    payload.get("command") or payload.get("path"),
                    payload.get("args"),
                    payload.get("rule_id"),
                )
        else:
            await self.events.append(payload)
        if kind == "spawn":
            _capture_spawn_evidence(self.session.root, payload)
        elif kind == "spawn_output":
            _capture_spawn_output_evidence(self.session.root, payload)

    # ---------- forge-v1 engagement wiring (channel_call IPC) ----------

    def _maybe_install_engagement(self) -> None:
        """If a weapon is equipped, install :class:`EngagementWiring`
        bound to the active session.

        Idempotent: subsequent calls (e.g. if the equipped-weapon file
        changes) re-install against the new weapon. v1 doesn't watch
        the equipped-weapon file at runtime; equipping mid-session
        requires a supervisor restart.
        """
        try:
            weapon_id = weapons_mod.equipped_weapon_id(self.state_root)
        except OSError:
            return
        if weapon_id is None:
            self.engagement = None
            return
        weapon_dir = weapons_mod.weapon_dir(self.state_root, weapon_id)
        if not weapons_mod.weapon_exists(self.state_root, weapon_id):
            return
        # Lazy import — keeps supervisor importable when forge-v1 isn't
        # in use (e.g. legacy ARA installs without the runtime package).
        from .runtime.bootstrap import install_engagement
        sessions_root = self.state_root / "sessions"
        try:
            self.engagement = install_engagement(
                sessions_root=sessions_root,
                engagement_id=self.session.id,
                weapon_dir=weapon_dir,
                install_anomaly_autofirer=True,
                install_chunk_runtime=True,
                auto_initialize_chunks=True,
            )
        except Exception:  # noqa: BLE001 — best-effort; never block supervisor start
            self.engagement = None

    async def _on_channel_call(self, msg: dict) -> dict:
        """Dispatch a typed-channel call from a guard / agent SDK.

        Message shape:
            {type: "channel_call", channel: str, body: dict, seq?: int}

        Caller is hard-coded to ``"agent"`` (Q4-A); operator-only
        channels reject. Tool-surface enforcement runs against the
        active chunk's allowed channels (Q5-A).
        """
        seq = msg.get("seq")
        wiring = self.engagement
        if wiring is None:
            debug_log.emit(
                "supervisor", "channel_call.reject",
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=msg.get("channel"),
                error_kind="no_weapon_equipped",
            )
            return {
                "type": "channel_call_response",
                "seq": seq, "ok": False,
                "error_kind": "no_weapon_equipped",
                "error": "no weapon is equipped; ara weapon equip <id>",
            }
        channel_name = msg.get("channel")
        if not isinstance(channel_name, str) or not channel_name:
            debug_log.emit(
                "supervisor", "channel_call.reject",
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=channel_name,
                error_kind="invalid_channel",
            )
            return {
                "type": "channel_call_response", "seq": seq, "ok": False,
                "error_kind": "invalid_channel",
                "error": "channel name required",
            }
        body = msg.get("body") or {}
        if not isinstance(body, dict):
            debug_log.emit(
                "supervisor", "channel_call.reject",
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=channel_name,
                error_kind="invalid_body",
            )
            return {
                "type": "channel_call_response", "seq": seq, "ok": False,
                "error_kind": "invalid_body",
                "error": "body must be a dict",
            }
        body = self._adapt_task_channel_body(channel_name, body)
        from .runtime.channels import registry as _registry
        from .runtime.channels.base import (
            ChannelContext as _Ctx,
            ChannelRejection as _Rej,
        )
        try:
            channel = _registry.get(channel_name)
        except KeyError:
            debug_log.emit(
                "supervisor", "channel_call.reject",
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=channel_name,
                body=body,
                error_kind="unknown_channel",
            )
            return {
                "type": "channel_call_response", "seq": seq, "ok": False,
                "error_kind": "unknown_channel",
                "error": f"channel {channel_name!r} not registered",
            }
        # Tool-surface enforcement (Q5-A) — only after we've confirmed
        # the channel exists, so a typo in the call surfaces as
        # 'unknown_channel' rather than misleading 'tool_not_in_surface'.
        chunk_runtime = getattr(wiring, "chunk_runtime", None)
        if chunk_runtime is not None and not chunk_runtime.is_tool_allowed(channel_name):
            debug_log.emit(
                "supervisor", "channel_call.reject",
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=channel_name,
                body=body,
                error_kind="tool_not_in_surface",
                chunk=chunk_runtime.current_chunk_id(),
            )
            return {
                "type": "channel_call_response", "seq": seq, "ok": False,
                "error_kind": "tool_not_in_surface",
                "error": (
                    f"channel {channel_name!r} not in active chunk's "
                    f"tool_surface ({chunk_runtime.current_chunk_id()!r})"
                ),
            }
        task_rejection = task_action_gate_mod.task_commit_channel_rejection_reason(
            self.state_root,
            channel_name,
        )
        if task_rejection:
            debug_log.emit(
                "supervisor", "channel_call.task_rejected",
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=channel_name,
                body=body,
                reason=task_rejection,
            )
            return {
                "type": "channel_call_response", "seq": seq, "ok": False,
                "error_kind": "task_channel_rejection",
                "error": task_rejection,
            }
        # Build channel context. Activation_id from the engagement's
        # ActivationManager (chunk-runtime mints a fresh one per swap).
        activation_manager = getattr(wiring, "activation_manager", None)
        activation_id = (
            activation_manager.current() if activation_manager is not None else None
        )
        ctx = _Ctx(
            engagement_id=self.session.id,
            activation_id=activation_id,
            caller="agent",  # Q4-A
            audit_sink=self.session.events_path,
        )
        try:
            debug_log.emit(
                "supervisor", "channel_call.start",
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=channel_name,
                activation_id=activation_id,
                body=body,
            )
            result = channel.call(body, ctx)
        except _Rej as e:
            debug_log.emit(
                "supervisor", "channel_call.reject",
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=channel_name,
                body=body,
                error_kind="channel_rejection",
                error=str(e),
            )
            return {
                "type": "channel_call_response", "seq": seq, "ok": False,
                "error_kind": "channel_rejection",
                "error": str(e),
            }
        except Exception as e:  # noqa: BLE001 — never crash the supervisor
            debug_log.emit_exception(
                "supervisor", "channel_call.error",
                e,
                state_root=self.state_root,
                session_id=self.session.id,
                seq=seq,
                channel=channel_name,
                body=body,
            )
            return {
                "type": "channel_call_response", "seq": seq, "ok": False,
                "error_kind": "channel_error",
                "error": f"{type(e).__name__}: {e}",
            }
        if channel_name in {
            "fact.assert",
            "lead.file",
            "finding.file",
            "chunks.emit_tag",
        }:
            task_action_gate_mod.record_commit(self.state_root, channel_name)
        debug_log.emit(
            "supervisor", "channel_call.ok",
            state_root=self.state_root,
            session_id=self.session.id,
            seq=seq,
            channel=channel_name,
            commit_channel=task_action_gate_mod.is_commit_channel(channel_name),
            result=result,
        )
        return {
            "type": "channel_call_response", "seq": seq, "ok": True,
            "channel": channel_name,
            "result": result,
        }

    def _adapt_task_channel_body(self, channel_name: object, body: dict) -> dict:
        """Normalize native MCP channel_call bodies like MITM pseudo-calls.

        Hermes sometimes emits canonical channel payloads with empty refs and
        expects evidence lookup/bridge behavior. The MITM pseudo-tool path
        already applies this adapter; native ForgeChannelClient calls need the
        same boundary normalization before strict channel validation.
        """
        if channel_name not in {"fact.assert", "lead.file", "finding.file"}:
            return body
        try:
            recent = self._latest_session_evidence_refs(limit=3)
            cleaned = self._drop_unresolved_channel_evidence_refs(body)
            adapted = adapt_task_channel_args(str(channel_name), cleaned, recent)
        except Exception:
            return body
        if adapted != body:
            debug_log.emit(
                "supervisor",
                "channel_call.adapted",
                state_root=self.state_root,
                session_id=self.session.id,
                channel=channel_name,
                before=body,
                after=adapted,
            )
        return adapted

    def _drop_unresolved_channel_evidence_refs(self, body: dict) -> dict:
        session_dir = self.session.root
        try:
            from .runtime.binder.api import EvidenceBinder
            binder = EvidenceBinder(session_dir)
        except Exception:
            return body
        try:
            return drop_unresolved_evidence_refs(
                body,
                lambda evidence_id: binder.get_record(evidence_id) is not None,
            )
        finally:
            try:
                binder.close()
            except Exception:
                pass

    def _latest_session_evidence_refs(self, *, limit: int = 3) -> list[str]:
        db_path = self.session.root / "evidence" / "index.db"
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT id FROM evidence ORDER BY captured_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return [
            str(row[0])
            for row in rows
            if row and isinstance(row[0], str) and row[0]
        ]

    # ---------- forge-v1 auto-brief injector ----------

    _TASK_COMMIT_MCP_TOOLS = {
        "mcp_ara_forge_fact_assert",
        "mcp_ara_forge_lead_file",
        "mcp_ara_forge_finding_file",
        "mcp_ara_forge_chunks_emit_tag",
    }
    _TASK_COMMIT_MCP_TOOL_ORDER = [
        "mcp_ara_forge_lead_file",
        "mcp_ara_forge_chunks_emit_tag",
        "mcp_ara_forge_fact_assert",
        "mcp_ara_forge_finding_file",
    ]

    def _task_channel_debt_active(self) -> bool:
        """Return true when an Armory task has consumed its exec budget
        without committing a fact/lead/finding channel.
        """
        import os as _os

        if _os.environ.get("ARA_TASK_SUPERVISOR") != "armory-hermes-loop":
            return False
        task_id = str(_os.environ.get("ARA_TASK_RUN_ID") or "").strip()
        if task_id:
            try:
                from . import task_session as _task_session_mod
                run = _task_session_mod.TaskRunSession.load(
                    self.state_root, task_id,
                )
                return run.phase == _task_session_mod.TaskPhase.COMMIT_REQUIRED
            except Exception:
                pass
        try:
            budget = int(_os.environ.get("ARA_TASK_CHANNEL_EXEC_BUDGET") or "12")
        except ValueError:
            budget = 12
        if budget < 1:
            budget = 12
        try:
            lines = self.session.events_path.read_text(errors="replace").splitlines()
        except OSError:
            return False
        spawns = 0
        commits = 0
        for line in lines:
            if '"kind": "spawn"' in line:
                spawns += 1
            if '"status": "rejected"' in line:
                continue
            if (
                '"channel": "fact.assert"' in line
                or '"channel": "lead.file"' in line
                or '"channel": "finding.file"' in line
                or '"kind": "channel.fact.assert"' in line
                or '"kind": "channel.lead.file"' in line
                or '"kind": "channel.finding.file"' in line
            ):
                commits += 1
        threshold = max(1, (budget * (commits + 1)) - 1)
        return spawns >= threshold

    def _maybe_filter_task_channel_debt_tools(self, payload: dict) -> dict | None:
        """When task channel debt is active, remove general action tools
        from the next LLM request so the agent can only commit structured
        ARA records before continuing work.
        """
        allowed_names = None
        try:
            from .task_action_gate import task_debt_mcp_tool_names
            allowed_names = task_debt_mcp_tool_names(self.state_root)
        except Exception:
            allowed_names = None
        if allowed_names is None:
            if not self._task_channel_debt_active():
                return None
            allowed_names = tuple(self._TASK_COMMIT_MCP_TOOL_ORDER)
        if not allowed_names:
            return None
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return None
        allowed_set = set(allowed_names)
        filtered = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            name = fn.get("name") if isinstance(fn, dict) else None
            if name in allowed_set:
                filtered.append(tool)
        if not filtered:
            return None
        name_to_tool = {}
        for tool in filtered:
            fn = tool.get("function") if isinstance(tool, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if name:
                name_to_tool[name] = tool
        ordered = [
            name_to_tool[name]
            for name in allowed_names
            if name in name_to_tool
        ]
        ordered.extend([
            tool for tool in filtered
            if tool not in ordered
        ])
        out = dict(payload)
        out["tools"] = ordered
        out["tool_choice"] = _tool_choice_for_filtered_tools(ordered)
        debug_log.emit(
            "supervisor", "task_debt.tools_filtered",
            state_root=self.state_root,
            session_id=self.session.id,
            original_count=len(tools),
            filtered_count=len(ordered),
            kept=[
                (tool.get("function") or {}).get("name")
                for tool in ordered
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            ],
        )
        return out

    def _maybe_inject_forge_brief(self, payload: dict) -> dict | None:
        """Build a brief from the active engagement state and inject it
        as a system message in ``payload``. Returns the augmented payload
        when injection happened, ``None`` when there's nothing to inject
        (no engagement, no chunk runtime, brief already present).
        """
        wiring = self.engagement
        if wiring is None:
            return None
        chunk_runtime = getattr(wiring, "chunk_runtime", None)
        if chunk_runtime is None:
            return None
        chunk = chunk_runtime.current_chunk()
        from . import forge_brief
        weapon_id = weapons_mod.equipped_weapon_id(self.state_root)
        resources: list[dict] = []
        if weapon_id:
            weapon_json = weapons_mod.weapon_dir(self.state_root, weapon_id) / "weapon.json"
            try:
                data = json.loads(weapon_json.read_text())
                raw_resources = data.get("resources") or []
                if isinstance(raw_resources, list):
                    resources = [r for r in raw_resources if isinstance(r, dict)]
            except Exception:
                resources = []
        # Pack name lives on the loaded weapon's draft; we read the
        # config directly via the ForgeSession wouldn't help here
        # (forge sessions are separate from engagement sessions). The
        # weapon's seal.json carries the pack name implicitly via its
        # weapon.json — but reading it here on every request is wasted
        # work. Pass None; the brief still works without it.
        import os as _os
        runtime_mode = (
            "task"
            if _os.environ.get("ARA_TASK_SUPERVISOR") == "armory-hermes-loop"
            else "forge"
        )
        available_channels = None
        if runtime_mode == "task":
            try:
                from .clients import forge_mcp_server as _forge_mcp_server
                available_channels = [
                    str(tool.get("name"))
                    for tool in _forge_mcp_server._tools_for_mode(
                        {"ARA_FORGE_MCP_MODE": "task"}
                    )
                ]
            except Exception:
                available_channels = None
        categories = None
        if weapon_id:
            try:
                import json as _json
                _cat_path = (
                    weapons_mod.weapon_dir(self.state_root, weapon_id)
                    / "alphabets" / "category.json"
                )
                if _cat_path.is_file():
                    _cat = _json.loads(_cat_path.read_text())
                    if isinstance(_cat, dict) and isinstance(
                        _cat.get("entries"), list
                    ):
                        categories = _cat["entries"]
            except Exception:  # noqa: BLE001
                categories = None
        brief = forge_brief.build_brief(
            chunk_id=chunk.id if chunk else None,
            chunk_title=chunk.title if chunk else None,
            chunk_prompt_fragment=chunk.prompt_fragment if chunk else None,
            tool_surface=list(chunk.tool_surface) if chunk else [],
            exits={tag: tgt.next_chunk_id for tag, tgt in chunk.exits.items()}
            if chunk else {},
            alphabet_pack_name=None,
            weapon_id=weapon_id,
            resources=resources,
            available_channels=available_channels,
            categories=categories,
            runtime_mode=runtime_mode,
        )
        injected = forge_brief.inject_into_payload(payload, brief)
        return injected

    # ---------- inline chunk-swap tag scanner (2.3 §7.1) ----------

    def _scan_inline_tags(self, payload: object) -> None:
        """Scan a transcript payload for inline ``<tag>`` markers
        matching the active chunk's exits; fire ``chunks.emit_tag``
        synthetically for each unique match.

        No-op when no engagement is mounted, no chunk runtime, or the
        active chunk has no exits. Tag-firing failures are logged to
        events.jsonl and never break transcript handling.
        """
        wiring = self.engagement
        if wiring is None:
            return
        chunk_runtime = getattr(wiring, "chunk_runtime", None)
        if chunk_runtime is None:
            return
        chunk = chunk_runtime.current_chunk()
        if chunk is None or not chunk.exits:
            return
        text = _payload_to_text(payload)
        if not text:
            return
        from .runtime.chunks.runtime import parse_inline_tags
        observed = parse_inline_tags(text)
        if not observed:
            return
        # Only fire for tags that are actually exits of the active chunk
        # — random `<thinking>` blocks in agent prose mustn't trigger
        # swaps. Operators tune chunk exit tags to be unambiguous
        # (`<readied_for_writeup>` etc.) precisely so this scanner
        # has low false-positive risk.
        valid_exits = set(chunk.exits.keys())
        seen: set[str] = set()
        for tag in observed:
            if tag not in valid_exits or tag in seen:
                continue
            seen.add(tag)
            result = chunk_runtime.emit_tag(tag, caller="agent")
            if not result.ok:
                self._maybe_log_event({
                    "kind": "inline_tag_rejected",
                    "tag": tag,
                    "error_kind": result.error_kind,
                    "error": result.error,
                })
            else:
                self._maybe_log_event({
                    "kind": "inline_tag_fired",
                    "tag": tag,
                    "from_chunk": result.from_chunk,
                    "to_chunk": result.to_chunk,
                    "new_activation_id": result.new_activation_id,
                })

    def _maybe_log_event(self, event: dict) -> None:
        """Schedule an events.jsonl append iff there's a running loop;
        otherwise drop. The chunk_runtime's history_path captures the
        transition independently, so dropping is non-fatal — avoids
        unawaited-coroutine warnings when the scanner runs from sync
        test contexts.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.create_task(self.events.append(event))
        except Exception:
            pass

    def guard_env(self, guard_mjs: Path) -> dict[str, str]:
        import os as _os

        task_supervisor = _os.environ.get("ARA_TASK_SUPERVISOR")
        channel_required = task_supervisor == "armory-hermes-loop"
        default_channel_budget = "12" if channel_required else "25"

        return {
            "NODE_OPTIONS": f"--import={guard_mjs}",
            "ARA_SOCKET": str(self.session.socket_path),
            "ARA_SESSION_ROOT": str(self.session.root),
            "ARA_SESSION_TOKEN": self.session.token,
            "ARA_SESSION_ID": self.session.id,
            "ARA_STATE_ROOT": str(self.state_root),
            "ARA_SUPERVISOR_PID": str(_os.getpid()),
            # Retired: the raw exec-count channel gate counted loop/shell
            # plumbing execs (locale, date, hermes --continue), not agent
            # actions, and deadlocked recon before any finding could be
            # committed. The phase machine (action_budget -> COMMIT_REQUIRED)
            # already forces findings-commit; supervisor/mitm still honor that
            # phase-based gate. Force the shim exec tally off.
            "ARA_TASK_CHANNEL_REQUIRED": "0",
            "ARA_TASK_CHANNEL_EXEC_BUDGET": _os.environ.get(
                "ARA_TASK_CHANNEL_EXEC_BUDGET", default_channel_budget
            ),
            # Tier-A transcript channel discovery: ARA-aware agents (e.g.
            # OpenClaw) detect this and call ara_intercept(role, payload)
            # over ARA_SOCKET before sending each LLM turn.
            "ARA_TRANSCRIPT_PROTOCOL": "v1",
        }


def _payload_to_text(payload: object) -> str:
    """Best-effort: extract the agent's prose from a transcript payload.

    Anthropic-style payloads put assistant text under
    ``content: [{type: "text", text: "..."}, ...]``; OpenAI-style
    completions use ``content: "..."`` directly; plain string payloads
    are also handled. Returns concatenated text from every textual
    block; non-text content (tool_use, image, etc.) is ignored — those
    are bridged via channel_call, not the inline scanner.
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    # Direct content field
    content = payload.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
    # Some shapes nest under message
    msg = payload.get("message")
    if isinstance(msg, dict):
        parts.append(_payload_to_text(msg))
    elif isinstance(msg, str):
        parts.append(msg)
    # Choices (OpenAI-style)
    choices = payload.get("choices")
    if isinstance(choices, list):
        for c in choices:
            if isinstance(c, dict):
                parts.append(_payload_to_text(c))
    # Final fallback: any "text" field
    text_field = payload.get("text")
    if isinstance(text_field, str):
        parts.append(text_field)
    return "\n".join(p for p in parts if p)


def _bulwark_append_from_approval(
    state_root: Path, record, action: str, *, expires_at: float | None,
) -> None:
    """Append an allow/deny rule to ``<state_root>/bulwark.md`` under
    ``## global`` when the operator responds allow-always/deny-always.

    Maps the approval's target to the right bulwark layer:
      - record.path → fs layer
      - record.command → exec layer
      - otherwise skip (nothing to record)
    """
    if record.path:
        layer, match = "fs", record.path
    elif record.command:
        layer, match = "exec", record.command
    else:
        return
    mods = ""
    if expires_at is not None:
        import time as _t
        remaining = int(max(1, expires_at - _t.time()))
        mods = f" ttl {remaining}s"
    line = f"{action}{mods} {layer} {match}  # approval:{record.id}\n"

    bw_path = state_root / "bulwark.md"
    existing = bw_path.read_text() if bw_path.exists() else ""
    import re as _re
    header_re = _re.compile(r"^##\s+global\s*$", _re.MULTILINE)
    if header_re.search(existing):
        new_text = header_re.sub(
            lambda m: m.group(0) + "\n" + line.rstrip(), existing, count=1,
        )
    else:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        new_text = existing + sep + f"\n## global\n{line}"
    bw_path.parent.mkdir(parents=True, exist_ok=True)
    bw_path.write_text(new_text)


_OP_TO_LAYER: dict[str, str] = {
    "fs.read": "fs", "fs.write": "fs", "fs.unlink": "fs", "fs.rename": "fs",
    "fs.stat": "fs", "fs.open": "fs",
    "fs.readdir": "nav", "fs.opendir": "nav", "fs.mkdir": "nav",
    "fs.rmdir": "nav", "fs.chdir": "nav",
    "fs.chmod": "perm", "fs.chown": "perm", "fs.setxattr": "perm",
    "fs.chattr": "perm",
    "exec.spawn": "exec",
    "net.connect": "net", "net.send": "net",
    "env.get": "env",
    # oc.tool_call maps to the oc:<tool> layer dynamically — handler
    # picks the sublayer from msg["tool"]. Presence in this dict
    # signals the event shaper.
    "oc.tool_call": "oc",
}


def _bulwark_event_from_msg(msg: dict) -> dict | None:
    """Shape a guard query as a Bulwark event.

    Returns ``None`` when the message doesn't map to a known layer — the
    caller then falls through to the legacy WALLS path without consulting
    Bulwark.
    """
    op = str(msg.get("op") or "")
    layer = _OP_TO_LAYER.get(op)
    if layer is None:
        return None
    if layer in ("fs", "nav"):
        return {"layer": layer, "path": str(msg.get("path") or "")}
    if layer == "perm":
        # Op mnemonic (chmod/chown/...): take the suffix after ``fs.``.
        op_name = op.split(".", 1)[1] if "." in op else op
        return {
            "layer": "perm",
            "path": str(msg.get("path") or ""),
            "op": op_name,
        }
    if layer == "exec":
        args = msg.get("args") or []
        argv = " ".join(str(a) for a in args)
        return {
            "layer": "exec",
            "path": str(msg.get("command") or ""),
            "argv": argv,
        }
    if layer == "net":
        return {
            "layer": "net",
            "host": str(msg.get("host") or ""),
            "port": msg.get("port"),
        }
    if layer == "env":
        return {"layer": "env", "name": str(msg.get("name") or "")}
    if layer == "oc":
        # oc.tool_call query: {tool, target}. Supervisor emits the
        # Bulwark event with layer="oc:<tool>" so tool-slot rules
        # (oc:bash / oc:write / …) and the oc-family catch-all both
        # match correctly.
        tool = str(msg.get("tool") or "").strip()
        target = str(msg.get("target") or "")
        if not tool:
            return None
        return {
            "layer": f"oc:{tool}",
            "tool": tool,
            "target": target,
        }
    return None


_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _is_ara_agent_launcher_spawn(command: str, argv: list[str]) -> bool:
    """True for ARA's own durable Hermes wrapper, not agent target work."""
    base = Path(command).name
    joined = " ".join(argv)
    if base == "hermes":
        return "--continue" in argv and any(a.startswith("ara-task-") for a in argv)
    if base == "timeout":
        return "/hermes" in joined and "--continue" in joined and "ara-task-" in joined
    return False


def _capture_spawn_evidence(session_dir: Path, payload: dict) -> None:
    args = payload.get("args") or []
    argv = [str(a) for a in args] if isinstance(args, list) else [str(args)]
    command = str(payload.get("command") or "").strip()
    if not command or _is_ara_agent_launcher_spawn(command, argv):
        return
    haystack = " ".join(argv)
    match = _URL_RE.search(haystack)
    if match is None:
        return
    url = match.group(0).rstrip("),.;")
    try:
        from .runtime.binder.api import EvidenceBinder
        EvidenceBinder(session_dir).put(
            json.dumps(payload, sort_keys=True, indent=2).encode("utf-8"),
            source="shim:spawn",
            source_metadata={
                "command": command,
                "args": argv,
                "pid": payload.get("pid"),
                "url": url,
                "source": str(payload.get("source") or ""),
            },
            summary=f"spawn {Path(command).name} {url}",
            content_type="application/json",
        )
    except Exception:
        pass


def _capture_spawn_output_evidence(session_dir: Path, payload: dict) -> None:
    args = payload.get("args") or []
    argv = [str(a) for a in args] if isinstance(args, list) else [str(args)]
    command = str(payload.get("command") or "").strip()
    if not command or _is_ara_agent_launcher_spawn(command, argv):
        return
    url = str(payload.get("url") or "").strip()
    if not url:
        match = _URL_RE.search(" ".join([command, *argv]))
        if match is None:
            return
        url = match.group(0).rstrip("),.;")
    if not _URL_RE.match(url):
        return
    stdout_text = payload.get("stdout_text")
    stderr_text = payload.get("stderr_text")
    if not isinstance(stdout_text, str):
        stdout_text = ""
    if not isinstance(stderr_text, str):
        stderr_text = ""
    if not stdout_text and not stderr_text:
        return
    doc = {
        "kind": "spawn_output",
        "command": command,
        "args": argv,
        "url": url,
        "pid": payload.get("pid"),
        "exit_code": payload.get("exit_code"),
        "signal": payload.get("signal"),
        "stdout_text": stdout_text,
        "stderr_text": stderr_text,
        "stdout_truncated": bool(payload.get("stdout_truncated")),
        "stderr_truncated": bool(payload.get("stderr_truncated")),
    }
    try:
        from .runtime.binder.api import EvidenceBinder
        EvidenceBinder(session_dir).put(
            json.dumps(doc, sort_keys=True, indent=2).encode("utf-8"),
            source="shim:spawn_output",
            source_metadata={
                "command": command,
                "args": argv,
                "pid": payload.get("pid"),
                "url": url,
                "source": str(payload.get("source") or ""),
                "exit_code": payload.get("exit_code"),
                "signal": payload.get("signal"),
                "stdout_truncated": bool(payload.get("stdout_truncated")),
                "stderr_truncated": bool(payload.get("stderr_truncated")),
            },
            summary=f"output {Path(command).name} {url}",
            content_type="application/json",
        )
    except Exception:
        pass


def _extract_tool_for_classification(msg: dict) -> tuple[str, list[str]]:
    """Pull (tool_name, args) from a supervisor message for the v0.1
    classification gate. Handles three message shapes:

    1. ``oc.tool_call`` — emitted by the openclaw runtime when the agent
       calls a tool through the gateway IPC. ``msg.tool`` is the slot
       name (``bash`` / ``write`` / etc.); for ``bash``, the actual
       sub-tool is the first token of ``msg.command``.

    2. ``exec.spawn`` — emitted by ``libara_shim.so`` whenever any
       process under the supervisor calls ``execve``. This is what
       fires when openclaw's bash-tool runtime spawns ``/bin/bash -c``
       — i.e. the ground-truth path for every agent shell command,
       even when the agent never emitted an oc.tool_call (e.g. the
       openclaw 2026.4.x bash-tool runtime). ``cmd[0]`` is the binary;
       ``args`` is everything else; for ``bash -c "<inner>"`` we
       recurse into the inner command to extract the *real* tool the
       agent intended to run.

    3. anything else (``fs.*``, ``net.*``, ``perm.*``) → ``("", [])``.
       These layers are bulwark's job; classification doesn't apply.

    Pure helper. Doesn't touch supervisor state.
    """
    op = str(msg.get("op", ""))

    if op == "oc.tool_call":
        tool = str(msg.get("tool") or "").strip()
        if tool == "bash":
            return _parse_bash_command(
                str(msg.get("command") or ""), fallback_tool="bash",
            )
        if tool:
            args = list(msg.get("args") or [])
            return (tool, [str(a) for a in args])
        return ("", [])

    if op == "exec.spawn":
        # Only classify *agent-initiated* shell commands — the openclaw
        # bash-tool runtime's standard invocation is ``/bin/bash -c "<cmd>"``.
        # Any other exec.spawn (node bootstrap, codex app-server, watchdog
        # helpers, ARA's own forge subprocess shells, etc.) is infrastructure,
        # not agent intent; skip classification and let bulwark's exec rules
        # (which DO see all spawns) decide. Without this narrowing, every
        # node spawn at openclaw startup hits the conservative-wiring approval
        # gate and the agent never gets to actually run.
        cmd = str(msg.get("command") or "")
        args = [str(a) for a in (msg.get("args") or [])]
        if not cmd:
            return ("", [])
        base = Path(cmd).name
        if base.endswith(".exe"):
            base = base[:-4]
        if base in ("bash", "sh", "dash", "zsh") and len(args) >= 2 and args[0] == "-c":
            inner_cmd = args[1]
            inner = _parse_bash_command(inner_cmd, fallback_tool=base)
            if inner[0] and inner[0] != base:
                return inner
        return ("", [])

    return ("", [])


def _parse_bash_command(cmd: str, *, fallback_tool: str) -> tuple[str, list[str]]:
    """shlex-split a bash command and return ``(basename(first), rest)``.
    Falls back to ``(fallback_tool, [])`` on unclosed quotes / empty cmd.
    """
    if not cmd:
        return (fallback_tool, [])
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return (fallback_tool, [])
    if not tokens:
        return (fallback_tool, [])
    inner = Path(tokens[0]).name
    if inner.endswith(".exe"):
        inner = inner[:-4]
    return (inner, tokens[1:])


async def run_supervisor_forever(
    walls_file: Path | None = None, state_root: Path | None = None
) -> None:
    if walls_file is not None:
        sup = await Supervisor.start_with_walls_file(walls_file, state_root=state_root)
    else:
        sup = await Supervisor.start(state_root=state_root)
    try:
        await sup.server.serve_forever()
    finally:
        await sup.stop()


if __name__ == "__main__":
    asyncio.run(run_supervisor_forever())
