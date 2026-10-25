"""Bitstamp public-feed capture daemon: `live_orders`, `live_trades` and `detail_order_book` to Parquet, with gap
detection and contiguous segments.

Gap detection. A *gap* is any point after which the recorded stream may be missing messages:

- `event_chain`: a `live_orders` message whose `pre_event_id` is not the `event_id` of the previous message of the
  same market on the same connection;
- `restart`: `pre_event_id` null after earlier messages of that market on the connection (Bitstamp restarted the
  chain; messages around the restart cannot be checked). A message without a `pre_event_id` member at all cannot be
  checked either; it is counted (`anomalies.no_pre_event_id`) and its `event_id` continues the chain;
- `reconnect`: a new connection after a closed one (server `bts:request_reconnect`, transport error, or a stall);
  everything between the last frame of the old connection and the first of the new one is unseen. The gap's
  `reason` says why the old connection ended. A connection silent for `stall_s` seconds is dropped as well
  (`anomalies.stalls`, reason `stall: ...`).

A *segment* is a maximal run of frames with no gap. Segment 0 starts with the capture; every gap starts a new
segment at the frame that revealed it. A chain break or restart in one market does not reset the chains of the other
markets (a loss that hits several markets at once is a gap in each); a reconnect resets them all. Each segment is
exported and ingested on its own, seeded by its own book snapshot (`export.py`), so a missing message never corrupts
the book of another segment. A REST book is stamped
with the connection and segment on which it was requested; an answer that arrives after a gap describes the market
before the gap and is stored as `kind = "stale_rest_book"`, never as a seed.

Storage errors (a full disk, a lost directory) are fatal: the daemon stops with `QT-INPUT-001` instead of
reconnecting with an ever-growing buffer. A repeated `event_id`
(the same message twice) is not a gap: the row is kept with `kind = "duplicate"` and never exported.

What the chain cannot see: `live_trades` carries no chain (a lost trade is caught later by the matcher's fill
consistency check against the order's amount changes), and a loss after the last received message of the capture
is invisible (there is no next message to reveal it).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import signal
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from livefill import feed
from livefill.store import GAPS_FILE, META_FILE, PartWriter, write_json_atomic
from queuetruth.errors import QtError

CONTINUE = "continue"
RECONNECT = "reconnect"
USER_AGENT = "queuetruth-livefill/1 (public market data capture)"


@dataclass
class CaptureConfig:
    """Daemon options."""

    out: Path
    markets: list[str]
    channels: list[str] = field(default_factory=lambda: list(feed.DEFAULT_CHANNELS))
    url: str = feed.WS_URL
    rest_url: str = feed.REST_URL
    duration_s: float | None = None
    #: fetch a REST `order_book?group=2` snapshot (full depth, with ids) per market after each subscription
    rest_snapshot: bool = True
    stall_s: float = 60.0
    heartbeat_s: float = 15.0
    flush_rows: int = 20_000
    flush_s: float = 10.0
    max_backoff_s: float = 30.0

    def __post_init__(self) -> None:
        self.out = Path(self.out)
        self.markets = [feed.check_market(m) for m in self.markets]
        if not self.markets:
            raise QtError("QT-USAGE-001", "markets: at least one market is required")
        if len(set(self.markets)) != len(self.markets):
            raise QtError("QT-USAGE-001", f"markets: duplicates in {self.markets}")
        self.channels = feed.check_channels(self.channels)
        if self.duration_s is not None and self.duration_s <= 0:
            raise QtError("QT-USAGE-001", "duration must be positive")
        if self.stall_s <= 0 or self.heartbeat_s <= 0 or self.heartbeat_s >= self.stall_s:
            raise QtError("QT-USAGE-001", "need 0 < heartbeat_s < stall_s")


class CaptureSession:
    """Synchronous capture state: rows, chain checks, gaps, segments and counters (no I/O besides the writer)."""

    def __init__(
        self,
        cfg: CaptureConfig,
        clock: Callable[[], int] = time.time_ns,
        mono: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.mono = mono
        cfg.out.mkdir(parents=True, exist_ok=True)
        if (cfg.out / META_FILE).exists():
            raise QtError("QT-USAGE-001", f"{cfg.out} already holds a capture; capture into a new directory")
        self.writer = PartWriter(cfg.out, cfg.flush_rows, cfg.flush_s)
        self.gaps_path = cfg.out / GAPS_FILE
        self.gaps_path.write_text("")
        self.seq = 0
        self.conn = -1
        self.segment = 0
        self.last_event: dict[str, str] = {}
        self.last_us: dict[str, int] = {}
        self.last_recv = 0
        self.pending_reason: str | None = None
        self.gaps: list[dict[str, Any]] = []
        self.segments: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self.anomalies: dict[str, int] = {
            "bad_frames": 0,
            "no_event_id": 0,
            "duplicate_event_id": 0,
            "chain_restarts_at_connect": 0,
            "microtimestamp_regressions": 0,
            "wall_clock_regressions": 0,
            "unknown_channel": 0,
            "stalls": 0,
            "no_pre_event_id": 0,
            "stale_rest_books": 0,
        }
        self.transport_errors: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.subscribed: list[str] = []
        self.reconnect_requests = 0
        self.heartbeats = 0
        self.rest_snapshots = 0
        self.rest_errors: list[dict[str, Any]] = []
        self.start_ns = clock()
        self.end_ns: int | None = None
        self.closed = False
        self.failed: str | None = None
        self._open_segment("start", None)
        self.write_meta()

    # -- segments and gaps ---------------------------------------------------------------------------------------
    def _open_segment(self, opened_by: str, gap_index: int | None) -> None:
        self.segments.append(
            {
                "segment": self.segment,
                "conn": max(self.conn, 0),
                "opened_by": opened_by,
                "gap": gap_index,
                "first_seq": None,
                "last_seq": None,
                "first_recv_ns": None,
                "last_recv_ns": None,
                "rows": 0,
            }
        )

    def _gap(self, kind: str, recv_ns: int, **detail: Any) -> None:
        g = {
            "gap": len(self.gaps),
            "kind": kind,
            "conn": self.conn,
            "segment_before": self.segment,
            "segment_after": self.segment + 1,
            "recv_ns": recv_ns,
            "last_recv_ns_before": self.segments[-1]["last_recv_ns"],
            **detail,
        }
        self.gaps.append(g)
        with self.gaps_path.open("a") as f:
            f.write(json.dumps(g, separators=(",", ":")) + "\n")
            f.flush()
        self.segment += 1
        if kind == "reconnect":
            self.last_event = {}
        self._open_segment(kind, g["gap"])

    # -- rows --------------------------------------------------------------------------------------------------------
    def _row(self, kind: str, raw: str, recv_ns: int, mono_ns: int, p: feed.Parsed | None, market: str | None) -> None:
        if recv_ns < self.last_recv:
            self.anomalies["wall_clock_regressions"] += 1
        self.last_recv = max(self.last_recv, recv_ns)
        self.writer.append(
            {
                "seq": self.seq,
                "recv_ns": recv_ns,
                "mono_ns": mono_ns,
                "conn": max(self.conn, 0),
                "segment": self.segment,
                "kind": kind,
                "channel": p.channel if p else None,
                "market": market if market is not None else (p.market if p else None),
                "event": p.event if p else None,
                "event_id": p.event_id if p else None,
                "pre_event_id": p.pre_event_id if p else None,
                "microtimestamp": p.microtimestamp if p else None,
                "order_id": p.order_id if p else None,
                "trade_id": p.trade_id if p else None,
                "buy_order_id": p.buy_order_id if p else None,
                "sell_order_id": p.sell_order_id if p else None,
                "raw": raw,
            }
        )
        s = self.segments[-1]
        if s["first_seq"] is None:
            s["first_seq"], s["first_recv_ns"] = self.seq, recv_ns
        s["last_seq"], s["last_recv_ns"] = self.seq, recv_ns
        s["rows"] += 1
        self.seq += 1
        if self.writer.due():
            self.flush()

    def _marker(self, what: str, **data: Any) -> None:
        raw = json.dumps({"event": "_capture", "data": {what: True, **data}}, separators=(",", ":"))
        self._row("marker", raw, self.clock(), self.mono(), None, None)

    # -- connection events -------------------------------------------------------------------------------------------
    def connected(self) -> None:
        """A new WebSocket connection is open (before subscribing)."""
        now = self.clock()
        if self.conn >= 0:
            self.conn += 1
            self._gap("reconnect", now, reason=self.pending_reason or "connection closed")
        else:
            self.conn = 0
        self.pending_reason = None
        self._marker("connect", conn=self.conn)

    def disconnected(self, reason: str) -> None:
        """The current connection is closed or abandoned (`reason` is kept for the next `reconnect` gap)."""
        if self.pending_reason is None:
            self.pending_reason = reason
        self._marker("disconnect", reason=reason)

    def stalled(self, seconds: float) -> None:
        """No frame for `seconds`: the caller drops the connection; the next `reconnect` gap names the stall."""
        self.anomalies["stalls"] += 1
        self.disconnected(f"stall: no frame for {seconds:.1f} s")

    def transport_error(self, error: str) -> None:
        """A connection attempt or an open connection failed (`error` is the exception text)."""
        self.transport_errors.append({"error": error, "recv_ns": self.clock()})
        if self.conn >= 0:
            self.disconnected(f"transport error: {error}")

    # -- frames ------------------------------------------------------------------------------------------------------
    def handle(self, raw: str, recv_ns: int | None = None, mono_ns: int | None = None) -> str:
        """Records one WebSocket text frame. Returns `RECONNECT` when the server asked for a reconnect."""
        recv_ns = self.clock() if recv_ns is None else recv_ns
        mono_ns = self.mono() if mono_ns is None else mono_ns
        try:
            p = feed.parse_message(raw)
        except QtError:
            self.anomalies["bad_frames"] += 1
            self._row("ws", raw, recv_ns, mono_ns, None, None)
            return CONTINUE
        ev = p.event or ""
        if ev == feed.RECONNECT_EVENT:
            self.reconnect_requests += 1
            self._row("ws", raw, recv_ns, mono_ns, p, None)
            return RECONNECT
        if ev in feed.CONTROL_EVENTS:
            if ev == "bts:subscription_succeeded" and p.channel:
                self.subscribed.append(p.channel)
            elif ev == "bts:error":
                self.errors.append({"data": p.data, "recv_ns": recv_ns})
            elif ev == "bts:heartbeat":
                self.heartbeats += 1
            self._row("ws", raw, recv_ns, mono_ns, p, None)
            return CONTINUE
        kind = "ws"
        if p.prefix not in feed.KNOWN_CHANNELS:
            self.anomalies["unknown_channel"] += 1
        if ev in feed.ORDER_EVENTS and p.market:
            kind = self._chain(p, recv_ns)
            last = self.last_us.get(p.market)
            if p.microtimestamp is not None:
                if last is not None and p.microtimestamp < last:
                    self.anomalies["microtimestamp_regressions"] += 1
                self.last_us[p.market] = max(p.microtimestamp, last or p.microtimestamp)
        if p.prefix:
            self.counts[p.prefix] = self.counts.get(p.prefix, 0) + 1
        self._row(kind, raw, recv_ns, mono_ns, p, None)
        return CONTINUE

    def _chain(self, p: feed.Parsed, recv_ns: int) -> str:
        m = p.market or ""
        prev = self.last_event.get(m)
        if p.event_id is None:
            self.anomalies["no_event_id"] += 1
            return "ws"
        if prev is not None and p.event_id == prev:
            self.anomalies["duplicate_event_id"] += 1
            return "duplicate"
        if not p.has_pre_event_id:
            self.anomalies["no_pre_event_id"] += 1
        elif p.pre_event_id is None:
            if prev is None:
                self.anomalies["chain_restarts_at_connect"] += 1
            else:
                self._gap("restart", recv_ns, market=m, expected_pre=prev, event_id=p.event_id, seq=self.seq)
        elif prev is not None and p.pre_event_id != prev:
            self._gap(
                "event_chain",
                recv_ns,
                market=m,
                expected_pre=prev,
                got_pre=p.pre_event_id,
                event_id=p.event_id,
                seq=self.seq,
            )
        self.last_event[m] = p.event_id
        return "ws"

    def rest_book(
        self,
        market: str,
        body: str,
        recv_ns: int | None = None,
        mono_ns: int | None = None,
        requested: tuple[int, int] | None = None,
    ) -> None:
        """Records a REST `order_book?group=2` body of `market` (validated by `feed.rest_seed_line`). `requested` is
        the (connection, segment) current when the request was sent: when either changed while it was in flight, the
        book predates a gap and is stored as `stale_rest_book`."""
        recv_ns = self.clock() if recv_ns is None else recv_ns
        mono_ns = self.mono() if mono_ns is None else mono_ns
        feed.rest_seed_line(market, body, recv_ns)
        us = json.loads(body).get("microtimestamp")
        p = feed.Parsed(
            event="rest_book",
            channel=f"rest_order_book_{market}",
            market=market,
            prefix="rest_order_book",
            event_id=None,
            pre_event_id=None,
            has_pre_event_id=False,
            microtimestamp=int(us),
            order_id=None,
            trade_id=None,
            buy_order_id=None,
            sell_order_id=None,
            data=None,
        )
        if requested is not None and requested != (self.conn, self.segment):
            self.anomalies["stale_rest_books"] += 1
            self._row("stale_rest_book", body.strip(), recv_ns, mono_ns, p, market)
            return
        self.rest_snapshots += 1
        self._row("rest_book", body.strip(), recv_ns, mono_ns, p, market)

    def position(self) -> tuple[int, int]:
        """(connection, segment) now (stamp a REST request with it)."""
        return self.conn, self.segment

    def rest_error(self, market: str, error: str) -> None:
        self.rest_errors.append({"market": market, "error": error, "recv_ns": self.clock()})

    # -- persistence ---------------------------------------------------------------------------------------------
    def meta(self) -> dict[str, Any]:
        return {
            "schema": "livefill.capture_meta",
            "schema_version": 1,
            "venue": "bitstamp",
            "markets": list(self.cfg.markets),
            "channels": list(self.cfg.channels),
            "url": self.cfg.url,
            "rest_snapshot": self.cfg.rest_snapshot,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "complete": self.closed and self.failed is None,
            "storage_error": self.failed,
            "rows": self.seq,
            "parts": self.writer.n_parts,
            "counts": dict(sorted(self.counts.items())),
            "connections": self.conn + 1,
            "reconnects": max(self.conn, 0),
            "reconnect_requests": self.reconnect_requests,
            "heartbeats": self.heartbeats,
            "rest_snapshots": self.rest_snapshots,
            "rest_errors": self.rest_errors,
            "subscribed": self.subscribed,
            "errors": self.errors,
            "transport_errors": self.transport_errors,
            "anomalies": dict(self.anomalies),
            "gaps": self.gaps,
            "segments": self.segments,
        }

    def write_meta(self) -> None:
        write_json_atomic(self.cfg.out / META_FILE, self.meta())

    def flush(self) -> None:
        """Writes the buffered rows and the metadata; a storage error stops the capture (`QT-INPUT-001`)."""
        try:
            self.writer.flush()
            self.write_meta()
        except OSError as e:
            self.failed = repr(e)
            raise QtError("QT-INPUT-001", f"{self.cfg.out}: cannot write the capture ({e}); capture stopped") from e

    def close(self) -> dict[str, Any]:
        """Final flush. After a storage error it only tries to record the failure in the metadata."""
        if not self.closed:
            self.end_ns = self.clock()
            self.closed = True
            if self.failed is None:
                self.flush()
            else:
                with contextlib.suppress(OSError):
                    self.write_meta()
        return self.meta()


def fetch_order_book(rest_url: str, market: str, timeout_s: float = 10.0) -> str:
    """GET `/api/v2/order_book/<market>/?group=2` (public, no credentials); returns the body text."""
    url = f"{rest_url.rstrip('/')}/api/v2/order_book/{market}/?group=2"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return r.read().decode("utf-8")


async def _snapshot(session: CaptureSession, market: str, fetch: Callable[[str, str], str]) -> None:
    requested = session.position()
    try:
        body = await asyncio.to_thread(fetch, session.cfg.rest_url, market)
    except Exception as e:
        session.rest_error(market, repr(e))
        return
    try:
        session.rest_book(market, body, requested=requested)
    except QtError as e:
        if session.failed is not None:
            raise
        session.rest_error(market, repr(e))


async def run_capture(
    cfg: CaptureConfig,
    *,
    fetch: Callable[[str, str], str] = fetch_order_book,
    stop: asyncio.Event | None = None,
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
    session: CaptureSession | None = None,
) -> dict[str, Any]:
    """Runs the daemon until `cfg.duration_s` elapses or `stop` is set (SIGINT / SIGTERM set it when running in the
    main thread); returns the final `capture_meta.json` document."""
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException
    except ImportError as e:  # pragma: no cover - exercised only without the optional dependency
        raise QtError(
            "QT-USAGE-001",
            "the capture daemon needs the 'websockets' package (pip install 'websockets>=13')",
        ) from e
    s = session or CaptureSession(cfg)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
    deadline = None if cfg.duration_s is None else time.monotonic() + cfg.duration_s
    subs = feed.subscribe_messages(cfg.markets, cfg.channels)
    backoff = 1.0
    tasks: set[asyncio.Task[None]] = set()

    def left() -> float:
        return float("inf") if deadline is None else deadline - time.monotonic()

    try:
        while not stop.is_set() and left() > 0:
            try:
                async with connect(cfg.url, ping_interval=20, ping_timeout=20, max_size=2**24, open_timeout=15) as ws:
                    s.connected()
                    backoff = 1.0
                    for m in subs:
                        await ws.send(m)
                    seeded: set[str] = set()
                    last_frame = time.monotonic()
                    while not stop.is_set() and left() > 0:
                        wait = min(left(), cfg.heartbeat_s, 1.0)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=wait)
                        except TimeoutError:
                            silent = time.monotonic() - last_frame
                            if silent >= cfg.stall_s:
                                s.stalled(silent)
                                break
                            if silent >= cfg.heartbeat_s:
                                await ws.send(json.dumps({"event": "bts:heartbeat"}))
                            if s.writer.due():
                                s.flush()
                            continue
                        last_frame = time.monotonic()
                        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                        n_subscribed = len(s.subscribed)
                        if s.handle(text) == RECONNECT:
                            s.disconnected("bts:request_reconnect")
                            break
                        if cfg.rest_snapshot and len(s.subscribed) > n_subscribed:
                            prefix, market = feed.split_channel(s.subscribed[-1])
                            if prefix == "live_orders" and market not in seeded:
                                seeded.add(market)
                                t = asyncio.create_task(_snapshot(s, market, fetch))
                                tasks.add(t)
                                t.add_done_callback(tasks.discard)
                    else:
                        s.disconnected("capture finished" if left() <= 0 else "stopped")
            except (OSError, WebSocketException, TimeoutError) as e:
                s.transport_error(repr(e))
                log(f"livefill capture: connection error {e!r}; reconnecting in {backoff:.0f} s")
            if stop.is_set() or left() <= 0:
                break
            pause = min(backoff, max(left(), 0.0)) * (0.5 + random.random() / 2)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=pause)
            backoff = min(backoff * 2, cfg.max_backoff_s)
        if tasks:
            await asyncio.wait(tasks, timeout=15)
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        meta = s.close()
    return meta
