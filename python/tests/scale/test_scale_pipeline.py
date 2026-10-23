"""W1 scale pipeline (`scripts/scale/`, `queuetruth.eval.blocks`): splitting never changes what a shard computes.

- `itch_split.py` routes every message of a symbol (from its `R` on) to one bucket and locate 0 to all; a bucket's
  ITCH ingest gives byte-identical partitions to an ingest of the same symbols from the unsplit day, and the same
  rows up to the numbering of `sequence` (an ITCH ingest numbers records across the selected locates) otherwise;
- `allocation: per_partition` draws a partition's shadows independently of the other partitions;
- `block_stats` + `evaluate_blocks` over per-instrument shards reproduce `evaluate_zoo` (value, CI, n) cell by cell;
- the Merkle proofs and the content digest of the manifests.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil

import common as K
import itch_split
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from scale_itch import FIXTURES, write_itch_day

from queuetruth.eval import blocks as B

S1 = 1_000_000_000
SYMS = ["AAA", "BBBB", "CC", "DDDDD", "EEE"]


def _split(tmp_path, src, k: int, symbols: str | None = None) -> dict:
    a = argparse.Namespace(
        date="01022020",
        url=f"file://{src}",
        buckets=k,
        out=tmp_path / "split",
        symbols=symbols,
        allow_missing=False,
        level=1,
        min_free_gib=0.0,
    )
    assert itch_split.run_split(a) == 0
    return json.loads((tmp_path / "split" / "01022020" / "split_manifest.json").read_text())


def _frames(raw: bytes) -> list[bytes]:
    out, pos = [], 0
    while pos < len(raw):
        n = int.from_bytes(raw[pos : pos + 2], "big")
        out.append(raw[pos : pos + 2 + n])
        pos += 2 + n
    return out


def test_split_routes_every_symbol_to_one_bucket_in_file_order(tmp_path) -> None:
    src = tmp_path / "01022020.NASDAQ_ITCH50.gz"
    raw = write_itch_day(src, SYMS, orders_per_symbol=60)
    m = _split(tmp_path, src, 3)
    assert m["ok"], [k for k, c in m["checks"].items() if c["ok"] is False]
    loc = {s: i + 1 for i, s in enumerate(SYMS)}
    frames = _frames(raw)
    seen = set()
    for b in m["buckets"]:
        syms = set(b["symbols"])
        assert all(itch_split.bucket_of(s, 3) == b["bucket"] for s in syms)
        seen |= syms
        locs = {0} | {loc[s] for s in syms}
        want = b"".join(f for f in frames if int.from_bytes(f[3:5], "big") in locs)
        got = gzip.decompress((tmp_path / "split" / "01022020" / b["file"]).read_bytes())
        assert got == want
        assert b["messages"] == len(_frames(want))
    assert seen == set(SYMS)
    # --symbols keeps only those (every other R and message dropped)
    m2 = _split(tmp_path / "sub", src, 2, "AAA,CC")
    assert sorted(s for b in m2["buckets"] for s in b["symbols"]) == ["AAA", "CC"]


@pytest.mark.requires_core
def test_bucket_ingest_equals_symbol_subset_ingest(tmp_path) -> None:
    import queuetruth as qt

    src = tmp_path / "01022020.NASDAQ_ITCH50.gz"
    write_itch_day(src, SYMS, orders_per_symbol=150)
    m = _split(tmp_path, src, 2)

    def ingest(path, syms, name):
        out = tmp_path / name
        qt.ingest(
            {"format": "itch", "inputs": [{"path": str(path)}], "out": str(out), "symbols": syms, **K.INGEST_DEFAULTS}
        )
        man = json.loads((out / "manifest.json").read_text())
        return out, {p["instrument_id"]: (p["rows"], [f["sha256"] for f in p["files"]]) for p in man["partitions"]}

    full_dir, full = ingest(src, SYMS, "full")
    assert len(full) == len(SYMS)
    for b in m["buckets"]:
        syms = sorted(b["symbols"])
        if not syms:
            continue
        _, from_bucket = ingest(tmp_path / "split" / "01022020" / b["file"], syms, f"b{b['bucket']}")
        _, from_day = ingest(src, syms, f"d{b['bucket']}")
        assert from_bucket == from_day  # same symbol set: byte-identical partition files
        for iid, (rows, _sha) in from_bucket.items():
            assert full[iid][0] == rows
            # other co-selected symbols renumber `sequence` only, in the same order
            part = f"mbo/date=2020-01-02/instrument_id={iid}/part-00000.parquet"
            a = pq.read_table(full_dir / part)
            c = pq.read_table(tmp_path / f"b{b['bucket']}" / part)
            assert a.drop_columns(["sequence"]).equals(c.drop_columns(["sequence"]))
            ranks = [np.unique(x.column("sequence").to_numpy(), return_inverse=True)[1] for x in (a, c)]
            assert np.array_equal(ranks[0], ranks[1])


@pytest.mark.requires_core
def test_per_partition_sampling_is_shard_invariant(tmp_path) -> None:
    import queuetruth as qt

    ds = {"path": str(FIXTURES / "synth_small")}
    base = {"dataset": ds, "seed": 5}
    qt.sample_shadows({**base, "out": str(tmp_path / "eq.parquet"), "n": 402})  # 101, 101, 100, 100
    eq = pq.read_table(tmp_path / "eq.parquet")
    keys = [f"{i}_{d}" for d in ("20260615", "20260616") for i in (1, 2)]
    quotas = dict(zip(keys, [101, 101, 100, 100], strict=True))
    qt.sample_shadows(
        {**base, "out": str(tmp_path / "pp.parquet"), "n": 0, "allocation": "per_partition", "partition_quotas": quotas}
    )
    assert pq.read_table(tmp_path / "pp.parquet").equals(eq)
    # one partition alone (a shard holding only it) draws exactly its rows of the whole
    qt.sample_shadows(
        {
            **base,
            "out": str(tmp_path / "one.parquet"),
            "n": 0,
            "allocation": "per_partition",
            "partition_quotas": {"2_20260616": 100},
        }
    )
    one = pq.read_table(tmp_path / "one.parquet")
    want = eq.filter(pc.starts_with(eq["shadow_id"], "s2_20260616_"))
    assert one.num_rows == 100 and one.equals(want)
    # the undated key applies to every date; n is the default per partition
    qt.sample_shadows(
        {
            **base,
            "out": str(tmp_path / "def.parquet"),
            "n": 7,
            "allocation": "per_partition",
            "partition_quotas": {"1": 3},
        }
    )
    ids = pq.read_table(tmp_path / "def.parquet").column("shadow_id").to_pylist()
    assert sum(s.startswith("s1_") for s in ids) == 6 and sum(s.startswith("s2_") for s in ids) == 14
    with pytest.raises(qt.QtError, match="per_partition"):
        qt.sample_shadows({**base, "n": 4, "partition_quotas": {"1": 3}})


@pytest.mark.requires_core
@pytest.mark.parametrize("min_dates", [2, 5])
def test_block_stats_over_shards_reproduce_evaluate_zoo(tmp_path, monkeypatch, min_dates) -> None:
    import queuetruth as qt
    import queuetruth.eval.blocks as blocks_mod
    import queuetruth.stats.resampling as R
    from queuetruth.api import _planned_life
    from queuetruth.eval import evaluate_zoo

    ds = {"path": str(FIXTURES / "synth_small")}
    qt.sample_shadows({"dataset": ds, "out": str(tmp_path / "sh.parquet"), "n": 240, "seed": 1})
    z = tmp_path / "zoo"
    qt.run_zoo(
        {
            "dataset": ds,
            "shadows": {"path": str(tmp_path / "sh.parquet")},
            "out": str(z),
            "models": ["all"],
            "seed": 0,
            "horizons_ns": [S1, 10 * S1, 60 * S1],
        }
    )
    pred = pq.read_table(z / "zoo_predictions.parquet")
    planned, _ = _planned_life(z)
    h_max = 300 * S1
    # 2 dates: 30-minute blocks; with the day threshold lowered to 2, the day-block path
    monkeypatch.setattr(blocks_mod, "MIN_DAY_BLOCKS", min_dates)
    original = R.block_labels

    def labels(session_date, ts_ns, min_dates=min_dates):
        return original(session_date, ts_ns, min_dates)

    monkeypatch.setattr("queuetruth.eval.zoo.block_labels", labels)
    want_t, _ = evaluate_zoo(pred, h_max_ns=h_max, bootstrap_b=200, seed=3, planned_life=planned)
    want = {
        (r["model_id"], r["metric"], r["horizon_ns"], r["stratum_kind"], r["stratum"]): r for r in want_t.to_pylist()
    }
    iids = pred.column("instrument_id").to_numpy()
    days = pc.cast(pred.column("session_date"), pa.int32()).to_numpy()
    shards = []
    for iid, day in sorted(set(zip(iids.tolist(), days.tolist(), strict=True))):
        sub = pred.filter(pa.array((iids == iid) & (days == day)))
        shards.append(B.block_stats(sub, planned, h_max_ns=h_max, extra={"shard": f"{iid}_{day}"}))
    assert len(shards) == 4
    stats = B.concat([s["stats"] for s in shards])
    shadows = B.concat([s["shadows"] for s in shards])
    got = {
        (r["model_id"], r["metric"], r["horizon_ns"], r["stratum_kind"], r["stratum"]): r
        for r in B.evaluate_blocks(stats, shadows, h_max_ns=h_max, bootstrap_b=200, seed=3)
    }
    metrics = {k[1] for k in got}
    assert {"fill_rate_bias", "brier", "logloss", "overfill_bias", "mcnemar_b"} <= metrics
    expected = {k: r for k, r in want.items() if k[1] in metrics}
    assert set(expected) == set(got)
    for k, w in expected.items():
        g = got[k]
        assert g["n"] == w["n"], k
        for f in ("value", "ci_lo", "ci_hi"):
            assert (w[f] is None) == (g[f] is None), (k, f)
            if w[f] is not None:
                assert abs(w[f] - g[f]) < 1e-12, (k, f, w[f], g[f])


def test_cluster_bootstrap_shrink_and_bh() -> None:
    rng = np.random.default_rng(0)
    rows = []
    for d in range(4):
        day_shift = rng.normal(0, 0.02)
        for s in range(6):
            n = 200
            p = np.clip(0.4 + day_shift + 0.03 * (s % 2), 0, 1)
            y = rng.random(n) < 0.4
            rows.append(
                {
                    "model_id": "m",
                    "horizon_ns": 60 * S1,
                    "session_date": d,
                    "symbol": f"S{s}",
                    "n": n,
                    "sum_d": float(n * p - y.sum()),
                    "sum_p": float(n * p),
                    "n_y": int(y.sum()),
                    "sum_sq": 0.0,
                    "sum_ll": 0.0,
                    "qty_n": 0,
                    "qty_num": 0.0,
                    "qty_den": 0.0,
                    "b": 0,
                    "c": 0,
                }
            )
    t = pa.Table.from_pylist(rows)
    two = B.cluster_bootstrap(t, clusters=("session_date", "symbol"), group=("model_id", "horizon_ns"))
    one = B.cluster_bootstrap(t, clusters=("symbol",), group=("model_id", "horizon_ns"))
    assert len(two) == 1 and two[0]["n_session_date"] == 4 and two[0]["n_symbol"] == 6
    want = sum(r["sum_d"] for r in rows) / sum(r["n"] for r in rows)
    assert abs(two[0]["value"] - want) < 1e-12
    assert two[0]["ci_lo"] < want < two[0]["ci_hi"]
    # day shocks shared by every symbol widen the two-way interval beyond the symbol-only one
    assert two[0]["ci_hi"] - two[0]["ci_lo"] > one[0]["ci_hi"] - one[0]["ci_lo"]
    per = B.cluster_bootstrap(t, clusters=("session_date",), group=("symbol",))
    sh = B.shrink([r["value"] for r in per], [r["se"] for r in per])
    post = sh["posterior"]
    assert np.all(np.abs(post - sh["mu"]) <= np.abs(np.array([r["value"] for r in per]) - sh["mu"]) + 1e-15)
    adj = B.bh([0.01, 0.04, 0.03, float("nan")])
    assert np.allclose(adj[:3], [0.03, 0.04, 0.04]) and np.isnan(adj[3])


def test_merkle_proofs_and_digest() -> None:
    for n in range(1, 10):
        leaves = [K.leaf_hash({"unit": i}) for i in range(n)]
        root = K.merkle_root(leaves)
        for i in range(n):
            assert K.verify_proof(leaves[i], K.merkle_proof(leaves, i), root)
        if n > 1:
            assert not K.verify_proof(K.leaf_hash({"unit": "x"}), K.merkle_proof(leaves, 0), root)
    t = pa.table(
        {
            "k": [2, 1, 3],
            "v": [0.5, 0.25, None],
            "s": ["b", "a", "c"],
            "d": pa.array([18260, 18291, None], pa.int32()).cast(pa.date32()),
            "ts": pa.array([1, 2, 3], pa.int64()).cast(pa.timestamp("ns", tz="UTC")),
            "f": [True, None, False],
        }
    )
    d = K.table_digest(t, ["k"])
    assert d == K.table_digest(t.take([2, 0, 1]), ["k"])
    assert d != K.table_digest(t.set_column(1, "v", pa.array([0.5, 0.25000000000000006, None])), ["k"])
    with pytest.raises(ValueError):
        K.canonical_json({"x": float("nan")})


def test_plan_breadth_quotas_match_equal_allocation(tmp_path) -> None:
    import plan as P

    day = [
        {"symbol": s, "locate": loc, "messages": 1000, "executions": 10}
        for s, loc in [("BAC", 688), ("F", 2714), ("GE", 3279), ("SIRI", 6118), ("T", 7202), ("PFE", 7648)]
    ]
    q = P.quotas_for("breadth", K.POPULATIONS, day)
    # breadth.json 2019-12-30 deep_queue: large 334, 334, 333, 333, 333, 333 in locate order
    assert [q["large"][s] for s in ("BAC", "F", "GE", "SIRI", "T", "PFE")] == [334, 334, 333, 333, 333, 333]
    assert sum(q["standard"].values()) == 3000
    s = P.quotas_for("scale", K.SCALE_POPULATIONS, [{**day[0], "executions": 3}])
    assert s["standard"]["BAC"] == 60 and s["large"]["BAC"] == 30
    assert P.pack([("a", 5.0), ("b", 4.0), ("c", 3.0), ("d", 9.0)], 9.0) == [["d"], ["a", "b"], ["c"]]
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.requires_core
def test_two_day_pipeline_reduce_and_combine(tmp_path) -> None:
    import atlas
    import plan as P
    from unit import run_unit

    day_dirs = []
    for date, seed in (("01022020", 3), ("01032020", 4)):
        src = tmp_path / f"{date}.NASDAQ_ITCH50.gz"
        write_itch_day(src, SYMS, orders_per_symbol=120, seed=seed)
        a = argparse.Namespace(
            date=date,
            url=f"file://{src}",
            buckets=2,
            out=tmp_path / "split",
            symbols=None,
            allow_missing=False,
            level=1,
            min_free_gib=0.0,
        )
        assert itch_split.run_split(a) == 0
        pl = tmp_path / f"plan-{date}.json"
        P.main(
            [
                "--split",
                str(tmp_path / "split" / date),
                "--out",
                str(pl),
                "--quota-rule",
                "fixed",
                "--fixed-n",
                "30",
                "--populations",
                "standard",
                "--modes",
                "faithful",
                "--unit-core-s",
                "1e9",
            ]
        )
        plan = json.loads(pl.read_text())
        assert {u["tier"] for u in plan["universe"]} == {"T1_top100"}
        assert all(u["ingest"] == K.INGEST_DEFAULTS for u in plan["units"])
        for u in plan["units"]:
            run_unit(u, tmp_path / "split" / date, tmp_path / "shards" / date / u["unit_id"], 1)
        r = atlas.reduce_day(plan, tmp_path / "shards" / date, tmp_path / "days" / date)
        assert r["units_valid"] == r["units_planned"] == len(plan["units"])
        day_dirs.append(tmp_path / "days" / date)
    res = atlas.combine(day_dirs, tmp_path / "atlas", bootstrap_b=100)
    assert res["days"] == 2 and res["symbol_days"] == 10 and res["distinct_symbols"] == 5
    man = json.loads((tmp_path / "atlas" / "atlas_manifest.json").read_text())
    assert man["atlas_root"] == K.merkle_root([d["merkle_root"] for d in man["days"]])
    lb = pq.read_table(tmp_path / "atlas" / "leaderboard_by_tier.parquet").to_pylist()
    row = next(r for r in lb if r["tier"] == "ALL" and r["horizon_ns"] == 10 * S1)
    assert row["n_dates"] == 2 and row["n_symbols"] == 5 and row["n"] > 0
    # the leaderboard bias equals the pooled ratio of the day cells
    cells = pa.concat_tables([pq.read_table(d / "cells_all.parquet") for d in day_dirs])
    sel = cells.filter(pc.and_(pc.equal(cells["model_id"], row["model_id"]), pc.equal(cells["horizon_ns"], 10 * S1)))
    assert abs(row["fill_rate_bias_value"] - pc.sum(sel["sum_d"]).as_py() / pc.sum(sel["n"]).as_py()) < 1e-12
    per = pq.read_table(tmp_path / "atlas" / "per_symbol.parquet").to_pylist()
    assert {r["symbol"] for r in per} == set(SYMS) and all(r["n_days"] == 2 for r in per)
    assert pq.read_table(tmp_path / "atlas" / "maps_by_tier.parquet").num_rows > 0


def test_session_hours_and_tiers() -> None:
    import plan as P

    sm = {"stream": {"system_events_ns_since_midnight": {"Q": 34_200_000_075_566, "M": 57_600_000_071_785}}}
    assert P.session_hours(sm) == {}
    sm["stream"]["system_events_ns_since_midnight"]["M"] = 46_800_000_012_345  # 13:00 close (half day)
    assert P.session_hours(sm) == {"open_s": 34_200, "close_s": 46_800}
    assert [P.tier_of(r) for r in (1, 100, 101, 500, 1500, 4000, 4001)] == [
        "T1_top100",
        "T1_top100",
        "T2_101_500",
        "T2_101_500",
        "T3_501_1500",
        "T4_1501_4000",
        "T5_rest",
    ]
