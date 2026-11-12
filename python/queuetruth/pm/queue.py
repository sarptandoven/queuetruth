"""Queue-position models on a prediction-market L2 series, run in one pass over the batches.

The engine replays one instrument's `qt.l2` rows (normally `book_own_free/`) and moves every live order's queue
estimate under several models at once. It runs in Rust (`qt_zoo::pm`, called through `queuetruth._core`); the
families are the registry `qt_zoo::pm::PM_FAMILIES` (`_core.pm_families()`), a sibling of the zoo's `FamilyDef`
table. Its dispatch is the corrected mode of the Rust zoo (SPEC 5.3.2 and 5.3.3, `crates/zoo/src/sim.rs` and
`pair.rs`), so the same orders on the same dataset give the same fills as `queuetruth zoo --mode corrected`:

- Activation: a time-entered order goes live at the first batch with `ts_event > ts_live`, with `F` = the visible
  size at its price before that batch; an order with a known entry point (`seq`, `inside`) goes live at the first
  batch after it. On arrival into a book that crosses its price an order fills as a taker up to the visible opposite
  size at or through its price (`front_of_queue` and the queue families; never `trade_through_only`); a post-only
  order is rejected instead.
- Rows in batch order: a trade at the order's price hitting its side (`F -= v`, then a fill of whatever volume went
  beyond the queue, in whole lots), a trade through its price (fills the leaves), a depth row at its level (the
  family's update), a `clear` (stops the order, censored). At the end of every batch that touched the order, a book
  that crosses its price fills it as a maker for at most the visible opposite size not yet taken; the trade netting
  `T` is reset. A cancel or the end of the order's life stops it at the first batch after its time, or after its
  placed position (`end_seq`) when the user's own log placed it in the published series.
- Families (corrected mode): `front_of_queue` (no queue ahead; fills on every at-price print; the optimistic fill
  bound), `trade_through_only` (fills only on a trade through its price; the pessimistic fill bound),
  `risk_adverse` (= `hbt_risk_adverse`: only prints shrink the queue; the upper queue bound), `cancels_ahead` (every
  decrease beyond prints leaves the queue ahead first; the lower queue bound) and the hbt probability families
  `power[n]`, `power2[n]`, `power3[n]`, `log`, `log2` (`pi` = probability that a decrease is behind the order).
  `power3[n]` spans the bounds: `n -> 0` is `cancels_ahead`, `n -> infinity` is `risk_adverse`.
- `atomic` variants (`power3[n=1;atomic]`, `log[atomic]`, ...) read a decrease beyond the prints as one order
  leaving, the reading of a feed that publishes every exchange action on its own (Kalshi: deltas that share a
  `clock` are one action): the order could only have been ahead if its size fits in the queue ahead, and only
  behind if it fits in the size behind. When exactly one of the two is possible the decrease is placed there;
  otherwise the family splits it as usual. A change that nets several orders breaks the reading, so the variants
  are estimates to calibrate, not bounds.

The queue bounds hold on an unconflated FIFO feed: every visible change is one order's action, prints consume the
level from the front, adds join behind. A change that nets an add against a cancel ahead can push the true queue
below `cancels_ahead`; Kalshi's official queue positions measure how often (`pm calibrate` bound coverage).

Day-boundary restatements (a `resync` identical to the book) are skipped, so orders live across UTC midnight; a
resync that changes the book (after a feed gap) stops every live order, as do the feed gaps of `data_quality.json`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import numpy as np

from queuetruth import _core
from queuetruth.errors import QtError
from queuetruth.pm.book import ASK, BID, Ladder  # noqa: F401  (importable from here before the Rust port)
from queuetruth.pm.dataset import KINDS, L2Arrays

EPS = 1e-9
K_DEPTH, K_TRADE, K_SNAPSHOT, K_CLEAR = (KINDS.index(k) for k in ("depth", "trade", "snapshot", "clear"))


@cache
def families() -> tuple[dict[str, Any], ...]:
    """The prediction-market family registry (`qt_zoo::pm::PM_FAMILIES`), one dict per family in canonical order."""
    return tuple(json.loads(_core.pm_families()))


#: Families that split a decrease with a probability `pi` (`cancels_ahead`: `pi` = 0).
PROB_FAMILIES = tuple(d["name"] for d in families() if d["prob"])
#: Families that take the one-order-per-change variant (`atomic`): the hbt probability families, not the bounds.
ATOMIC_FAMILIES = tuple(d["name"] for d in families() if d["atomic"])
BOUND_FAMILIES = tuple(d["name"] for d in families() if d["role"] == "bound")
QUEUE_FAMILIES = tuple(d["name"] for d in families() if d["role"] == "queue")
ALL_FAMILIES = tuple(d["name"] for d in families())
#: Families with a parameter `n`.
_N_FAMILIES = tuple(d["name"] for d in families() if d["param_keys"] == ["n"])
_ID = re.compile(r"^([a-z_0-9]+?)(?:\[(.*)\])?$")


@dataclass(frozen=True)
class Model:
    """One model: family, parameter `n` (power families) and the one-order-per-change variant (`atomic`)."""

    family: str
    n: float | None = None
    atomic: bool = False

    def __post_init__(self) -> None:
        if self.family not in ALL_FAMILIES:
            raise QtError("QT-CONFIG-001", f"model family {self.family!r}: expected one of {list(ALL_FAMILIES)}")
        needs_n = self.family in _N_FAMILIES
        if needs_n and (self.n is None or not math.isfinite(self.n) or self.n <= 0):
            raise QtError("QT-CONFIG-001", f"model {self.family} needs a positive finite parameter n")
        if not needs_n and self.n is not None:
            raise QtError("QT-CONFIG-001", f"model {self.family} takes no parameter")
        if self.atomic and self.family not in ATOMIC_FAMILIES:
            raise QtError("QT-CONFIG-001", f"model {self.family}: only {list(ATOMIC_FAMILIES)} take `atomic`")

    @property
    def id(self) -> str:
        """Model id (`power3[n=1.5]`, `power3[n=1.5;atomic]`, `log[atomic]`, `risk_adverse`, ...)."""
        parts = [] if self.n is None else [f"n={_num(self.n)}"]
        if self.atomic:
            parts.append("atomic")
        return f"{self.family}[{';'.join(parts)}]" if parts else self.family

    @property
    def zoo_id(self) -> str | None:
        """The same model in the zoo's canonical spelling (`hbt_power3[exch=partial;mode=corrected;n=1.5]`), which
        `queuetruth zoo --models` and `calibrate-live` take; None when the zoo has no such model (`cancels_ahead`,
        the atomic variants)."""
        if self.atomic or self.family == "cancels_ahead":
            return None
        if self.family in BOUND_FAMILIES:
            return f"{self.family}[mode=corrected]"
        keys = ["exch=partial", "mode=corrected"] + ([] if self.n is None else [f"n={_num(self.n)}"])
        return f"hbt_{self.family}[{';'.join(keys)}]"

    @classmethod
    def parse(cls, text: str) -> Model:
        """`power3[n=2]`, `power3[n=2;atomic]`, `hbt_power3[exch=partial;mode=corrected;n=2]` (the zoo spelling),
        `risk_adverse`, ... The engine runs the zoo's corrected mode with partial fills: another `mode` or `exch` is
        `QT-CONFIG-001`."""
        m = _ID.match(text.strip())
        if not m:
            raise QtError("QT-CONFIG-001", f"model id {text!r} is not family[key=value;...]")
        fam = m.group(1)
        if fam.startswith("hbt_"):
            fam = fam[4:]
        params = {}
        for kv in (m.group(2) or "").split(";"):
            if not kv.strip():
                continue
            k, _, v = kv.partition("=")
            params[k.strip()] = v.strip()
        mode = params.pop("mode", "corrected")
        exch = params.pop("exch", None)
        if mode != "corrected":
            raise QtError("QT-CONFIG-001", f"model {text!r}: the prediction-market engine runs corrected mode only")
        if exch is not None and (fam in (*BOUND_FAMILIES, "cancels_ahead") or exch != "partial"):
            raise QtError(
                "QT-CONFIG-001",
                f"model {text!r}: exch={exch} is not supported (the engine fills partially, the zoo's exch=partial, "
                "which only the hbt families name)",
            )
        n = params.pop("n", None)
        atomic = params.pop("atomic", None)
        if atomic not in (None, "", "true", "1"):
            raise QtError("QT-CONFIG-001", f"model {text!r}: atomic takes no value (or true)")
        if params:
            raise QtError("QT-CONFIG-001", f"model {text!r}: unknown parameters {sorted(params)}")
        try:
            return cls(fam, float(n) if n is not None else None, atomic is not None)
        except ValueError:
            raise QtError("QT-CONFIG-001", f"model {text!r}: n must be a number") from None


@dataclass
class SimOrder:
    """One order to simulate. Entry by time (`ts_live`, Rust-compatible) or by position (`entry_seq`, `inside`:
    joined during that published batch, else just before it). `amends` are priority-keeping reductions `(ts, new
    total qty)`; `ts_end` stops the order at the first batch after it, or, with `end_seq`, at the first batch after
    that position (`end_inside`: it left during batch `end_seq`, else just before it), `ts_end` then being the
    reported end time."""

    key: str
    side: int
    price: int
    qty: float
    ts_live: int | None = None
    entry_seq: int | None = None
    inside: bool = False
    ts_end: int | None = None
    end_seq: int | None = None
    end_inside: bool = False
    post_only: bool = False
    amends: list[tuple[int, float]] = field(default_factory=list)
    samples: list[int] = field(default_factory=list)  # local times at which to record the queue estimate
    probes: list[int] = field(default_factory=list)  # published batch seqs: record the estimate just before them


@dataclass
class OrderResult:
    """Per order: fills per model `(ts, qty, reason, liquidity, price)`, end state, activation, recorded estimates."""

    key: str
    activated_ts: int | None = None
    activated_seq: int | None = None
    visible_at_live: float | None = None
    end: str = "pending"
    end_ts: int | None = None
    fills: list[list[tuple[int, float, str, str, float]]] = field(default_factory=list)
    samples: list[tuple[int, np.ndarray]] = field(default_factory=list)  # (local time, F per model)
    probes: list[tuple[int, np.ndarray]] = field(default_factory=list)  # (seq, F per model before that batch)
    samples_before_live: int = 0
    samples_after_end: int = 0


class Engine:
    """Corrected-mode queue models over one instrument's L2 rows, every model at once (Rust, `qt_zoo::pm::run`)."""

    def __init__(self, models: Sequence[Model], lot: float = 1.0) -> None:
        if not models:
            raise QtError("QT-CONFIG-001", "no models to run")
        self.models = list(models)
        self.k = len(self.models)
        self.lot = float(lot)

    def run(
        self,
        rows: L2Arrays,
        orders: Sequence[SimOrder],
        *,
        gaps: Sequence[tuple[int, int]] = (),
        end_ts: int | None = None,
    ) -> list[OrderResult]:
        """Simulates `orders` over `rows` (one instrument) and returns one result per order (same order).

        Queue estimates are recorded at each order's `samples` (local times, sorted): a sample sees the state after
        every batch whose running maximum `ts_recv` is at or before it. Samples before the order went live in the
        replay are counted in `samples_before_live`, those after it ended in `samples_after_end`."""
        cols = [
            np.ascontiguousarray(getattr(rows, c), dtype=np.int64).tobytes()
            for c in (
                "ts_event",
                "ts_recv",
                "event_id",
                "sequence",
                "kind",
                "side",
                "price",
                "new_qty",
                "trade_qty",
                "flags",
                "part",
            )
        ]
        models = [(m.family, None if m.n is None else float(m.n), bool(m.atomic)) for m in self.models]
        try:
            out = _core.pm_queue_run(
                models,
                self.lot,
                cols,
                list(orders),
                [(int(a), int(b)) for a, b in gaps],
                None if end_ts is None else int(end_ts),
            )
        except _core.QtCoreError as e:
            raise QtError(str(e.args[0]), str(e.args[1])) from None
        results = []
        for o, (a_ts, a_seq, vis, end, e_ts, fills, samples, probes, before, after) in zip(orders, out, strict=True):
            results.append(
                OrderResult(
                    o.key,
                    activated_ts=a_ts,
                    activated_seq=a_seq,
                    visible_at_live=vis,
                    end=end,
                    end_ts=e_ts,
                    fills=[list(f) for f in fills],
                    samples=[(t, np.array(v, dtype=np.float64)) for t, v in samples],
                    probes=[(q, np.array(v, dtype=np.float64)) for q, v in probes],
                    samples_before_live=before,
                    samples_after_end=after,
                )
            )
        return results


def _num(x: float) -> str:
    """Shortest round-trip decimal without an exponent (the zoo's Rust `{}` of an f64)."""
    return str(np.format_float_positional(x, unique=True, trim="-"))


def _batch_starts(rows: L2Arrays) -> list[int]:
    """Row index where each batch starts (a new `event_id` or partition, or after a `LAST_IN_BATCH` row)."""
    n = len(rows)
    starts = [0]
    for r in range(1, n):
        if rows.part[r] != rows.part[r - 1] or rows.event_id[r] != rows.event_id[r - 1] or (rows.flags[r - 1] & 1) != 0:
            starts.append(r)
    return starts


__all__ = [
    "ALL_FAMILIES",
    "ATOMIC_FAMILIES",
    "BOUND_FAMILIES",
    "QUEUE_FAMILIES",
    "Engine",
    "Model",
    "OrderResult",
    "SimOrder",
]
