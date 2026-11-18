"""Single entrypoint that wires every typed channel for one engagement.

A supervisor / engagement bootstrap should call
:func:`install_engagement_channels` once at session start. It registers
every subsystem's channels against the registry and returns a flat
``{name: Channel}`` dict for direct lookup. Replaces 4–5 separate
``install_*_channels(...)`` calls scattered across callers.

The companion :func:`engagement_context_factory` builds a
:class:`ChannelContext` factory bound to the engagement, so callers
don't have to thread ``engagement_id`` / ``audit_sink`` through every
call site.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ara.runtime.activation import ActivationManager
from ara.runtime.anomaly.autofire import (
    AnomalyAutoFirer,
    register_hook as register_autofire_hook,
)
from ara.runtime.anomaly.channels import install_anomaly_channels
from ara.runtime.anomaly.crumb_tracker import CrumbTracker, for_session
from ara.runtime.anomaly.weapon_shapes import (
    WeaponShapes,
    empty as empty_weapon_shapes,
    load_from_weapon,
)
from ara.runtime.channels.base import Channel, ChannelContext
from ara.runtime.chunks.channels import install_chunks_channels
from ara.runtime.binder.channels import install_evidence_channels
from ara.runtime.chunks.runtime import ChunkRuntime, WeaponChunkGraph
from ara.runtime.facts.channels import install_fact_channels
from ara.runtime.findings.channels import install_finding_channels
from ara.runtime.leads.channels import install_lead_channels
from ara.runtime.report.channels import install_report_channel


@dataclass(frozen=True)
class EngagementWiring:
    """Bundle of per-engagement runtime wiring: channels + auto-firer +
    crumb tracker + chunk runtime. Returned by :func:`install_engagement`
    for callers that need direct access.
    """
    channels: dict[str, Channel]
    crumb_tracker: CrumbTracker | None = None
    autofirer: AnomalyAutoFirer | None = None
    weapon_shapes: WeaponShapes | None = None
    chunk_runtime: ChunkRuntime | None = None
    activation_manager: "ActivationManager | None" = None


def install_engagement_channels(
    *,
    sessions_root: Path,
    weapon_dir: Path | None = None,
    in_scope_assets: set[str] | None = None,
    anomaly_cap_per_crumb_per_activation: int = 50,
    install_style: bool = False,
    weapon_style=None,
    engagement_id: str | None = None,
    install_anomaly_autofirer: bool = False,
) -> dict[str, Channel]:
    """Install every typed channel for an engagement against ``sessions_root``.

    Subsystems wired:
    - 1.1 facts:    ``fact.assert``, ``facts.query``, ``facts.history``,
                    ``facts.neighbors``
    - 1.2 leads:    ``lead.file``, ``lead.investigate``, ``lead.touch``,
                    ``lead.dismiss``, ``lead.promote``, ``lead.confirm``,
                    ``lead.priority_override``
    - 1.4 findings: ``finding.file``, ``finding.sign_off``,
                    ``finding.rescind``, ``finding.enrich``,
                    ``finding.enrichment_review``
    - 3.2 anomaly:  ``anomaly.emit``, ``anomaly.promote``,
                    ``anomaly.dismiss``
    - 2.4 style:    ``style.score`` (only when ``install_style=True`` —
                    needs a populated WeaponStyle which most engagements
                    don't have until reforge)

    Returns a flat ``{channel_name: Channel}`` dict combining all
    subsystems. Each channel is also auto-registered in the global
    registry so :func:`ara.runtime.channels.registry.get` works.
    """
    sessions_root = Path(sessions_root)
    chans: dict[str, Channel] = {}
    chans.update(install_evidence_channels(sessions_root=sessions_root))
    chans.update(install_fact_channels(sessions_root=sessions_root))
    chans.update(install_lead_channels(sessions_root=sessions_root))
    chans.update(install_finding_channels(
        sessions_root=sessions_root,
        weapon_dir=weapon_dir,
        in_scope_assets=in_scope_assets,
    ))
    chans.update(install_anomaly_channels(
        sessions_root=sessions_root,
        cap_per_crumb_per_activation=anomaly_cap_per_crumb_per_activation,
    ))
    chans["report.render"] = install_report_channel(sessions_root=sessions_root)

    # v0.1 §8 — crumb-intent gate. Wraps the per-engagement fact store
    # in the predicate-counting adapter and gives the channel a handle
    # to the engagement's CrumbTracker so `fire` decisions activate
    # mechanically. Best-effort: missing engagement_id (e.g. tests that
    # don't bootstrap a session) means we register the channel without
    # a fact store; it returns "fire" for everything (degrades safely).
    if engagement_id:
        try:
            from ara.runtime.chunks.crumb_intent import (
                CrumbIntentChannel as _CIC,
                FactStorePredicateAdapter as _FSA,
            )
            from ara.runtime.facts.store import FactStore as _FStore
            from ara.runtime.anomaly import crumb_tracker as _ct
            session_dir = sessions_root / engagement_id
            fstore = _FStore(session_dir)
            tracker = _ct.for_session(session_dir)
            ci = _CIC(
                fact_store=_FSA(fstore),
                state_root=sessions_root.parent,
                crumb_tracker=tracker,
            )
            from ara.runtime.channels import registry as _registry
            _registry.register(ci)
            chans[ci.name] = ci
        except Exception:
            # Channel install is best-effort; existing engagements
            # bootstrap without crumb.intent fall through cleanly.
            pass
    if install_style:
        from ara.runtime.style.channels import install_style_channel
        ch = install_style_channel(
            weapon_dir=weapon_dir,
            weapon_style=weapon_style,
        )
        chans[ch.name] = ch
    if install_anomaly_autofirer:
        if engagement_id is None:
            raise ValueError(
                "install_anomaly_autofirer requires engagement_id"
            )
        _install_autofirer(
            sessions_root=sessions_root,
            engagement_id=engagement_id,
            weapon_dir=weapon_dir,
        )
    return chans


def _install_autofirer(
    *,
    sessions_root: Path,
    engagement_id: str,
    weapon_dir: Path | None,
) -> AnomalyAutoFirer:
    session_dir = Path(sessions_root) / engagement_id
    tracker = for_session(session_dir)
    shapes = (
        load_from_weapon(weapon_dir) if weapon_dir is not None
        else empty_weapon_shapes()
    )
    firer = AnomalyAutoFirer(
        sessions_root=Path(sessions_root),
        engagement_id=engagement_id,
        crumb_tracker=tracker,
        weapon_shapes=shapes,
    )
    register_autofire_hook(session_dir, firer.on_evidence)
    return firer


def install_engagement(
    *,
    sessions_root: Path,
    engagement_id: str,
    weapon_dir: Path | None = None,
    in_scope_assets: set[str] | None = None,
    install_style: bool = False,
    install_anomaly_autofirer: bool = True,
    install_chunk_runtime: bool = True,
    auto_initialize_chunks: bool = True,
) -> EngagementWiring:
    """Full per-engagement wiring: channels + crumb tracker +
    auto-firer + weapon shapes + chunk runtime. Returns the bundle so
    callers can drive the tracker
    (``wiring.crumb_tracker.set_active(crumb_id)``), emit chunk-swap
    tags (``wiring.chunk_runtime.emit_tag(...)``), and inspect
    auto-firer stats.
    """
    chans = install_engagement_channels(
        sessions_root=sessions_root,
        weapon_dir=weapon_dir,
        in_scope_assets=in_scope_assets,
        install_style=install_style,
    )
    session_dir = Path(sessions_root) / engagement_id
    session_dir.mkdir(parents=True, exist_ok=True)
    tracker = for_session(session_dir)
    shapes = (
        load_from_weapon(weapon_dir) if weapon_dir is not None
        else empty_weapon_shapes()
    )
    firer: AnomalyAutoFirer | None = None
    if install_anomaly_autofirer:
        firer = AnomalyAutoFirer(
            sessions_root=Path(sessions_root),
            engagement_id=engagement_id,
            crumb_tracker=tracker,
            weapon_shapes=shapes,
        )
        register_autofire_hook(session_dir, firer.on_evidence)

    activation_manager = ActivationManager(session_dir)
    chunk_runtime: ChunkRuntime | None = None
    if install_chunk_runtime and weapon_dir is not None:
        graph = WeaponChunkGraph.load(weapon_dir)
        if graph.chunks:
            chunk_runtime = ChunkRuntime(
                graph=graph,
                activation_manager=activation_manager,
                crumb_tracker=tracker,
                history_path=session_dir / "chunk_history.jsonl",
            )
            chunks_chans = install_chunks_channels(runtime=chunk_runtime)
            chans.update(chunks_chans)
            if auto_initialize_chunks:
                chunk_runtime.initialize()

    return EngagementWiring(
        channels=chans,
        crumb_tracker=tracker,
        autofirer=firer,
        weapon_shapes=shapes,
        chunk_runtime=chunk_runtime,
        activation_manager=activation_manager,
    )


def engagement_context_factory(
    *,
    engagement_id: str,
    activation_manager: ActivationManager,
    audit_sink: Path,
    forge_id: str | None = None,
) -> Callable[..., ChannelContext]:
    """Returns ``make_ctx(caller="agent" | "operator" | "system")``.

    Each invocation pulls the *current* activation_id from the manager,
    so chunk-swap mints are picked up automatically.
    """

    def make_ctx(caller: str = "agent") -> ChannelContext:
        return ChannelContext(
            engagement_id=engagement_id,
            activation_id=activation_manager.current(),
            forge_id=forge_id,
            caller=caller,
            audit_sink=audit_sink,
        )

    return make_ctx
