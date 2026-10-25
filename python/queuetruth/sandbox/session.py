"""The sandbox exchange: an episode over one instrument-day of an MBO dataset in which the agent's orders fill exactly
as the real queue would have filled them.

Clock and information. The agent's clock starts at the episode start and moves only forward, by `advance`. What the
agent can see is the visible cut `clock - feed_latency_ns`: the tape (`queuetruth.sandbox.tape`) releases the source
records of complete events up to that cut into a scratch dataset, and every observation, fill, queue position and
markout is computed from that scratch dataset alone. Instructions are stamped with the clock (there is no way to
backdate one), so an order sent at `T` arrives at `T + latency` and can only touch events after the cut it was
decided on.

Decide-then-replay. `queuetruth._core.Session` advances a book step by step but carries no shadow orders, and a replay
runs a whole dataset, so each `advance` re-runs the exact replay (SPEC 5.1, 5.2, fill rule `exact`) of the scratch
dataset, i.e. of the episode's prefix, with every order that is still open (orders that have ended keep their final
state: shadows never see each other, so dropping them changes nothing). Because instructions only take effect after
the cut they were made at and prefixes nest, the fills and order states of each step are exactly those of one full
replay of the same orders whose events lie in that prefix; `finish` runs that single full replay on the original
files and checks the equality (`Consistency`), and a fill reported earlier that a longer prefix would change raises
`QT-ENGINE-001`. The agent acts only at the times it advances to, so its reaction time to a market event is its step
plus `latency_ns`.

Instruction arguments are validated in their logged form (`_util.wire`), so `verify` meets every rejection again.
"""

from __future__ import annotations

import heapq
import json
import re
import shutil
import tempfile
import threading
import time
import weakref
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, TypeVar

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ValidationError

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.sandbox import scorecard as sc
from queuetruth.sandbox._util import (
    config_error,
    date_of_days,
    digest,
    local_time,
    money,
    parse_duration,
    parse_time,
    to_fixed,
    usage_error,
    wire,
)
from queuetruth.sandbox.book import BookView, TradeStatsAcc
from queuetruth.sandbox.log import CommitLog
from queuetruth.sandbox.models import (
    SandboxAck,
    SandboxObservation,
    SandboxScorecard,
    SandboxSession,
    SandboxSpec,
)
from queuetruth.sandbox.orders import FILL_COLUMNS, Account, Order, absorb, fee_of, pnl, schedule
from queuetruth.sandbox.tape import Contain, SourceDataset, Tape, certification_view, open_source
from queuetruth.schemas.io import write_table
from queuetruth.score._io import rows as table_rows
from queuetruth.stats import stable_seed

M = TypeVar("M", bound=BaseModel)
_CLIENT_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")
#: The rules of the simulation, stated in every session document.
RULES = (
    "Fills are exact: your orders are shadow orders placed in the real FIFO queue of the recorded order-level data "
    "(fill rule exact, SPEC 5.2); a resting order fills when the orders ahead of it are gone and volume trades at "
    "its price, or when the market trades through it.",
    "No market impact: your orders never change the recorded book, never trade with each other and never see each "
    "other; two of your orders at one price both assume the whole queue ahead (see the scorecard diagnostics).",
    "Your clock moves only with advance; you see the market and your orders up to clock - feed_latency_ns. Orders "
    "and cancels are stamped with your clock and take effect latency_ns (+ jitter) later. You act only at the times "
    "you advance to: your reaction time to a market event is your step plus latency_ns.",
    "An order is confirmed at the first market event after its arrival; until then it shows as pending, even when "
    "it is marketable. A marketable order takes the displayed liquidity at or through its price on arrival (taker); "
    "the rest rests (day) or is cancelled (ioc, market).",
    "Market orders are IOC limit orders at a collar (market_collar_bps through the opposite best you saw).",
    "Hidden liquidity is not modelled except through the venue's hidden-print policy; auctions and halts end your "
    "orders as censored.",
)


def _validate(model: type[M], data: Any) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as e:
        errors = json.loads(e.json(include_url=False))
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        msg = f"{model.__name__}: {loc + ': ' if loc else ''}{first.get('msg', 'invalid')}"
        if len(errors) > 1:
            msg += f" (+{len(errors) - 1} more)"
        raise QtError("QT-CONFIG-001", msg, details={"errors": errors}) from None


_DURATION_ALIASES = {"latency": "latency_ns", "jitter": "jitter_ns", "feed_latency": "feed_latency_ns"}
_TIME_ALIASES = {"start": "start_ns", "end": "end_ns"}


def normalize_spec(raw: dict[str, Any], midnight_utc_ns: int | None) -> dict[str, Any]:
    """A spec dict with friendly values converted: `start`/`end` (or `start_ns`/`end_ns`) as local `HH:MM[:SS]`
    times of the session day, `latency`/`jitter`/`feed_latency` (or the `_ns` fields) and `markout_horizons(_ns)`
    as duration strings. Without `midnight_utc_ns` time strings are left as they are (a first validation pass)."""
    out = dict(raw)
    for alias, key in _DURATION_ALIASES.items():
        if alias in out:
            if key in out:
                raise config_error(f"give {alias} or {key}, not both")
            out[key] = out.pop(alias)
        if isinstance(out.get(key), str):
            out[key] = parse_duration(out[key], key)
    if "markout_horizons" in out:
        if "markout_horizons_ns" in out:
            raise config_error("give markout_horizons or markout_horizons_ns, not both")
        out["markout_horizons_ns"] = out.pop("markout_horizons")
    if isinstance(out.get("markout_horizons_ns"), list):
        out["markout_horizons_ns"] = [
            parse_duration(h, "markout_horizons_ns") if isinstance(h, str) else h for h in out["markout_horizons_ns"]
        ]
    for alias, key in _TIME_ALIASES.items():
        if alias in out:
            if key in out:
                raise config_error(f"give {alias} or {key}, not both")
            out[key] = out.pop(alias)
        if isinstance(out.get(key), str):
            out[key] = None if midnight_utc_ns is None else parse_time(out[key], midnight_utc_ns, key)
    return out


def _identity(spec: SandboxSpec, dataset_id: str) -> dict[str, Any]:
    doc = spec.model_dump(mode="json")
    doc["dataset"] = {"sha256": dataset_id}
    return doc


def spec_digest(spec: SandboxSpec, dataset_id: str) -> str:
    """sha256(canonical_json({engine_version, spec with the dataset as its id})) (SPEC 7.3); the log's `open` entry
    commits to it, so the log root commits to every setting of the episode."""
    return digest({"engine_version": __version__, "spec": _identity(spec, dataset_id)})


def spec_id_of(spec: SandboxSpec, dataset_id: str) -> str:
    """`spec_id`: the first 16 hex of `spec_digest`."""
    return spec_digest(spec, dataset_id)[:16]


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


class Sandbox:
    """One sandbox episode (module docstring). Open with `Sandbox.open(spec)`; use as a context manager or call
    `finish()` / `close()`. Methods are serialized by a lock, so one session may be shared across threads."""

    def __init__(
        self,
        *,
        spec: SandboxSpec,
        src: SourceDataset,
        workdir: Path,
        owned_workdir: bool,
        keep_workdir: bool,
        session_id: str | None,
    ) -> None:
        self.spec = spec
        self.src = src
        self.session_id = session_id
        self._lock = threading.RLock()
        self._workdir = workdir
        self._finalizer = weakref.finalize(self, _rmtree, str(workdir)) if owned_workdir and not keep_workdir else None
        inst = src.instrument
        midnight = int(inst["midnight_utc_ns"])
        self.instrument = {
            "instrument_id": int(inst["instrument_id"]),
            "symbol": str(inst.get("symbol") or ""),
            "session_date": date_of_days(src.partition.session_date),
            "venue": str(inst.get("venue") or src.manifest.get("venue") or ""),
            "rule_set": src.rule_set,
            "tick_size": int(inst["tick_size"]),
            "lot_size": int(inst.get("lot_size") or 1),
            "price_exp": int(inst["price_exp"]),
            "size_exp": int(inst.get("size_exp") or 0),
            "contract_multiplier": float(inst.get("contract_multiplier") or 1.0),
            "currency": str(inst.get("currency") or ""),
            "session_tz": str(inst.get("session_tz") or "UTC"),
            "midnight_utc_ns": midnight,
            "open_ns": midnight + int(inst.get("open_s") or 0) * 1_000_000_000,
            "close_ns": midnight + int(inst.get("close_s") or 86_400) * 1_000_000_000,
        }
        self.start_ns = spec.start_ns if spec.start_ns is not None else self.instrument["open_ns"]
        self.end_ns = spec.end_ns if spec.end_ns is not None else self.instrument["close_ns"]
        if self.end_ns <= self.start_ns:
            raise config_error(
                f"episode window is empty: end {local_time(self.end_ns, midnight)} <= start "
                f"{local_time(self.start_ns, midnight)}"
            )
        self.clock_ns = self.start_ns
        self.visible_ns = self.start_ns - spec.feed_latency_ns
        self.step = 0
        self.closed = False
        self.orders: list[Order] = []
        self._by_id: dict[str, Order] = {}
        self._by_client: dict[str, Order] = {}
        #: Orders not ended yet, in submission order.
        self._open: dict[str, Order] = {}
        #: Running account of every fill reported so far.
        self._acct = Account()
        self.dataset_id = src.dataset_id
        self.spec_id = spec_id_of(spec, self.dataset_id)
        self.log = CommitLog(
            engine_version=__version__,
            dataset_id=self.dataset_id,
            spec_id=self.spec_id,
            spec=spec.model_dump(mode="json"),
        )
        self.tape = Tape(src, workdir / "tape")
        self.book = BookView(
            instrument_id=self.instrument["instrument_id"],
            modify_records_present=src.modify_records_present,
            supports_hidden_orders=src.supports_hidden_orders,
            starts_after=self.start_ns,
        )
        self._session_stats = TradeStatsAcc()
        self._step_stats = TradeStatsAcc()
        self._step_prints: list[tuple[int, int, int, str, str]] = []
        self._step_fills: list[dict[str, Any]] = []
        self._step_markouts: list[dict[str, Any]] = []
        self._step_changed: list[str] = []
        #: Markouts not visible yet: (due time, order index, fill_seq, horizon index, fill row).
        self._mo_due: list[tuple[int, int, int, int, dict[str, Any]]] = []
        self._last_print_px: int | None = None
        self._cuts: list[tuple[int, int]] = []
        self._final_markouts: pa.Table | None = None
        self.window_replays = 0
        self._replayed_rows = -1
        self.rejected = 0
        self._timings: dict[str, float] = {"tape_s": 0.0, "book_s": 0.0, "replay_s": 0.0}
        self._sync()
        obs = self._observation()
        self.log.append(
            "open",
            clock_ns=self.clock_ns,
            visible_ns=self.visible_ns,
            rows_seen=self.tape.rows,
            data={
                "engine_version": __version__,
                "dataset_id": self.dataset_id,
                "spec_id": self.spec_id,
                "spec_sha256": spec_digest(spec, self.dataset_id),
                "instrument_id": self.instrument["instrument_id"],
                "session_date": self.instrument["session_date"],
                "start_ns": self.start_ns,
                "end_ns": self.end_ns,
                "observation_sha256": digest(obs.model_dump(mode="json")),
            },
        )
        self.session = SandboxSession.model_validate(
            {
                "schema": "qt.sandbox_session",
                "schema_version": 1,
                "session_id": session_id,
                "engine_version": __version__,
                "spec_id": self.spec_id,
                "dataset_id": self.dataset_id,
                "spec": spec.model_dump(mode="json"),
                "instrument": self.instrument,
                "start_ns": self.start_ns,
                "end_ns": self.end_ns,
                "rules": list(RULES),
                "observation": obs.model_dump(mode="json"),
            }
        )

    # ---- construction -------------------------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        spec: SandboxSpec | dict[str, Any],
        *,
        workdir: str | Path | None = None,
        keep_workdir: bool = False,
        session_id: str | None = None,
        contain: Contain | None = None,
    ) -> Sandbox:
        """Opens an episode. `spec` is a `SandboxSpec` or a dict (friendly values allowed: `normalize_spec`).
        Scratch files go to a new temporary directory (inside `workdir` when given), removed by `close()`, `finish()`
        or garbage collection unless `keep_workdir`. `contain(path)` (MCP local mode) must return the real path of
        every dataset file it accepts and raise for one it refuses; it is applied before each file is read
        (`tape.open_source`)."""
        raw = spec.model_dump(mode="json") if isinstance(spec, BaseModel) else dict(spec)
        first = normalize_spec(raw, None)
        _validate(SandboxSpec, first)
        ds = first["dataset"]["path"]
        src = open_source(
            ds,
            instrument_id=first.get("instrument_id"),
            symbol=first.get("symbol"),
            session_date=first.get("session_date"),
            contain=contain,
        )
        full = _validate(SandboxSpec, normalize_spec(raw, int(src.instrument["midnight_utc_ns"])))
        base = Path(workdir) if workdir is not None else None
        if base is not None:
            base.mkdir(parents=True, exist_ok=True)
        wd = Path(tempfile.mkdtemp(prefix="qt-sandbox-", dir=base))
        try:
            return cls(
                spec=full,
                src=src,
                workdir=wd,
                owned_workdir=True,
                keep_workdir=keep_workdir,
                session_id=session_id,
            )
        except BaseException:
            if not keep_workdir:
                shutil.rmtree(wd, ignore_errors=True)
            raise

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Ends the session without a scorecard and removes its scratch files (unless `keep_workdir`)."""
        with self._lock:
            self.closed = True
            if self._finalizer is not None:
                self._finalizer()

    @property
    def workdir(self) -> Path:
        """The scratch directory (tape, replay outputs)."""
        return self._workdir

    # ---- helpers ------------------------------------------------------------------------------------------------

    def _check_open(self) -> None:
        if self.closed:
            raise usage_error("the session is finished or closed", "open a new session")

    def _units(self) -> dict[str, Any]:
        i = self.instrument
        return {"price_exp": i["price_exp"], "size_exp": i["size_exp"], "mult": i["contract_multiplier"]}

    def to_price(self, raw: int) -> Decimal:
        """A raw fixed-point price (observations, acks, scorecards) as an exact decimal price."""
        return Decimal(int(raw)).scaleb(self.instrument["price_exp"])

    def to_raw(self, price: str | Decimal | int) -> int:
        """A decimal price (`"291.65"`) as the raw fixed-point integer the documents use (an int is returned as is)."""
        return to_fixed(wire(price), self.instrument["price_exp"], "price")

    def _order_latency(self, order_id: str) -> int:
        j = self.spec.jitter_ns
        if j <= 0:
            return self.spec.latency_ns
        return self.spec.latency_ns + stable_seed(self.spec.seed, [b"sandbox_latency", order_id]) % (j + 1)

    # ---- the step ----------------------------------------------------------------------------------------------

    def _sync(self) -> None:
        """Extends the tape to the visible cut, updates the market view, replays open orders on the new prefix and
        collects the step's prints, fills and markouts."""
        t0 = time.perf_counter()
        mbo, prints = self.tape.extend_to(self.visible_ns)
        t1 = time.perf_counter()
        self.book.add_cross_prints(prints)
        new_prints = self.book.apply(mbo)
        if self.tape.cursor.eof:
            self.book.end_partial_event()
        t2 = time.perf_counter()
        self._timings["tape_s"] += t1 - t0
        self._timings["book_s"] += t2 - t1
        self._step_prints = new_prints
        self._step_stats = TradeStatsAcc()
        open_ns = self.instrument["open_ns"]
        for t, px, size, _, _ in new_prints:
            self._step_stats.add(t, px, size)
            if t >= open_ns:
                self._session_stats.add(t, px, size)
            self._last_print_px = px
        self._step_fills = []
        self._step_changed = []
        self._cuts.append((self.step, self.visible_ns))
        active = list(self._open.values())
        # Without new records since the last window replay nothing can have changed: an instruction sent since then
        # takes effect after that replay's cut, and no event of the prefix lies after it.
        if active and not self.tape.empty and self.tape.rows != self._replayed_rows:
            before = {o.order_id: (o.status, o.filled_qty) for o in active}
            self._replay_window(active)
            self._replayed_rows = self.tape.rows
            for o in active:
                if before[o.order_id] != (o.status, o.filled_qty):
                    self._step_changed.append(o.order_id)
                if o.status == "working" and o.working_step is None:
                    o.working_step = self.step
                if o.final:
                    o.final_step = self.step
                    o.shown_final = (o.status, o.reason, None if o.ts_done is None else int(o.ts_done), o.filled_qty)
                    del self._open[o.order_id]
        self._step_markouts = self._new_markouts()

    def _run_replay(self, dataset: Path, orders: list[Order], out: Path, *, bbo: str) -> dict[str, Any]:
        from queuetruth.api import replay

        sched = self._workdir / "sched"
        sched.mkdir(exist_ok=True)
        shadows, amends = schedule(orders, self.instrument["instrument_id"])
        write_table(shadows, sched / "shadows.parquet")
        spec: dict[str, Any] = {
            "dataset": {"path": str(dataset)},
            "out": str(out),
            "shadows": {"path": str(sched / "shadows.parquet")},
            "bbo": bbo,
            "depth_levels": 0,
            "trajectory": "ahead",
            "seed": self.spec.seed,
            "hidden_print_policy": self.spec.hidden_print_policy,
        }
        if amends.num_rows:
            write_table(amends, sched / "amends.parquet")
            spec["amends"] = {"path": str(sched / "amends.parquet")}
        if out.exists():
            shutil.rmtree(out)
        t0 = time.perf_counter()
        summary = replay(spec)
        self._timings["replay_s"] += time.perf_counter() - t0
        res = {r["shadow_id"]: r for r in table_rows(pq.read_table(out / "shadow_results.parquet"))}
        fills: dict[str, list[dict[str, Any]]] = {}
        for r in table_rows(pq.read_table(out / "shadow_fills.parquet")):
            fills.setdefault(r["shadow_id"], []).append({k: r.get(k) for k in FILL_COLUMNS})
        traj: dict[str, dict[str, Any]] = {}
        tpath = out / "queue_trajectory.parquet"
        if tpath.exists():
            for r in table_rows(pq.read_table(tpath)):
                traj[r["shadow_id"]] = r
        return {"summary": summary, "results": res, "fills": fills, "traj": traj}

    def _replay_window(self, active: list[Order]) -> None:
        out = self._run_replay(self.tape.root, active, self._workdir / "runs" / "window", bbo="none")
        self.window_replays += 1
        for o in active:
            r = out["results"].get(o.order_id)
            if r is None:
                raise QtError("QT-ENGINE-001", f"order {o.order_id}: no shadow_results row in the window replay")
            new = absorb(
                o, r, out["fills"].get(o.order_id, []), out["traj"].get(o.order_id), final_replay=False, step=self.step
            )
            for f in new:
                self._step_fills.append(self._fill_view(o, f, self.step))
                q, px = int(f["fill_qty"]), int(f["fill_px"])
                self._acct.add(o.sign, q, px, str(f["liquidity"]), self.spec.fees, **self._units())
                h0 = self.spec.markout_horizons_ns[0]
                heapq.heappush(self._mo_due, (int(f["ts_event"]) + h0, o.index, int(f["fill_seq"]), 0, f))

    def _fill_view(self, o: Order, f: dict[str, Any], step: int | None) -> dict[str, Any]:
        u = self._units()
        return {
            "order_id": o.order_id,
            "fill_seq": int(f["fill_seq"]),
            "ts_event": int(f["ts_event"]),
            "ts_known": o.known_ts(f),
            "side": o.side,
            "price": int(f["fill_px"]),
            "qty": int(f["fill_qty"]),
            "liquidity": str(f["liquidity"]),
            "reason": str(f["reason"]),
            "queue_ahead_before": int(f["queue_ahead_before"]),
            "fee": fee_of(int(f["fill_qty"]), int(f["fill_px"]), str(f["liquidity"]), self.spec.fees, **u),
            "step": step,
        }

    def _markout(self, o: Order, f: dict[str, Any], h: int) -> dict[str, Any]:
        """Markout of one fill at one horizon against the visible best-quote history, the formula of
        `queuetruth.score.markouts.compute_markouts` with `last_event_ts` = the visible cut."""
        u = self._units()
        t, p, q = int(f["ts_event"]), int(f["fill_px"]), int(f["fill_qty"])
        i = self.book.quote_at(t + h)
        mo = bps = mo_ccy = None
        if i >= 0:
            b, a = self.book.h_bid[i], self.book.h_ask[i]
            if b is not None and a is not None:
                m_rel = float(int(b) - p) + float(int(a) - int(b)) * 0.5
                d = o.sign
                mo = d * m_rel / self.instrument["tick_size"]
                mo_ccy = d * m_rel * 10.0 ** u["price_exp"] * q * 10.0 ** u["size_exp"] * u["mult"]
                bps = d * m_rel / abs(p) * 1e4 if p != 0 else None
        return {
            "order_id": o.order_id,
            "fill_seq": int(f["fill_seq"]),
            "horizon_ns": h,
            "mo_ticks": mo,
            "mo_bps": bps,
            "mo_ccy": mo_ccy,
        }

    def _new_markouts(self) -> list[dict[str, Any]]:
        """Markouts whose horizon became visible at this step, by order, fill and horizon (a heap keyed by due time,
        so a step costs the markouts it reports, not the fills of the whole episode)."""
        due: list[tuple[int, int, int]] = []
        out: list[dict[str, Any]] = []
        hs = self.spec.markout_horizons_ns
        while self._mo_due and self._mo_due[0][0] <= self.visible_ns:
            _, idx, seq, k, f = heapq.heappop(self._mo_due)
            due.append((idx, seq, k))
            out.append(self._markout(self.orders[idx], f, hs[k]))
            if k + 1 < len(hs):
                heapq.heappush(self._mo_due, (int(f["ts_event"]) + hs[k + 1], idx, seq, k + 1, f))
        return [m for _, m in sorted(zip(due, out, strict=True), key=lambda x: x[0])]

    # ---- views --------------------------------------------------------------------------------------------------

    def _order_view(self, o: Order) -> dict[str, Any]:
        filled = o.filled_qty
        notional = sum(int(f["fill_px"]) * int(f["fill_qty"]) for f in o.fills)
        price = o.resolved_price if o.resolved_price is not None else o.price
        working = o.status == "working"
        level = None
        if working and price is not None:
            level = self.book.level_qty(o.side, price)
        return {
            "order_id": o.order_id,
            "client_order_id": o.client_order_id,
            "side": o.side,
            "type": o.type,
            "tif": o.tif,
            "price": price,
            "peg_offset_ticks": o.peg_offset_ticks,
            "qty": o.qty,
            "filled_qty": filled,
            "leaves_qty": max(0, o.qty - filled) if not o.final else 0,
            "status": o.status,
            "reason": o.reason,
            "ts_submit": o.ts_submit,
            "ts_activated": o.ts_activated,
            "ts_done": None if o.ts_done is None else int(o.ts_done),
            "queue_ahead": o.queue_ahead if working else None,
            "rank": o.rank if working else None,
            "level_qty": level,
            "cancel_sent_ns": o.cancel_send_ns,
            "n_fills": len(o.fills),
            "avg_fill_price": (
                float((Decimal(notional) / Decimal(filled)).scaleb(self.instrument["price_exp"])) if filled else None
            ),
        }

    def _position(self) -> dict[str, Any]:
        u = self._units()
        acc = self._acct
        mark_x2, source = None, None
        if self.book.h_ts:
            m2 = self.book.mid_x2(len(self.book.h_ts) - 1)
            if m2 is not None:
                mark_x2, source = m2, "mid"
        if mark_x2 is None and self._last_print_px is not None:
            mark_x2, source = 2 * self._last_print_px, "last_trade"
        gross = pnl(acc, mark_x2, **u)
        return {
            "qty": acc.qty,
            "bought_qty": acc.bought,
            "sold_qty": acc.sold,
            "cash": float(money(acc.cash_raw, u["price_exp"] + u["size_exp"], u["mult"])),
            "fees": float(acc.fees),
            "mark_price": None if mark_x2 is None else float(Decimal(mark_x2).scaleb(u["price_exp"]) / 2),
            "mark_source": source,
            "pnl_gross": None if gross is None else float(gross),
            "pnl_net": None if gross is None else float(gross - acc.fees),
            "currency": self.instrument["currency"],
        }

    def _observation(self) -> SandboxObservation:
        bid, ask = self.book.best()
        bids, asks = self.book.depth(self.spec.depth_levels)
        tick = self.instrument["tick_size"]
        spread = None
        if bid is not None and ask is not None and tick > 0:
            spread = (ask[0] - bid[0]) // tick
        pe = self.instrument["price_exp"]
        prints = self._step_prints[-self.spec.max_trades :] if self.spec.max_trades else []
        ended = [self._by_id[i] for i in self._step_changed if self._by_id[i].final]
        show = sorted([*self._open.values(), *ended], key=lambda o: o.index)
        warnings = []
        if self.clock_ns >= self.end_ns:
            warnings.append("the clock reached the episode end: call finish for the scorecard")
        doc = {
            "schema": "qt.sandbox_observation",
            "schema_version": 1,
            "step": self.step,
            "clock_ns": self.clock_ns,
            "visible_ns": self.visible_ns,
            "time_local": local_time(self.clock_ns, self.instrument["midnight_utc_ns"]),
            "done": self.clock_ns >= self.end_ns,
            "book": {
                "ts_event": self.book.last_event_ts,
                "bid": bid,
                "ask": ask,
                "bids": bids,
                "asks": asks,
                "spread_ticks": spread,
            },
            "trades": [{"ts_event": t, "price": p, "size": s, "aggressor": a, "kind": k} for t, p, s, a, k in prints],
            "trades_since_last": self._step_stats.doc(pe),
            "session_trades": self._session_stats.doc(pe),
            "orders": [self._order_view(o) for o in show],
            "n_orders": len(self.orders),
            "n_open": len(self._open),
            "fills": self._step_fills,
            "markouts": self._step_markouts,
            "position": self._position(),
            "warnings": warnings,
        }
        return SandboxObservation.model_validate(doc)

    # ---- public API ---------------------------------------------------------------------------------------------

    def observe(self) -> SandboxObservation:
        """The current observation (read-only; the same document `advance` returned, with orders sent since)."""
        with self._lock:
            self._check_open()
            return self._observation()

    def advance(self, *, by: int | str | None = None, to: int | str | None = None) -> SandboxObservation:
        """Moves the clock forward by a duration (`by`: ns or `"500ms"`) or to a time (`to`: ns or local
        `"10:31:05"`), capped at the episode end, and returns the new observation."""
        with self._lock:
            self._check_open()
            if (by is None) == (to is None):
                raise usage_error("advance needs exactly one of by, to")
            if self.clock_ns >= self.end_ns:
                raise usage_error("the episode is over (the clock is at its end)", "call finish for the scorecard")
            if self.step >= self.spec.max_steps:
                raise usage_error(f"max_steps ({self.spec.max_steps}) reached", "call finish")
            if by is not None:
                d = parse_duration(by, "by")
                if d <= 0:
                    raise usage_error("by must be a positive duration")
                target = self.clock_ns + d
            else:
                target = parse_time(to, self.instrument["midnight_utc_ns"], "to")
                if target <= self.clock_ns:
                    raise usage_error(
                        f"to ({local_time(target, self.instrument['midnight_utc_ns'])}) is not after the clock "
                        f"({local_time(self.clock_ns, self.instrument['midnight_utc_ns'])})",
                        "the clock only moves forward",
                    )
            target = min(target, self.end_ns)
            self.clock_ns = target
            self.visible_ns = target - self.spec.feed_latency_ns
            self.step += 1
            self._sync()
            obs = self._observation()
            self.log.append(
                "advance",
                clock_ns=self.clock_ns,
                visible_ns=self.visible_ns,
                rows_seen=self.tape.rows,
                data={"to_ns": target, "observation_sha256": digest(obs.model_dump(mode="json"))},
            )
            return obs

    def _normalize_request(
        self,
        side: Any,
        qty: Any,
        type: Any,
        price: Any,
        peg_offset_ticks: Any,
        tif: Any,
        client_order_id: Any,
    ) -> dict[str, Any]:
        s = str(side).strip().lower() if isinstance(side, str) else side
        sides = {"buy": "buy", "b": "buy", "bid": "buy", "sell": "sell", "s": "sell", "a": "sell", "ask": "sell"}
        if s not in sides:
            raise config_error(f"side: {side!r} is not buy or sell")
        if isinstance(qty, bool) or not isinstance(qty, int | str):
            raise config_error(f"qty: expected an integer size, got {qty!r}")
        q = to_fixed(qty, self.instrument["size_exp"], "qty") if isinstance(qty, str) else int(qty)
        t = str(type).strip().lower()
        if t not in ("limit", "market"):
            raise config_error(f"type: {type!r} is not limit or market")
        f = str(tif).strip().lower()
        if f not in ("day", "ioc"):
            raise config_error(f"tif: {tif!r} is not day or ioc")
        px = None if price is None else to_fixed(price, self.instrument["price_exp"], "price")
        peg = None
        if peg_offset_ticks is not None:
            if isinstance(peg_offset_ticks, bool) or not isinstance(peg_offset_ticks, int):
                raise config_error("peg_offset_ticks: expected an integer")
            peg = int(peg_offset_ticks)
        cid = None
        if client_order_id is not None:
            cid = str(client_order_id)
            if not _CLIENT_ID.fullmatch(cid):
                raise config_error("client_order_id: 1 to 64 characters of [A-Za-z0-9_.:-]")
        return {
            "side": sides[s],
            "qty": q,
            "type": t,
            "tif": "ioc" if t == "market" else f,
            "price": px,
            "peg_offset_ticks": peg,
            "client_order_id": cid,
        }

    def _check_request(self, req: dict[str, Any], new_latency: int) -> tuple[int | None, int | None]:
        """Validation and risk checks of a normalized request (`new_latency` = the new order's latency); returns
        (limit price, price estimate for STP)."""
        i = self.instrument
        tick, lot = i["tick_size"], i["lot_size"]
        qty = req["qty"]
        if qty <= 0:
            raise config_error(f"qty: must be > 0 (got {qty})")
        if lot > 0 and qty % lot:
            raise config_error(f"qty: {qty} is not a multiple of the lot size {lot}")
        if self.spec.max_order_qty is not None and qty > self.spec.max_order_qty:
            raise config_error(
                f"order rejected: qty {qty} exceeds max_order_qty {self.spec.max_order_qty}", reason="max_order_qty"
            )
        open_orders = list(self._open.values())
        if len(open_orders) >= self.spec.max_open_orders:
            raise config_error(
                f"order rejected: {len(open_orders)} open orders (max_open_orders {self.spec.max_open_orders})",
                reason="max_open_orders",
            )
        bid, ask = self.book.best()
        price: int | None = None
        est: int | None = None
        if req["type"] == "market":
            if req["price"] is not None or req["peg_offset_ticks"] is not None:
                raise config_error("a market order takes no price and no peg (it is priced at the collar)")
            opp = ask if req["side"] == "buy" else bid
            if opp is None:
                raise config_error(
                    "order rejected: no opposite quote in view to price the market order", reason="no_quote"
                )
            best = int(opp[0])
            delta = Decimal(abs(best)) * Decimal(repr(self.spec.market_collar_bps)) / Decimal(10_000)
            step = max(tick, 1)
            k = int((delta / step).to_integral_value(rounding=ROUND_CEILING))
            delta_raw = max(1, k) * step
            price = best + delta_raw if req["side"] == "buy" else best - delta_raw
            if req["side"] == "sell" and best > 0 and price <= 0:
                price = step
            est = price
        elif req["peg_offset_ticks"] is not None:
            if req["price"] is not None:
                raise config_error("give price or peg_offset_ticks, not both")
            if req["tif"] == "ioc":
                raise config_error("a pegged order must be a day order (an IOC at the touch never trades)")
            same = bid if req["side"] == "buy" else ask
            if same is not None:
                d = req["peg_offset_ticks"] * tick
                est = same[0] + d if req["side"] == "buy" else same[0] - d
        else:
            if req["price"] is None:
                raise config_error("a limit order needs a price (or peg_offset_ticks)")
            price = req["price"]
            if price == 2**63 - 1 or abs(price) >= 2**62:
                raise config_error(f"price: {price} is out of range")
            if tick > 0 and price % tick:
                raise config_error(
                    f"price: {price} is not on the tick grid (tick {tick} at price_exp {i['price_exp']})",
                    "round it to a multiple of the tick size",
                )
            est = price
        if self.spec.max_position is not None:
            pos = self._acct.qty
            long_open = sum(max(0, o.qty - o.filled_qty) for o in open_orders if o.side == "buy")
            short_open = sum(max(0, o.qty - o.filled_qty) for o in open_orders if o.side == "sell")
            if req["side"] == "buy" and pos + long_open + qty > self.spec.max_position:
                raise config_error(
                    f"order rejected: worst-case long position {pos + long_open + qty} exceeds max_position "
                    f"{self.spec.max_position}",
                    reason="max_position",
                )
            if req["side"] == "sell" and -pos + short_open + qty > self.spec.max_position:
                raise config_error(
                    f"order rejected: worst-case short position {-pos + short_open + qty} exceeds max_position "
                    f"{self.spec.max_position}",
                    reason="max_position",
                )
        if self.spec.self_trade_prevention == "reject_new" and est is not None:
            # An opposite order that is gone by the time the new one arrives cannot trade with it: an IOC that arrives
            # first (or with it: it was sent first) is done on arrival, and a cancel effective by then removed the
            # order. Arrivals are exact here: per-order latency is deterministic (`_order_latency`).
            arrival = self.clock_ns + new_latency
            for o in open_orders:
                if o.side == req["side"]:
                    continue
                if o.aggressive and o.ts_submit + o.latency_ns <= arrival:
                    continue
                if o.cancel_send_ns is not None and o.cancel_send_ns + o.latency_ns <= arrival:
                    continue
                other = o.resolved_price if o.resolved_price is not None else o.est_price
                if other is None:
                    continue
                crosses = est >= other if req["side"] == "buy" else est <= other
                if crosses:
                    raise config_error(
                        f"order rejected: it would cross your own {o.side} order {o.order_id} (self-trade prevention)",
                        "cancel that order first, or open the session with self_trade_prevention='off'",
                        reason="self_trade",
                        order_id=o.order_id,
                    )
        return price, est

    def _reject(self, kind: str, request: dict[str, Any], e: QtError) -> None:
        """Logs a rejected instruction: `request` holds the arguments in the form they were validated in (`wire`),
        so re-executing it from the log meets the same rejection."""
        self.rejected += 1
        self.log.append(
            "reject",
            clock_ns=self.clock_ns,
            visible_ns=self.visible_ns,
            rows_seen=self.tape.rows,
            data={
                "action": kind,
                "request": {k: wire(v) for k, v in request.items()},
                "code": e.code,
                "message": e.message,
            },
        )

    def submit(
        self,
        side: str,
        qty: int | str,
        *,
        type: str = "limit",
        price: int | str | Decimal | None = None,
        peg_offset_ticks: int | None = None,
        tif: str = "day",
        client_order_id: str | None = None,
    ) -> SandboxAck:
        """Sends an order at the current clock. `price` is a decimal string (`"291.65"`, exact at the instrument's
        price_exp) or an integer in fixed-point units; `peg_offset_ticks` prices a day limit order against the
        same-side best at arrival instead (0 joins it, -1 is one tick behind). `type="market"` sends an IOC limit
        order at the collar. A rejected order raises `QtError` (`QT-CONFIG-001` with `details.reason`) and is
        logged; a repeated `client_order_id` with the same request returns the original ack. Arguments are validated in
        their logged form (`_util.wire`: a float stays a float and is refused as a price or size, a `Decimal` becomes
        its string)."""
        with self._lock:
            self._check_open()
            raw = {
                "side": wire(side),
                "qty": wire(qty),
                "type": wire(type),
                "price": wire(price),
                "peg_offset_ticks": wire(peg_offset_ticks),
                "tif": wire(tif),
                "client_order_id": wire(client_order_id),
            }
            try:
                req = self._normalize_request(**raw)
            except QtError as e:
                self._reject("submit", raw, e)
                raise
            cid = req["client_order_id"]
            if cid is not None and cid in self._by_client:
                prior = self._by_client[cid]
                if prior.request != req:
                    e = QtError(
                        "QT-INPUT-003",
                        f"duplicate client_order_id: {cid} was used for a different order ({prior.order_id})",
                    )
                    self._reject("submit", req, e)
                    raise e
                return self._ack("submit", prior, duplicate=True, entry=None)
            order_id = f"o{len(self.orders) + 1}"
            latency = self._order_latency(order_id)
            try:
                price_fixed, est = self._check_request(req, latency)
            except QtError as e:
                self._reject("submit", req, e)
                raise
            mid_x2 = None
            if self.book.h_ts:
                mid_x2 = self.book.mid_x2(len(self.book.h_ts) - 1)
            o = Order(
                order_id=order_id,
                index=len(self.orders),
                client_order_id=cid,
                side=req["side"],
                type=req["type"],
                tif=req["tif"],
                price=price_fixed,
                peg_offset_ticks=req["peg_offset_ticks"],
                orig_qty=req["qty"],
                ts_submit=self.clock_ns,
                latency_ns=latency,
                est_price=est,
                decision_mid_x2=mid_x2,
                request=req,
            )
            self.orders.append(o)
            self._by_id[order_id] = o
            self._open[order_id] = o
            if cid is not None:
                self._by_client[cid] = o
            entry = self.log.append(
                "submit",
                clock_ns=self.clock_ns,
                visible_ns=self.visible_ns,
                rows_seen=self.tape.rows,
                data={
                    "request": req,
                    "order_id": order_id,
                    "price": price_fixed,
                    "latency_ns": o.latency_ns,
                    "decision_mid_x2": mid_x2,
                },
            )
            return self._ack("submit", o, duplicate=False, entry=entry)

    def cancel(self, order_id: str, qty: int | str | None = None) -> SandboxAck:
        """Sends a cancel at the current clock: of the whole order, or of `qty` units (a priority-keeping size
        reduction). It takes effect after the order's latency; fills until then stand."""
        with self._lock:
            self._check_open()
            req = {"order_id": wire(order_id), "qty": wire(qty)}
            try:
                o = self._by_id.get(str(req["order_id"]))
                if o is None:
                    raise usage_error(f"unknown order_id {req['order_id']!r}")
                if o.aggressive:
                    raise usage_error(f"order {o.order_id} is {o.type}/{o.tif}: it is cancelled on arrival already")
                if o.final:
                    raise usage_error(f"order {o.order_id} is already {o.status}: nothing to cancel")
                if o.cancel_send_ns is not None:
                    raise usage_error(f"a cancel of order {o.order_id} was already sent")
                k: int | None = None
                new_qty: int | None = None
                q = req["qty"]
                if q is not None:
                    k = to_fixed(q, self.instrument["size_exp"], "qty") if isinstance(q, str) else q
                    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
                        raise config_error(f"qty: a partial cancel needs a positive size (got {q!r})")
                    lot = self.instrument["lot_size"]
                    if lot > 0 and k % lot:
                        raise config_error(f"qty: {k} is not a multiple of the lot size {lot}")
                    if k >= o.qty:
                        raise config_error(
                            f"qty: cancelling {k} of {o.qty} would leave nothing", "omit qty to cancel the whole order"
                        )
                    new_qty = o.qty - k
            except QtError as e:
                self._reject("cancel", req, e)
                raise
            if new_qty is None:
                o.cancel_send_ns = self.clock_ns
                o.cancel_by = "agent"
            else:
                o.reduces.append((self.clock_ns, new_qty))
                o.qty = new_qty
            entry = self.log.append(
                "cancel",
                clock_ns=self.clock_ns,
                visible_ns=self.visible_ns,
                rows_seen=self.tape.rows,
                data={"order_id": o.order_id, "qty": k, "new_qty": new_qty, "by": "agent"},
            )
            return self._ack("cancel", o, duplicate=False, entry=entry)

    def _ack(self, action: str, o: Order, *, duplicate: bool, entry: dict[str, Any] | None) -> SandboxAck:
        if entry is None:
            entry = next(e for e in self.log.entries if e["kind"] == "submit" and e["data"]["order_id"] == o.order_id)
        warnings = []
        if self.clock_ns >= self.end_ns:
            warnings.append("the clock is at the episode end: this order can only act during the final cancel latency")
        return SandboxAck.model_validate(
            {
                "schema": "qt.sandbox_ack",
                "schema_version": 1,
                "action": action,
                "clock_ns": self.clock_ns,
                "order": self._order_view(o),
                "duplicate": duplicate,
                "log_seq": entry["seq"],
                "log_hash": entry["hash"],
                "warnings": warnings,
            }
        )

    def order_log(self) -> dict[str, Any]:
        """The committed order log so far (`qt.sandbox_order_log` as a dict)."""
        with self._lock:
            return self.log.document().model_dump(mode="json")

    # ---- finish -------------------------------------------------------------------------------------------------

    def finish(self, *, out: str | Path | None = None) -> SandboxScorecard:
        """Cancels every open day order at the current clock, runs one exact replay of all committed orders on the
        original files, checks it against what the session reported, and returns the scorecard (also written to
        `out`, with the order log and the final replay's tables, when given). The session is closed afterwards."""
        with self._lock:
            self._check_open()
            t0 = time.perf_counter()
            finish_ns = self.clock_ns
            for o in self.orders:
                if not o.final and not o.aggressive and o.cancel_send_ns is None:
                    o.cancel_send_ns = finish_ns
                    o.cancel_by = "finish"
                    self.log.append(
                        "cancel",
                        clock_ns=finish_ns,
                        visible_ns=self.visible_ns,
                        rows_seen=self.tape.rows,
                        data={"order_id": o.order_id, "qty": None, "new_qty": None, "by": "finish"},
                    )
            view = certification_view(self.src, self._workdir / "view")
            final = None
            if self.orders:
                final = self._run_replay(view, self.orders, self._workdir / "runs" / "final", bbo="change")
            card = sc.build(self, final, finish_ns=finish_ns)
            self.log.append(
                "finish",
                clock_ns=finish_ns,
                visible_ns=self.visible_ns,
                rows_seen=self.tape.rows,
                data={
                    "finish_ns": finish_ns,
                    "final_sha256": digest({"orders": card["orders"], "fills": card["fills"]}),
                },
            )
            self._timings["total_finish_s"] = time.perf_counter() - t0
            doc = sc.certify(self, card, timings=dict(self._timings))
            if out is not None:
                doc = sc.write_outputs(self, doc, Path(out), final)
            self.closed = True
            if self._finalizer is not None:
                self._finalizer()
            return doc


__all__ = ["RULES", "Sandbox", "normalize_spec", "spec_digest", "spec_id_of"]
