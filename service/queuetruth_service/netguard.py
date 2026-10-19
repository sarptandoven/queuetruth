"""Zero-egress guard for job child processes (SPEC 8.8): patches `socket.socket.connect`, `connect_ex`, `sendto`,
`sendmsg` and `socket.create_connection` so that only the allow-listed `(host, port)` pairs (the database and the
store endpoint) can be reached. Unix-domain sockets (local IPC, e.g. the Postgres socket) are allowed. Any other
connection raises `EgressBlocked`. Host names in the allow list are resolved once at install time.

Limits (security review L2; this is an in-process tripwire, not a sandbox): it only sees calls that go through the
patched Python methods. Code that calls the C base class directly (`_socket.socket.connect(s, addr)`), native code
(the Rust core, C extensions, libc) and name resolution inside libc (`getaddrinfo`) are not intercepted, and nothing
here survives `exec` of another program. The enforced boundary is the OS: the compose stack attaches the worker (and
so every job child) only to an `internal` network that has no default route, so the only reachable peers are
Postgres and the object store, and job children hold no credentials for either (`executor.child_env`).
"""

from __future__ import annotations

import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import psycopg

_installed: dict[str, Any] = {}


class EgressBlocked(PermissionError):
    """A connection outside the allow list was attempted."""


def _resolve(host: str, port: int) -> set[tuple[str, int]]:
    out = {(host, port)}
    try:
        for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
            out.add((str(info[4][0]), port))
    except OSError:
        pass
    return out


def allowlist_from(dsn: str | None, store_endpoint: str | None) -> list[tuple[str, int]]:
    """`(host, port)` pairs of a Postgres DSN (TCP hosts only) and a store endpoint URL."""
    pairs: list[tuple[str, int]] = []
    if dsn:
        info = psycopg.conninfo.conninfo_to_dict(dsn)
        port = int(str(info.get("port") or 5432).split(",")[0])
        for host in str(info.get("host") or "").split(","):
            if host and not host.startswith("/"):
                pairs.append((host, port))
    if store_endpoint:
        u = urlsplit(store_endpoint)
        if u.hostname:
            pairs.append((u.hostname, u.port or (443 if u.scheme == "https" else 80)))
    return pairs


def install(allowed: Iterable[tuple[str, int]]) -> None:
    """Installs the guard in this process (idempotent; the allow list is replaced)."""
    allow: set[tuple[str, int]] = set()
    for host, port in allowed:
        allow |= _resolve(host, int(port))

    def check(sock: socket.socket, address: Any) -> None:
        if sock.family == getattr(socket, "AF_UNIX", None):
            return
        if isinstance(address, tuple) and len(address) >= 2 and (str(address[0]), int(address[1])) in allow:
            return
        raise EgressBlocked(f"zero-egress: connection to {address!r} blocked")

    if not _installed:
        _installed["connect"] = socket.socket.connect
        _installed["connect_ex"] = socket.socket.connect_ex
        _installed["sendto"] = socket.socket.sendto
        _installed["sendmsg"] = socket.socket.sendmsg
        _installed["create_connection"] = socket.create_connection
    orig_connect = _installed["connect"]
    orig_connect_ex = _installed["connect_ex"]
    orig_sendto = _installed["sendto"]
    orig_sendmsg = _installed["sendmsg"]
    orig_create = _installed["create_connection"]

    def connect(self: socket.socket, address: Any) -> None:
        check(self, address)
        return orig_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(self, address)
        return orig_connect_ex(self, address)

    def sendto(self: socket.socket, data: Any, *args: Any) -> int:
        check(self, args[-1])
        return orig_sendto(self, data, *args)

    def sendmsg(self: socket.socket, buffers: Any, ancdata: Any = (), flags: int = 0, address: Any = None) -> int:
        if address is not None:
            check(self, address)
            return orig_sendmsg(self, buffers, ancdata, flags, address)
        return orig_sendmsg(self, buffers, ancdata, flags)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        host, port = address[0], int(address[1])
        if not any((h, port) in allow for h in {host} | {a for a, _ in _resolve(host, port)}):
            raise EgressBlocked(f"zero-egress: connection to {address!r} blocked")
        return orig_create(address, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = sendto  # type: ignore[method-assign]
    socket.socket.sendmsg = sendmsg  # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]


def uninstall() -> None:
    """Restores the original socket functions (tests)."""
    if _installed:
        socket.socket.connect = _installed["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = _installed["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = _installed["sendto"]  # type: ignore[method-assign]
        socket.socket.sendmsg = _installed["sendmsg"]  # type: ignore[method-assign]
        socket.create_connection = _installed["create_connection"]
        _installed.clear()
