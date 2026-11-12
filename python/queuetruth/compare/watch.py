"""`queuetruth compare --watch`: the comparison again whenever new live records arrive, and the drift since a
baseline window.

Each check:

1. Reads a signature of the live path(s): every file's relative path, size and modification time (a directory is
   walked; a growing file changes size). Unchanged since the last check: nothing is recomputed.
2. Runs `compare` into `--out` and takes one row per paired order: the backtest's and live's fill (the fill-share
   gap is backtest minus live, in percentage points of orders), the P&L gap per order (with `--data`) and the
   backtest-minus-live markout of the first fill at 1 s in ticks of mid (with `--data`).
3. The first check is the baseline window: its orders, aggregated per instrument-day cluster (`queuetruth.compare.
   stats.Agg`), go to the state file `<out>/watch.json` with the entry time of its last order. Every later check
   takes the orders entered after that time as the new window and prints the drift of each gap (new minus
   baseline) with its 95% interval (`queuetruth.compare.stats.drift`).
4. ALERT when an interval excludes zero and the drift is at least its threshold: `alert_pp` percentage points for
   the fill-share gap, `alert_ticks` ticks for the markout gap, `alert_pnl` (currency per order, off by default) for
   the P&L gap.

No network and no notifications: one line per check on stdout, ALERT lines, and the exit code (0 no alert, 1 alert,
2 error), so cron or a CI schedule can act on it. Deleting `watch.json` starts a new baseline.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from queuetruth import units
from queuetruth.compare import stats as cs
from queuetruth.compare.run import _CLUSTER, _TS
from queuetruth.errors import EXIT_ERROR, EXIT_OK, QtError

STATE_SCHEMA = "qt.compare_watch"
STATE_FILE = "watch.json"
#: Exit code of a check that raised an alert (`fit`'s convention: 0 no alert, 1 alert, 2 error).
EXIT_ALERT = 1
#: Default thresholds: the scorer's equivalence margins (2 pp of fill rate, 0.25 ticks of markout).
ALERT_PP = 2.0
ALERT_TICKS = 0.25
#: Default polling interval.
INTERVAL = "60s"

#: (key in the state, label, unit) of each gap the watch follows.
GAPS = (
    ("fill", "fill-share gap", "pp"),
    ("pnl", "P&L gap per order", "ccy"),
    ("markout", "markout gap 1s", "ticks"),
)


def signature(paths: Sequence[str | Path]) -> str:
    """A hash of every live file's relative path, size and modification time (directories walked)."""
    items: list[tuple[str, int, int]] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    st = (Path(root) / f).stat()
                    items.append((str((Path(root) / f).relative_to(p)), st.st_size, st.st_mtime_ns))
        elif p.exists():
            st = p.stat()
            items.append((p.name, st.st_size, st.st_mtime_ns))
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]


@dataclass
class Window:
    """The per-cluster aggregates of one window of paired orders."""

    n_orders: int
    aggs: dict[str, cs.Agg]
    last_entry_ns: int | None

    @classmethod
    def of(cls, rows: list[dict[str, Any]]) -> Window:
        keys = [json.dumps(r.get(_CLUSTER), default=str) for r in rows]

        def filled(r: dict[str, Any], side: str) -> int:
            return int(r["outcome"] in ("both", f"{side}_only"))

        fill = [filled(r, "backtest") - filled(r, "live") for r in rows]
        ts = [r[_TS] for r in rows if r.get(_TS) is not None]
        return cls(
            len(rows),
            {
                "fill": cs.Agg.of(keys, fill),
                "pnl": cs.Agg.of(keys, [r.get("pnl_gap") for r in rows]),
                "markout": cs.Agg.of(keys, [r.get("markout_gap") for r in rows]),
            },
            max(ts) if ts else None,
        )

    def to_doc(self) -> dict[str, Any]:
        return {
            "n_orders": self.n_orders,
            "last_entry_ns": self.last_entry_ns,
            **{k: a.to_doc() for k, a in self.aggs.items()},
        }

    @classmethod
    def from_doc(cls, d: dict[str, Any]) -> Window:
        return cls(int(d["n_orders"]), {k: cs.Agg.from_doc(d.get(k)) for k, _, _ in GAPS}, d.get("last_entry_ns"))


def _num(v: float | None, unit: str, places: int = 2) -> str:
    """A signed gap: percentage points with one decimal, ticks with two, currency with `places` decimals."""
    if v is None:
        return "n/a"
    if unit == "pp":
        return f"{v * 100:+.1f}"
    if unit == "ticks":
        return f"{v:+.2f}"
    return f"{v:+.{places}f}"


def _places(*vs: float | None) -> int:
    """Decimals that give the largest of `vs` four significant digits (at least 2), so an estimate and its interval
    share one precision."""
    m = max((abs(v) for v in vs if v is not None and math.isfinite(v)), default=0.0)
    return 2 if m == 0 else max(2, 3 - math.floor(math.log10(m)))


def _unit(unit: str, ccy: str | None) -> str:
    return {"pp": "pp", "ticks": "ticks"}.get(unit, ccy or "per order (quote)")


def _part(label: str, unit: str, ccy: str | None, est: float | None, lo: float | None, hi: float | None) -> str:
    if est is None:
        return f"{label} n/a" + (" (needs --data)" if unit != "pp" else "")
    k = _places(est, lo, hi)
    ci = f" [{_num(lo, unit, k)}, {_num(hi, unit, k)}]" if lo is not None and hi is not None else ""
    return f"{label} {_num(est, unit, k)} {_unit(unit, ccy)}{ci}"


def check(
    backtest: Any,
    live: Any,
    *,
    out: str | Path,
    alert_pp: float = ALERT_PP,
    alert_ticks: float = ALERT_TICKS,
    alert_pnl: float | None = None,
    now_ns: int | None = None,
    force: bool = False,
    **compare_kw: Any,
) -> tuple[int, list[str], dict[str, Any]]:
    """One check: `(exit code, printed lines, state)`. Recomputes only when the live records changed (or
    `force`); the state is written to `<out>/watch.json`. Raises `QtError` on a comparison error."""
    from queuetruth.compare.run import _compare

    outp = Path(out)
    state_path = outp / STATE_FILE
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise QtError("QT-INPUT-001", f"compare --watch: {state_path} is not JSON ({e})", "delete it") from e
        if state.get("schema") != STATE_SCHEMA:
            raise QtError("QT-INPUT-001", f"compare --watch: {state_path} is not a watch state", "delete it")
    lpaths = [live] if isinstance(live, str | Path) else list(live)
    sig = signature(lpaths)
    now = time.time_ns() if now_ns is None else int(now_ns)
    stamp = units.local_time(now, "UTC", digits=0)
    last = state.get("last") or {}
    if not force and state.get("baseline") and last.get("signature") == sig:
        line = f"{stamp}  no new live records since {units.local_time(last['at_ns'], 'UTC', digits=0)}"
        return (EXIT_ALERT if last.get("alert") else EXIT_OK), [line], state

    res, rows = _compare(backtest, live, out=outp, **compare_kw)
    ccy = (res.doc.get("pnl") or {}).get("currency")
    lines: list[str] = []
    alert = False
    runs = int(state.get("runs") or 0) + 1
    if not state.get("baseline"):
        base = Window.of(rows)
        parts = []
        levels = {}
        for key, label, unit in GAPS:
            lv = cs.mean_ci(base.aggs[key], f"compare/watch/{key}")
            levels[key] = lv
            parts.append(_part(label, unit, ccy, lv["estimate"], lv["ci_lo"], lv["ci_hi"]))
        until = units.local_time(base.last_entry_ns, res.doc.get("tz") or "UTC", digits=0)
        lines.append(
            f"{stamp}  baseline: {base.n_orders} paired orders entered until {until}: " + "; ".join(parts)
            + f" ({levels['fill']['interval']['label']}); later checks report the drift from these"
        )  # fmt: skip
        state = {"schema": STATE_SCHEMA, "schema_version": 1, "baseline": base.to_doc(), "currency": ccy}
        state["baseline"]["at_ns"] = now
        summary: dict[str, Any] = {"baseline": True, "levels": levels}
    else:
        base = Window.from_doc(state["baseline"])
        cut = base.last_entry_ns
        new_rows = [r for r in rows if cut is None or (r.get(_TS) or 0) > cut]
        new = Window.of(new_rows)
        if not new_rows:
            lines.append(f"{stamp}  no new orders since the baseline ({base.n_orders} paired orders)")
            summary = {"baseline": False, "n_new": 0}
        else:
            thresholds = {"fill": alert_pp / 100.0, "markout": alert_ticks, "pnl": alert_pnl}
            parts = []
            drifts: dict[str, Any] = {}
            alerts: list[str] = []
            for key, label, unit in GAPS:
                d = cs.drift(base.aggs[key], new.aggs[key], f"compare/watch/{key}")
                drifts[key] = d
                parts.append(_part(label, unit, ccy, d["drift"], d["ci_lo"], d["ci_hi"]))
                thr = thresholds[key]
                lo, hi = d["ci_lo"], d["ci_hi"]
                if thr is None or d["drift"] is None or lo is None or hi is None:
                    continue
                if (lo > 0 or hi < 0) and abs(d["drift"]) >= thr:
                    flag = {"fill": "--alert-pp", "markout": "--alert-ticks", "pnl": "--alert-pnl"}[key]
                    shown = alert_pp if key == "fill" else thr
                    k = _places(d["drift"], lo, hi)
                    alerts.append(
                        f"ALERT: {label} moved {_num(d['drift'], unit, k)} {_unit(unit, ccy)} since the baseline "
                        f"(95% CI {_num(lo, unit, k)} to {_num(hi, unit, k)}, {d['interval']['label']}), at "
                        f"least {flag} {shown:g}"
                    )
            lines.append(
                f"{stamp}  drift since the baseline: {new.n_orders} new paired orders (baseline {base.n_orders}): "
                + "; ".join(parts) + f" ({drifts['fill']['interval']['label']})"
            )  # fmt: skip
            lines += alerts
            alert = bool(alerts)
            summary = {"baseline": False, "n_new": new.n_orders, "drift": drifts, "alerts": alerts}
    state["runs"] = runs
    state["last"] = {"at_ns": now, "signature": sig, "alert": alert, "lines": lines, **summary}
    state_path.write_text(json.dumps(state, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")
    return (EXIT_ALERT if alert else EXIT_OK), lines, state


def watch(
    backtest: Any,
    live: Any,
    *,
    out: str | Path,
    interval: str | int = INTERVAL,
    once: bool = False,
    max_checks: int | None = None,
    echo: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    **kw: Any,
) -> int:
    """`compare --watch`: checks every `interval` (a duration such as `"5min"`) until interrupted, or once with
    `once`. Returns 0 when no check raised an alert, 1 when one did, 2 when a check failed (`once`; a failing check
    in the loop prints the error and the loop goes on). `max_checks` stops the loop (tests)."""
    try:
        every = units.parse_duration(interval, what="--interval", bare_ns=False)
    except ValueError as e:
        raise QtError("QT-USAGE-001", f"compare --watch: {e}", "e.g. --interval 5min") from e
    if every <= 0:
        raise QtError("QT-USAGE-001", "compare --watch: --interval must be positive")
    alerted = False
    n = 0
    try:
        while True:
            try:
                code, lines, _ = check(backtest, live, out=out, **kw)
            except Exception as e:  # a check that fails (a half-written file) is reported; the loop goes on
                err = e if isinstance(e, QtError) else QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}")
                echo(f"{units.local_time(time.time_ns(), 'UTC', digits=0)}  error: {err}")
                if once:
                    return EXIT_ERROR
                code, lines = EXIT_OK, []
            for line in lines:
                echo(line)
            alerted = alerted or code == EXIT_ALERT
            n += 1
            if once or (max_checks is not None and n >= max_checks):
                return code if once else (EXIT_ALERT if alerted else EXIT_OK)
            sleep(every / 1e9)
    except KeyboardInterrupt:
        return EXIT_ALERT if alerted else EXIT_OK


__all__ = [
    "ALERT_PP",
    "ALERT_TICKS",
    "EXIT_ALERT",
    "STATE_FILE",
    "STATE_SCHEMA",
    "Window",
    "check",
    "signature",
    "watch",
]
