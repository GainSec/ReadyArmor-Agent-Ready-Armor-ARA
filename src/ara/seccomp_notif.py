"""Seccomp user-notification egress enforcement — kernel-level, unbypassable.

This is the enforcement *muscle* for ARA's network policy: a seccomp-BPF filter
installed at the top of the contained agent's process tree traps every
``connect()`` (regardless of libc/static-binary/raw-syscall) and hands it to the
guard, which decides using the SAME bulwark policy the shim + MITM use. The
filter is inherited across ``execve`` and cannot be removed, so a static binary
or raw syscall the agent spawns is covered automatically.

Two sides:
  * ``install_and_handoff`` — runs in the forked agent child (``preexec_fn``):
    ``PR_SET_NO_NEW_PRIVS`` + install the filter, then send the notify-fd to the
    guard over an inherited socketpair.
  * ``NotifHandler`` — runs in the guard: recv a notification, read the
    destination from the target's memory (TOCTOU-guarded), and respond
    allow (``FLAG_CONTINUE``) or deny (``EACCES``).

The exact mechanism is proven on kernel 5.15 (see the PoC). Requires kernel
>= 5.5 for ``SECCOMP_USER_NOTIF_FLAG_CONTINUE`` (feature-detected).

x86-64 only for now (the BPF checks ``AUDIT_ARCH_X86_64`` and kills a mismatched
arch to prevent a 32-bit-ABI bypass); other arches fall back to shim-only.
"""
from __future__ import annotations

import ctypes
import errno
import os
import socket
import struct

# ── syscall / constant numbers (x86-64) ──────────────────────────────────
SYS_seccomp = 317
SYS_connect = 42
SYS_sendto = 44
SYS_io_uring_setup = 425
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_FILTER_FLAG_NEW_LISTENER = 8
SECCOMP_RET_USER_NOTIF = 0x7FC00000
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_KILL_THREAD = 0x00000000
AUDIT_ARCH_X86_64 = 0xC000003E
SECCOMP_USER_NOTIF_FLAG_CONTINUE = 1

# struct sizes → ioctl request numbers (signed for fcntl.ioctl)
_IOCTL_NOTIF_RECV = 0xC0502100      # _IOWR('!', 0, struct seccomp_notif[80])
_IOCTL_NOTIF_SEND = 0xC0182101      # _IOWR('!', 1, struct seccomp_notif_resp[24])
_IOCTL_NOTIF_ID_VALID = 0x40082102  # _IOW('!', 2, __u64[8])


def _signed(x: int) -> int:
    return x - 0x100000000 if x >= 0x80000000 else x


IOCTL_NOTIF_RECV = _signed(_IOCTL_NOTIF_RECV)
IOCTL_NOTIF_SEND = _signed(_IOCTL_NOTIF_SEND)
IOCTL_NOTIF_ID_VALID = _signed(_IOCTL_NOTIF_ID_VALID)

_NOTIF_SZ = 80   # struct seccomp_notif: id(8) pid(4) flags(4) data(64)
_DATA_OFF = 16   # seccomp_data starts here: nr(4) arch(4) ip(8) args[6](48)


def supported() -> bool:
    """True iff this platform can run the seccomp-notif enforcement (x86-64 +
    kernel >= 5.5 for FLAG_CONTINUE). Best-effort; the actual install is still
    guarded so a failure falls back to shim-only."""
    if os.uname().machine != "x86_64":
        return False
    try:
        rel = os.uname().release.split("-")[0]
        maj, minor = (int(x) for x in rel.split(".")[:2])
        return (maj, minor) >= (5, 5)
    except Exception:  # noqa: BLE001
        return False


# ── BPF program ──────────────────────────────────────────────────────────
def _build_filter() -> "ctypes.Array":
    """Comprehensive network-egress mediation:
      * connect        -> always trap (USER_NOTIF): all TCP + connected-UDP.
      * sendto         -> trap ONLY when the dest-addr arg is non-NULL
                          (connectionless UDP); connected sends (dest=NULL, the
                          high-frequency path) pass with zero overhead.
      * io_uring_setup -> deny (EPERM): closes the async-submission bypass.
      * everything else -> allow.
    Arch mismatch is killed (no 32-bit-ABI bypass). ``seccomp_data.args[4]``
    (sendto dest_addr) is a u64 at offset 48; checked as low@48 / high@52 since
    BPF is 32-bit. All jumps are forward (BPF has no backward jumps)."""
    NOTIF = SECCOMP_RET_USER_NOTIF
    ERRNO = SECCOMP_RET_ERRNO | (errno.EPERM & 0xFFFF)
    ALLOW = SECCOMP_RET_ALLOW
    KILL = SECCOMP_RET_KILL_THREAD
    insns = [
        (0x20, 0, 0, 4),                       # 0  LD arch
        (0x15, 0, 11, AUDIT_ARCH_X86_64),      # 1  arch==x86_64 ? next : KILL(13)
        (0x20, 0, 0, 0),                       # 2  LD nr
        (0x15, 6, 0, SYS_connect),             # 3  connect ? NOTIF(10) : next
        (0x15, 1, 0, SYS_sendto),              # 4  sendto  ? CHECK(6) : next
        (0x15, 5, 6, SYS_io_uring_setup),      # 5  io_uring_setup ? ERRNO(11) : ALLOW(12)
        (0x20, 0, 0, 48),                      # 6  LD args[4].lo (dest_addr low)
        (0x15, 0, 2, 0),                       # 7  lo==0 ? next(check hi) : NOTIF(10)
        (0x20, 0, 0, 52),                      # 8  LD args[4].hi
        (0x15, 2, 0, 0),                       # 9  hi==0 ? ALLOW(12) : NOTIF(10)
        (0x06, 0, 0, NOTIF),                   # 10
        (0x06, 0, 0, ERRNO),                   # 11
        (0x06, 0, 0, ALLOW),                   # 12
        (0x06, 0, 0, KILL),                    # 13
    ]
    raw = b"".join(struct.pack("HBBI", *i) for i in insns)
    return ctypes.create_string_buffer(raw, len(raw))


class _Fprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _libc() -> "ctypes.CDLL":
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.syscall.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_long,
                             ctypes.c_void_p]
    libc.prctl.restype = ctypes.c_int
    libc.prctl.argtypes = [ctypes.c_int] * 5
    return libc


def install_and_handoff(handoff_fd: int) -> None:
    """Run in the forked agent child (``preexec_fn``). Set NO_NEW_PRIVS,
    install the seccomp filter, and send the resulting notify-fd to the guard
    over ``handoff_fd`` (an inherited socketpair end). Raises on failure so the
    caller can fall back to shim-only — must be cheap + async-signal-safe-ish
    (only syscalls, no Python allocation storms)."""
    libc = _libc()
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    filt = _build_filter()
    n = len(filt.raw) // 8
    prog = _Fprog(n, ctypes.cast(filt, ctypes.c_void_p))
    nfd = libc.syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                       SECCOMP_FILTER_FLAG_NEW_LISTENER,
                       ctypes.addressof(prog))
    if nfd < 0:
        raise OSError(ctypes.get_errno(), "seccomp NEW_LISTENER failed")
    hs = socket.socket(fileno=handoff_fd)
    try:
        socket.send_fds(hs, [b"n"], [nfd])
    finally:
        hs.detach()
        os.close(int(nfd))


# ── guard side ───────────────────────────────────────────────────────────
class Notification:
    __slots__ = ("id", "pid", "nr", "args")

    def __init__(self, nid: int, pid: int, nr: int, args: tuple):
        self.id = nid
        self.pid = pid
        self.nr = nr
        self.args = args


class NotifHandler:
    """Guard-side helper around a seccomp notify-fd. All calls are blocking
    ioctls; the guard drives them off a thread/reader so its loop isn't
    stalled."""

    def __init__(self, notif_fd: int):
        self.fd = notif_fd

    def recv(self) -> "Notification | None":
        """Block for the next trapped syscall. Returns None on a benign
        interrupt (caller retries)."""
        import fcntl
        buf = bytearray(_NOTIF_SZ)
        try:
            fcntl.ioctl(self.fd, IOCTL_NOTIF_RECV, buf, True)
        except OSError as exc:
            if exc.errno in (errno.EINTR, errno.ENOENT):
                return None
            raise
        nid, pid, _flags = struct.unpack_from("QII", buf, 0)
        nr = struct.unpack_from("i", buf, _DATA_OFF)[0]
        args = struct.unpack_from("6Q", buf, _DATA_OFF + 16)
        return Notification(nid, pid, nr, args)

    def id_valid(self, nid: int) -> bool:
        import fcntl
        try:
            fcntl.ioctl(self.fd, IOCTL_NOTIF_ID_VALID, struct.pack("Q", nid))
            return True
        except OSError:
            return False

    def read_dest(self, pid: int, ptr: int, addrlen: int):
        """Read the connect() sockaddr from the target's memory. Returns
        ``(family, host, port)`` — host/port None for non-inet families
        (AF_UNIX etc.). Returns ``(None, None, None)`` on read failure."""
        try:
            with open(f"/proc/{pid}/mem", "rb", buffering=0) as m:
                m.seek(ptr)
                sa = m.read(min(max(addrlen, 2), 128))
        except OSError:
            return (None, None, None)
        if len(sa) < 2:
            return (None, None, None)
        fam = struct.unpack_from("H", sa, 0)[0]
        if fam == socket.AF_INET and len(sa) >= 8:
            port = struct.unpack_from("!H", sa, 2)[0]
            host = socket.inet_ntoa(sa[4:8])
            return (fam, host, port)
        if fam == socket.AF_INET6 and len(sa) >= 24:
            port = struct.unpack_from("!H", sa, 2)[0]
            host = socket.inet_ntop(socket.AF_INET6, sa[8:24])
            return (fam, host, port)
        return (fam, None, None)

    def _send(self, nid: int, val: int, err: int, flags: int) -> None:
        import fcntl
        resp = bytearray(struct.pack("QqiI", nid, val, err, flags))
        try:
            fcntl.ioctl(self.fd, IOCTL_NOTIF_SEND, resp, True)
        except OSError as exc:
            # target may have died / notif expired — nothing to do
            if exc.errno not in (errno.ENOENT,):
                raise

    def allow(self, nid: int) -> None:
        """Let the real syscall proceed unchanged."""
        self._send(nid, 0, 0, SECCOMP_USER_NOTIF_FLAG_CONTINUE)

    def deny(self, nid: int, err: int = errno.EACCES) -> None:
        """Fail the syscall with ``err`` (default EACCES) — no packet leaves."""
        self._send(nid, 0, -err, 0)


LOOPBACK_ALLOW_FAMILIES = frozenset({socket.AF_UNIX})

__all__ = [
    "supported", "install_and_handoff", "NotifHandler", "Notification",
    "SYS_connect", "SYS_sendto", "LOOPBACK_ALLOW_FAMILIES",
]
