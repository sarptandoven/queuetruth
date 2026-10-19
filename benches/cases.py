"""Benchmark case catalog for queuetruth (SPEC 7.1). Every case names the public API it drives.

Markers used below (grep for them when an API lands or changes):
  API[cli]   public CLI command and flags of SPEC 4.4 (console script `queuetruth` or binary `queuetruth-core`)
  API[py]    public Python API of SPEC 4.3
  PROPOSED   hidden `queuetruth-core bench ...` subcommands (SPEC 4.4 hidden rows, crates/cli/src/bench.rs)
Every case is a separate process; RSS and CPU come from wait4(). In-process timings (qt.bench `wall_s`) are used
where the kernel must be isolated from setup (record generation, Parquet decode into memory).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from qtbench.runner import Case, ProcRun

ZOO_ALL = [
    "hbt_risk_adverse",
    "hbt_power[n=1]",
    "hbt_power[n=2]",
    "hbt_power[n=3]",
    "hbt_power2[n=2]",
    "hbt_power3[n=1]",
    "hbt_power3[n=2]",
    "hbt_power3[n=3]",
    "hbt_log",
    "hbt_log2",
    "hbt_l3_fifo",
    "nt_queue_l2",
    "nt_prob_touch[p=1]",
    "databento_mc[k=-0.8]",
    "databento_mc[k=-0.95]",
    "front_of_queue",
    "trade_through_only",
]  # SPEC 5.3.4 (17)

DAY1 = {"dir": "out/itch-12302019", "midnight": 1577682000000000000, "records": 6_103_262, "date": "2019-12-30"}
T71 = "SPEC 7.1"


@dataclass
class Group:
    name: str
    cases: list[Case]
    warmup: int = 1
    repeat: int = 5
    derived: list[tuple] = field(default_factory=list)  # (id, num_case, den_case, kind, target|None)


@dataclass
class Ctx:
    repo: Path  # queuetruth checkout
    scratch: Path  # .scratch/bench (synthetic) ; .realdata/bench (real) -- both gitignored
    core: Path  # target/release/queuetruth-core
    py: Path  # .venv/bin/python
    qt: Path  # .venv/bin/queuetruth
    et_root: Path | None
    synth_records: int = 10_000_000

    @property
    def fixture(self) -> Path:
        return self.repo / "tests/fixtures/synth_small"


# ----------------------------------------------------------------------------------------------------------------
# parse hooks
# ----------------------------------------------------------------------------------------------------------------


def qt_bench(pr: ProcRun) -> dict[str, list[float]]:
    """stdout = one qt.bench document (SPEC 3.10): {data, records, wall_s[], records_per_s_median}."""
    d = json.loads(pr.stdout)
    if d.get("schema") != "qt.bench":
        raise ValueError(f"expected qt.bench, got {d.get('schema')}")
    if not d["wall_s"]:
        raise ValueError("empty wall_s")
    return {"metric": [d["records"] / w for w in d["wall_s"]]}


def summary(schema: str, work_path: tuple[str, ...] | None = None, check=None):
    """stdout = the command's JSON summary (--format json). Metric: work/wall (if work_path) else process wall_s."""

    def parse(pr: ProcRun) -> dict[str, list[float]]:
        d = json.loads(pr.stdout)
        if d.get("schema") != schema:
            raise ValueError(f"expected {schema}, got {d.get('schema')}: {pr.stdout[:200]}")
        if check:
            check(d)
        extra = {}
        t = d.get("timings") or {}
        if "wall_s" in t:
            extra["inproc_wall_s"] = [t["wall_s"]]
        if work_path:
            v = d
            for k in work_path:
                v = v[k]
            return {"metric": [v / pr.wall_s], **extra}
        return {"metric": [pr.wall_s], **extra}

    return parse


def fresh(sub: str):
    def setup(wd: Path) -> None:
        shutil.rmtree(wd / sub, ignore_errors=True)

    return setup


def wipe(sub: str):
    return fresh(sub)


# ----------------------------------------------------------------------------------------------------------------
# groups
# ----------------------------------------------------------------------------------------------------------------


def core_kernels(ctx: Ctx, data: Path | None, n_records: int | None, tag: str) -> Group:
    """In-process Rust kernels (PROPOSED bench subcommands). data=None -> synthetic records generated in memory."""
    # synthetic: one dataset per size, shared by the group's cases (written by the first run, untimed; scratch is
    # deleted after the suite) instead of one copy per case working directory
    synth = ["--synth-records", str(n_records), "--seed", "7", "--scratch", str(ctx.scratch / f"synth-{n_records}-7")]
    src = ["--data", str(data)] if data else synth
    rep = ["--warmup", "1", "--repeat", "3", "--format", "json"]
    c = str(ctx.core)

    def k(cid, argv, unit="records/s", target=None, notes=None):
        return Case(
            f"core.{tag}.{cid}",
            f"core.{tag}",
            lambda w, a=argv: [c, "bench", *a, *rep],
            unit,
            "higher_is_better",
            parse=qt_bench,
            target=target,
            notes=notes or [],
        )

    shadows = ["--shadows", str(ctx.scratch / "shadows_3k.parquet")] if data else ["--synth-shadows", "3000"]
    shadows100k = ["--shadows", str(ctx.scratch / "shadows_100k.parquet")] if data else ["--synth-shadows", "100000"]
    cases = [
        # PROPOSED: bench replay --mode book: Engine::apply over preloaded Vec<MboEvent>, invariants on, no sinks
        k("replay_book", ["replay", *src, "--mode", "book"], target={"op": ">=", "value": 8e6, "spec": T71}),
        # PROPOSED: --mode sinks: change-only BBO + trades Parquet sinks (qt_schema::io::TableWriter) to scratch
        k(
            "replay_sinks",
            ["replay", *src, "--mode", "sinks", "--out", str(ctx.scratch / "sinks")],
            target={"op": ">=", "value": 4e6, "spec": T71},
        ),
        # PROPOSED: --mode rank-oracle: EngineConfig.rank_oracle = true + rank_trace sink
        k(
            "replay_rank",
            ["replay", *src, "--mode", "rank-oracle", "--out", str(ctx.scratch / "rank")],
            target={"op": ">=", "value": 3e6, "spec": T71},
        ),
        k("replay_shadows_3k", ["replay", *src, "--mode", "book", *shadows, "--trajectory", "none"]),
        k("replay_shadows_3k_ahead", ["replay", *src, "--mode", "book", *shadows, "--trajectory", "ahead"]),
        k("replay_shadows_100k", ["replay", *src, "--mode", "book", *shadows100k, "--trajectory", "none"]),
        # PROPOSED: bench l2: qt_l2::L2Builder::push over preloaded events, depth 10, cadence 0, rows discarded
        k(
            "l2_d10",
            ["l2", *src, "--l2-depth", "10", "--l2-cadence-ns", "0"],
            target={"op": ">=", "value": 3e6, "spec": T71},
        ),
    ]
    if data is None:
        # PROPOSED: bench itch: qt_ingest::itch parse + Mapper over an in-memory synthetic stream, no output
        cases.insert(
            0,
            k(
                "itch_parse",
                ["itch", "--synth-messages", str(n_records)],
                unit="msg/s",
                target={"op": ">=", "value": 5e6, "spec": T71},
            ),
        )
    p = f"core.{tag}."
    return Group(
        f"core.{tag}",
        cases,
        warmup=0,
        repeat=2,  # 2 processes x (1 warmup + 3 in-process reps)
        derived=[
            (
                p + "shadow_overhead_3k",
                p + "replay_shadows_3k",
                p + "replay_book",
                "throughput_loss",
                {"op": "<=", "value": 0.15, "spec": T71},
            ),
            (
                p + "shadow_overhead_100k",
                p + "replay_shadows_100k",
                p + "replay_book",
                "cost_ratio",
                {"op": "<=", "value": 2.5, "spec": T71},
            ),
            (
                p + "trajectory_ahead_extra",
                p + "replay_shadows_3k_ahead",
                p + "replay_shadows_3k",
                "throughput_loss",
                {"op": "<=", "value": 0.10, "spec": T71},
            ),
        ],
    )


def binding_overhead(ctx: Ctx, data: Path, tag: str) -> Group:
    """qt.replay() in Python vs `queuetruth-core replay`, same spec, interleaved (SPEC 7.1: within 5%)."""
    flags = ["--rank-oracle", "--bbo", "change"]
    cli = Case(
        f"bind.{tag}.cli_replay",
        f"bind.{tag}",
        # API[cli] replay --data --out --rank-oracle --bbo change --format json  -> qt.replay_summary
        lambda w: [str(ctx.core), "replay", "--data", str(data), "--out", str(w / "out"), *flags, "--format", "json"],
        "records/s",
        "higher_is_better",
        setup=fresh("out"),
        cleanup=wipe("out"),
        parse=summary("qt.replay_summary", ("stats", "records")),
    )
    py = Case(
        f"bind.{tag}.py_replay",
        f"bind.{tag}",
        # API[py] qt.replay(ReplaySpec) via benches/py_overhead.py (times the call only, not import)
        lambda w: [
            str(ctx.py),
            str(ctx.repo / "benches/py_overhead.py"),
            "--data",
            str(data),
            "--out",
            str(w / "out"),
            "--rank-oracle",
        ],
        "records/s",
        "higher_is_better",
        setup=fresh("out"),
        cleanup=wipe("out"),
        parse=lambda pr: {
            "metric": [json.loads(pr.stdout)["records_per_s"]],
            "import_s": [json.loads(pr.stdout)["import_s"]],
        },
        notes=["metric is records / (qt.replay call wall); CLI metric is records / process wall"],
    )
    return Group(
        f"bind.{tag}",
        [cli, py],
        warmup=1,
        repeat=5,
        derived=[(f"bind.{tag}.py_vs_cli", py.id, cli.id, "cost_ratio", {"op": "<=", "value": 1.05, "spec": T71})],
    )


def zoo_group(ctx: Ctx, data: Path, shadows: Path, tag: str, per_model: bool) -> list[Group]:
    """Zoo: each of the 17 models alone (marginal = model - trade_through_only floor); all 17 at 1 and 4 threads."""

    def zoo(cid, models, threads, notes=None, target=None):
        # API[cli] zoo (hybrid): Rust run_zoo + Python qt.eval leaderboard -> qt.zoo_summary
        return Case(
            f"zoo.{tag}.{cid}",
            f"zoo.{tag}",
            lambda w: [
                str(ctx.qt),
                "zoo",
                "--data",
                str(data),
                "--shadows",
                str(shadows),
                "--out",
                str(w / "out"),
                "--models",
                models,
                "--threads",
                str(threads),
                "--format",
                "json",
            ],
            "s",
            "lower_is_better",
            setup=fresh("out"),
            cleanup=wipe("out"),
            parse=summary("qt.zoo_summary", check=_n_models(models)),
            timeout_s=1800,
            target=target,
            notes=notes or [],
        )

    groups = []
    if per_model:
        cases = [zoo("model." + _slug(m), m, 1) for m in ZOO_ALL]
        groups.append(Group(f"zoo.{tag}.models", cases, warmup=0, repeat=3))
    allc = [
        zoo("all_t1", "all", 1, target={"op": "<=", "value": 240.0, "spec": T71 + " (4-symbol day)"}),
        zoo("all_t4", "all", 4, target={"op": "<=", "value": 90.0, "spec": T71 + " (4-symbol day)"}),
    ]
    groups.append(Group(f"zoo.{tag}.all", allc, warmup=0, repeat=3))
    return groups


def _slug(m: str) -> str:
    return m.replace("[", "_").replace("]", "").replace("=", "").replace("-", "m").replace(".", "p")


def _n_models(models: str):
    want = 17 if models == "all" else len(models.split(","))

    def chk(d: dict) -> None:
        if len(d["models"]) != want:
            raise ValueError(f"models {len(d['models'])} != {want}")

    return chk


def analysis_group(ctx: Ctx, data: Path, shadows: Path, tag: str, train: str, test: str, score_sim: list[str]) -> Group:
    """Calibration fits, features, fit-hazard, score, report on prepared inputs (process wall, lower is better)."""
    s = ctx.scratch
    q = str(ctx.qt)

    def case(cid, argv, schema, target=None, timeout=1800):
        return Case(
            f"ana.{tag}.{cid}",
            f"ana.{tag}",
            lambda w, a=argv: [q, *a(w), "--format", "json"],
            "s",
            "lower_is_better",
            setup=fresh("out"),
            cleanup=wipe("out"),
            parse=summary(schema),
            target=target,
            timeout_s=timeout,
        )

    common = ["--data", str(data), "--shadows", str(shadows), "--train", train, "--test", test]
    return Group(
        f"ana.{tag}",
        [
            # API[cli] calibrate alloc|survival|nt-touch (hybrid) -> qt.calibration_result
            case(
                "calib_alloc_power3",
                lambda w: ["calibrate", "alloc", "--family", "power3", *common, "--out", str(w / "out")],
                "qt.calibration_result",
            ),
            case(
                "calib_alloc_databento",
                lambda w: ["calibrate", "alloc", "--family", "databento", *common, "--out", str(w / "out")],
                "qt.calibration_result",
            ),
            case(
                "calib_survival_databento",
                lambda w: ["calibrate", "survival", "--family", "databento", *common, "--out", str(w / "out")],
                "qt.calibration_result",
            ),
            case(
                "calib_nt_touch",
                lambda w: ["calibrate", "nt-touch", *common, "--out", str(w / "out")],
                "qt.calibration_result",
            ),
            # API[cli] features (native) on 20,000 shadows x 3 landmarks -> qt.feature_summary
            case(
                "features_20k",
                lambda w: [
                    "features",
                    "--data",
                    str(data),
                    "--shadows",
                    str(s / "shadows_20k.parquet"),
                    "--landmarks",
                    "0,1s,5s",
                    "--out",
                    str(w / "out"),
                ],
                "qt.feature_summary",
                target={"op": "<=", "value": 120.0, "spec": T71},
            ),
            # API[cli] fit-hazard (python) on the prepared 20k x 3 = 60,000 order-landmarks -> qt.hazard_model_card
            case(
                "fit_hazard_60k",
                lambda w: ["fit-hazard", "--features", str(s / "features_20k"), "--out", str(w / "out")],
                "qt.hazard_model_card",
                target={"op": "<=", "value": 120.0, "spec": T71},
            ),
            # API[cli] score (python), 10,000 live orders, B = 1,000 -> qt.scorecard_summary
            case(
                "score_10k",
                lambda w: [
                    "score",
                    "--live",
                    str(s / "live_10k"),
                    *score_sim,
                    "--bootstrap-b",
                    "1000",
                    "--out",
                    str(w / "out"),
                ],
                "qt.scorecard_summary",
                target={"op": "<=", "value": 60.0, "spec": T71},
            ),
            # API[cli] report (python) over the prepared output dirs -> qt.report_summary
            case(
                "report",
                lambda w: [
                    "report",
                    "--in",
                    str(s / "zoo_prep"),
                    "--in",
                    str(s / "score_prep"),
                    "--out",
                    str(w / "out" / "report.html"),
                ],
                "qt.report_summary",
            ),
        ],
        warmup=0,
        repeat=3,
    )


def e2e_group(ctx: Ctx, tag: str, argv_tail: list[str], target: dict | None, repeat: int) -> Group:
    """Whole pipeline as one process tree (benches/e2e_pipeline.py); stage walls and peak RSS as extras."""
    c = Case(
        f"e2e.{tag}",
        f"e2e.{tag}",
        lambda w: [
            str(ctx.py),
            str(ctx.repo / "benches/e2e_pipeline.py"),
            "--qt",
            str(ctx.qt),
            "--core",
            str(ctx.core),
            "--work",
            str(w / "run"),
            *argv_tail,
        ],
        "s",
        "lower_is_better",
        setup=fresh("run"),
        cleanup=wipe("run"),
        parse=_e2e_parse,
        timeout_s=3600,
        target=target,
        notes=["peak RSS is the max over stages (each stage is its own process; wait4 per stage)"],
    )
    return Group(f"e2e.{tag}", [c], warmup=0, repeat=repeat)


def _e2e_parse(pr: ProcRun) -> dict[str, list[float]]:
    d = json.loads(pr.stdout)
    if not d["ok"]:
        raise ValueError(f"stage failed: {d['failed_stage']}")
    out = {
        "metric": [d["total_s"]],
        "peak_rss_bytes_stages": [d["peak_rss_bytes"]],
        "scratch_bytes": [d["scratch_bytes"]],
    }
    for st in d["stages"]:
        out[f"stage_{st['name']}_s"] = [st["wall_s"]]
    return out


def ingest_etcsv_group(ctx: Ctx) -> Group:
    d1 = ctx.et_root / DAY1["dir"]
    c = Case(
        "ingest.real_day1.etcsv",
        "ingest.real_day1",
        # API[cli] ingest --format etcsv (RD-1 flags) -> qt.ingest_summary
        lambda w: [
            str(ctx.core),
            "ingest",
            "--format",
            "etcsv",
            "--in",
            str(d1 / "mbo.csv"),
            "--in",
            str(d1 / "trades.csv"),
            "--instruments",
            str(d1 / "instruments.csv"),
            "--venue",
            "nasdaq",
            "--midnight-ns",
            str(DAY1["midnight"]),
            "--price-exp",
            "-9",
            "--tick-size",
            "0.01",
            "--lot-size",
            "1",
            "--out",
            str(w / "out"),
            "--format",
            "json",
        ],
        "records/s",
        "higher_is_better",
        setup=fresh("out"),
        cleanup=wipe("out"),
        parse=summary("qt.ingest_summary", ("data_quality", "records")),
        target={"op": ">=", "value": 1.5e6, "spec": T71},
        reference={"et": "no Parquet path in et"},
    )
    return Group("ingest.real_day1", [c], warmup=1, repeat=3)


def ingest_itch_group(ctx: Ctx, itch_file: Path, n_messages: int) -> Group:
    c = Case(
        "ingest.synth.itch",
        "ingest.synth",
        # API[cli] ingest --format itch (synthetic file written in prep by PROPOSED `bench itch --write`)
        lambda w: [
            str(ctx.core),
            "ingest",
            "--format",
            "itch",
            "--in",
            str(itch_file),
            "--venue",
            "nasdaq",
            "--date",
            "2019-12-30",
            "--out",
            str(w / "out"),
            "--format",
            "json",
        ],
        "msg/s",
        "higher_is_better",
        work=n_messages,
        setup=fresh("out"),
        cleanup=wipe("out"),
        parse=lambda pr: {"metric": [n_messages / pr.wall_s]},
        target={"op": ">=", "value": 2.5e6, "spec": T71},
    )
    return Group("ingest.synth", [c], warmup=1, repeat=3)
