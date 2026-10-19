"""Calibrate hftbacktest's `power3` queue model against exact truth, then export the config snippet.

Shows: `qt.calibrate` with `kind="alloc"`, `family="power3"` and a train/test split that fits this 2-day dataset
(train on 2026-06-15, test on 2026-06-16; a `{"blocks": {"index": [...], "n_blocks": 5}}` window works too), the
returned `qt.schemas.CalibrationResult` (estimate, profile and bootstrap CIs, `held_out` for the fitted model vs
`baseline_held_out` for the library default `n=2` on the test day), the trust verdict (`CalibrationResult.trust`:
exportable or refused, with `beats_default` per horizon), and the fail-safe export: `qt.api.export` raises
`QT-CALIB-001` for a refused calibration, and `force=True` writes the snippet under a `# !! NOT VALIDATED` header.

Needs the compiled extension (`make develop` or the wheel). Synthetic data only: the numbers mean nothing about
real venues.
"""

from __future__ import annotations

from pathlib import Path

from _data import dataset_path, fmt, horizon_label, out_dir, sample_example_shadows

import queuetruth as qt
from queuetruth.schemas import CalibrationResult, HeldOut

TRAIN = {"dates": ["2026-06-15"]}
TEST = {"dates": ["2026-06-16"]}
#: Bootstrap replicates for `ci_bootstrap` (the spec default is 200).
BOOTSTRAP_B = 200


def run(dataset: Path, out: Path, shadows: Path, name: str = "cal", grid: dict | None = None) -> CalibrationResult:
    """Fits power3's `n` on the train day and evaluates fitted vs default on the test day (outputs `out/<name>`)."""
    extra = {} if grid is None else {"grid": {"alloc": grid}}
    return qt.calibrate(
        {
            **extra,
            "kind": "alloc",
            "family": "power3",
            "dataset": {"path": str(dataset)},
            "shadows": {"path": str(shadows)},
            "out": str(out / name),
            "train": TRAIN,
            "test": TEST,
            "bootstrap": {"b": BOOTSTRAP_B},
        }
    )


def _ci(ci: tuple[float | None, float | None] | None) -> str:
    return "n/a" if ci is None else f"[{fmt(ci[0])}, {fmt(ci[1])}]"


def _held_rows(label: str, held: dict[str, HeldOut]) -> None:
    for model_id, h in held.items():
        print(f"  {label}: {model_id}")
        for hz in sorted(h.brier, key=int):
            print(
                f"    {horizon_label(hz):>6}  brier {fmt(h.brier.get(hz))}  logloss {fmt(h.logloss.get(hz))}  "
                f"fill_rate_bias {fmt(h.fill_rate_bias.get(hz))}"
            )


def show(c: CalibrationResult) -> None:
    """Prints the estimate, its CIs and the held-out comparison."""
    print(f"\ncalibration run_id {c.run_id}: kind {c.kind}, family {c.family}, mode {c.mode}")
    print(f"  train {c.train.model_dump(exclude_none=True)}, test {c.test.model_dump(exclude_none=True)}")
    print(f"  {c.param_name} = {fmt(c.estimate)}  profile CI {_ci(c.ci_profile)}  bootstrap CI {_ci(c.ci_bootstrap)}")
    print(f"  loglik {fmt(c.loglik, 2)} over {c.n_events} events from {c.n_shadows} train shadows")
    print("held-out (test window), fitted vs library default:")
    _held_rows("held_out", c.held_out)
    _held_rows("baseline_held_out", c.baseline_held_out)
    for d in c.data_sources:
        print(f"  data source {d.venue} ({d.source_venue}): {d.validation_status}")
    if c.trust is not None:
        print(f"trust: verdict {c.trust.verdict} {list(c.trust.reasons)}; flags {list(c.trust.flags)}")
        print(f"  beats_default by horizon: { {horizon_label(h): v for h, v in c.trust.beats_default.items()} }")


def export_snippet(out: Path, name: str = "cal") -> None:
    """Exports the calibration as an hftbacktest snippet; on a refusal, shows it and exports again with force."""
    # `qt.api.export` is the same function as `qt.export`. Once `queuetruth.export` (the subpackage) has been
    # imported, which `qt.calibrate` does, the package attribute `qt.export` refers to that subpackage instead.
    spec = {"target": "hftbacktest", "calibration": {"path": str(out / name)}, "out": str(out / f"hbt_{name}.py")}
    try:
        ex = qt.api.export(spec)
    except qt.QtError as e:
        if e.code != "QT-CALIB-001":
            raise
        print(f"\nexport refused ({e.code}, CLI exit code {e.exit_code}): {e.message}")
        print("exporting again with force=True: the snippet carries a NOT VALIDATED header")
        ex = qt.api.export({**spec, "force": True})
    print(f"\nexport target {ex.target}: files {[f.name for f in ex.files]}")
    print("----- snippet -----")
    print(ex.text)
    print("----- end -----")


def main() -> None:
    """Samples shadows, calibrates power3 and prints the export."""
    dataset = dataset_path()
    out = out_dir(__file__)
    print(f"queuetruth {qt.__version__}; dataset {dataset}; outputs {out}")
    shadows = sample_example_shadows(dataset, out)
    show(run(dataset, out, shadows))
    export_snippet(out)
    # The same fit on a deliberately narrow grid (ln n from 1 to 2, n = 2.7 to 7.4) puts the maximum on the grid
    # edge: the verdict is `refused` (`at_grid_edge`), so the export raises QT-CALIB-001 and is then forced.
    print("\n===== the same fit on a narrow grid that cannot contain the maximum =====")
    show(run(dataset, out, shadows, "cal_narrow", grid={"lo": 1.0, "hi": 2.0, "n": 21}))
    export_snippet(out, "cal_narrow")


if __name__ == "__main__":
    main()
