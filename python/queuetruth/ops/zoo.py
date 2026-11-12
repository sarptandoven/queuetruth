"""`zoo`: the Rust run, then `queuetruth.eval.evaluate_zoo` on `zoo_predictions.parquet` with the planned-life
columns of `zoo_shadows.parquet` (audit ruling A2); writes `zoo_errors.parquet` and rewrites `summary.json` with the
leaderboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth import units
from queuetruth.errors import QtError
from queuetruth.ledger import recorded as _ledger_recorded
from queuetruth.ops._call import _native, _out_dir, _validate, _write_json
from queuetruth.ops.replay import _expand_order_ids
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s
from queuetruth.schemas.io import write_table


def _planned_life(out: Path) -> tuple[pa.Table, list[str]]:
    """The `planned_life` input of `evaluate_zoo` from `zoo_shadows.parquet` (audit ruling A2). A planned end before
    the shadow's `ts_live` (e.g. an administrative end before activation) is clamped to `ts_live`: no horizon of
    that shadow has a defined label."""
    p = out / "zoo_shadows.parquet"
    if not p.exists():
        raise QtError("QT-INPUT-001", f"{p}: missing (the leaderboard needs the planned-life table, E38 OC-7)")
    t = pq.read_table(p)
    cols = ["shadow_id", "ts_cancel_planned", "ts_admin_end"]
    if not all(c in t.column_names for c in [*cols, "ts_live"]):
        raise QtError("QT-INPUT-002", f"{p}: lacks the planned-life columns {[*cols, 'ts_live']}")
    live = t.column("ts_live")
    notes = []
    out_cols = {"shadow_id": t.column("shadow_id")}
    for c in cols[1:]:
        col = t.column(c)
        early = pc.fill_null(pc.less(col, live), False)
        n = pc.sum(pc.cast(early, pa.int64())).as_py() or 0
        if n:
            notes.append(f"planned_life: {n} shadow(s) with {c} before ts_live clamped to ts_live")
        out_cols[c] = pc.if_else(early, live, col)
    return pa.table(out_cols), notes


def _cut_by_data_end(out: Path, h_max_ns: int) -> tuple[int, int] | None:
    """(never-cancel shadows whose administrative end (end of data) comes before `ts_live + h_max`, never-cancel
    shadows) from `zoo_shadows.parquet`, or None when there are none."""
    try:
        t = pq.read_table(out / "zoo_shadows.parquet", columns=["never_cancel", "ts_live", "ts_admin_end"])
    except (OSError, KeyError, ValueError, pa.ArrowException):
        return None
    nc = pc.fill_null(t.column("never_cancel"), False)
    live = pc.cast(t.column("ts_live"), pa.int64())
    end = pc.cast(t.column("ts_admin_end"), pa.int64())
    short = pc.fill_null(pc.less(pc.subtract(end, live), h_max_ns), False)
    n_cut = pc.sum(pc.cast(pc.and_(nc, short), pa.int64())).as_py() or 0
    n_nc = pc.sum(pc.cast(nc, pa.int64())).as_py() or 0
    return (int(n_cut), int(n_nc)) if n_cut else None


def _evaluate(
    preds: pa.Table,
    out: Path,
    *,
    h_max_ns: int,
    bootstrap_b: int,
    seed: int,
    trajectory: pa.Table | None,
    mcs: bool = False,
    block: str = "auto",
) -> tuple[pa.Table, list[dict[str, Any]], list[str], dict[str, Any]]:
    from queuetruth.eval import aborted_shadows, evaluate_zoo, leaderboard_population, zoo_ci_blocks
    from queuetruth.eval.leaderboard import annotate_mcs
    from queuetruth.stats import MIN_CI_BLOCKS

    planned, notes = _planned_life(out)
    errors, board = evaluate_zoo(
        preds,
        h_max_ns=h_max_ns,
        trajectory=trajectory,
        bootstrap_b=bootstrap_b,
        seed=seed,
        planned_life=planned,
        block=block,
    )
    if mcs:  # the zoo leaderboard's model confidence set (`queuetruth.eval.leaderboard`)
        board, mcs_notes = annotate_mcs(
            board, preds, planned, h_max_ns=h_max_ns, bootstrap_b=bootstrap_b, seed=seed, block=block
        )
        notes += mcs_notes
    cut = _cut_by_data_end(out, h_max_ns)
    if cut:
        notes.append(
            f"data end: {cut[0]:,} of {cut[1]:,} never-cancel shadows end with the data before "
            f"{units.format_duration(h_max_ns)}, "
            "so the longer horizons score fewer orders than the leaderboard population; shadows sample keeps "
            "every life inside the data unless --allow-past-end"
        )
    cb = zoo_ci_blocks(preds, block)
    if cb.n and not cb.available:
        notes.append(
            f"ci: {cb.n} bootstrap block(s) ({cb.kind}) < {MIN_CI_BLOCKS}: intervals and significance not available "
            "(more data, or another --bootstrap-block, gives one)"
        )
    population = {
        "n_excluded_hbt_312": len(aborted_shadows(preds)),
        "n_leaderboard": leaderboard_population(preds, planned, h_max_ns),
        "ci_block": cb.kind if cb.n else None,
        "n_ci_blocks": cb.n if cb.n else None,
    }
    return errors, board, notes, population


@_ledger_recorded("zoo")
def run_zoo(spec: s.ZooSpec | dict[str, Any]) -> m.ZooSummary:
    """`zoo`: Rust run (`zoo_predictions`, `zoo_fills`, `zoo_trajectory`, `zoo_shadows`) + `queuetruth.eval`
    metrics (`zoo_errors.parquet`, leaderboard in `summary.json`). Bare order ids in a spec dict's
    `exclude_order_ids` apply to every instrument of the dataset (`resolve_order_ids`, as the CLI)."""
    sp = _validate(s.ZooSpec, _expand_order_ids(spec))
    with _out_dir(sp.out, "qt-zoo-") as out:
        run = sp.model_copy(update={"out": str(out)})
        summ = _native("run_zoo", run, m.ZooSummary)
        preds = pq.read_table(out / "zoo_predictions.parquet")
        traj_p = out / "zoo_trajectory.parquet"
        traj = pq.read_table(traj_p) if sp.write_trajectory and traj_p.exists() else None
        errors, board, notes, population = _evaluate(
            preds,
            out,
            h_max_ns=sp.h_max_ns,
            bootstrap_b=sp.bootstrap_b,
            seed=sp.seed,
            trajectory=traj,
            mcs=True,
            block=sp.bootstrap_block,
        )
        from queuetruth.eval.impact import zoo_spec_warnings

        notes += zoo_spec_warnings(out, sp)
        entry = m.FileEntry.model_validate(write_table(errors, out / "zoo_errors.parquet"))
        files = [f for f in summ.files if f.name != entry.name] + [entry]
        summary = summ.model_copy(
            update={
                "leaderboard": [m.LeaderboardEntry.model_validate(x) for x in board],
                "files": files,
                "warnings": [*summ.warnings, *notes],
                **population,
            }
        )
        summary = m.ZooSummary.model_validate(summary.model_dump(mode="json"))
        if sp.out:
            _write_json(summary.model_dump(mode="json"), out / "summary.json")
    return summary


OPERATIONS = (
    OperationDef(
        "zoo",
        s.ZooSpec,
        run_zoo,
        m.ZooSummary,
        "L2 queue models run on a local dataset's L2 view and scored against exact truth; returns qt.zoo_summary "
        "with the leaderboard.",
        cost="minutes",
        run_id=True,
        tool="qt_local_run_zoo",
        title="Run model zoo locally",
        service_kind="zoo",
    ),
)
