"""Measure the author's earlier private et prototype on its stored real-data artifacts: the reference to beat.

Read-only on et: runs `target/release/replay` and the release test binaries of its two ignored throughput tests;
every output goes under --scratch and is deleted after each process (large CSV sinks are symlinked to /dev/null
except in the `et.day1_disk` group, which writes them for real and deletes them right after).

Usage:
  python3 scripts/bench/et_baseline.py --et-root <et checkout> \
      --scratch <dir> --out benches/reference_et.json [--repeat 5] [--groups micro,day1,day1_disk,day2]

Each run's output is checked against the RD-2 / RD-6 counters (records, executions_checked, parity) before its
timing counts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benches"))

from qtbench import env as envmod
from qtbench.doc import case_result, derived_ratio, results_doc, write_json
from qtbench.runner import Case, ProcRun, run_group

DAY1 = {
    "dir": "out/itch-12302019",
    "midnight": 1577682000000000000,
    "records": 6_103_262,
    "exec": 176_103,
    "trades": 211_867,
}
DAY2 = {
    "dir": "out/itch-01302020",
    "midnight": 1580360400000000000,
    "records": 10_651_300,
    "exec": 314_369,
    "trades": 421_420,
}
NULL_SINKS = ("bbo.csv", "trades.csv", "rank_trace.csv", "depth.csv", "queue_trace.csv", "shadow_fills.csv")


def test_binaries(et: Path) -> dict[str, Path]:
    """Release test executables of et's `replay` lib and `itch` roundtrip test (built if stale; build cache only)."""
    cmd = [
        "cargo",
        "test",
        "--release",
        "--locked",
        "--offline",
        "-p",
        "replay",
        "-p",
        "itch",
        "--no-run",
        "--message-format=json",
    ]
    out = subprocess.run(cmd, cwd=et, capture_output=True, text=True, check=True).stdout
    found = {}
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-artifact" or not msg.get("executable"):
            continue
        t = msg["target"]
        if t["name"] == "replay" and "lib" in t["kind"]:
            found["replay_lib"] = Path(msg["executable"])
        if t["name"] == "roundtrip":
            found["itch_roundtrip"] = Path(msg["executable"])
    return found


def null_sinks(names=NULL_SINKS):
    def setup(wd: Path) -> None:
        out = wd / "out"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        for n in names:
            (out / n).symlink_to("/dev/null")

    return setup


def real_sinks(wd: Path) -> None:
    shutil.rmtree(wd / "out", ignore_errors=True)


def wipe(wd: Path) -> None:
    shutil.rmtree(wd / "out", ignore_errors=True)


def checked(wd_of, day: dict, rank: bool = False, shadows: int | None = None, parity: bool = False):
    """parse hook: verify run_report.json (and parity) before the run's records/s counts."""

    def parse(pr: ProcRun) -> dict[str, list[float]]:
        out = wd_of() / "out"
        rep = json.loads((out / "run_report.json").read_text())["summary"]
        st = rep["stats"]
        if st["records"] != day["records"]:
            raise ValueError(f"records {st['records']} != {day['records']}")
        if any(v != 0 for v in st["invariants"].values()):
            raise ValueError(f"invariants {st['invariants']}")
        if rank and st["rank_oracle"]["executions_checked"] != day["exec"]:
            raise ValueError(f"executions_checked {st['rank_oracle']['executions_checked']}")
        if shadows is not None and rep["shadows"] != shadows:
            raise ValueError(f"shadows {rep['shadows']} != {shadows}")
        if parity:
            par = json.loads((out / "parity_report.json").read_text())
            if not par.get("pass", par.get("passed")):
                raise ValueError("trade parity failed")
        return {"metric": [day["records"] / pr.wall_s]}

    return parse


def replicate_shadows(src: Path, dst: Path, n: int) -> int:
    """n shadows from et's 3,000 (ids suffixed, ts_submit/ts_cancel shifted by k ms per copy); shadows are passive."""
    lines = src.read_text().splitlines()
    header, rows = lines[0], lines[1:]
    out = [header]
    k = 0
    while len(out) - 1 < n:
        for row in rows:
            if len(out) - 1 >= n:
                break
            f = row.split(",")
            f[0] = f"{f[0]}_k{k}"
            f[5] = str(int(f[5]) + k * 1_000_000)
            if f[7]:
                f[7] = str(int(f[7]) + k * 1_000_000)
            out.append(",".join(f))
        k += 1
    dst.write_text("\n".join(out) + "\n")
    return len(out) - 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--et-root",
        type=Path,
        default=os.environ.get("QT_ET_ROOT"),
        required=not os.environ.get("QT_ET_ROOT"),
        help="local checkout of the et prototype (default: $QT_ET_ROOT)",
    )
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--groups", default="micro,day1,day1_disk,day2")
    ap.add_argument("--max-load", type=float, default=None, help="wait (<= 30 s) for load1 below this before a run")
    a = ap.parse_args()
    et = a.et_root.resolve()
    root = a.scratch.resolve()
    root.mkdir(parents=True, exist_ok=True)
    groups = a.groups.split(",")
    env0 = envmod.capture()
    replay = et / "target/release/replay"
    if not replay.exists():
        print(f"missing {replay}; build with `cargo build --release --locked -p replay` in et", file=sys.stderr)
        return 2
    import hashlib

    env0["binary_sha256"] = {"replay": hashlib.sha256(replay.read_bytes()).hexdigest()}
    env0["et_head"] = "06b2396 (SPEC 0; not re-read: agents run no git)"

    all_results: list[dict] = []
    derived: list[dict] = []

    def wd(cid: str):
        return lambda: root / cid

    # ---------------- micro: et's own ignored throughput tests (synthetic, in memory / temp DBN) -----------------
    if "micro" in groups:
        bins = test_binaries(et)

        def parse_replay(pr: ProcRun) -> dict[str, list[float]]:
            m = re.search(
                r"decode only ([\d.]+)M rec/s; replay NullSink ([\d.]+)M rec/s; replay CsvSink ([\d.]+)M", pr.stdout
            )
            if not m:
                raise ValueError("no throughput line")
            d, n, c = (float(x) * 1e6 for x in m.groups())
            return {"metric": [n], "decode_records_per_s": [d], "csv_sink_records_per_s": [c]}

        def parse_itch(pr: ProcRun) -> dict[str, list[float]]:
            m = re.search(
                r"parse ([\d.]+)M msg/s .*?parse\+convert ([\d.]+)M msg/s .*?gunzip\+parse ([\d.]+)M", pr.stdout
            )
            if not m:
                raise ValueError("no throughput line")
            p, c, g = (float(x) * 1e6 for x in m.groups())
            return {"metric": [c], "parse_only_msg_per_s": [p], "gunzip_parse_msg_per_s": [g]}

        micro = [
            Case(
                "et.micro.replay_2m_nullsink",
                "et.micro",
                lambda w: [
                    str(bins["replay_lib"]),
                    "--ignored",
                    "--nocapture",
                    "--exact",
                    "stress::throughput_2m_records",
                ],
                "records/s",
                "higher_is_better",
                parse=parse_replay,
                env={"TMPDIR": str(root / "et.micro.replay_2m_nullsink")},
                notes=[
                    "et stress::throughput_2m_records: generated DBN (2M records), replay to NullSink; extras: "
                    "decode-only and CsvSink rates",
                    "audit 2026-10-18: 10.6M (NullSink), 4.7M (CSV)",
                ],
            ),
            Case(
                "et.micro.itch_10m_parse_convert",
                "et.micro",
                lambda w: [
                    str(bins["itch_roundtrip"]),
                    "--ignored",
                    "--nocapture",
                    "--exact",
                    "throughput_10m_messages",
                ],
                "msg/s",
                "higher_is_better",
                parse=parse_itch,
                notes=[
                    "et itch roundtrip::throughput_10m_messages: in-memory synthetic ITCH, parse+convert",
                    "audit: parse 52.2M, parse+convert 6.05M, gunzip+parse 13.5M msg/s",
                ],
            ),
        ]
        raw = run_group(micro, root, warmup=a.warmup, repeat=a.repeat, max_load=a.max_load)
        all_results += [case_result(c, raw[c.id]) for c in micro]

    # ---------------- day one, sinks to /dev/null, interleaved ------------------------------------------------------
    d1 = et / DAY1["dir"]
    base1 = [
        str(replay),
        "run",
        "--mbo-csv",
        str(d1 / "mbo.csv"),
        "--venue",
        "nasdaq",
        "--midnight-ns",
        str(DAY1["midnight"]),
        "--sweep-gap-ns",
        "2000000",
    ]
    rd2_flags = ["--invariants", "--rank-trace", "--trades-csv", str(d1 / "trades.csv")]
    sh3k = et / "out/fidelity-itch/shadows.csv"
    if "day1" in groups:
        sh100k = root / "shadows_100k.csv"
        n100k = replicate_shadows(sh3k, sh100k, 100_000)
        ids = [ln.split(",")[0] for ln in sh3k.read_text().splitlines()[1:]]
        trace_flags = [x for i in ids for x in ("--queue-trace", i)]

        def mk(cid, extra, **kw):
            return Case(
                cid,
                "et.day1",
                lambda w, e=extra: base1 + e + ["--out", str(w / "out")],
                "records/s",
                "higher_is_better",
                work=DAY1["records"],
                setup=null_sinks(),
                cleanup=wipe,
                parse=checked(wd(cid), DAY1, **kw.pop("chk", {})),
                **kw,
            )

        day1 = [
            mk("et.day1.replay_plain", [], notes=["book only; bbo.csv/trades.csv still formatted, sent to /dev/null"]),
            mk(
                "et.day1.replay_rd2",
                rd2_flags,
                chk={"rank": True, "parity": True},
                notes=[
                    "RD-2 config: invariants + rank oracle/rank_trace + trade parity; CSV sinks to /dev/null",
                    "audit (sinks on disk): 5.71 s = 1.07M rec/s, 94 MB RSS",
                ],
            ),
            mk(
                "et.day1.replay_depth10",
                ["--depth", "10"],
                notes=[
                    "depth.csv with 10 levels/side after every F_LAST (to /dev/null): closest et analogue of "
                    "L2Builder depth 10 cadence 0"
                ],
            ),
            mk(
                "et.day1.shadows_3k",
                ["--shadows", str(sh3k)],
                chk={"shadows": 3000},
                notes=["et fidelity-itch/shadows.csv (3,000 mirror shadows, latency 0)"],
            ),
            mk(
                "et.day1.shadows_3k_trace",
                ["--shadows", str(sh3k), *trace_flags],
                chk={"shadows": 3000},
                notes=["+ --queue-trace for all 3,000 ids (et's analogue of trajectory `ahead`)"],
            ),
            mk(
                "et.day1.shadows_100k",
                ["--shadows", str(sh100k)],
                chk={"shadows": n100k},
                notes=["100,000 shadows: et's 3,000 replicated, ts shifted k ms per copy"],
            ),
            Case(
                "et.day1.read_floor",
                "et.day1",
                lambda w: ["/bin/sh", "-c", f"cat '{d1 / 'mbo.csv'}' > /dev/null"],
                "records/s",
                "higher_is_better",
                work=DAY1["records"],
                notes=["page-cache read of mbo.csv (538 MB) to /dev/null: I/O floor of the CSV path"],
            ),
        ]
        raw = run_group(day1, root, warmup=a.warmup, repeat=a.repeat, max_load=a.max_load)
        res = {c.id: case_result(c, raw[c.id]) for c in day1}
        all_results += list(res.values())
        p = res["et.day1.replay_plain"]
        derived += [
            derived_ratio(
                "et.day1.shadow_overhead_3k",
                res["et.day1.shadows_3k"],
                p,
                "throughput_loss",
                {"op": "<=", "value": 0.15, "spec": "SPEC 7.1 shadow overhead 3,000 (qt target)"},
            ),
            derived_ratio(
                "et.day1.shadow_overhead_100k",
                res["et.day1.shadows_100k"],
                p,
                "cost_ratio",
                {"op": "<=", "value": 2.5, "spec": "SPEC 7.1 shadow overhead 100,000 (qt target)"},
            ),
            derived_ratio(
                "et.day1.trace_extra",
                res["et.day1.shadows_3k_trace"],
                res["et.day1.shadows_3k"],
                "throughput_loss",
                {"op": "<=", "value": 0.10, "spec": "SPEC 7.1 trajectory ahead"},
            ),
            derived_ratio("et.day1.rd2_vs_plain", res["et.day1.replay_rd2"], p, "cost_ratio"),
            derived_ratio("et.day1.depth10_vs_plain", res["et.day1.replay_depth10"], p, "cost_ratio"),
        ]
        sh100k.unlink(missing_ok=True)

    # ---------------- day one, CSV sinks written to disk (audit-comparable), deleted after each run ---------------
    if "day1_disk" in groups:
        cid = "et.day1_disk.replay_rd2"
        disk = [
            Case(
                cid,
                "et.day1_disk",
                lambda w: base1 + rd2_flags + ["--out", str(w / "out")],
                "records/s",
                "higher_is_better",
                work=DAY1["records"],
                setup=real_sinks,
                cleanup=wipe,
                parse=checked(wd(cid), DAY1, rank=True, parity=True),
                notes=[
                    "RD-2 config with bbo.csv (360 MB), trades.csv, rank_trace.csv on disk (APFS SSD); "
                    "the configuration the audit timed at 5.71 s"
                ],
            )
        ]
        raw = run_group(disk, root, warmup=a.warmup, repeat=max(3, a.repeat - 2), max_load=a.max_load)
        all_results += [case_result(disk[0], raw[cid])]

    # ---------------- day two, sinks to /dev/null ------------------------------------------------------------------
    if "day2" in groups:
        d2 = et / DAY2["dir"]
        base2 = [
            str(replay),
            "run",
            "--mbo-csv",
            str(d2 / "mbo.csv"),
            "--venue",
            "nasdaq",
            "--midnight-ns",
            str(DAY2["midnight"]),
            "--sweep-gap-ns",
            "2000000",
        ]
        rd2_2 = ["--invariants", "--rank-trace", "--trades-csv", str(d2 / "trades.csv")]
        day2 = []
        for cid, extra, chk in (
            ("et.day2.replay_plain", [], {}),
            ("et.day2.replay_rd6", rd2_2, {"rank": True, "parity": True}),
        ):
            day2.append(
                Case(
                    cid,
                    "et.day2",
                    lambda w, e=extra: base2 + e + ["--out", str(w / "out")],
                    "records/s",
                    "higher_is_better",
                    work=DAY2["records"],
                    setup=null_sinks(),
                    cleanup=wipe,
                    parse=checked(wd(cid), DAY2, **chk),
                )
            )
        raw = run_group(day2, root, warmup=a.warmup, repeat=max(3, a.repeat - 2), max_load=a.max_load)
        all_results += [case_result(c, raw[c.id]) for c in day2]

    doc = results_doc(
        "et-reference",
        env0,
        {
            "warmup": a.warmup,
            "repeat": a.repeat,
            "groups": groups,
            "et_root": "<et-root>",
            "sinks": "CSV sinks symlinked to /dev/null except group et.day1_disk",
        },
        all_results,
        derived,
    )
    write_json(doc, a.out)
    for c in all_results:
        s = c["summary"]
        med = s["median"]
        print(
            f"{c['id']:40s} {c['status']:8s} median {med / 1e6 if med else float('nan'):8.3f}M {c['unit']:10s}"
            f" p10 {(s['p10'] or 0) / 1e6:7.3f}M p90 {(s['p90'] or 0) / 1e6:7.3f}M n={s['n']} "
            f"rss {(c['peak_rss_bytes_max'] or 0) / 2**20:6.0f}MiB"
        )
    for d in derived:
        print(f"{d['id']:40s} median {d['summary']['median']:.4f} ({d['kind']}) n={d['summary']['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
