"""Opt-in DBN conformance on Databento's own MBO test files (real DBN bytes, Apache-2.0, not committed here).

Fetches the MBO test files of github.com/databento/dbn at a pinned commit (the DBN 0.71.0 release, the version of the
`dbn` crate queuetruth decodes with) into `.realdata/dbn-samples/`, checks each against the sha256 recorded below,
then runs `queuetruth ingest --format dbn` and `queuetruth replay --rank-oracle --parity-trades` on each file and
writes `.realdata/dbn-samples/results.json` (counters and invariants only).

    uv run --frozen --no-sync python scripts/realdata/dbn_samples.py [--out .realdata/dbn-samples] [--offline]

Attribution: the files are test data of Databento's `dbn` repository (Copyright Databento, Inc., Apache License 2.0;
its LICENSE is fetched next to them). The records are a handful of ES futures (GLBX.MDP3) MBO messages. Downloads
happen only when this script is run by hand; `--offline` uses files already present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

COMMIT = "d368005a636bae84bff443cd6ddae6430a0ffd00"  # databento/dbn "VER: Release DBN 0.71.0", 2026-09-29
BASE = f"https://raw.githubusercontent.com/databento/dbn/{COMMIT}"
FILES = {  # path in the dbn repo -> sha256 recorded on first fetch (2026-10-19)
    "tests/data/test_data.mbo.dbn": "31403d17bf9801842acf59bca668e6b8f19ff4b5d70dce05f0fe70d995fc70d6",
    "tests/data/test_data.mbo.v1.dbn.zst": "553590d8f28e348de34877b71f7b676db3ccdbe515c8086b6211192c2c4e6519",
    "tests/data/test_data.mbo.v2.dbn.zst": "821461fc2a66ec68e049bd9a6fbe2b4822bfdddc0fc3bee7dea8fa4d8a3e585b",
    "tests/data/test_data.mbo.v3.dbn": "d2a526d952f845ab8f03ceac42ec828a2541e0b5e2e8a64df2e8bbaa1e898184",
    "tests/data/test_data.mbo.v3.dbn.zst": "6b2fcfa985cc34f6b40470929035945ce9c131a2f97ffcb736cc284094218c47",
}
TICK = "0.25"  # ES futures tick (the files carry no instrument definition)


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fetch(rel: str, dest: Path, offline: bool) -> None:
    if dest.exists() or offline:
        return
    with urllib.request.urlopen(f"{BASE}/{rel}", timeout=60) as r:
        dest.write_bytes(r.read())


def decoded_rows(ds: Path) -> list[dict[str, int]]:
    """The decoded MBO rows of a dataset (every partition, file order), as plain integers."""
    import pyarrow.parquet as pq

    cols = ["ts_event", "instrument_id", "action", "side", "price", "size", "order_id", "flags", "sequence"]
    out: list[dict[str, int]] = []
    for f in sorted((ds / "mbo").rglob("*.parquet")):
        for row in pq.read_table(f, columns=cols).to_pylist():
            out.append({k: int(v.value if hasattr(v, "value") else v) for k, v in row.items()})
    return out


def cli(*args: str, cwd: Path) -> tuple[int, Any, str]:
    exe = Path(sys.executable).parent / "queuetruth"
    p = subprocess.run([str(exe), "--format", "json", *args], cwd=cwd, capture_output=True, text=True, timeout=600)
    try:
        doc = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        doc = None
    return p.returncode, doc, p.stderr[-2000:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=Path(".realdata/dbn-samples"))
    ap.add_argument("--offline", action="store_true", help="use files already in --out; download nothing")
    a = ap.parse_args(argv)
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    fetch("LICENSE", out / "LICENSE", a.offline)
    results: dict[str, Any] = {}
    ok_all = True
    for rel, want in FILES.items():
        name = Path(rel).name
        f = out / name
        fetch(rel, f, a.offline)
        r: dict[str, Any] = {"source": f"{BASE}/{rel}"}
        if not f.exists():
            r["status"] = "missing"
            ok_all = False
            results[name] = r
            continue
        got = sha256(f)
        r.update(sha256=got, bytes=f.stat().st_size, sha256_ok=got == want)
        work = out / f"work-{name}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        rc, ing, err = cli("ingest", "--format", "dbn", "--in", str(f), "--venue", "cme", "--tick-size", TICK,
                           "--out", "ds", cwd=work)  # fmt: skip
        r["ingest_exit"] = rc
        if rc == 0 and ing:
            man = ing["manifest"]
            r["ingest"] = {
                "partitions": [(p["instrument_id"], p["rows"]) for p in man["partitions"]],
                "records": sum(p["rows"] for p in man["partitions"]),
                "verdict": ing["data_quality"]["verdict"],
                "reasons": ing["data_quality"]["reasons"],
            }
            rc2, rep, err2 = cli("replay", "--data", "ds", "--out", "replay", "--rank-oracle", "--parity-trades",
                                 cwd=work)  # fmt: skip
            r["replay_exit"] = rc2
            if rc2 == 0 and rep:
                r["replay"] = {
                    "stats": rep["stats"],
                    "invariants": rep["invariants"],
                    "parity_pass": (rep.get("parity") or {}).get("pass"),
                    "executions_checked": (rep.get("rank_oracle") or {}).get("executions_checked"),
                }
            else:
                r["replay_error"] = (rep or {}).get("message") or err2
        else:
            r["ingest_error"] = (ing or {}).get("message") or err
        rows = decoded_rows(work / "ds")
        r["decoded_rows_sha256"] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        # the files are a mid-stream excerpt: a cancel/modify/fill of an order the book never saw is an unknown
        # order reference by design; every other invariant must be 0
        seen: set[int] = set()
        expect_unknown = 0
        for row in rows:
            if row["action"] == ord("A"):
                seen.add(row["order_id"])
            elif row["action"] in (ord("C"), ord("M"), ord("F")) and row["order_id"] not in seen:
                expect_unknown += 1
        inv = (r.get("replay") or {}).get("invariants")
        inv_ok = inv is not None and inv.pop("unknown_order_refs", None) == expect_unknown and not any(inv.values())
        r["expected_unknown_order_refs"] = expect_unknown
        r["status"] = "pass" if (r["sha256_ok"] and rc == 0 and r.get("replay_exit") == 0 and inv_ok) else "fail"
        ok_all &= r["status"] == "pass"
        shutil.rmtree(work, ignore_errors=True)
        results[name] = r
    digests = {r.get("decoded_rows_sha256") for r in results.values()}
    same = len(digests) == 1 and None not in digests
    ok_all &= same
    doc = {
        "schema": "qtdev.dbn_samples",
        "all_encodings_decode_identically": same,
        "commit": COMMIT,
        "license": "Apache-2.0 (databento/dbn)",
        "files": results,
    }
    (out / "results.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    for name, r in results.items():
        print(f"{name:28s} {r['status']:5s} {r.get('ingest', {}).get('records')} records")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
