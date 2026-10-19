"""The L2 model zoo against exact truth on sampled shadows.

Shows: `qt.run_zoo` with every zoo model (`models=["all"]`) on the shared shadow sample, the leaderboard of the
returned `qt.schemas.ZooSummary` (`leaderboard[].fill_rate_bias[horizon]` with its 95% block-bootstrap CI,
`brier[horizon]`, the plain McNemar counts at `h_max`), sorted by the absolute fill-rate bias at 60 s, and one
model's per-queue-decile bias read from the `qt.zoo_errors` table. Output file names come from the summary's
`files` list (located by their `qt.schema` metadata), not from hard-coded names.

Needs the compiled extension (`make develop` or the wheel). Synthetic data only: the numbers mean nothing about
real venues.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.compute as pc
from _data import dataset_path, fmt, fmt_p, horizon_label, out_dir, print_rows, sample_example_shadows, table_by_schema

import queuetruth as qt
from queuetruth.schemas import CiValue, LeaderboardEntry, ZooSummary

#: Horizon used to sort the leaderboard (ns, a member of the default `horizons_ns`).
SORT_HORIZON = str(60_000_000_000)
#: Bootstrap replicates for the leaderboard CIs (the spec default is 1000).
BOOTSTRAP_B = 500


def run(dataset: Path, out: Path, shadows: Path) -> ZooSummary:
    """Runs every zoo model on the shadows and returns the summary (outputs under `out/zoo`)."""
    return qt.run_zoo(
        {
            "dataset": {"path": str(dataset)},
            "shadows": {"path": str(shadows)},
            "out": str(out / "zoo"),
            "models": ["all"],
            "bootstrap_b": BOOTSTRAP_B,
        }
    )


def _sort_key(e: LeaderboardEntry) -> float:
    v = e.fill_rate_bias.get(SORT_HORIZON)
    return float("inf") if v is None or v.value is None else abs(v.value)


def _pp(v: CiValue | None) -> str:
    if v is None or v.value is None:
        return "n/a"
    lo = "n/a" if v.ci_lo is None else f"{100 * v.ci_lo:+.2f}"
    hi = "n/a" if v.ci_hi is None else f"{100 * v.ci_hi:+.2f}"
    return f"{100 * v.value:+.2f} [{lo}, {hi}]"


def show_leaderboard(z: ZooSummary) -> None:
    """Prints fill-rate bias with CI at each horizon, Brier and McNemar counts, best first."""
    print(f"\nzoo run_id {z.run_id}: {z.n_shadows} shadows, {len(z.models)} models")
    board = sorted(z.leaderboard, key=_sort_key)
    horizons = sorted({h for e in board for h in e.fill_rate_bias}, key=int)
    print(
        f"leaderboard sorted by |fill_rate_bias| at {horizon_label(SORT_HORIZON)}; fill_rate_bias = mean model "
        "p_fill minus exact fill rate, in percentage points with the 95% block-bootstrap CI"
    )
    head = "".join(f"{horizon_label(h):>23}" for h in horizons)
    print(f"{'':12}{head}")
    for rank, e in enumerate(board, start=1):
        mc = e.mcnemar
        print(f"{rank:2d}. {e.model_id}  (h_max McNemar b={mc.b} c={mc.c} p_exact={fmt_p(mc.p_exact)})")
        bias = "".join(f"{_pp(e.fill_rate_bias.get(h)):>23}" for h in horizons)
        brier = "".join(f"{fmt(e.brier.get(h)):>23}" for h in horizons)
        print(f"{'    bias pp':12}{bias}")
        print(f"{'    brier':12}{brier}")
    if z.warnings:
        print(f"\n{len(z.warnings)} warning(s); first: {z.warnings[0][:160]}")


def show_errors(z: ZooSummary, out: Path, model_id: str) -> None:
    """Prints one model's fill-rate bias per queue decile at the sort horizon from `qt.zoo_errors`."""
    print("\nfiles: " + ", ".join(f"{f.name} ({f.rows} rows)" if f.rows is not None else f.name for f in z.files))
    errors = qt.read_table(str(table_by_schema(out / "zoo", z.files, "qt.zoo_errors")), expect="qt.zoo_errors")
    mask = pc.and_(
        pc.and_(pc.equal(errors.column("model_id"), model_id), pc.equal(errors.column("metric"), "fill_rate_bias")),
        pc.and_(
            pc.equal(errors.column("horizon_ns"), int(SORT_HORIZON)),
            pc.equal(pc.cast(errors.column("stratum_kind"), "string"), "queue_decile"),
        ),
    )
    rows = errors.filter(mask)
    print(f"\nqt.zoo_errors: {model_id} fill_rate_bias at {horizon_label(SORT_HORIZON)} by queue decile")
    print_rows(rows, ["stratum", "value", "ci_lo", "ci_hi", "n"], n=10)


def main() -> None:
    """Samples shadows, runs the zoo and prints the leaderboard."""
    dataset = dataset_path()
    out = out_dir(__file__)
    print(f"queuetruth {qt.__version__}; dataset {dataset}; outputs {out}")
    shadows = sample_example_shadows(dataset, out)
    z = run(dataset, out, shadows)
    show_leaderboard(z)
    worst = max((e for e in z.leaderboard if _sort_key(e) != float("inf")), key=_sort_key, default=None)
    if worst is not None:
        show_errors(z, out, worst.model_id)


if __name__ == "__main__":
    main()
