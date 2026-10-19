"""RNG parity: Rust `qt_types` (via schemas/rng_kat.json) == reference/rng.py == queuetruth.stats.stable_seed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from queuetruth import stats
from reference import rng

KAT = json.loads((Path(__file__).resolve().parents[3] / "schemas" / "rng_kat.json").read_text())


def test_splitmix64_reference_values() -> None:
    expected = [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F, 0xF88BB8A8724C81EC]
    x = 0
    for e in expected:
        assert rng.splitmix64(x) == e
        assert stats.splitmix64(x) == e
        x = (x + rng.GAMMA) & rng.M64


@pytest.mark.parametrize("case", KAT["stable_seed"], ids=lambda c: f"{c['run_seed']}:{'/'.join(c['parts'])}")
def test_stable_seed_parity(case: dict) -> None:
    parts = [p.encode() for p in case["parts"]]
    assert rng.stable_seed(case["run_seed"], parts) == case["value"]
    assert stats.stable_seed(case["run_seed"], parts) == case["value"]
    assert stats.stable_seed(case["run_seed"], case["parts"]) == case["value"]


def test_stable_seed_7_a_bc() -> None:
    assert rng.stable_seed(7, [b"a", b"bc"]) == KAT["stable_seed_7_a_bc"]
    assert stats.stable_seed(7, [b"a", b"bc"]) == KAT["stable_seed_7_a_bc"]


@pytest.mark.parametrize("stream", KAT["streams"], ids=lambda s: str(s["seed"]))
def test_xoshiro_first_1000_next_u64(stream: dict) -> None:
    r = rng.Xoshiro256StarStar(stream["seed"])
    got = [r.next_u64() for _ in range(1000)]
    assert len(stream["next_u64"]) == 1000
    assert got == stream["next_u64"]


def test_next_f64_and_range_inclusive() -> None:
    r = rng.Xoshiro256StarStar(0)
    assert [r.next_f64() for _ in range(20)] == KAT["next_f64_seed_0"]
    r = rng.Xoshiro256StarStar(1)
    assert [r.range_inclusive(0, 999) for _ in range(20)] == KAT["range_inclusive_seed_1_0_999"]


def test_streams_seed_set() -> None:
    assert [s["seed"] for s in KAT["streams"]] == [0, 1, KAT["stable_seed_7_a_bc"]]


def test_stable_seed_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        stats.stable_seed(-1, [])
    with pytest.raises(ValueError):
        stats.stable_seed(1 << 64, [])
