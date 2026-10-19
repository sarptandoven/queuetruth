"""O-ZOO-2 (Python side): the transliteration `reference/hbt_ref.py` against real hftbacktest and the fixtures.

- Regenerates the 200-sequences-per-family parity fixtures in memory and compares them with the committed Parquet
  (the Rust test `o_zoo_2_transliteration_parity` reads the same files).
- Replays every recorded hook of the adopted real-hftbacktest runs (`tests/fixtures/zoo/hbt_real/hooks.json.gz`,
  5f3ec40 Rust build == 2.4.4 wheel) through `hbt_ref.QueueModel`, chained from `new_order`; every F, T and
  `is_filled` return must match bit for bit.
- The O-ZOO-1 rows computed by hbt's own code reproduce through `hbt_ref`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq
from reference import hbt_ref

from zoo import gen_fixtures

FIX = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "zoo"


def test_o_zoo_2_regenerated_fixtures_equal_committed() -> None:
    seq, exp = gen_fixtures.build()
    assert pq.read_table(FIX / "hbt_parity" / "sequences.parquet").to_pylist() == seq.to_pylist()
    assert pq.read_table(FIX / "hbt_parity" / "expected.parquet").to_pylist() == exp.to_pylist()
    assert len(set(seq.column("seq_id").to_pylist())) == 6 * gen_fixtures.N_PER_FAMILY
    errors = sum(exp.column("error").to_pylist())
    assert errors > 0, "the #312 zombie error must be exercised"


def test_o_zoo_2_hbt_ref_matches_real_hftbacktest_hooks() -> None:
    runs = json.loads((FIX / "hbt_real" / "runs.json").read_text())["runs"]
    scenarios = {s["id"]: s for s in json.loads((FIX / "hbt_real" / "scenarios.json").read_text())["scenarios"]}
    with gzip.open(FIX / "hbt_real" / "hooks.json.gz", "rt") as fh:
        hooks = json.loads(fh.read())
    models: dict[int, hbt_ref.QueueModel] = {}
    states: dict[tuple[int, int], list[float]] = {}
    checked = 0
    for run, _step, oid, hook, a, b, ret, f, t in hooks:
        r = runs[run]
        if run not in models:
            mid = r["model_id"]
            fam = mid.split("[", 1)[0]
            params = dict(kv.split("=") for kv in mid[len(fam) + 1 : -1].split(";"))
            n = float(params["n"]) if "n" in params else None
            models[run] = hbt_ref.QueueModel(fam, n, scenarios[r["scenario"]]["lot_size"])
        m = models[run]
        key = (run, oid)
        if hook == 0:
            states[key] = m.new_order(hbt_ref.b2f(f))
        elif hook == 1:
            m.trade(states[key], hbt_ref.b2f(a))
        elif hook == 2:
            m.depth(states[key], hbt_ref.b2f(a), hbt_ref.b2f(b))
        else:
            assert hbt_ref.f2b(m.is_filled(states[key])) == ret, (r["scenario"], r["model_id"], oid)
        s = states[key]
        assert hbt_ref.f2b(s[0]) == f, (r["scenario"], r["model_id"], oid, hook)
        if t is not None:
            assert hbt_ref.f2b(s[1]) == t
        checked += 1
    assert checked == len(hooks) > 40_000


def test_o_zoo_1_rows_through_hbt_ref() -> None:
    g = json.loads((FIX / "hbt_real" / "golden_ozoo1_rust.json").read_text())
    for row in g["prob_rows"]:
        words = row["case"].replace(",", "").split()
        fam = "hbt_" + words[0]
        n = next((float(w[2:]) for w in words if w.startswith("n=")), None)
        m = hbt_ref.QueueModel(fam, n, 1.0)
        s = [hbt_ref.b2f(int(row["F_bits"])), float(row["T"])]
        m.depth(s, float(row["prev"]), float(row["new"]))
        assert hbt_ref.f2b(s[0]) == int(row["F_after_bits"]), row["case"]


def test_zoo_fixture_manifest() -> None:
    lines = (FIX / "MANIFEST.sha256").read_text().splitlines()
    assert lines
    for line in lines:
        digest, rel = line.split("  ", 1)
        assert hashlib.sha256((FIX / rel).read_bytes()).hexdigest() == digest, rel


def test_o_zoo_4_l3_faithful_fixture_regenerates() -> None:
    committed = json.loads((FIX / "l3_faithful.json").read_text())
    assert committed == json.loads(json.dumps(gen_fixtures.build_l3()))
    kinds = {(s["expected"] or [0, None])[1] for s in committed["sequences"]}
    assert {"queue", "through", "cross", "expired", None} <= kinds
