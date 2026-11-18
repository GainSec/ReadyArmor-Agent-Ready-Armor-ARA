"""`ara launch ...` — spawn a worker under ARA supervision.

The worker command is passed through; its env is the host env plus the
guard loader credentials plus any ``extra_env`` an adapter provides.
"""

from __future__ import annotations

import asyncio
import os
import resource
import signal
import sys
from pathlib import Path
from typing import Optional

from . import armors as armors_mod
from . import containment as _containment
from . import debug_log
from .mitm_proxy import start_proxy as start_mitm_proxy, ca_cert_path as mitm_ca_path
from .session import default_state_root
from .supervisor import Supervisor
from . import seccomp_notif
from .walls import Walls, WallsValidationError, parse_file, scrub_env, validate_file


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD_MJS = REPO_ROOT / "src" / "guard" / "index.mjs"
DEFAULT_WALLS = REPO_ROOT / "WALLS.md"
LIBC_SHIM = REPO_ROOT / "build" / "libara_shim.so"
ARA_SRC_DIR = REPO_ROOT / "src"
PYBOOTSTRAP_DIR = ARA_SRC_DIR / "ara" / "clients" / "_pybootstrap"


def _undici_module_path(env: dict[str, str]) -> str:
    """Resolve the optional Node runtime dependency from the operator environment."""
    configured = env.get("ARA_UNDICI_MODULE_PATH")
    default = Path.home() / ".npm-global/lib/node_modules/openclaw/node_modules/undici"
    return str(Path(configured or default).expanduser())


def _libc_shim_status(
    path: Path = LIBC_SHIM,
    *,
    platform_name: str | None = None,
) -> tuple[bool, str]:
    """Return whether the libc shim can be loaded on this platform."""
    platform_name = platform_name or sys.platform
    if platform_name.startswith("linux"):
        if not path.is_file():
            return False, f"libc shim missing: {path}"
        try:
            magic = path.read_bytes()[:4]
        except OSError as exc:
            return False, f"libc shim unreadable: {exc}"
        if magic != b"\x7fELF":
            return False, f"libc shim is not a Linux ELF shared object: {path}"
    return True, "ok"


def _raise_nofile_limit(*, min_soft: int = 8192) -> None:
    """Best-effort lift for long task sessions with many guard IPC clients."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(max(int(soft), int(min_soft)), int(hard))
        if target > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:
        pass


async def _tee_stream(src, passthrough, logfile) -> None:
    """Read from ``src`` (asyncio stream), write to both the operator's
    terminal and the session-scoped log file. Unbuffered so operators see
    progress live."""
    if src is None:
        return
    while True:
        chunk = await src.read(4096)
        if not chunk:
            break
        try:
            passthrough.write(chunk)
            passthrough.flush()
        except Exception:
            pass
        try:
            if logfile is not None:
                logfile.write(chunk)
        except Exception:
            pass


async def launch(
    command: list[str],
    walls_file: Optional[Path] = None,
    state_root: Optional[Path] = None,
    extra_env: Optional[dict[str, str]] = None,
    *,
    contain: bool = False,
    contain_profile: str = "default",
    instrumentation: bool = False,
    tee_logs: bool = False,
) -> int:
    if not command:
        raise SystemExit("ara launch: need a command to run")

    _raise_nofile_limit()

    # If no explicit WALLS was given and an armor is equipped, use its walls.
    effective_state_root = state_root or default_state_root()
    debug_log.emit(
        "launch", "start",
        state_root=effective_state_root,
        command=command,
        walls_file=str(walls_file) if walls_file else None,
        contain=contain,
        contain_profile=contain_profile,
        instrumentation=instrumentation,
        tee_logs=tee_logs,
    )
    if walls_file is None:
        active_walls = armors_mod.active_walls_file(effective_state_root)
        if active_walls is not None:
            walls_file = active_walls
            active_name = armors_mod.active_armor_name(effective_state_root)
            print(
                f"[ara] using equipped armor: {active_name} ({walls_file})",
                file=sys.stderr,
            )

    if walls_file:
        try:
            issues = validate_file(walls_file)
        except WallsValidationError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        for w in issues:
            print(f"ara launch: warning: {w}", file=sys.stderr)
    walls = parse_file(walls_file) if walls_file else Walls(path_rules=())
    # Optionally resume (adopt) an existing session — e.g. an operator "Polish"
    # task that should attach to a prior engagement's findings/leads/facts and
    # write updates back into it. Only honored if the session dir exists.
    _resume_id = (os.environ.get("ARA_RESUME_SESSION_ID") or "").strip() or None
    if _resume_id is not None:
        if not (effective_state_root / "sessions" / _resume_id).is_dir():
            print(
                f"[ara] resume session {_resume_id!r} not found; starting fresh",
                file=sys.stderr,
            )
            _resume_id = None
    sup = await Supervisor.start(
        state_root=state_root, walls=walls, walls_file=walls_file,
        resume_id=_resume_id,
    )
    try:
        debug_log.emit(
            "launch", "supervisor.started",
            state_root=effective_state_root,
            session_id=sup.session.id,
            session_root=str(sup.session.root),
            socket=str(sup.session.socket_path),
            walls_file=str(walls_file) if walls_file else None,
        )
        print(
            f"[ara] session={sup.session.id} socket={sup.session.socket_path}",
            file=sys.stderr,
        )
        # --- MITM HTTPS proxy (session-scoped) ---
        # Start the proxy BEFORE injecting env so we can point the worker at
        # the right port + CA file. Kept local to this session so each launch
        # gets fresh leaf certs and its own transcript.
        #
        # Escape hatch: `ARA_SKIP_MITM=1` skips starting the proxy and does
        # not inject HTTPS_PROXY env vars. Useful for workers whose plugins
        # make HTTPS calls during init that don't tolerate a transparent
        # proxy (e.g. the openclaw gateway's plugin bootstrap). Outbound
        # request auditing is then limited to Node-level hooks + the shim's
        # net.connect events; TLS body inspection is off for this session.
        skip_mitm = os.environ.get("ARA_SKIP_MITM", "").strip() not in ("", "0")
        mitm_server = None
        mitm_port = None
        if skip_mitm:
            print("[ara] mitm proxy: SKIPPED (ARA_SKIP_MITM=1)", file=sys.stderr)
            debug_log.emit(
                "launch", "mitm.skipped",
                state_root=effective_state_root,
                session_id=sup.session.id,
            )
        else:
            mitm_server, mitm_port = await start_mitm_proxy(
                effective_state_root, sup.session.root,
            )
            ca_crt = mitm_ca_path(effective_state_root)
            debug_log.emit(
                "launch", "mitm.started",
                state_root=effective_state_root,
                session_id=sup.session.id,
                port=mitm_port,
                ca_cert=str(ca_crt),
            )
            print(
                f"[ara] mitm proxy: http://127.0.0.1:{mitm_port}  "
                f"ca: {ca_crt}",
                file=sys.stderr,
            )
        env = {
            **os.environ,
            **sup.guard_env(GUARD_MJS),
            **(extra_env or {}),
        }
        if not skip_mitm:
            ca_crt = mitm_ca_path(effective_state_root)
            env.update({
                # Route every HTTPS-capable HTTP client through our proxy.
                "HTTPS_PROXY": f"http://127.0.0.1:{mitm_port}",
                "HTTP_PROXY": f"http://127.0.0.1:{mitm_port}",
                "https_proxy": f"http://127.0.0.1:{mitm_port}",
                "http_proxy": f"http://127.0.0.1:{mitm_port}",
                "NO_PROXY": "localhost,127.0.0.1,169.254.169.254",
                "no_proxy": "localhost,127.0.0.1,169.254.169.254",
                # Teach Node + Python + curl to trust our CA.
                "NODE_EXTRA_CA_CERTS": str(ca_crt),
                "SSL_CERT_FILE": str(ca_crt),
                "REQUESTS_CA_BUNDLE": str(ca_crt),
                "CURL_CA_BUNDLE": str(ca_crt),
                # OpenClaw-specific: their bundled `fetch-guard` SSRF
                # layer defaults to STRICT mode (ignores HTTPS_PROXY)
                # for most provider HTTP clients. These vars opt
                # OpenClaw into TRUSTED_EXPLICIT_PROXY mode and route
                # all its LLM traffic through our MITM, which is what
                # makes the v0.2 pause-and-ask flow fire end-to-end
                # against agent runtimes whose HTTP stack would
                # otherwise bypass the proxy. Discovered 2026-04-27
                # debugging a contained gateway; see openclaw
                # `runtime.applyDebugProxyEnv`.
                "OPENCLAW_DEBUG_PROXY_ENABLED": "1",
                "OPENCLAW_DEBUG_PROXY_URL": f"http://127.0.0.1:{mitm_port}",
                "OPENCLAW_DEBUG_PROXY_REQUIRE": "1",
            })
            # Force every undici fetch in the worker process (including
            # bundled deps like @anthropic-ai/sdk inside @mariozechner/pi-ai
            # used by openclaw's litellm provider) to honor HTTPS_PROXY
            # by installing a global EnvHttpProxyAgent dispatcher at
            # process start. Without this, undici's default fetch
            # silently bypasses proxy env vars and the agent's LLM
            # turns never reach our MITM. See
            # ``src/guard/undici_proxy_preload.mjs``.
            preload = REPO_ROOT / "src" / "guard" / "undici_proxy_preload.mjs"
            undici_pkg = _undici_module_path(env)
            existing_node_opts = env.get("NODE_OPTIONS", "")
            preload_arg = f"--import=file://{preload}"
            env["NODE_OPTIONS"] = (
                f"{existing_node_opts} {preload_arg}".strip()
            )
            env["ARA_UNDICI_MODULE_PATH"] = undici_pkg
            # Force libuv off io_uring back to the epoll+libc-syscall
            # path. Node 22 / libuv 1.49+ submits TCP connect/send/recv
            # via io_uring opcodes when the kernel supports them, which
            # bypasses libc — so libara_shim's connect() hook never
            # sees the syscall, the MITM proxy is never reached, and
            # `connect()` never appears in strace either. With
            # UV_USE_IO_URING=0 libuv falls back to legacy syscalls,
            # which the shim and any HTTPS_PROXY hooks can observe.
            # Confirmed by stracing the agent worker: io_uring_setup +
            # io_uring_enter calls but zero AF_INET connect() syscalls.
            env["UV_USE_IO_URING"] = "0"
        else:
            # Strip any parent-shell proxy vars so the worker goes direct.
            for k in ("HTTPS_PROXY","HTTP_PROXY","https_proxy","http_proxy",
                      "NO_PROXY","no_proxy"):
                env.pop(k, None)
        # --- Containment overlay (opt-in via --contain) -----------------
        # Redirects HOME, TMPDIR, XDG_*, and OpenClaw/codex state into the
        # session root so every side-effect is bounded and inspectable.
        # Kept after the default env merge so containment wins on conflict.
        if contain:
            shim_ok, shim_reason = _libc_shim_status()
            if not shim_ok:
                print(f"ara launch: containment unavailable: {shim_reason}", file=sys.stderr)
                return 2
            contain_env = _containment.build_contained_env(
                sup.session.root, profile=contain_profile,
            )
            env.update(contain_env)
            manifest = {
                "session": sup.session.id,
                "profile": contain_profile,
                "root": contain_env["ARA_CONTAIN_ROOT"],
                "home": contain_env["HOME"],
                "bootstrapped": sorted(
                    str(p) for p in Path(contain_env["HOME"]).rglob("*") if p.is_file()
                ),
            }
            (sup.session.root / "contain.manifest.json").write_text(
                __import__("json").dumps(manifest, indent=2, sort_keys=True),
            )
            print(
                f"[ara] contain: HOME={contain_env['HOME']}",
                file=sys.stderr,
            )
            debug_log.emit(
                "launch", "containment.enabled",
                state_root=effective_state_root,
                session_id=sup.session.id,
                contain_profile=contain_profile,
                manifest=manifest,
            )
        # Shim-level redirect targets — defense-in-depth on top of the
        # contained openclaw.json rewrite. The LD_PRELOAD libc shim and
        # the Node net hook consult these and rewrite sockaddr/connect
        # args before the real syscall, catching bypass attempts and
        # non-contained worker paths.
        _tap_port_file = effective_state_root / "gateway-ws-tap-port"
        if _tap_port_file.is_file():
            try:
                _tap_port = _tap_port_file.read_text().strip()
                if _tap_port:
                    env["ARA_GATEWAY_TAP_PORT"] = _tap_port
                    from . import gateway_check as _gwc
                    env.setdefault(
                        "ARA_GATEWAY_REAL_PORT",
                        str(_gwc.discover_gateway_port()),
                    )
            except OSError:
                pass
        _tap_sock = effective_state_root / "exec-approvals-tap.sock"
        if _tap_sock.exists():
            env["ARA_EXEC_APPROVALS_TAP_PATH"] = str(_tap_sock)
            # REAL_PATH must equal whatever path openclaw will actually
            # connect() to — its `exec-approvals.json` carries an
            # absolute path baked at install time, which points at the
            # *uncontained* home (`~/.clawdbot/exec-approvals.sock`).
            # Containment doesn't rewrite that file (`_wire_exec_approvals_redirect`
            # was deliberately removed — see containment.py header), so
            # using the contained HOME here would make the shim's
            # strncmp() rewrite miss and the agent hang on first exec
            # tool call. Read the path from the on-disk config when
            # available so any operator-customized location is honored;
            # fall back to the standard ~/.clawdbot/exec-approvals.sock.
            _real_path = None
            _oc_cfg = Path(os.path.expanduser("~")) / ".openclaw" / "exec-approvals.json"
            if _oc_cfg.is_file():
                try:
                    import json as _json
                    _cfg = _json.loads(_oc_cfg.read_text())
                    _p = ((_cfg.get("socket") or {}).get("path") or "").strip()
                    if _p:
                        _real_path = _p
                except (OSError, ValueError):
                    pass
            if not _real_path:
                _real_path = str(
                    Path(os.path.expanduser("~")) / ".clawdbot" / "exec-approvals.sock"
                )
            env.setdefault("ARA_EXEC_APPROVALS_REAL_PATH", _real_path)
        if instrumentation:
            for k, v in _containment.instrumentation_env().items():
                if k == "NODE_OPTIONS_APPEND_SOURCEMAPS":
                    # Append-only so the guard --import stays intact.
                    existing = env.get("NODE_OPTIONS", "")
                    if "--enable-source-maps" not in existing:
                        env["NODE_OPTIONS"] = (
                            f"{existing} --enable-source-maps".strip()
                        )
                    continue
                env[k] = v
        # Strip env vars the active WALLS forbids. This runs BEFORE spawn so
        # the worker's process table never contains them — Node's process.env
        # is populated at startup, so any post-start getenv hook would be
        # too late. Descendants inherit only the scrubbed env.
        env, stripped = scrub_env(env, walls)
        for k, rule_id in stripped:
            print(f"[ara] env scrubbed: {k} ({rule_id})", file=sys.stderr)
        if stripped:
            debug_log.emit(
                "launch", "env.scrubbed",
                state_root=effective_state_root,
                session_id=sup.session.id,
                stripped=[{"key": key, "rule_id": rule_id} for key, rule_id in stripped],
            )
        if LIBC_SHIM.exists():
            shim_path = str(LIBC_SHIM)
            if sys.platform == "darwin":
                # macOS uses DYLD_INSERT_LIBRARIES and requires flat-namespace
                # resolution for classic LD_PRELOAD-style symbol override
                # (default two-level namespace routes libc calls back to
                # libSystem, bypassing our wrappers).
                existing = env.get("DYLD_INSERT_LIBRARIES", "").strip()
                if shim_path not in existing.split(":"):
                    env["DYLD_INSERT_LIBRARIES"] = (
                        f"{shim_path}:{existing}" if existing else shim_path
                    )
                env.setdefault("DYLD_FORCE_FLAT_NAMESPACE", "1")
            else:
                existing = env.get("LD_PRELOAD", "").strip()
                if shim_path not in existing.split():
                    env["LD_PRELOAD"] = (
                        f"{shim_path} {existing}" if existing else shim_path
                    )
        # Tier-A auto-patch: prepend the bootstrap dir (so Python's site
        # picks up our sitecustomize.py) and the ara source root (so it
        # can import ara.clients.transcript). Both come BEFORE any
        # caller-supplied PYTHONPATH so we always win the import race.
        existing_pp = env.get("PYTHONPATH", "")
        injected_pp = os.pathsep.join([
            str(PYBOOTSTRAP_DIR), str(ARA_SRC_DIR)
        ])
        env["PYTHONPATH"] = (
            f"{injected_pp}{os.pathsep}{existing_pp}" if existing_pp else injected_pp
        )
        stdout_log_path = sup.session.root / "worker.stdout.log"
        stderr_log_path = sup.session.root / "worker.stderr.log"
        stdout_f = open(stdout_log_path, "ab", buffering=0) if tee_logs else None
        stderr_f = open(stderr_log_path, "ab", buffering=0) if tee_logs else None
        try:
            debug_log.emit(
                "launch", "worker.spawn",
                state_root=effective_state_root,
                session_id=sup.session.id,
                command=command,
                contain=contain,
                mitm_port=mitm_port,
                stdout_log=str(stdout_log_path) if tee_logs else None,
                stderr_log=str(stderr_log_path) if tee_logs else None,
                env_summary={
                    "ARA_SOCKET": env.get("ARA_SOCKET"),
                    "ARA_SESSION_ID": env.get("ARA_SESSION_ID"),
                    "ARA_TASK_RUN_ID": env.get("ARA_TASK_RUN_ID"),
                    "ARA_TASK_SUPERVISOR": env.get("ARA_TASK_SUPERVISOR"),
                    "HOME": env.get("HOME"),
                    "HERMES_HOME": env.get("HERMES_HOME"),
                    "LD_PRELOAD": env.get("LD_PRELOAD"),
                    "DYLD_INSERT_LIBRARIES": env.get("DYLD_INSERT_LIBRARIES"),
                    "HTTPS_PROXY": env.get("HTTPS_PROXY"),
                    "NODE_OPTIONS": env.get("NODE_OPTIONS"),
                },
            )
            # ── seccomp-notif egress enforcement: install the kernel filter in
            # the agent child (preexec) and hand the notify-fd to the in-process
            # guard. Gated + feature-detected; any failure silently falls back
            # to the (still-active) shim layer, so it can never block a launch.
            import socket as _socket
            _seccomp_gp = None
            _seccomp_child_fd = -1
            _seccomp_preexec = None  # set below when enforcement is on
            _seccomp_on = (
                contain
                and str(os.environ.get("ARA_SECCOMP_NET_ENFORCE") or "").strip()
                    .lower() in ("1", "true", "on", "yes")
                and seccomp_notif.supported()
            )
            if _seccomp_on:
                _seccomp_gp, _cp = _socket.socketpair(
                    _socket.AF_UNIX, _socket.SOCK_DGRAM)
                _seccomp_child_fd = _cp.detach()

                def _seccomp_preexec():  # runs in the forked agent child
                    try:
                        seccomp_notif.install_and_handoff(_seccomp_child_fd)
                    except BaseException:  # noqa: BLE001
                        pass  # fall back to shim-only; never block the exec
            _seccomp_passfds = (
                (_seccomp_child_fd,) if _seccomp_on else ())

            if tee_logs:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    env=env,
                    stdin=sys.stdin,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=_seccomp_preexec,
                    pass_fds=_seccomp_passfds,
                )
                tee_tasks = [
                    asyncio.create_task(_tee_stream(proc.stdout, sys.stdout.buffer, stdout_f)),
                    asyncio.create_task(_tee_stream(proc.stderr, sys.stderr.buffer, stderr_f)),
                ]
            else:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    env=env,
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    preexec_fn=_seccomp_preexec,
                    pass_fds=_seccomp_passfds,
                )
                tee_tasks = []

            # Receive the notify-fd from the child and attach the guard loop.
            if _seccomp_gp is not None:
                try:
                    os.close(_seccomp_child_fd)  # close the parent's copy
                except OSError:
                    pass
                try:
                    _seccomp_gp.settimeout(5.0)
                    _m, _fds, _fl, _ad = _socket.recv_fds(_seccomp_gp, 16, 1)
                    if _fds:
                        sup.attach_seccomp_notif(_fds[0])
                    else:
                        debug_log.emit(
                            "seccomp-egress", "no_fd_received",
                            state_root=effective_state_root,
                            session_id=sup.session.id)
                except Exception as _e:  # noqa: BLE001
                    debug_log.emit(
                        "seccomp-egress", "attach_failed",
                        state_root=effective_state_root,
                        session_id=sup.session.id, error=repr(_e))
                finally:
                    try:
                        _seccomp_gp.close()
                    except OSError:
                        pass

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: proc.send_signal(s),
                )

            rc = await proc.wait()
            if tee_tasks:
                await asyncio.gather(*tee_tasks, return_exceptions=True)
            debug_log.emit(
                "launch", "worker.exited",
                state_root=effective_state_root,
                session_id=sup.session.id,
                pid=proc.pid,
                exit_code=rc,
            )
            return rc or 0
        finally:
            if stdout_f is not None:
                stdout_f.close()
            if stderr_f is not None:
                stderr_f.close()
    finally:
        if mitm_server is not None:
            try:
                mitm_server.close()
                await mitm_server.wait_closed()
            except Exception:
                pass
        await sup.stop()


def launch_cli(
    command: list[str],
    walls_file: Optional[Path] = None,
    extra_env: Optional[dict[str, str]] = None,
    state_root: Optional[Path] = None,
    *,
    contain: bool = False,
    contain_profile: str = "default",
    instrumentation: bool = False,
    tee_logs: bool = False,
) -> int:
    return asyncio.run(
        launch(
            command,
            walls_file=walls_file,
            extra_env=extra_env,
            state_root=state_root,
            contain=contain,
            contain_profile=contain_profile,
            instrumentation=instrumentation,
            tee_logs=tee_logs,
        )
    )
