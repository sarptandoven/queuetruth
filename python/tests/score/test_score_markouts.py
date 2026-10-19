"""O-SCORE-3: markouts (prevailing-quote as-of, null past end of data, RS = 2 mo identity, mo_ccy units) and the
position series, against tests/fixtures/reference/markouts.json (exact rational reference values)."""

from __future__ import annotations

import json

import pyarrow as pa
import pytest

from queuetruth.score import compute_markouts, position_series
from queuetruth.score._io import rows

DAY0 = 20_454
MIDNIGHT = DAY0 * 86_400_000_000_000


@pytest.fixture(scope="module")
def ref(repo_root):
    return json.loads((repo_root / "tests" / "fixtures" / "reference" / "markouts.json").read_text())


def _inputs(ref, *, size_exp=None, mult=None):
    ins = ref["instrument"]
    instruments = pa.table(
        {
            "instrument_id": [1],
            "session_date": [DAY0],
            "midnight_utc_ns": [MIDNIGHT],
            "tick_size": [ins["tick_size"]],
            "price_exp": [ins["price_exp"]],
            "size_exp": [ins["size_exp"] if size_exp is None else size_exp],
            "contract_multiplier": [ins["contract_multiplier"] if mult is None else mult],
        }
    )
    b = ref["bbo"]
    bbo = pa.table({k: [r[k] for r in b] for k in ("ts_event", "bid_px", "bid_sz", "ask_px", "ask_sz")})
    f = ref["fills"]
    fills = pa.table(
        {
            "order_key": [f"o{i}" for i in range(len(f))],
            "arm": ["live"] * len(f),
            "fill_seq": [1] * len(f),
            "ts": [r["ts_event"] for r in f],
            "qty": [r["qty"] for r in f],
            "px": [r["px"] for r in f],
            "liquidity": ["maker"] * len(f),
            "side": [r["side"] for r in f],
        }
    )
    return fills, bbo, instruments


def _compute(ref, **kw):
    fills, bbo, instruments = _inputs(ref, **kw)
    t = compute_markouts(
        fills,
        bbo,
        instruments,
        ref["horizons_ns"],
        ["mid", "microprice"],
        last_event_ts={(1, DAY0): ref["instrument"]["last_event_ts"]},
    )
    assert t.schema.metadata[b"qt.schema"] == b"qt.score_markouts"
    return {(r["order_key"], r["horizon_ns"], r["ref"]): r for r in rows(t)}


def test_o_score_3_markouts_match_reference(ref):
    got = _compute(ref)
    assert len(got) == len(ref["markouts"]) == 80
    for e in ref["markouts"]:
        g = got[(f"o{e['fill_index']}", e["horizon_ns"], e["ref"])]
        for k in ("mo_ticks", "mm_ticks"):
            assert (g[k] is None) == (e[k] is None), (e, k)
            if e[k] is not None:
                assert g[k] == pytest.approx(e[k], abs=1e-12)
        if e["mo_ccy"] is None:
            assert g["mo_ccy"] is None
        else:
            assert g["mo_ccy"] == pytest.approx(e["mo_ccy"], rel=1e-12, abs=1e-15)


def test_o_score_3_null_past_end_and_inclusive_asof(ref):
    got = _compute(ref)
    assert got[("o4", 10_000_000_000, "mid")]["mo_ticks"] is not None  # t + h == last event: valid
    assert got[("o4", 15_000_000_000, "mid")]["mo_ticks"] is None  # beyond the last event
    assert got[("o1", 60_000_000, "mid")]["mo_ticks"] == pytest.approx(0.5)  # BBO at exactly t + h counts (else 1.0)
    assert got[("o0", 50_000_000, "mid")]["mm_ticks"] is None  # no quote strictly before t


def test_o_score_3_rs_identity(ref):
    got = _compute(ref)
    for e in ref["markouts"]:
        g = got[(f"o{e['fill_index']}", e["horizon_ns"], e["ref"])]
        if e["rs_taker_ticks"] is not None:
            assert 2 * g["mo_ticks"] == pytest.approx(e["rs_taker_ticks"], abs=1e-12)


def test_o_score_3_mo_ccy_units(ref):
    base = _compute(ref)
    scaled = _compute(ref, size_exp=-2, mult=5.0)
    for k, r in base.items():
        if r["mo_ccy"] is not None:
            assert scaled[k]["mo_ccy"] == pytest.approx(r["mo_ccy"] * 0.01 * 5.0, rel=1e-12)
            assert scaled[k]["mo_ticks"] == r["mo_ticks"]


def test_o_score_3_position_series_end_equals_signed_sum(ref):
    fills, _, _ = _inputs(ref)
    fills = fills.append_column("instrument_id", pa.array([1] * fills.num_rows, pa.uint32()))
    t = position_series(fills)
    pos = rows(t)
    assert [(r["ts"], r["position"]) for r in pos] == [(p["ts_event"], p["position"]) for p in ref["position_series"]]
    assert pos[-1]["position"] == sum(r["qty"] * (1 if r["side"] == "buy" else -1) for r in ref["fills"])


def test_position_series_collapses_equal_timestamps_and_splits_arms():
    fills = pa.table(
        {
            "order_key": ["a", "b", "c"],
            "arm": ["sim", "live", "live"],
            "fill_seq": [1, 1, 1],
            "ts": [5, 5, 5],
            "qty": [3, 2, 7],
            "px": [1, 1, 1],
            "side": ["sell", "buy", "buy"],
            "instrument_id": [1, 1, 1],
        }
    )
    assert [(r["arm"], r["ts"], r["position"]) for r in rows(position_series(fills))] == [
        ("live", 5, 9),
        ("sim", 5, -3),
    ]
