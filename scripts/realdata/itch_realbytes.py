#!/usr/bin/env python3
"""queuetruth on real Nasdaq ITCH 5.0 bytes : its own ITCH reader on a streamed subset, then the RD-2 /
RD-5 replays, record identity with the earlier prototype (et), and the independent shadow oracle with true P / Q prints.

    uv run --frozen --no-sync python scripts/realdata/itch_realbytes.py --date 12302019
    uv run --frozen --no-sync python scripts/realdata/itch_realbytes.py --date 01302020

Input: the subset written by `itch_subset.py` (`.realdata/itch/<date>.subset.<SYMS>.itch50.gz` + manifest). Its
sha256 is re-checked against the manifest first, so a run is repeatable from the subset alone.

Steps (each a check block in `.realdata/itch/realbytes-<date>.json`, every number with its provenance):
- ITCH-1 ingest: `queuetruth ingest --format itch` (CLI, `uv run --frozen --no-sync`) on the subset; partitions,
  rows, ts ranges, instruments, action counts and trades_source rows against `expected.json` (et converter
  artifacts); per-instrument sha256 of every MBO record and every source print rendered as et `mbo.csv` /
  `trades.csv` lines against the same digests of the et CSVs (byte identity of the two converters); trades_source
  `kind` split (hidden = ITCH P, cross = ITCH Q with shares > 0) against the P / Q counts seen in the raw stream.
- ITCH-2 replay: `validate_et.rd_replay_exact` (the RD-2 code of the realdata driver, unchanged) on the ITCH
  dataset: records, events, trades, 8 invariants, rank oracle (raw and sweep-adjusted shares, buckets, first 60 s),
  trade parity, engine BBO parity with et `bbo.csv`, per-instrument BBO and rank_trace sha256.
- ITCH-5 shadows: `validate_et.rd_shadows` (fill_rule = et, hidden_print_policy = ignore) on et's 3,000 mirror
  shadows (`shadows.csv`): per-shadow equality with et `shadow_fills.csv`, queue_trace digest, SPEC-reference fills.
- ITCH-O oracle: `scripts/oracle/run_oracle.py --dataset <itch ds> --shadows <et shadows.csv> --run-qt` with the
  cross prints taken from the real Q messages (trades_source kind = cross; `--cross-window-s 0` so the time-window
  heuristic needed for et CSVs is off), i.e. the A3d hidden-print rule and the A4b off-price rule judged on real
  P prints only; agreement counts by category. `--oracle-windows 0,1` also runs the et-CSV heuristic (1 s windows)
  for comparison.

The earlier prototype (et) is optional. Without `--et-root` (or `QT_ET_ROOT`), ITCH-1 checks the ingest against
the committed `expected.json` counts and the raw-stream P / Q counts only (the et CSV digests and stats.json checks
are skipped), and ITCH-2 / ITCH-5 / ITCH-O, which compare with et's bbo.csv, shadow_fills.csv and mirror shadows, are
skipped. The public reproduction without any et artifact is `scripts/repro/reproduce.py`.

Disk: free space on / is checked before every step (abort below `--min-free-gib`, default 2.5). Intermediates (the
dataset, replay and oracle directories) are deleted at the end unless `--keep`; small JSON outputs stay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import validate_et as V  # noqa: E402  (the realdata driver: same replay calls, renderers and comparison rules)

OUT_DEFAULT = REPO / ".realdata" / "itch"
DATES = {"12302019": V.DAY1, "01302020": V.DAY2}
UV = ["uv", "run", "--frozen", "--no-sync"]
GIB = 1 << 30


class DiskGuard(RuntimeError):
    pass


def free_gib() -> float:
    return shutil.disk_usage("/").free / GIB


def guard(min_free: float, where: str) -> float:
    fg = free_gib()
    if fg < min_free:
        raise DiskGuard(f"{where}: free space {fg:.2f} GiB < {min_free} GiB")
    return fg


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def core_build() -> dict[str, Any]:
    """Provenance of the queuetruth extension and CLI binary in use (other agents rebuild them while this runs)."""
    out: dict[str, Any] = {}
    for key, p in (("_core", REPO / "python" / "queuetruth" / "_core.abi3.so"),):
        if p.exists():
            out[key] = {
                "sha256": sha256_file(p),
                "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p.stat().st_mtime)),
            }
    return out


def run(cmd: list[str], log: Path) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    log.write_text(
        f"$ {' '.join(cmd)}\n[exit {r.returncode}, {wall:.1f} s]\n--- stdout\n{r.stdout}\n--- stderr\n{r.stderr}"
    )
    return r.returncode, r.stdout, wall


# ---- et CSV digests (streamed) -----------------------------------------------------------------------------------


def et_csv_digests(path: Path, inst_col: int) -> tuple[dict[str, str], dict[str, int]]:
    """Per-instrument sha256 over the data lines of an et CSV in file order (each line with its '\\n')."""
    hs: dict[str, Any] = {}
    n: dict[str, int] = defaultdict(int)
    with open(path, "rb") as f:
        f.readline()
        for ln in f:
            i = ln.split(b",", inst_col + 1)[inst_col].decode()
            h = hs.get(i)
            if h is None:
                h = hs[i] = hashlib.sha256()
            h.update(ln)
            n[i] += 1
    return {k: hs[k].hexdigest() for k in sorted(hs, key=int)}, {k: n[k] for k in sorted(n, key=int)}


def qt_mbo_digests(ds: Path) -> tuple[dict[str, str], dict[str, int], dict[str, dict[str, int]]]:
    """qt MBO partitions rendered as et mbo.csv lines; also action counts per instrument."""
    man = json.loads((ds / "manifest.json").read_text())
    cols = [
        "ts_event",
        "ts_recv",
        "instrument_id",
        "action",
        "side",
        "price",
        "size",
        "order_id",
        "flags",
        "sequence",
        "channel_id",
    ]
    hs, n, acts = {}, {}, {}
    for p in man["partitions"]:
        i = str(p["instrument_id"])
        h = hashlib.sha256()
        cnt = 0
        ac: dict[str, int] = defaultdict(int)
        for t, tr, iid, a, s, px, sz, oid, fl, q, ch in V.iter_cols([ds / f["path"] for f in p["files"]], cols):
            h.update(f"{t},{tr},{iid},{chr(a)},{chr(s)},{px},{sz},{oid},{fl},{q},{ch}\n".encode())
            ac[chr(a)] += 1
            cnt += 1
        hs[i], n[i], acts[i] = h.hexdigest(), cnt, dict(sorted(ac.items()))
    order = sorted(hs, key=int)
    return {k: hs[k] for k in order}, {k: n[k] for k in order}, {k: acts[k] for k in order}


def qt_trade_digests(
    ds: Path, windows_ns: list[tuple[int, int]]
) -> tuple[dict[str, str], dict[str, int], dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """qt trades_source rendered as et trades.csv lines (per instrument, in partition order); kind counts; and the
    side-N prints inside the oracle's et-CSV cross windows ([open, open + 1 s), [close, close + 1 s)) by kind."""
    win: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    hs: dict[str, Any] = {}
    n: dict[str, int] = defaultdict(int)
    kinds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    root = ds / "trades_source"
    files = sorted(root.rglob("*.parquet"), key=lambda p: (int(p.parent.name.split("=")[1]), p.name))
    cols = ["ts_event", "instrument_id", "price", "size", "aggressor_side", "sequence", "kind"]
    for t, i, px, sz, ag, q, k in V.iter_cols(files, cols, dict_cols=("kind",)):
        key = str(i)
        h = hs.get(key)
        if h is None:
            h = hs[key] = hashlib.sha256()
        h.update(f"{t},{i},{px},{sz},{chr(ag)},{q}\n".encode())
        n[key] += 1
        kinds[key][k] += 1
        if k != "displayed":
            inside = any(lo <= t < hi for lo, hi in windows_ns)
            win[key][f"{k}_{'in' if inside else 'outside'}_windows"] += 1
    order = sorted(hs, key=int)
    return (
        {k: hs[k].hexdigest() for k in order},
        {k: n[k] for k in order},
        {k: dict(sorted(kinds[k].items())) for k in order},
        {k: dict(sorted(win[k].items())) for k in order},
    )


# ---- steps -----------------------------------------------------------------------------------------------------


def step_ingest(ctx: V.Ctx, a: argparse.Namespace, date: str, sub: Path, sman: dict, work: Path) -> V.Check:
    c = V.Check("ITCH-1", f"queuetruth ingest --format itch on the real-bytes subset {date}")
    E = ctx.exp["days"][date]
    e = E["ingest"]
    ds = work / "ds"
    if ds.exists():
        shutil.rmtree(ds)
    cmd = [*UV, "queuetruth", "--output-format", "json", "ingest", "--format", "itch", "--in", str(sub)]
    cmd += ["--venue", "nasdaq", "--out", str(ds), "--symbols", ",".join(a.symbols)]
    cmd += ["--price-exp", "-9", "--tick-size", "0.01", "--lot-size", "1"]
    rc, out, wall = run(cmd, work / "ingest.log")
    c.eq("exit code", rc, 0)
    if rc != 0:
        c.notes.append((work / "ingest.log").read_text()[-3000:])
        return c
    ctx.ds[date] = ds
    try:
        summ = json.loads(out)
    except json.JSONDecodeError:
        summ = {}
    man = json.loads((ds / "manifest.json").read_text())
    dq = json.loads((ds / "data_quality.json").read_text())
    c.eq("source_format", man["source_format"], "itch")
    c.eq(
        "input sha256 == subset sha256 (manifest)",
        [i["sha256"] for i in man["inputs"]],
        [sman["subset"]["file_sha256"]],
    )
    c.eq("partitions", len(man["partitions"]), e["partitions"])
    c.eq("instrument_ids", sorted(p["instrument_id"] for p in man["partitions"]), e["instrument_ids"])
    c.eq("records", sum(p["rows"] for p in man["partitions"]), e["records"])
    c.eq("rows_by_instrument", {str(p["instrument_id"]): p["rows"] for p in man["partitions"]}, e["rows_by_instrument"])
    c.eq(
        "ts_min_by_instrument",
        {str(p["instrument_id"]): p["ts_min"] for p in man["partitions"]},
        e["ts_min_by_instrument"],
    )
    c.eq(
        "ts_max_by_instrument",
        {str(p["instrument_id"]): p["ts_max"] for p in man["partitions"]},
        e["ts_max_by_instrument"],
    )
    c.eq("data_quality.records", dq["records"], e["records"])
    c.eq("venue", man["venue"], "nasdaq")
    c.eq("validation_status", man["validation_status"], "real_data")
    inst = pq.read_table(ds / "instruments.parquet").to_pylist()
    c.eq("session_date (from the MMDDYYYY file name)", sorted({str(r["session_date"]) for r in inst}), [date])
    c.eq("instruments midnight_utc_ns", sorted({r["midnight_utc_ns"] for r in inst}), [E["midnight_utc_ns"]])
    c.eq(
        "instruments open_s/close_s", sorted({(r["open_s"], r["close_s"]) for r in inst}), [(E["open_s"], E["close_s"])]
    )
    c.eq("instruments tick_size", sorted({r["tick_size"] for r in inst}), [10_000_000])
    c.eq("instruments price_exp", sorted({r["price_exp"] for r in inst}), [-9])
    c.eq("symbols (from R messages)", {str(r["instrument_id"]): r["symbol"] for r in inst}, E["instruments"])
    c.eq(
        "locates (subset manifest) == et instrument ids",
        {str(v): k for k, v in sman["selection"]["locates"].items()},
        E["instruments"],
    )
    # record identity with the et converter (only with --et-root)
    has_et = a.et_root is not None
    t0 = time.perf_counter()
    q_dig, q_n, q_act = qt_mbo_digests(ds)
    if has_et:
        e_dig, e_n = et_csv_digests(ctx.et_path(date, "itch", "mbo.csv"), 2)
        c.eq("mbo rows by instrument (qt render) == et mbo.csv", q_n, e_n)
        c.eq("mbo per-instrument sha256 (et mbo.csv render) == et mbo.csv", q_dig, e_dig)
    else:
        c.eq("mbo rows by instrument (qt render) == expected.json rows_by_instrument", q_n, e["rows_by_instrument"])
        c.record(mbo_et_render_sha256_by_instrument=q_dig)
    c.eq(
        "action counts by instrument",
        {k: {x: v[x] for x in v if x in "ACFT"} for k, v in q_act.items()},
        e["action_counts_by_instrument"],
    )
    tot: dict[str, int] = defaultdict(int)
    for v in q_act.values():
        for k2, n2 in v.items():
            tot[k2] += n2
    c.eq("action counts (A C F T M R)", {k: tot.get(k, 0) for k in "ACFTMR"}, e["action_counts"])
    mid = E["midnight_utc_ns"]
    windows = [(mid + E[x] * 10**9, mid + (E[x] + 1) * 10**9) for x in ("open_s", "close_s")]
    t_dig, t_n, t_kind, t_win = qt_trade_digests(ds, windows)
    c.eq("trades_source rows", sum(t_n.values()), e["trades_source_rows"])
    c.eq("trades_source rows by instrument", t_n, e["trades_source_rows_by_instrument"])
    if has_et:
        et_t_dig, et_t_n = et_csv_digests(ctx.et_path(date, "itch", "trades.csv"), 1)
        c.eq("trades_source per-instrument sha256 (et trades.csv render) == et trades.csv", t_dig, et_t_dig)
        c.eq("et trades.csv rows by instrument", et_t_n, e["trades_source_rows_by_instrument"])
    else:
        c.record(trades_et_render_sha256_by_instrument=t_dig)
    render_s = round(time.perf_counter() - t0, 1)
    # P / Q split from the raw bytes
    loc2sym = {str(v): k for k, v in sman["selection"]["locates"].items()}
    pr = sman["selection"]["prints_by_symbol"]
    hid = {k: v.get("hidden", 0) for k, v in t_kind.items()}
    crs = {k: v.get("cross", 0) for k, v in t_kind.items()}
    c.eq(
        "hidden + cross prints by instrument == et side-N prints",
        {k: hid[k] + crs[k] for k in hid},
        e["side_N_trade_prints_by_instrument"],
    )
    c.eq("hidden prints by instrument == ITCH P messages (raw stream)", hid, {k: pr[loc2sym[k]]["P"] for k in hid})
    c.eq(
        "cross prints by instrument == ITCH Q messages with shares > 0 (raw stream)",
        crs,
        {k: pr[loc2sym[k]]["Q"] - pr[loc2sym[k]]["Q_zero_shares"] for k in crs},
    )
    src = dq.get("source_specific", {})
    c.eq(
        "itch_zero_share_cross == Q messages with 0 shares (raw stream)",
        src.get("itch_zero_share_cross", 0),
        sum(v["Q_zero_shares"] for v in pr.values()),
    )
    if has_et:
        st = json.loads(ctx.et_path(date, "itch", "stats.json").read_text())
        c.eq(
            "itch_zero_share_cross == et stats.json anomalies",
            src.get("itch_zero_share_cross", 0),
            st["anomalies"]["zero_share_cross"],
        )
    c.eq(
        "other ITCH mapper anomalies (all 0 in et stats.json)",
        {k: v for k, v in src.items() if k.startswith("itch_") and k != "itch_zero_share_cross" and v},
        {},
    )
    c.record(
        cli=" ".join(cmd),
        ingest_wall_s=round(wall, 2),
        records_per_s=round(e["records"] / wall) if wall else None,
        subset_messages=sman["subset"]["messages"],
        subset_messages_per_s=round(sman["subset"]["messages"] / wall) if wall else None,
        dataset_mb=V.du_mb(ds),
        dataset_id=man["dataset_id"],
        data_quality={k: dq[k] for k in dq if k not in ("schema", "schema_version", "feed_gaps")},
        trades_kind_by_instrument=t_kind,
        side_n_vs_oracle_1s_cross_windows=t_win,
        side_n_vs_oracle_1s_cross_windows_total={
            k2: sum(v.get(k2, 0) for v in t_win.values())
            for k2 in ("hidden_in_windows", "hidden_outside_windows", "cross_in_windows", "cross_outside_windows")
        },
        q_cross_types_by_symbol={s: v["Q_cross_type"] for s, v in pr.items()},
        q_zero_share_by_symbol={s: v["Q_zero_shares"] for s, v in pr.items()},
        render_check_s=render_s,
        summary_keys=sorted(summ)[:20] if isinstance(summ, dict) else None,
    )
    if dq.get("verdict") != "clean":
        c.notes.append(f"data-quality verdict {dq.get('verdict')}: {dq.get('reasons')}")
    return c


def step_oracle(ctx: V.Ctx, date: str, work: Path, window: float, tag: str) -> V.Check:
    c = V.Check(
        f"ITCH-O{tag}", f"oracle vs queuetruth (exact rules) on et mirror shadows, cross_window_s={window} {date}"
    )
    out = work / f"oracle_w{tag}"
    if out.exists():
        shutil.rmtree(out)
    shadows = ctx.et_path(date, "fidelity", "shadows.csv")
    cmd = [*UV, "python", "scripts/oracle/run_oracle.py", "--dataset", str(ctx.ds[date]), "--shadows", str(shadows)]
    cmd += ["--run-qt", "--cross-window-s", str(window), "--out", str(out)]
    rc, stdout, wall = run(cmd, work / f"oracle_w{tag}.log")
    c.eq("exit code", rc, 0)
    if rc != 0:
        c.notes.append((work / f"oracle_w{tag}.log").read_text()[-3000:])
        return c
    summ = json.loads((out / "oracle_summary.json").read_text())
    rep_p = out / "discrepancy_report.json"
    c.true("discrepancy report written (qt replay ran)", rep_p.exists())
    if not rep_p.exists():
        return c
    rep = json.loads(rep_p.read_text())
    s = rep["summary"]
    qsum = {}
    for p in sorted((out / "qt_replay").glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "shadows" in d:
            qsum = d
    c.eq("n_compared", s["n_compared"], 3000)
    c.record(
        n_agree=s["n_agree"],
        n_disagree=s["n_disagree"],
        by_category=s["by_category"],
        by_kind=s["by_kind"],
        filled_qty_direction_by_category=s["filled_qty_direction_by_category"],
        filled_qty_total=s["filled_qty_total"],
        oracle_end_states=summ["end_states"],
        oracle_stats=summ["stats"],
        oracle_wall_s=summ["wall_s"],
        wall_s=round(wall, 1),
        qt_shadow_summary=qsum.get("shadows"),
        qt_fill_rule=(qsum.get("spec") or {}).get("fill_rule") if isinstance(qsum.get("spec"), dict) else None,
        stdout_tail=stdout.strip().splitlines()[-3:],
        discrepancies_first=[
            {k: d[k] for k in ("shadow_id", "category", "categories_at_divergence", "ts_divergence")}
            for d in rep["discrepancies"][:40]
        ],
    )
    return c


# ---- main ------------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, choices=sorted(DATES))
    ap.add_argument("--symbols", default="AAPL,MSFT,SPY,TSLA")
    ap.add_argument(
        "--et-root",
        type=Path,
        default=Path(os.environ["QT_ET_ROOT"]) if os.environ.get("QT_ET_ROOT") else None,
        help="path to the author's earlier private prototype (et) outputs, optional; also QT_ET_ROOT. Without it the "
        "et comparisons are skipped",
    )
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--min-free-gib", type=float, default=2.5)
    ap.add_argument("--oracle-windows", default="0", help="comma list of oracle --cross-window-s values ('' = skip)")
    ap.add_argument("--skip", default="", help="comma list of ITCH-1,ITCH-2,ITCH-5,ITCH-O")
    ap.add_argument("--keep", action="store_true", help="keep the dataset, replay and oracle directories")
    a = ap.parse_args(argv)
    a.symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    skip = {x.strip() for x in a.skip.split(",") if x.strip()}
    date = DATES[a.date]
    tag = "-".join(a.symbols)
    sub = a.out / f"{a.date}.subset.{tag}.itch50.gz"
    sman = json.loads((a.out / f"{a.date}.subset.{tag}.manifest.json").read_text())
    work = a.out / f"work-{a.date}"
    work.mkdir(parents=True, exist_ok=True)
    res_path = a.out / f"realbytes-{a.date}.json"
    t_all = time.perf_counter()
    docs: dict[str, Any] = {}
    prev = json.loads(res_path.read_text()).get("results", {}) if res_path.exists() else {}
    meta: dict[str, Any] = {
        "schema": "qt.realdata.itch_realbytes",
        "date": date,
        "subset": str(sub),
        "subset_manifest_ok": sman.get("ok"),
        "expected": str(HERE / "expected.json"),
        "et_root": str(a.et_root) if a.et_root else None,
        "free_gib_start": round(free_gib(), 2),
    }

    def save() -> None:
        res_path.write_text(V.jdump({**meta, "results": docs}) + "\n")

    try:
        guard(a.min_free_gib, "start")
        got = sha256_file(sub)
        meta["subset_sha256"] = got
        if got != sman["subset"]["file_sha256"]:
            raise RuntimeError(f"subset sha256 {got} != manifest {sman['subset']['file_sha256']}")
        ctx = V.Ctx(
            et=a.et_root or Path("/nonexistent-et-root"),
            out=work,
            exp=V.load_expected(None, a.et_root, work),
            threads=a.threads,
            keep=a.keep,
        )
        steps = [
            ("ITCH-1", lambda: step_ingest(ctx, a, date, sub, sman, work)),
            ("ITCH-2", lambda: V.rd_replay_exact(ctx, date, "ITCH-2")),
            ("ITCH-5", lambda: V.rd_shadows(ctx, date, "ITCH-5")),
        ]
        windows = [x.strip() for x in a.oracle_windows.split(",") if x.strip()]
        carried = [k for k in prev if k.startswith("ITCH-O") and k[len("ITCH-O") :] not in windows]
        for w in windows:
            steps.append((f"ITCH-O{w}", lambda w=w: step_oracle(ctx, date, work, float(w), w)))
        for name in carried:  # oracle runs of a previous invocation that this one does not repeat
            if prev[name].get("status") not in (None, "skipped"):
                docs[name] = {**prev[name], "from_previous_run": True}
        et_steps = {"ITCH-2", "ITCH-5", "ITCH-O"}
        for name, fn in steps:
            base = name if not name.startswith("ITCH-O") else "ITCH-O"
            if a.et_root is None and base in et_steps:
                docs[name] = {
                    "rd": name,
                    "status": "skipped",
                    "notes": ["needs --et-root (et bbo.csv / shadows); see scripts/repro/reproduce.py"],
                }
                continue
            if base in skip or name in skip:
                docs[name] = (
                    {**prev[name], "from_previous_run": True}
                    if name in prev and prev[name].get("status") != "skipped"
                    else {"rd": name, "status": "skipped"}
                )
                continue
            if name != "ITCH-1" and date not in ctx.ds:
                docs[name] = {"rd": name, "status": "skipped", "notes": ["ITCH-1 did not produce a dataset"]}
                continue
            fg = guard(a.min_free_gib, name)
            build_before = core_build()
            print(f"[{name}] ... (free {fg:.2f} GiB)", file=sys.stderr, flush=True)
            t0 = time.perf_counter()
            try:
                docs[name] = fn().doc()
            except DiskGuard:
                raise
            except Exception as e:
                docs[name] = {
                    "rd": name,
                    "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc()[-4000:],
                }
            docs[name]["wall_s"] = round(time.perf_counter() - t0, 2)
            docs[name]["qt_build_before"] = build_before
            docs[name]["qt_build_after"] = core_build()
            docs[name]["free_gib_after"] = round(free_gib(), 2)
            docs[name]["work_mb_after"] = V.du_mb(work)
            print(f"[{name}] {docs[name]['status']} in {docs[name]['wall_s']} s", file=sys.stderr, flush=True)
            save()
    except DiskGuard as e:
        meta["aborted"] = str(e)
        print(f"ABORT: {e}", file=sys.stderr)
    finally:
        meta["wall_s"] = round(time.perf_counter() - t_all, 1)
        meta["peak_rss"] = V.peak_rss_mb()
        meta["work_mb_peak_end"] = V.du_mb(work)
        if not a.keep:
            keep_small = work / "kept"
            keep_small.mkdir(exist_ok=True)
            for p in list(work.rglob("*.json")) + list(work.rglob("*.md")) + list(work.glob("*.log")):
                if keep_small in p.parents or p.stat().st_size > 8 << 20:
                    continue
                rel = p.relative_to(work)
                dst = keep_small / str(rel).replace("/", "__")
                shutil.copy2(p, dst)
            for p in work.iterdir():
                if p != keep_small:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
        meta["free_gib_end"] = round(free_gib(), 2)
        save()
    st = {k: v.get("status") for k, v in docs.items()}
    print(json.dumps({"statuses": st, "wall_s": meta["wall_s"], "results": str(res_path)}, indent=1))
    return 0 if docs and all(s in ("pass", "skipped") for s in st.values()) and "aborted" not in meta else 1


if __name__ == "__main__":
    sys.exit(main())
