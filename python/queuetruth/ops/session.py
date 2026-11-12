"""`Session`: an interactive replay that can also take shadow orders."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.ops._call import _core, _dump, _qt_error, _validate
from queuetruth.ops.replay import _expand_order_ids
from queuetruth.schemas import specs as s
from queuetruth.schemas.arrow import SCHEMAS


class Session:
    """Interactive replay session: `.advance_to(ts)`, `.bbo(inst)`, `.depth(inst, n)`, `.queue_ahead(...)`.

    `bbo` returns `{"bid": (price, qty, count) | None, "ask": ...}` (None before the instrument's first record);
    `depth` returns `(bids, asks)` lists of `(price, qty, count)`, best first; `queue_ahead(inst, side, px)` returns
    the displayed `(qty, count)` at that price (side `"B"` or `"A"`)."""

    def __init__(self, dataset: str, venue: str | None = None) -> None:
        core = _core()
        try:
            self._s = core.Session(str(dataset), venue)
        except core.QtCoreError as e:
            raise _qt_error(e) from None

    def _wrap(self, fn: Callable[..., Any], *args: Any) -> Any:
        core = _core()
        try:
            return fn(*args)
        except core.QtCoreError as e:
            raise _qt_error(e) from None

    def advance_to(self, ts: int) -> None:
        """Replays every record with `ts_event <= ts` (GIL released)."""
        self._wrap(self._s.advance_to, int(ts))

    def bbo(self, instrument_id: int) -> dict[str, tuple[int, int, int] | None] | None:
        """Best bid and ask."""
        return self._wrap(self._s.bbo, int(instrument_id))

    def depth(self, instrument_id: int, n: int) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
        """Top `n` levels per side."""
        return self._wrap(self._s.depth, int(instrument_id), int(n))

    def queue_ahead(self, instrument_id: int, side: str, price: int) -> tuple[int, int] | None:
        """Displayed quantity and order count at a price."""
        return self._wrap(self._s.queue_ahead, int(instrument_id), str(side), int(price))

    # ---- shadow orders (sessions opened with `with_orders`) ----------------------------------------------------

    @classmethod
    def with_orders(cls, spec: s.ReplaySpec | dict[str, Any]) -> Session:
        """A session that also takes shadow orders while it runs. Every partition replays as `replay` would replay
        it with this spec (latency model and seed, fill rule, hidden-print policy, exclusions, the dataset's feed
        gaps and trading halts); `shadows` and `amends` stay unset, orders come from `submit`, `cancel` and
        `amend`, each at or after `position`. The session reads the data once however many orders it takes, and its
        fills and results equal those of one `replay` with every order in its input."""
        core = _core()
        sp = _validate(s.ReplaySpec, _expand_order_ids(spec))
        self = cls.__new__(cls)
        try:
            self._s = core.Session.from_spec(json.dumps(_dump(sp)))
        except core.QtCoreError as e:
            raise _qt_error(e) from None
        return self

    @property
    def position(self) -> int:
        """Every record with `ts_event` at or before this was applied, except after `advance_events_to` the records of
        an event not complete by then, held until it completes or the next `advance_to`."""
        return int(self._s.position)

    def advance_events_to(self, ts: int) -> None:
        """Replays whole events up to `ts`: in each partition, the complete events (runs ending with `F_LAST`) whose
        records are all at or before `ts`, read in feed order up to the first record past it; the records of an
        event not complete by then wait for a later call. When the data ends with every remaining record at or
        before `ts`, an unterminated tail is applied and ended as the end of a `replay` ends it (orders are still
        taken until `finish`). This is the cut of the agent sandbox's tape, so the state after a call is that of a
        `replay` of exactly that prefix (GIL released). `position` becomes `ts`; the held records of an incomplete
        event are applied first by the next `advance_to`, whatever its time."""
        self._wrap(self._s.advance_events_to, int(ts))

    def shadow_states(self, shadow_ids: list[str]) -> list[dict[str, Any]]:
        """Each shadow as a `replay` ending now would report it, one dict per id: `status` (`pending`, `live` or
        `done`); the `qt.shadow_results` columns its state determines (`price` as resolved, `ts_live`,
        `ts_activated_event`, `ts_cancel_effective`, `ts_first_fill`, `ts_last_fill`, `ts_full_fill`, `filled_qty`,
        `n_fills`, `out_of_session`, ...) with `end_state` None until it is done; `ts_end`, when it ended; and
        `queue_ahead` / `rank` after the last processed event once it went live (the values a `queue_trajectory`
        row would show; sessions keep no trajectory)."""
        return list(self._wrap(self._s.shadow_states, [str(i) for i in shadow_ids]))

    def submit(self, shadows: Any) -> None:
        """Sends shadows, each with `ts_submit` at or after `position`: a `qt.shadow_spec` table (any Arrow stream)
        or a list of row dicts (`shadow_id`, `instrument_id`, `side` `"B"` or `"A"`, `qty`, `ts_submit` in ns, and
        `price` for an absolute price or `offset_ticks` for a touch-relative one; optional `latency_ns`, `group`,
        `order_key`). All or none: when any row is refused (a duplicate id, a `ts_submit` before `position`, an
        instrument the dataset has no data for), none of them is sent. A row of a known instrument that no partition
        covers (a time outside its days) is taken and ends `censored_gap` at its `ts_live` without being replayed, as
        in `replay`. A cancel or amend is sent later with `cancel` / `amend`."""
        if isinstance(shadows, list):
            shadows = _shadow_rows_table(shadows)
        self._wrap(self._s.submit, shadows)

    def cancel(self, shadow_id: str, ts: int) -> None:
        """Sends a cancel at `ts` (at or after `position`); it takes effect after the cancel latency."""
        self._wrap(self._s.cancel, str(shadow_id), int(ts))

    def amend(self, shadow_id: str, ts: int, new_qty: int) -> None:
        """Sends a priority-keeping reduction to `new_qty` at `ts` (at or after `position`)."""
        self._wrap(self._s.amend, str(shadow_id), int(ts), int(new_qty))

    def take_fills(self) -> pa.Table:
        """Shadow fills since the last call (`qt.shadow_fills`); if building the table fails, they stay for the next."""
        return self._wrap(self._s.take_fills)

    def take_results(self) -> pa.Table:
        """Results of the shadows that ended since the last call (`qt.shadow_results`)."""
        return self._wrap(self._s.take_results)

    def finish(self) -> None:
        """Replays to the end of the data; the shadows still open end as at the end of a `replay`."""
        self._wrap(self._s.finish)


def _shadow_rows_table(rows: list[dict[str, Any]]) -> pa.Table:
    """`qt.shadow_spec` rows from plain dicts (`Session.submit`)."""
    out = []
    for r in rows:
        side = str(r["side"]).upper()[:1]
        if side not in ("B", "A"):
            raise QtError("QT-INPUT-002", f"shadow {r.get('shadow_id')}: side must be 'B' or 'A', got {r['side']!r}")
        touch = r.get("price") is None
        out.append(
            {
                "shadow_id": str(r["shadow_id"]),
                "instrument_id": int(r["instrument_id"]),
                "side": ord(side),
                "price_mode": "touch" if touch else "abs",
                "price": None if touch else int(r["price"]),
                "offset_ticks": int(r.get("offset_ticks") or 0),
                "qty": int(r["qty"]),
                "ts_submit": int(r["ts_submit"]),
                "latency_ns": int(r.get("latency_ns") or 0),
                "ts_cancel": None,
                "group": r.get("group"),
                "source": "user",
                "order_key": r.get("order_key"),
            }
        )
    return pa.Table.from_pylist(out, schema=SCHEMAS["qt.shadow_spec"])
