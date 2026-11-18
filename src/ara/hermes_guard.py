"""ARA-owned contained hermes gateway lifecycle: rogue detection,
circuit-breaker reconciliation, canonical-unit re-assertion, and a
lifecycle-faithful restart. Side effects are injected for testability.

OpenClaw is not used; this module is fully self-contained and carries its
own tiny process-ancestry helpers.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

STRIKE_WINDOW_S = 180.0
STRIKE_LIMIT = 2
_STATE_FILE = "hermes_guard.json"


def _parent_pid(pid: int):
    """PPid of ``pid`` via /proc, or None."""
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        return None
    try:
        for line in status.read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def is_descendant_of(pid: int, ancestor_pids: set) -> bool:
    """True if ``pid``'s ancestry includes any PID in ``ancestor_pids``."""
    if not ancestor_pids:
        return False
    current, seen = pid, set()
    for _ in range(64):  # safety bound
        if current in ancestor_pids:
            return True
        if current in seen or current <= 1:
            return False
        seen.add(current)
        ppid = _parent_pid(current)
        if ppid is None or ppid == current:
            return False
        current = ppid
    return False


# A rogue hermes runs its gateway loop; the contained one does too but as a
# descendant of the ARA launch wrapper.
_HERMES_RUN_MARKERS = ("hermes gateway run", "hermes_cli.main gateway run")
_LAUNCHER_MARKERS = ("ara.cli launch", "contain-profile hermes-gateway")


def list_procs() -> list:
    """Return (pid, cmdline) for every process, via /proc. Empty if no /proc."""
    out: list = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "cmdline").read_bytes()
        except OSError:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if cmd:
            out.append((int(d.name), cmd))
    return out


def _is_launcher(cmd: str) -> bool:
    return all(m in cmd for m in _LAUNCHER_MARKERS)


def contained_launcher_pids(list_procs=list_procs) -> set:
    """PIDs of the ARA launch wrappers that contain the hermes gateway."""
    return {pid for pid, cmd in list_procs() if _is_launcher(cmd)}


def find_uncontained_hermes(list_procs=list_procs) -> list:
    """PIDs of hermes gateway processes NOT descended from an ARA launcher."""
    launchers = contained_launcher_pids(list_procs=list_procs)
    rogue: list = []
    for pid, cmd in list_procs():
        if _is_launcher(cmd):
            continue  # the wrapper itself is not a rogue gateway
        if not any(m in cmd for m in _HERMES_RUN_MARKERS):
            continue
        if pid in launchers or is_descendant_of(pid, launchers):
            continue
        rogue.append(pid)
    return rogue


# ---- guard state (circuit breaker) ----------------------------------

@dataclass
class GuardState:
    strikes: list = field(default_factory=list)      # unix timestamps of kills
    held_off: bool = False
    held_off_since: "float | None" = None
    last_detail: str = ""


def load_state(state_root: Path) -> "GuardState":
    p = Path(state_root) / _STATE_FILE
    if not p.exists():
        return GuardState()
    try:
        d = json.loads(p.read_text())
        return GuardState(
            strikes=list(d.get("strikes", [])),
            held_off=bool(d.get("held_off", False)),
            held_off_since=d.get("held_off_since"),
            last_detail=str(d.get("last_detail", "")),
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return GuardState()


def save_state(state_root: Path, st: "GuardState") -> None:
    p = Path(state_root) / _STATE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(st)))


def recent_strikes(st: "GuardState", now: float) -> int:
    return sum(1 for t in st.strikes if now - t <= STRIKE_WINDOW_S)


# ---- canonical unit template + re-assertion -------------------------

_TMPL_PATH = Path(__file__).parent / "deploy" / "hermes-gateway.service.tmpl"


def render_unit(*, home: str, state_root: str, hermes_bin: str,
                python_bin: str = "/usr/bin/python3.9", pythonpath: str,
                path_env: str) -> str:
    """Render the canonical CONTAINED unit from the repo template."""
    tmpl = _TMPL_PATH.read_text()
    return tmpl.format(home=home, state_root=state_root, hermes_bin=hermes_bin,
                       python_bin=python_bin, pythonpath=pythonpath,
                       path_env=path_env)


def ensure_contained_unit(unit_path: Path, rendered: str, reload=None) -> bool:
    """Write ``rendered`` to ``unit_path`` iff it differs (drift). Returns
    True when it wrote (and called ``reload``), False when already correct."""
    unit_path = Path(unit_path)
    current = unit_path.read_text() if unit_path.exists() else ""
    if current == rendered:
        return False
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(rendered)
    if reload is not None:
        reload()
    return True


# ---- lifecycle-faithful restart -------------------------------------

def write_restart_marker(hermes_home: Path, now: float = 0.0) -> None:
    """Drop hermes's own ``.restart_notify.json`` marker so hermes posts its
    Discord "restarted" ping on startup. Same file hermes itself reads."""
    hermes_home = Path(hermes_home)
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / ".restart_notify.json").write_text(json.dumps({
        "reason": "ara-restart",
        "via_service": True,
        "requested_at": now,
    }))


def restart_contained(hermes_home: Path, systemctl, now: float = 0.0) -> None:
    """Marker-then-restart. ``systemctl('restart')`` sends SIGTERM (honoring
    TimeoutStopSec) so hermes's graceful shutdown notify fires too."""
    write_restart_marker(hermes_home, now=now)
    systemctl("restart")


# ---- reconcile: the circuit-breaker state machine -------------------

@dataclass
class ReconcileResult:
    action: str            # "none" | "remediated" | "held_off"
    rogue_pids: list
    held_off: bool
    detail: str


def reconcile(state_root, now, *, list_procs=list_procs, kill, relaunch,
              alert) -> "ReconcileResult":
    """Detect rogue hermes; force-kill and either relaunch contained or, on a
    2nd strike within STRIKE_WINDOW_S, hold hermes off and alert.

    ``kill(pid)`` force-kills; ``relaunch()`` re-asserts unit + restarts
    contained; ``alert(detail)`` raises the operator alert.
    """
    state_root = Path(state_root)
    rogue = find_uncontained_hermes(list_procs=list_procs)
    st = load_state(state_root)
    if not rogue:
        return ReconcileResult("none", [], st.held_off, "contained")

    for pid in rogue:
        kill(pid)                       # force-kill; uncontained is a bad sign

    # prune expired strikes, then record this one
    st.strikes = [t for t in st.strikes if now - t <= STRIKE_WINDOW_S]
    st.strikes.append(now)

    if st.held_off or recent_strikes(st, now) >= STRIKE_LIMIT:
        if not st.held_off:
            st.held_off = True
            st.held_off_since = now
        st.last_detail = f"rogue hermes {rogue} — held off (strikes={len(st.strikes)})"
        save_state(state_root, st)
        alert(st.last_detail)
        return ReconcileResult("held_off", rogue, True, st.last_detail)

    st.last_detail = f"rogue hermes {rogue} — force-killed, relaunched contained"
    save_state(state_root, st)
    relaunch()
    return ReconcileResult("remediated", rogue, False, st.last_detail)


# ---- real side-effect wiring + entrypoints --------------------------

_UNIT_NAME = "hermes-gateway.service"

# The gateway needs hermes's full node/tooling PATH. This must NOT be derived
# from the guard process's own os.environ["PATH"] — the guard runs inside the
# armory, which may have a sanitized/thin PATH, and writing that into the unit
# would strip hermes's tooling on the next restart. Use a stable canonical PATH
# (override with ARA_HERMES_UNIT_PATH if a box's layout differs).
_USER_TOOL_PATHS = (
    ".local/bin", ".npm-global/bin", "bin", ".volta/bin", ".asdf/shims",
    ".bun/bin", ".nvm/current/bin", ".fnm/current/bin", ".local/share/pnpm",
)


def sig_kill(pid: int) -> None:
    """Force-kill (SIGKILL); swallow already-gone / permission errors."""
    import signal
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass


def make_systemctl(unit: str = _UNIT_NAME):
    import subprocess

    def _systemctl(action: str) -> None:
        subprocess.run(["systemctl", "--user", action, unit],
                       check=False, timeout=240)
    return _systemctl


def _daemon_reload() -> None:
    import subprocess
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=60)


def default_paths() -> dict:
    home = Path(os.environ.get("HOME") or Path.home()).expanduser()
    ara_root = os.environ.get("ARA_ROOT")
    source_root = Path(ara_root).expanduser() / "src" if ara_root else Path(__file__).resolve().parents[1]
    canonical_path = os.pathsep.join(
        [str(home / part) for part in _USER_TOOL_PATHS]
        + ["/usr/local/bin", "/usr/bin", "/bin"]
    )
    return {
        "home": str(home),
        "state_root": str(Path(os.environ.get("ARA_STATE_ROOT", str(home / "ARA/dashboard/.ara"))).expanduser()),
        "hermes_bin": str(Path(os.environ.get("HERMES_BIN", str(home / ".local/bin/hermes"))).expanduser()),
        "pythonpath": str(source_root),
        "path_env": os.environ.get("ARA_HERMES_UNIT_PATH", canonical_path),
        "unit_path": home / ".config/systemd/user" / _UNIT_NAME,
        "hermes_home": home / ".hermes",
    }


def bring_up_contained(now: float, *, paths=None, reload=None, systemctl=None) -> None:
    """ARA-start entry: clear any held-off, re-assert the contained unit, and
    restart contained (marker + graceful). An explicit bring-up is also the
    operator's held-off clear."""
    p = paths or default_paths()
    if reload is None:
        reload = _daemon_reload
    if systemctl is None:
        systemctl = make_systemctl()
    st = load_state(p["state_root"])
    if st.held_off:
        st.held_off = False
        st.held_off_since = None
        st.strikes = []
        save_state(p["state_root"], st)
    rendered = render_unit(home=p["home"], state_root=str(p["state_root"]),
                           hermes_bin=p["hermes_bin"], pythonpath=p["pythonpath"],
                           path_env=p["path_env"])
    ensure_contained_unit(p["unit_path"], rendered, reload=reload)
    restart_contained(p["hermes_home"], systemctl=systemctl, now=now)


def reassert_contained_unit(paths=None, reload=None) -> bool:
    """Re-write the systemd unit to the CONTAINED form if it has drifted (e.g.
    hermes rewrites it to its native/uncontained form on startup). Does NOT
    restart — the running process is unaffected; this only keeps the FILE
    contained so a reboot / systemctl-start comes up contained. Returns True
    if it corrected drift."""
    p = paths or default_paths()
    if reload is None:
        reload = _daemon_reload
    rendered = render_unit(home=p["home"], state_root=str(p["state_root"]),
                           hermes_bin=p["hermes_bin"], pythonpath=p["pythonpath"],
                           path_env=p["path_env"])
    return ensure_contained_unit(p["unit_path"], rendered, reload=reload)


def run_reconcile(now: float, *, paths=None) -> "ReconcileResult":
    """Production reconcile with real kill/relaunch/alert wired. Also re-asserts
    the contained unit file every cycle to fight hermes's native self-install."""
    p = paths or default_paths()

    try:
        reassert_contained_unit(p)
    except Exception:
        pass

    def _relaunch():
        bring_up_contained(now, paths=p)

    def _alert(detail: str):
        try:
            from .hermes_guard_alert import raise_alert
            raise_alert(p["state_root"], detail, now=now)
        except Exception:
            pass

    return reconcile(p["state_root"], now, kill=sig_kill,
                     relaunch=_relaunch, alert=_alert)
