"""Score an hftbacktest backtest against exact-queue truth: `prepare`, `run` (or run the script yourself), `score`.

Everything here wraps queuetruth's own commands:

1. `prepare(data, orders, out)` replays `orders` (a `qt.shadow_spec` table, for example from
   `queuetruth shadows sample` or `shadows from-live`) with the exact FIFO engine (`queuetruth.replay`). Touch-mode
   orders get the price the engine resolved at entry; orders the engine rejects (a touch-relative price that would
   cross on arrival, or no touch to price against) have no price to give hftbacktest and are left out, as are orders
   with no life (cancelled at or before entry) and later legs of one live order. The remaining orders
   become an absolute-price schedule (`schedule.parquet`, `order_key` = `qthbt:qthbt:<shadow_id>`), which is
   replayed once more with the exact engine (`exact/`, the truth `score` compares with). `prepare` then writes one
   hftbacktest event file per instrument-day (`queuetruth export l2-npz`, the L2 view the zoo and the calibration
   use) and the hftbacktest replay script of that schedule (`queuetruth export replay-schedule --target
   hftbacktest`, `hbt_replay.py`).
2. `run(prepared, python=..., snippet=...)` runs `hbt_replay.py` in the venv that has hftbacktest, with your
   `BacktestAsset` settings (a file or code defining `asset`, e.g. a `queuetruth export hftbacktest` snippet or the
   code `calibrate` returns). It writes hftbacktest's order states (`hbt_orders.jsonl`) and the replay summary
   (`hbt_orders.jsonl.summary.json`).
3. `score(prepared, hbt_orders)` imports hftbacktest's order states (`queuetruth.live.import_sim("hftbacktest")`),
   referees them order by order against the exact replay (`queuetruth.referee`) and compares the fill rates per
   horizon (exact first fill within h against hftbacktest's, paired difference with a cluster-robust 95% CI and an
   exact McNemar p, `queuetruth.stats.clustered.clustered_mcnemar`; clusters are instrument x session date x 30
   minutes, as in `integrations/hftbacktest/reconcile.py`). An order counts at horizon h only when its planned end
   (`qt.shadow_results.ts_planned_end`) is at or after its entry plus h.

hftbacktest records fills at the end of each `--step-ns` step of the replay (10 ms by default), so first-fill times
are compared with a tolerance of one step (`ttf_tolerance_ns`).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from queuetruth_hftbacktest.errors import QtHbtError

PREPARED_SCHEMA = "qthbt.prepared"
SCORE_SCHEMA = "qthbt.score"
SCHEMA_VERSION = 1
#: Default horizons of the fill-rate comparison: 1 s, 10 s, 60 s, 5 min.
HORIZONS_NS = (1_000_000_000, 10_000_000_000, 60_000_000_000, 300_000_000_000)
#: Default replay step while an order is open (queuetruth's `export replay-schedule` default).
STEP_NS = 10_000_000
#: Cluster width of the cluster-robust CI (30 minutes, as reconcile.py and the scorecard).
BUCKET_NS = 1_800_000_000_000
REJECTED = ("rejected_would_cross", "rejected_no_touch")
ACCOUNT = "qthbt"
KEY_PREFIX = f"{ACCOUNT}:{ACCOUNT}:"


def _rows(path: Path) -> list[dict[str, Any]]:
    """Rows of a queuetruth Parquet table (`queuetruth.read_table`), timestamps as int ns."""
    import pyarrow as pa
    import queuetruth as qt

    t = qt.read_table(str(path))
    cols = {}
    for name in t.column_names:
        col = t.column(name)
        if pa.types.is_timestamp(col.type):
            col = col.cast(pa.int64())
        cols[name] = col.to_pylist()
    return [{k: v[i] for k, v in cols.items()} for i in range(t.num_rows)]


def _need(path: Path, what: str) -> Path:
    if not path.exists():
        raise QtHbtError("QT-INPUT-001", f"{what}: {path} does not exist")
    return path


def order_key(shadow_id: str) -> str:
    """The order key of a scheduled order: `<venue>:<account>:<id>`, which the hftbacktest importer keeps as is."""
    return f"{KEY_PREFIX}{shadow_id}"


@dataclass(frozen=True)
class Prepared:
    """What `prepare` wrote (`prepared.json` in `out`)."""

    out: Path
    data: Path
    orders: Path
    schedule: Path
    exact: Path
    script: Path
    npz: dict[int, list[Path]]
    n_orders: int
    skipped: dict[str, int]
    step_ns: int

    @classmethod
    def load(cls, path: str | Path) -> Prepared:
        """A `prepare` output directory (or its `prepared.json`)."""
        p = Path(path)
        f = p / "prepared.json" if p.is_dir() else p
        try:
            doc = json.loads(_need(f, "prepared").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise QtHbtError("QT-INPUT-001", f"{f}: {e}") from e
        if not isinstance(doc, dict) or doc.get("schema") != PREPARED_SCHEMA:
            raise QtHbtError("QT-INPUT-002", f"{f}: not a {PREPARED_SCHEMA} document", "run `prepare` first")
        base = f.parent.resolve()
        return cls(
            out=base,
            data=Path(doc["data"]),
            orders=Path(doc["orders"]),
            schedule=base / doc["schedule"],
            exact=base / doc["exact"],
            script=base / doc["script"],
            npz={int(k): [base / x for x in v] for k, v in doc["npz"].items()},
            n_orders=int(doc["n_orders"]),
            skipped={str(k): int(v) for k, v in doc["skipped"].items()},
            step_ns=int(doc["step_ns"]),
        )

    def command(
        self, python: str = "python", *, snippet: str | Path | None = None, out: str | Path | None = None
    ) -> list[str]:
        """The command line that runs the replay in the hftbacktest venv `python`."""
        cmd = [str(python), str(self.script)]
        for iid, paths in sorted(self.npz.items()):
            cmd += ["--data", f"{iid}={','.join(str(p) for p in paths)}"]
        if snippet is not None:
            cmd += ["--snippet", str(snippet)]
        cmd += ["--step-ns", str(self.step_ns), "--out", str(out if out is not None else self.out / "hbt_orders.jsonl")]
        return cmd


def _instrument_days(data: Path) -> dict[int, list[tuple[str, int, int]]]:
    """instrument_id -> [(session_date, open_utc_ns, close_utc_ns)] from the dataset's `instruments.parquet`."""
    out: dict[int, list[tuple[str, int, int]]] = {}
    for r in _instruments(data):
        lo = int(r["midnight_utc_ns"]) + int(r["open_s"]) * 1_000_000_000
        hi = int(r["midnight_utc_ns"]) + int(r["close_s"]) * 1_000_000_000
        out.setdefault(int(r["instrument_id"]), []).append((_session_date(r), lo, hi))
    return {k: sorted(v) for k, v in out.items()}


def _instruments(data: Path) -> list[dict[str, Any]]:
    rows = _rows(_need(data / "instruments.parquet", "data"))
    return sorted(rows, key=lambda r: (int(r["instrument_id"]), _session_date(r)))


def _session_date(r: dict[str, Any]) -> str:
    sd = r["session_date"]
    return sd.isoformat() if hasattr(sd, "isoformat") else _date_text(int(sd))


def _date_text(days: int) -> str:
    import datetime as dt

    return (dt.date(1970, 1, 1) + dt.timedelta(days=int(days))).isoformat()


def _session_of(days: list[tuple[str, int, int]], ts: int) -> str | None:
    for date, lo, hi in days:
        if lo <= ts <= hi:
            return date
    return None


def prepare(
    data: str | Path,
    orders: str | Path,
    out: str | Path,
    *,
    step_ns: int = STEP_NS,
    window_ns: int | None = None,
) -> Prepared:
    """Exact truth, hftbacktest event files and the hftbacktest replay script for `orders` on `data` (module doc).

    `window_ns` is the replay's safety cancel of orders still open that long after entry (queuetruth's default, 300
    s, when None); orders with an earlier planned cancel are cancelled at that time."""
    import pyarrow as pa
    import queuetruth as qt
    from queuetruth.api import export_replay_schedule
    from queuetruth.schemas.arrow import SCHEMAS
    from queuetruth.schemas.io import write_table

    data, orders, out = Path(data), Path(orders), Path(out)
    _need(data / "manifest.json", "data (a queuetruth dataset directory)")
    _need(orders, "orders")
    if step_ns < 1:
        raise QtHbtError("QT-CONFIG-001", f"step_ns: expected >= 1, got {step_ns}")
    out.mkdir(parents=True, exist_ok=True)

    # 1. resolve prices and entry times with the exact engine
    qt.replay({"dataset": {"path": str(data)}, "shadows": {"path": str(orders)}, "out": str(out / "resolve"),
               "bbo": "none", "trajectory": "none"})  # fmt: skip
    given = {str(r["shadow_id"]): r for r in _rows(orders)}
    res = _rows(out / "resolve" / "shadow_results.parquet")
    skipped: dict[str, int] = {}
    legs: list[dict[str, Any]] = []
    for r in sorted(res, key=lambda r: (int(r["ts_live"]), str(r["shadow_id"]))):
        sid = str(r["shadow_id"])
        reason = None
        if r["price"] is None:
            reason = "no_price"
        elif str(r["end_state"]) in REJECTED:
            reason = str(r["end_state"])
        elif r.get("ts_planned_end") is not None and int(r["ts_planned_end"]) <= int(r["ts_live"]):
            reason = "zero_life"
        elif str(given.get(sid, {}).get("order_key") or "") not in ("", sid):
            reason = "multi_leg"  # legs of one live order: replay those with `export replay-schedule` directly
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        src = given.get(sid, {})
        legs.append(
            {
                "shadow_id": sid,
                "instrument_id": int(r["instrument_id"]),
                "side": int(r["side"]),
                "price_mode": "abs",
                "price": int(r["price"]),
                "offset_ticks": 0,
                "qty": int(r["qty"]),
                "ts_submit": int(r["ts_live"]),
                "latency_ns": 0,
                "ts_cancel": src.get("ts_cancel"),
                "group": src.get("group"),
                "source": "user",
                "order_key": order_key(sid),
            }
        )
    if not legs:
        raise QtHbtError(
            "QT-INPUT-002",
            f"{orders}: no order can be replayed in hftbacktest (skipped: {json.dumps(skipped, sort_keys=True)})",
            "orders that would cross the book on arrival are rejected as post-only; check prices and times",
        )
    schema = SCHEMAS["qt.shadow_spec"]
    cols = {f.name: [leg[f.name] for leg in legs] for f in schema}
    table = pa.table({k: pa.array(v, type=schema.field(k).type) for k, v in cols.items()}, schema=schema)
    write_table(table, out / "schedule.parquet")

    # 2. exact truth of exactly that schedule
    qt.replay({"dataset": {"path": str(data)}, "shadows": {"path": str(out / "schedule.parquet")},
               "out": str(out / "exact"), "bbo": "none", "trajectory": "none"})  # fmt: skip

    # 3. one hftbacktest event file per instrument-day with orders
    days = _instrument_days(data)
    need: dict[int, set[str]] = {}
    for leg in legs:
        iid = leg["instrument_id"]
        date = _session_of(days.get(iid, []), leg["ts_submit"])
        if date is None:
            raise QtHbtError("QT-DATA-001", f"order {leg['shadow_id']}: no session of instrument {iid} at entry")
        need.setdefault(iid, set()).add(date)
    npz: dict[int, list[str]] = {}
    for iid in sorted(need):
        for date in sorted(need[iid]):
            rel = f"npz/{iid}_{date}.npz"
            qt.export({"target": "l2_npz", "dataset": {"path": str(data)}, "instrument_id": iid,
                       "session_date": date, "out": str(out / rel)})  # fmt: skip
            npz.setdefault(iid, []).append(rel)

    # 4. the instrument map that brings hftbacktest's real units back to the dataset's fixed point
    _write_instrument_map(data, set(need), out / "instrument_map.csv")

    # 5. the hftbacktest replay script of the schedule (its usage notes are for the live-log workflow: not shown)
    script = out / "hbt_replay.py"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        export_replay_schedule(
            "hftbacktest", out=str(script), schedule=str(out / "schedule.parquet"), dataset=str(data)
        )
    if window_ns is not None or step_ns != STEP_NS:
        text = script.read_text(encoding="utf-8")
        if window_ns is not None:
            text = _set_const(text, "WINDOW_NS", int(window_ns))
        text = _set_const(text, "STEP_NS", int(step_ns))
        script.write_text(text, encoding="utf-8")

    doc = {
        "schema": PREPARED_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "queuetruth_version": qt.__version__,
        "data": str(data.resolve()),
        "orders": str(orders.resolve()),
        "schedule": "schedule.parquet",
        "exact": "exact",
        "script": "hbt_replay.py",
        "npz": {str(k): v for k, v in sorted(npz.items())},
        "n_orders": len(legs),
        "skipped": dict(sorted(skipped.items())),
        "step_ns": int(step_ns),
    }
    (out / "prepared.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return Prepared.load(out)


def _write_instrument_map(data: Path, ids: set[int], path: Path) -> None:
    """`venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size` rows (SPEC 3.9) for the
    instruments `ids`, venue `qthbt`, instrument = the dataset symbol (what the replay script writes)."""
    from decimal import Decimal

    lines = ["venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size"]
    for r in sorted(
        _rows(data / "instruments.parquet"), key=lambda r: (int(r["instrument_id"]), str(r["session_date"]))
    ):
        if int(r["instrument_id"]) not in ids:
            continue
        pe, se = int(r["price_exp"]), int(r["size_exp"])
        tick = format(Decimal(int(r["tick_size"])).scaleb(pe).normalize(), "f")
        lot = format(Decimal(int(r["lot_size"])).scaleb(se).normalize(), "f")
        date = _session_date(r)
        lines.append(f"{ACCOUNT},{r['symbol']},{int(r['instrument_id'])},{date},{pe},{se},{tick},{lot}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _set_const(text: str, name: str, value: int) -> str:
    import re

    new, n = re.subn(rf"^{name} = -?\d+$", f"{name} = {value}", text, count=1, flags=re.M)
    if n != 1:
        raise QtHbtError("QT-ENGINE-001", f"hbt_replay.py: no {name} constant to set (queuetruth layout changed)")
    return new


def run(
    prepared: Prepared | str | Path,
    *,
    python: str | Path,
    snippet: str | Path | None = None,
    code: str | None = None,
    out: str | Path | None = None,
    timeout: float | None = None,
    allow_stop: bool = False,
) -> Path:
    """Run the replay in the hftbacktest venv `python` and return the path of hftbacktest's order states.

    The fill model comes from `snippet` (a file defining `asset`, such as `queuetruth export hftbacktest` writes) or
    `code` (the same as text, e.g. `HbtCalibration.code`). An hftbacktest error (for example code 14 of issue #312)
    stops the replay: the states so far are kept and `QT-USAGE-001` names the code, unless `allow_stop`; `score`
    scores the orders entered before the stop either way."""
    p = prepared if isinstance(prepared, Prepared) else Prepared.load(prepared)
    if (snippet is None) == (code is None):
        raise QtHbtError("QT-CONFIG-001", "run: give exactly one of snippet= (a file) or code= (its text)")
    if code is not None:
        snippet = p.out / "hbt_asset.py"
        Path(snippet).write_text(code, encoding="utf-8")
    _need(Path(snippet), "snippet")  # type: ignore[arg-type]
    dest = Path(out) if out is not None else p.out / "hbt_orders.jsonl"
    cmd = p.command(str(python), snippet=snippet, out=dest)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    _record_engine(str(python), dest, env)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except OSError as e:
        raise QtHbtError("QT-USAGE-001", f"cannot run {python}: {e}", "pass the python of the hftbacktest venv") from e
    (p.out / "hbt_replay.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode == 3 and dest.exists() and allow_stop:
        return dest
    if proc.returncode == 3 and dest.exists():
        fatal = _replay_summary(dest).get("fatal") or {}
        raise QtHbtError(
            "QT-USAGE-001",
            f"hftbacktest stopped the replay with error {fatal.get('rc')} at {fatal.get('at_ns')} ns",
            "the order states up to the stop are in the output; `score` compares the orders entered before it "
            "(code 14 is hftbacktest #312, see integrations/hftbacktest/UPSTREAM.md)",
            {"out": str(dest), "fatal": fatal},
        )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        raise QtHbtError("QT-USAGE-001", f"hftbacktest replay failed (exit {proc.returncode}): {' | '.join(tail)}")
    return dest


_ENGINE_PROBE = (
    "import importlib.metadata as m, json, sys; "
    "print(json.dumps({'hftbacktest_version': m.version('hftbacktest'), 'python': sys.version.split()[0]}))"
)


def _record_engine(python: str, dest: Path, env: dict[str, str]) -> None:
    """`<out>.engine.json`: the hftbacktest version the replay runs on (the referee report names it)."""
    try:
        proc = subprocess.run([python, "-I", "-c", _ENGINE_PROBE], capture_output=True, text=True, env=env, check=False)
    except OSError as e:
        raise QtHbtError("QT-USAGE-001", f"cannot run {python}: {e}", "pass the python of the hftbacktest venv") from e
    if proc.returncode != 0:
        raise QtHbtError(
            "QT-USAGE-001",
            f"{python} has no hftbacktest: {(proc.stderr.strip().splitlines() or ['?'])[-1]}",
            "create the venv with integrations/hftbacktest/setup_venvs.sh, or pip install hftbacktest==2.4.4 there",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.with_name(dest.name + ".engine.json").write_text(proc.stdout, encoding="utf-8")


def _engine(hbt_orders: Path) -> dict[str, Any]:
    try:
        doc = json.loads(hbt_orders.with_name(hbt_orders.name + ".engine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _replay_summary(hbt_orders: Path) -> dict[str, Any]:
    p = hbt_orders.with_name(hbt_orders.name + ".summary.json")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


@dataclass(frozen=True)
class HorizonScore:
    """Fill rates at one horizon over the orders whose label is defined there."""

    horizon_ns: int
    n: int
    exact_fill_rate: float | None
    hbt_fill_rate: float | None
    bias: float | None
    ci_lo: float | None
    ci_hi: float | None
    p_mcnemar: float | None
    exact_only: int
    hbt_only: int

    @property
    def relative(self) -> float | None:
        """hftbacktest's fill rate relative to the exact one, minus 1 (-0.08 = 8% fewer fills)."""
        if not self.exact_fill_rate or self.hbt_fill_rate is None:
            return None
        return self.hbt_fill_rate / self.exact_fill_rate - 1.0


@dataclass(frozen=True)
class HbtScore:
    """The result of `score` (`hbt_score.json` in `out`)."""

    n_orders: int
    n_scored: int
    horizons: list[HorizonScore]
    referee: dict[str, Any]
    hftbacktest: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": SCORE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "n_orders": self.n_orders,
            "n_scored": self.n_scored,
            "horizons": [{**h.__dict__, "relative": h.relative} for h in self.horizons],
            "referee": self.referee,
            "hftbacktest": self.hftbacktest,
            "warnings": list(self.warnings),
        }

    def text(self) -> str:
        """Plain-words summary, the gap first."""
        lines = []
        for h in self.horizons:
            if h.bias is None:
                lines.append(f"{_dur(h.horizon_ns):>6}: no order with a defined label")
                continue
            rel = h.relative
            word = "more" if h.bias > 0 else "fewer"
            rel_t = "" if rel is None or h.bias == 0 else f" ({abs(rel) * 100:.1f}% {word} fills)"
            lines.append(
                f"{_dur(h.horizon_ns):>6}: hftbacktest {_pct(h.hbt_fill_rate)} vs exact {_pct(h.exact_fill_rate)}, "
                f"bias {h.bias * 100:+.2f} pp [{h.ci_lo * 100:+.2f}, {h.ci_hi * 100:+.2f}]{rel_t}, n={h.n}"  # type: ignore[operator]
            )
        r = self.referee
        disc = r.get("discordant", {})
        head = (
            f"hftbacktest vs exact FIFO queue: {self.n_scored} of {self.n_orders} orders scored; order by order "
            f"{disc.get('exact_only', 0)} filled only in the exact queue, {disc.get('sim_only', 0)} only in "
            f"hftbacktest, {r.get('qty_mismatch', 0)} with another filled quantity"
        )
        if self.n_scored < self.n_orders:
            head += f" (order by order over all {self.n_orders} scheduled orders, also those after the stop)"
        return "\n".join([head, *lines, *(f"warning: {w}" for w in self.warnings)]) + "\n"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"


def _dur(ns: int) -> str:
    s = ns / 1e9
    if s >= 60 and s % 60 == 0:
        return f"{int(s // 60)} min"
    return f"{s:g} s"


def score(
    prepared: Prepared | str | Path,
    hbt_orders: str | Path | None = None,
    *,
    out: str | Path | None = None,
    horizons_ns: tuple[int, ...] | list[int] = HORIZONS_NS,
    ttf_tolerance_ns: int | None = None,
    bootstrap_b: int = 1000,
    seed: int = 0,
) -> HbtScore:
    """Score hftbacktest's order states (`hbt_orders`, default `<prepared>/hbt_orders.jsonl`) against the exact
    replay of `prepare` (module doc). Writes `hbt_log/` (the imported log), `referee_report.json` and
    `hbt_score.json` to `out` (default: the `prepare` directory)."""
    import numpy as np
    from queuetruth import live as qlive
    from queuetruth.api import referee
    from queuetruth.score.referee import load_sim
    from queuetruth.stats.clustered import clustered_mcnemar

    p = prepared if isinstance(prepared, Prepared) else Prepared.load(prepared)
    src = Path(hbt_orders) if hbt_orders is not None else p.out / "hbt_orders.jsonl"
    _need(src, "hbt_orders (hftbacktest order states)")
    dest = Path(out) if out is not None else p.out
    dest.mkdir(parents=True, exist_ok=True)
    hs = sorted({int(h) for h in horizons_ns})
    if not hs or hs[0] < 1:
        raise QtHbtError("QT-CONFIG-001", f"horizons_ns: expected positive horizons, got {list(horizons_ns)}")
    tol = p.step_ns if ttf_tolerance_ns is None else int(ttf_tolerance_ns)

    log = dest / "hbt_log"
    qlive.import_sim(
        "hftbacktest",
        [str(src)],
        str(log),
        account=ACCOUNT,
        instrument_map=str(p.out / "instrument_map.csv"),
        venue=ACCOUNT,
    )
    summ = _replay_summary(src)
    engine = _engine(src)
    rep = referee(
        {
            "dataset": {"path": str(p.data)},
            "schedule": {"path": str(p.schedule)},
            "sim_fills": {"path": str(log)},
            "simulator": {"name": "hftbacktest", "version": engine.get("hftbacktest_version")},
            "tolerance": {"max_abs_ttf_err_ns": tol},
            "out": str(dest),
        }
    )
    sim, _ = load_sim(str(log))
    warnings: list[str] = []
    stop = _stop_ts(summ)
    exact = _rows(p.exact / "shadow_results.parquet")
    exact_ts, entry, planned, clusters = {}, {}, {}, {}
    days = _instrument_days(p.data)
    for r in exact:
        sid = str(r["shadow_id"])
        ts_live = int(r["ts_live"])
        if stop is not None and ts_live >= stop:
            continue
        entry[sid] = ts_live
        exact_ts[sid] = None if r["ts_first_fill"] is None else int(r["ts_first_fill"])
        planned[sid] = None if r.get("ts_planned_end") is None else int(r["ts_planned_end"])
        date = _session_of(days.get(int(r["instrument_id"]), []), ts_live) or ""
        clusters[sid] = f"{int(r['instrument_id'])}_{date}_{ts_live // BUCKET_NS}"
    if stop is not None:
        warnings.append(
            f"hftbacktest stopped with error {summ['fatal'].get('rc')} at {stop} ns: only the "
            f"{len(entry)} of {len(exact)} orders entered before the stop are scored, on their fills before it"
        )
    hbt_ts: dict[str, int | None] = {}
    for sid in entry:
        q, ts = sim.get(order_key(sid), (0, None))
        hbt_ts[sid] = int(ts) if q > 0 and ts is not None and (stop is None or ts < stop) else None
    out_h: list[HorizonScore] = []
    for h in hs:
        ids = [s for s in sorted(entry) if planned[s] is None or planned[s] >= entry[s] + h]  # type: ignore[operator]
        if stop is not None:
            ids = [s for s in ids if entry[s] + h <= stop]
        ex = np.array([exact_ts[s] is not None and exact_ts[s] - entry[s] <= h for s in ids], bool)  # type: ignore[operator]
        mo = np.array([hbt_ts[s] is not None and hbt_ts[s] - entry[s] <= h for s in ids], bool)  # type: ignore[operator]
        n = len(ids)
        if n == 0:
            out_h.append(HorizonScore(h, 0, None, None, None, None, None, None, 0, 0))
            continue
        d = clustered_mcnemar(ex, mo, np.array([clusters[s] for s in ids]), b=bootstrap_b, seed=seed)
        out_h.append(
            HorizonScore(
                horizon_ns=h,
                n=n,
                exact_fill_rate=float(ex.mean()),
                hbt_fill_rate=float(mo.mean()),
                bias=_f(d.estimate),
                ci_lo=_f(d.ci_lo),
                ci_hi=_f(d.ci_hi),
                p_mcnemar=_f(d.mcnemar.p_exact),
                exact_only=int(np.sum(ex & ~mo)),
                hbt_only=int(np.sum(~ex & mo)),
            )
        )
    if p.skipped:
        warnings.append(f"not replayed in hftbacktest: {json.dumps(p.skipped, sort_keys=True)} (see prepare)")
    result = HbtScore(
        n_orders=p.n_orders,
        n_scored=len(entry),
        horizons=out_h,
        referee=rep.model_dump(mode="json"),
        hftbacktest={**engine, "replay": summ},
        warnings=warnings,
    )
    (dest / "hbt_score.json").write_text(
        json.dumps(result.to_document(), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def _f(x: Any) -> float | None:
    if x is None:
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _stop_ts(summ: dict[str, Any]) -> int | None:
    """The time hftbacktest stopped at with an error (`fatal.at_ns` of the replay summary), if it did."""
    fatal = summ.get("fatal")
    if isinstance(fatal, dict) and isinstance(fatal.get("at_ns"), int):
        return int(fatal["at_ns"])
    return None
