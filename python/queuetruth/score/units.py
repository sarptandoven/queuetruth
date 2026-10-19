"""Fixed-point unit normalisation of live (and external-simulator) logs to the dataset's units (review F4).

A canonical log stores each order's prices as integers at its `price_exp` and its quantities at its `size_exp`; a
dataset stores them at the exponents of its `qt.instruments` rows. The two need not agree (a live log imported with
`size_exp 0` scored on a Tardis dataset with `size_exp -8`), and an engine queue, a 2x2 quantity, a
`queue_ahead_at_entry` or a currency markout mixing them is off by a power of ten. The scorer therefore converts
every log price and quantity to the dataset's units before scheduling: orders `limit_px`, `orig_qty`; events `px`,
`qty`, `leaves_qty`, `cum_qty`; schedule rows `price`, `qty`; amend rows `new_qty`. Scaling to a finer exponent
multiplies exactly; scaling to a coarser one must divide exactly, else `QT-INPUT-004` (unit conversion not exact)
names the instrument, the value and both exponents. An instrument whose dataset rows disagree on the exponents is a
`QT-INPUT-002`. Rows without an instrument id or without exponents are left as they are (a simulator log without
`price_exp`/`size_exp` is taken to be in the target units already). Without a dataset the target is the live
log's own units (the external arm is converted to them).

The live log's exponents come from `queuetruth.live.instrument_scales(live_dir)` (the importer's
`qt.live.instrument_scales` metadata, else derived from the order rows; `live_scales`). An instrument with more than
one `(price_exp, size_exp)` there changed scale within the log (date-dependent instrument-map rows); the scorer
refuses it (`QT-INPUT-002` naming the instrument and each scale with its order count) rather than guess which
orders use which scale.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.score._io import Row, input_format

I64_MAX = 2**63 - 1
U64_MAX = 2**64 - 1
#: Order columns scaled by `price_exp` / `size_exp`.
ORDER_PX = ("limit_px",)
ORDER_QTY = ("orig_qty",)
#: Event columns scaled by `price_exp` / `size_exp`.
EVENT_PX = ("px",)
EVENT_QTY = ("qty", "leaves_qty", "cum_qty")

Exps = tuple[int, int]


def units_error(msg: str) -> QtError:
    """`QT-INPUT-004` (unit conversion not exact)."""
    return QtError(
        "QT-INPUT-004",
        msg,
        "re-import the live log with an instrument map whose price_exp/size_exp match the dataset's instruments",
    )


def rescale(value: Any, exp_from: int, exp_to: int, what: str) -> Any:
    """`value` (an integer at `10^exp_from`) at `10^exp_to`; None stays None. Raises `QT-INPUT-004` when the value is
    not representable exactly or overflows 64 bits."""
    if value is None or exp_from == exp_to:
        return value
    v = int(value)
    k = int(exp_from) - int(exp_to)
    if k > 0:
        out = v * 10**k
    else:
        q, r = divmod(v, 10**-k)
        if r:
            raise units_error(f"{what}: {v} at exponent {exp_from} is not representable at exponent {exp_to}")
        out = q
    if not -I64_MAX - 1 <= out <= U64_MAX:
        raise units_error(f"{what}: {v} at exponent {exp_from} overflows 64 bits at exponent {exp_to}")
    return out


def dataset_exps(instrument_rows: Iterable[Row]) -> dict[int, Exps]:
    """`(price_exp, size_exp)` per instrument of `qt.instruments` rows; `QT-INPUT-002` when one instrument has
    rows with different exponents."""
    out: dict[int, Exps] = {}
    for r in instrument_rows:
        iid = int(r["instrument_id"])
        e = (int(r.get("price_exp", -9)), int(r.get("size_exp", 0) or 0))
        if iid in out and out[iid] != e:
            raise input_format(
                "instruments.parquet",
                f"instrument {iid}: price_exp/size_exp differ across session dates ({out[iid]} vs {e}); score one "
                "exponent regime at a time",
            )
        out[iid] = e
    return out


def scales_by_instrument(entries: Iterable[Row], where: str = "live log") -> dict[int, Exps]:
    """`(price_exp, size_exp)` per `instrument_id` of `instrument_scales` entries (`instrument_id`, `price_exp`,
    `size_exp`, optional `n_orders`); entries without an instrument id are skipped. `QT-INPUT-002` when an instrument
    has more than one scale (date-dependent map rows)."""
    acc: dict[int, dict[Exps, int]] = {}
    for e in entries:
        iid, pe, se = e.get("instrument_id"), e.get("price_exp"), e.get("size_exp")
        if iid is None or pe is None or se is None:
            continue
        d = acc.setdefault(int(iid), {})
        d[(int(pe), int(se))] = d.get((int(pe), int(se)), 0) + int(e.get("n_orders") or 1)
    bad = {iid: v for iid, v in acc.items() if len(v) > 1}
    if bad:
        iid = min(bad)
        parts = ", ".join(f"price_exp {pe} / size_exp {se} on {n} order(s)" for (pe, se), n in sorted(bad[iid].items()))
        raise QtError(
            "QT-INPUT-002",
            f"{where}: instrument {iid} changes scale within the log ({parts}); the scorer needs one scale per "
            "instrument",
            "split the live log at the scale change (score each period separately) or re-import it with one "
            "instrument-map row per instrument",
        )
    return {iid: next(iter(v)) for iid, v in acc.items()}


def live_scales(live_dir: str | Path, orders: list[Row]) -> dict[int, Exps]:
    """Per-instrument exponents of a live log from `queuetruth.live.instrument_scales` (a log the live package
    cannot read, e.g. a minimal simulator-style log, falls back to its order rows); see `scales_by_instrument`."""
    try:
        from queuetruth.live import instrument_scales

        entries = instrument_scales(live_dir)
    except QtError:
        entries = [
            {"instrument_id": o.get("instrument_id"), "price_exp": o.get("price_exp"), "size_exp": o.get("size_exp")}
            for o in orders
        ]
    return scales_by_instrument(entries, str(live_dir))


def _order_exps(o: Row) -> Exps | None:
    pe, se = o.get("price_exp"), o.get("size_exp")
    if pe is None or se is None:
        return None
    return int(pe), int(se)


class UnitMap:
    """Per order (`order_key`) the source exponents and the target exponents of its instrument."""

    def __init__(self, target: dict[int, Exps]) -> None:
        self.target = dict(target)
        self.by_key: dict[str, tuple[int, Exps, Exps]] = {}
        self.changed: dict[int, tuple[Exps, Exps]] = {}

    def add_orders(
        self,
        orders: list[Row],
        fallback_iid: dict[str, int] | None = None,
        source: dict[int, Exps] | None = None,
    ) -> None:
        """Registers the orders of a log (`instrument_id`, `price_exp`, `size_exp`); `fallback_iid` gives the
        instrument of an order without one (a simulator log matched to the live log by `order_key`); `source` gives
        the log's exponents per instrument (`live_scales`), which take precedence over the row's."""
        for o in orders:
            key = str(o["order_key"])
            iid = o.get("instrument_id")
            if iid is None and fallback_iid is not None:
                iid = fallback_iid.get(key)
            src = source.get(int(iid)) if source is not None and iid is not None else None
            src = src or _order_exps(o)
            if iid is None or src is None:
                continue
            tgt = self.target.get(int(iid))
            if tgt is None or tgt == src:
                continue
            self.by_key[key] = (int(iid), src, tgt)
            self.changed.setdefault(int(iid), (src, tgt))

    def _conv(self, key: str, r: Row, px_cols: tuple[str, ...], qty_cols: tuple[str, ...], where: str) -> Row:
        hit = self.by_key.get(key)
        if hit is None:
            return r
        iid, (spe, sse), (tpe, tse) = hit
        out = dict(r)
        for c in px_cols:
            if c in out:
                out[c] = rescale(out[c], spe, tpe, f"instrument {iid} {where}.{c} (order {key})")
        for c in qty_cols:
            if c in out:
                out[c] = rescale(out[c], sse, tse, f"instrument {iid} {where}.{c} (order {key})")
        return out

    def orders(self, rows: list[Row]) -> list[Row]:
        """Order rows in target units (`price_exp` / `size_exp` set to the target's)."""
        out = []
        for o in rows:
            r = self._conv(str(o["order_key"]), o, ORDER_PX, ORDER_QTY, "orders")
            hit = self.by_key.get(str(o["order_key"]))
            if hit is not None:
                r["price_exp"], r["size_exp"] = hit[2]
            out.append(r)
        return out

    def events(self, rows: list[Row]) -> list[Row]:
        """Event rows in target units."""
        return [self._conv(str(e["order_key"]), e, EVENT_PX, EVENT_QTY, "events") for e in rows]

    def schedule(self, rows: list[Row]) -> list[Row]:
        """`qt.shadow_spec` rows in target units (keyed by `order_key`, else `shadow_id`)."""
        return [
            self._conv(str(r.get("order_key") or r["shadow_id"]), r, ("price",), ("qty",), "schedule") for r in rows
        ]

    def amends(self, rows: list[Row], schedule: list[Row]) -> list[Row]:
        """`qt.shadow_amend` rows in target units (the leg's `order_key` from `schedule`)."""
        owner = {str(r["shadow_id"]): str(r.get("order_key") or r["shadow_id"]) for r in schedule}
        return [
            self._conv(owner.get(str(a["shadow_id"]), str(a["shadow_id"])), a, (), ("new_qty",), "amends") for a in rows
        ]

    def notes(self, label: str) -> list[str]:
        """One warning line per converted instrument."""
        return [
            f"units: {label} instrument {iid} price_exp {s[0]} -> {t[0]}, size_exp {s[1]} -> {t[1]} (converted to the "
            "dataset's units)"
            for iid, (s, t) in sorted(self.changed.items())
        ]
