#!/usr/bin/env python3
"""Ground-truth shadow-fill oracle (audit finding 11, ruling A11): run it on a dataset and compare with queuetruth.

    uv run --frozen --no-sync python scripts/oracle/run_oracle.py \
        --dataset tests/fixtures/synth_small --shadows tests/fixtures/synth_small/truth/shadows.parquet \
        --amends tests/fixtures/synth_small/truth/amends.parquet --qt-results tests/fixtures/synth_small/truth \
        --out .scratch/oracle/synth_small

Inputs:
- `--dataset DIR`: a qt dataset (SPEC 3.3), or `--et-mbo-csv FILE` (et `mbo.csv`) with `--venue`, `--midnight-ns`,
  `--open-s`, `--close-s`, `--tick-size` (and optionally `--instrument-ids`).
- `--shadows FILE`: qt.shadow_spec Parquet, et `shadows.csv`, or a JSON list; `--amends FILE` (qt.shadow_amend).
- Reference to compare with (optional, one of): `--qt-results DIR` (a queuetruth replay output dir or a testkit
  `truth/` dir), `--et-results DIR` (et engine dir with shadow_fills.csv / queue_trace.csv), `--expectations FILE`
  (hand-derived JSON), or `--run-qt` (runs `queuetruth.api.replay` on the dataset into OUT/qt_replay; skipped with
  a warning while the bindings are not built).
- `--exclude-order-ids FILE`: the user's own MBO order ids (ruling A1). An order stays on the tape for every shadow
  except the one that enters at its record, which takes its queue place and is filled by its executions; an order
  no shadow enters at is removed for everyone. `--no-twin-linking` removes every listed order for everyone.

Outputs in `--out`: `oracle_results.jsonl` (per shadow: fills, queue-ahead trajectory, notes), `oracle_summary.json`,
and with a reference `discrepancy_report.json` + `discrepancy_report.md`. Real-data runs belong under `.realdata/`
(never committed; only aggregate numbers may leave it).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "tests" / "oracle"))

from qt_oracle import ORACLE_VERSION  # noqa: E402
from qt_oracle.compare import (  # noqa: E402
    build_report,
    read_et_results,
    read_json_expectations,
    read_qt_results,
    render_markdown,
)
from qt_oracle.engine import OracleConfig  # noqa: E402
from qt_oracle.runner import read_amends, read_order_ids, read_shadows, run_et_csv_stream, run_oracle  # noqa: E402
from qt_oracle.tape import read_qt_dataset  # noqa: E402


def _run_qt(dataset: str, shadows: str, amends: str | None, out: Path, exclude: str | None) -> Path | None:
    try:
        import queuetruth.api as api
        from queuetruth.schemas.specs import ReplaySpec
    except Exception as e:  # pragma: no cover - environment dependent
        print(f"warning: queuetruth not importable ({e}); no comparison", file=sys.stderr)
        return None
    spec = {"dataset": {"path": dataset}, "out": str(out), "shadows": {"path": shadows}, "trajectory": "ahead"}
    if amends:
        spec["amends"] = {"path": amends}
    if exclude:
        if "exclude_order_ids" not in ReplaySpec.model_fields:
            print(
                "warning: ReplaySpec has no exclude_order_ids yet (ruling A1); qt runs without exclusion",
                file=sys.stderr,
            )
        else:
            spec["exclude_order_ids"] = sorted(read_order_ids(exclude))
    try:
        api.replay(spec)
    except NotImplementedError as e:
        print(f"warning: queuetruth.api.replay not implemented yet ({e}); no comparison", file=sys.stderr)
        return None
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dataset")
    src.add_argument("--et-mbo-csv")
    ap.add_argument("--venue", default="nasdaq")
    ap.add_argument("--midnight-ns", type=int, default=0)
    ap.add_argument("--open-s", type=int, default=34_200)
    ap.add_argument("--close-s", type=int, default=57_600)
    ap.add_argument("--tick-size", type=int, default=10_000_000)
    ap.add_argument("--session-date", default="unknown")
    ap.add_argument("--instrument-ids", default=None, help="comma-separated filter")
    ap.add_argument("--shadows", required=True)
    ap.add_argument("--amends")
    ap.add_argument("--limit-shadows", type=int, default=None)
    ref = ap.add_mutually_exclusive_group()
    ref.add_argument("--qt-results")
    ref.add_argument("--et-results")
    ref.add_argument("--expectations")
    ref.add_argument("--run-qt", action="store_true")
    ap.add_argument("--exclude-order-ids")
    ap.add_argument("--no-twin-linking", action="store_true")
    ap.add_argument("--match-mode", choices=["fifo", "directed"], default="fifo")
    ap.add_argument("--sweep-gap-ns", type=int, default=0)
    ap.add_argument("--cross-window-s", type=float, default=1.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    inst_filter = {int(x) for x in a.instrument_ids.split(",")} if a.instrument_ids else None
    shadows = read_shadows(a.shadows)
    if inst_filter:
        shadows = [s for s in shadows if s.instrument_id in inst_filter]
    if a.limit_shadows is not None:
        shadows = shadows[: a.limit_shadows]
    amends = read_amends(a.amends)
    exclude = read_order_ids(a.exclude_order_ids) if a.exclude_order_ids else frozenset()
    cfg = OracleConfig(
        match_mode=a.match_mode,
        sweep_gap_ns=a.sweep_gap_ns,
        exclude_order_ids=exclude,
        twin_linking=not a.no_twin_linking,
    )
    progress = lambda m: print(m, file=sys.stderr)  # noqa: E731
    if a.dataset:
        parts = read_qt_dataset(a.dataset)
        if inst_filter:
            parts = [p for p in parts if p.meta.instrument_id in inst_filter]
        res = run_oracle(parts, shadows, amends, cfg, cross_window_s=a.cross_window_s, progress=progress)
    else:
        res = run_et_csv_stream(
            a.et_mbo_csv,
            shadows,
            amends,
            cfg,
            venue=a.venue,
            midnight_utc_ns=a.midnight_ns,
            open_s=a.open_s,
            close_s=a.close_s,
            tick_size=a.tick_size,
            session_date=a.session_date,
            instrument_ids=inst_filter,
            cross_window_s=a.cross_window_s,
            progress=progress,
        )
    with open(out / "oracle_results.jsonl", "w") as f:
        for r in res["results"]:
            f.write(json.dumps(r) + "\n")
    config = {
        "oracle_version": ORACLE_VERSION,
        "match_mode": a.match_mode,
        "sweep_gap_ns": a.sweep_gap_ns,
        "cross_window_s": a.cross_window_s,
        "n_excluded_order_ids": len(exclude),
        "twin_linking": not a.no_twin_linking,
        "dataset": a.dataset or a.et_mbo_csv,
        "shadows": a.shadows,
        "amends": a.amends,
    }
    end_states: dict[str, int] = {}
    for r in res["results"]:
        end_states[r["end_state"]] = end_states.get(r["end_state"], 0) + 1
    summary = {
        "config": config,
        "n_shadows": len(res["results"]),
        "orphans": len(res["orphans"]),
        "end_states": end_states,
        "stats": res["stats"],
        "wall_s": round(time.time() - t0, 2),
    }
    (out / "oracle_summary.json").write_text(json.dumps(summary, indent=2))

    reference = None
    kind = None
    if a.qt_results:
        reference, kind = read_qt_results(a.qt_results), "queuetruth"
    elif a.et_results:
        reference, kind = read_et_results(a.et_results), "et"
    elif a.expectations:
        reference, kind = read_json_expectations(a.expectations), "expectations"
    elif a.run_qt:
        if not a.dataset:
            print("warning: --run-qt needs --dataset", file=sys.stderr)
        else:
            d = _run_qt(a.dataset, a.shadows, a.amends, out / "qt_replay", a.exclude_order_ids)
            if d is not None:
                reference, kind = read_qt_results(d), "queuetruth"
    if reference is not None:
        if a.limit_shadows is not None or inst_filter:
            keep = {r["shadow_id"] for r in res["results"]}
            reference = {k: v for k, v in reference.items() if k in keep}
        rep = build_report(res["results"], reference, reference_kind=kind, oracle_stats=res["stats"], config=config)
        (out / "discrepancy_report.json").write_text(json.dumps(rep, indent=2))
        (out / "discrepancy_report.md").write_text(render_markdown(rep))
        s = rep["summary"]
        print(
            f"compared {s['n_compared']}: agree {s['n_agree']}, disagree {s['n_disagree']}; "
            f"by category {s['by_category']}"
        )
    print(f"oracle: {summary['n_shadows']} shadows, end states {end_states}, {summary['wall_s']} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
