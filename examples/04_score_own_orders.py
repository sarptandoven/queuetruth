"""Score a live order log against simulators: the exact arm and one zoo model.

The fixture's `own_orders/live/` is a canonical live log of 200 real order lives of the synthetic tape, taken as
"your own orders" (audit ruling A1). Shows: `qt.validate_live`, then `qt.score` with `sim={"kind": "exact"}` (the
scorer removes your own orders from the tape, matched by venue order id, so the exact shadow does not queue behind
its own twin), the same run with `self_exclude=False` for contrast, and `sim={"kind": "zoo", "model": ...}`. For each
run it prints `two_by_two`, `bias`, `mcnemar`, the cluster-robust primary discordance test (row
`discordance_clustered` of the `qt.score_tests` table) and the verdict headline.

Expected for the exact arm with self-exclusion: zero discordance (`live_only == sim_only == 0`), asserted by
python/tests/api/test_api_hybrid.py::test_score_exact_self_excluded_is_concordant and by O-SCORE-4
(python/tests/score/test_score_gate.py::test_o_score_4_self_exclusion_real_orders). This script prints the
observed counts next to that expectation; it does not assert it.

Needs the compiled extension (`make develop` or the wheel). Synthetic data only: the numbers mean nothing about
real venues.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _data import dataset_path, fmt, fmt_ci, fmt_p, out_dir, table_by_schema

import queuetruth as qt
from queuetruth.schemas import ScorecardSummary

#: A zoo model id; short forms are completed with the defaults (`hbt_power3[exch=partial;mode=faithful;n=2]`).
ZOO_MODEL = "hbt_power3[n=2]"
#: Bootstrap and permutation replicates (spec defaults: 1000 and 2000).
BOOT_B = 500
PERM_B = 500


def run(dataset: Path, out: Path, name: str, sim: dict[str, Any], **extra: Any) -> ScorecardSummary:
    """Scores `own_orders/live` against `sim`; outputs under `out/<name>`."""
    # `qt.api.score` is the same function as `qt.score`. Once `queuetruth.score` (the subpackage) has been
    # imported, which the first call does, the package attribute `qt.score` refers to that subpackage instead.
    return qt.api.score(
        {
            "live": {"path": str(dataset / "own_orders" / "live")},
            "sim": sim,
            "dataset": {"path": str(dataset)},
            "out": str(out / name),
            "bootstrap": {"b": BOOT_B},
            "perm_b": PERM_B,
        },
        **extra,
    )


def show(title: str, s: ScorecardSummary, out: Path) -> None:
    """Prints the 2x2 table, the bias, both discordance tests and the verdict of one scorecard."""
    print(f"\n== {title} ==")
    src = s.sim_source
    sim = f"{src.kind} {src.model_id}" if src.model_id else src.kind
    print(f"  sim {sim}; orders {s.n_orders}, matched {s.n_matched}")
    t = s.two_by_two
    print(f"  two_by_two: both {t.both}, neither {t.neither}, live_only {t.live_only}, sim_only {t.sim_only}")
    b = s.bias
    print(
        f"  bias: fill_rate_bias {fmt_ci(b.fill_rate_bias, b.ci_lo, b.ci_hi)} ({b.direction}); "
        f"qty_bias {fmt_ci(b.qty_bias, b.qty_bias_ci_lo, b.qty_bias_ci_hi)}"
    )
    mc = s.mcnemar
    print(
        f"  mcnemar (secondary, assumes independent orders): b {mc.b}, c {mc.c}, chi2_cc {fmt(mc.chi2_cc, 3)}, "
        f"p_exact {fmt_p(mc.p_exact)}"
    )
    tests = qt.read_table(str(table_by_schema(out, s.files, "qt.score_tests")), expect="qt.score_tests")
    for r in tests.to_pylist():
        if r["test_id"] == "discordance_clustered":
            print(
                f"  discordance_clustered (primary): d {fmt(r['value'])}, p {fmt_p(r['p_value'])}, "
                f"CI [{fmt(r['ci_lo'])}, {fmt(r['ci_hi'])}], n {r['n']}; {r['note']}"
            )
    v = s.verdict
    print(f"  verdict: {v.headline}")
    print(f"  gates_passed {v.gates_passed} {v.failed_gates or ''}".rstrip())
    for w in s.warnings:
        print(f"  warning: {w}")


def main() -> None:
    """Validates the live log and scores it three ways."""
    dataset = dataset_path()
    out = out_dir(__file__)
    print(f"queuetruth {qt.__version__}; dataset {dataset}; outputs {out}")
    live = dataset / "own_orders" / "live"
    warnings = qt.validate_live(str(live))
    print(f"live log {live}: valid, {len(warnings)} warning(s)")

    exact = run(dataset, out, "score_exact", {"kind": "exact"})
    show("exact arm, own orders excluded from the tape (default)", exact, out / "score_exact")
    t = exact.two_by_two
    print(
        f"  expected live_only = sim_only = 0 (test_score_exact_self_excluded_is_concordant, O-SCORE-4); "
        f"observed live_only {t.live_only}, sim_only {t.sim_only}"
    )

    no_excl = run(dataset, out, "score_exact_no_exclusion", {"kind": "exact"}, self_exclude=False)
    show("exact arm, own orders left in the tape (self_exclude=False)", no_excl, out / "score_exact_no_exclusion")

    zoo = run(dataset, out, "score_zoo", {"kind": "zoo", "model": ZOO_MODEL})
    show(f"zoo arm {ZOO_MODEL}", zoo, out / "score_zoo")


if __name__ == "__main__":
    main()
