"""`queuetruth.eval.toxicity`: fill-type tags on hand-built engine tapes (every exact mechanism through the real
replay), statuses and the PnL decomposition on hand-built zoo tables (every status, particle, touch and taker rows,
every population exclusion), markouts against a brute-force reference and the scorer's `compute_markouts`, the
shared block bootstrap, the end-to-end runner and CLI on the synthetic Nasdaq fixture, and the failure modes."""

from __future__ import annotations

import json
import random
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from queuetruth.errors import QtError
from queuetruth.eval import planned_life_table
from queuetruth.eval.toxicity import (
    BOOTSTRAP_TEST_ID,
    MECHANISMS,
    STATUSES,
    TABLES,
    QuoteBook,
    ToxicitySpec,
    evaluate_toxicity,
    exact_mechanism,
    mid_markouts,
    model_mechanism,
    render_toxicity,
    run_zoo_toxicity,
    table_schema,
)
from queuetruth.schemas import arrow as qa
from queuetruth.schemas import models as m
from queuetruth.schemas.io import write_table
from queuetruth.score._io import make_table
from queuetruth.stats import block_counts, bootstrap_seed, percentile_ci

S = 1_000_000_000
MS = 1_000_000
DAY = 86_400 * S
MIDNIGHT = 1_767_225_600_000_000_000  # 2026-01-01 00:00:00 UTC
DAY0 = MIDNIGHT // DAY
T0 = MIDNIGHT + 10 * 3600 * S  # 10:00:00 UTC, on a half-hour boundary
TICK = 10_000_000
BASE = 100_000_000_000
B, A, N = ord("B"), ord("A"), ord("N")
LAST = 128
REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "synth_small_nasdaq"


def P(k: int) -> int:
    return BASE + k * TICK


# ---- brute-force markout reference ------------------------------------------------------------------------------


def _day_of(midnights: list[int], t: int) -> int:
    """Index of the instrument-day of `t`: latest midnight <= t, the first when t precedes all."""
    best = 0
    for i, mn in enumerate(midnights):
        if mn <= t:
            best = i
    return best


def ref_markouts(
    quotes: list[dict[str, Any]],
    days: dict[int, list[tuple[int, int, int]]],
    last: dict[tuple[int, int], int],
    fill: dict[str, Any],
    h: int,
) -> dict[str, Fraction | None]:
    """The module docstring's definitions, one fill at a time: linear scans, exact rationals."""
    iid, t, d, p = fill["iid"], fill["ts"], fill["d"], fill["px"]
    cal = sorted(days[iid])
    k = _day_of([c[0] for c in cal], t)
    date, tick = cal[k][1], cal[k][2]
    mids = [c[0] for c in cal]

    def prevailing(at: int, strict: bool) -> tuple[int, int] | None:
        pick = None
        for pos, q in enumerate(quotes):
            if q["iid"] != iid or cal[_day_of(mids, q["ts"])][1] != date:
                continue
            if (q["ts"] < at) if strict else (q["ts"] <= at):
                if pick is None or (q["ts"], pos) >= (pick[0], pick[1]):
                    pick = (q["ts"], pos, q)
        if pick is None or pick[2]["bid"] is None or pick[2]["ask"] is None:
            return None
        return pick[2]["bid"], pick[2]["ask"]

    def mid(q: tuple[int, int]) -> Fraction:
        return Fraction(q[0] + q[1], 2)

    end = last.get((iid, date))
    if end is None:
        end = max(q["ts"] for q in quotes if q["iid"] == iid and cal[_day_of(mids, q["ts"])][1] == date)
    q1 = prevailing(t + h, False) if t + h <= end else None
    q0 = prevailing(t, True)
    out: dict[str, Fraction | None] = {"mo_ticks": None, "mo_bps": None, "mm_ticks": None, "mm_bps": None}
    if q1 is not None and p is not None:
        out["mo_ticks"] = d * (mid(q1) - p) / tick
        if p != 0:
            out["mo_bps"] = d * (mid(q1) - p) / abs(p) * 10_000
    if q1 is not None and q0 is not None:
        out["mm_ticks"] = d * (mid(q1) - mid(q0)) / tick
        if mid(q0) > 0:
            out["mm_bps"] = d * (mid(q1) - mid(q0)) / mid(q0) * 10_000
    return out


def _bbo_table(quotes: list[dict[str, Any]]) -> pa.Table:
    return make_table(
        "qt.bbo",
        [
            {
                "ts_event": q["ts"],
                "instrument_id": q["iid"],
                "sequence": i + 1,
                "bid_px": q["bid"],
                "bid_sz": 0 if q["bid"] is None else 5,
                "bid_ct": 0 if q["bid"] is None else 1,
                "ask_px": q["ask"],
                "ask_sz": 0 if q["ask"] is None else 7,
                "ask_ct": 0 if q["ask"] is None else 1,
            }
            for i, q in enumerate(quotes)
        ],
    )


def _instruments(days: dict[int, list[tuple[int, int, int]]]) -> pa.Table:
    rows = [
        {"instrument_id": iid, "session_date": date, "midnight_utc_ns": mn, "tick_size": tick}
        for iid, cal in days.items()
        for mn, date, tick in cal
    ]
    return pa.table(
        {
            "instrument_id": pa.array([r["instrument_id"] for r in rows], pa.uint32()),
            "session_date": pa.array([r["session_date"] for r in rows], pa.int32()).cast(pa.date32()),
            "midnight_utc_ns": pa.array([r["midnight_utc_ns"] for r in rows], pa.int64()),
            "tick_size": pa.array([r["tick_size"] for r in rows], pa.int64()),
        }
    )


def test_markouts_match_brute_force_reference_and_scorer() -> None:
    """Random two-instrument, two-day quotes (equal timestamps, empty sides, fills before the first quote and past
    the end of data, invalid prices): every measure equals the brute-force reference, and `mo_ticks`, `mm_ticks`,
    `mo_bps` equal the scorer's `compute_markouts` wherever it defines them."""
    rng = random.Random(11)
    days = {
        3: [(MIDNIGHT, DAY0, TICK), (MIDNIGHT + DAY, DAY0 + 1, TICK)],
        9: [(MIDNIGHT, DAY0, 5_000_000), (MIDNIGHT + DAY, DAY0 + 1, 5_000_000)],
    }
    quotes: list[dict[str, Any]] = []
    for iid, cal in days.items():
        tick = cal[0][2]
        for mn, _, _ in cal:
            ts = mn + 9 * 3600 * S
            for _ in range(120):
                ts += rng.choice([0, 0, 1, 7, 250 * MS, 3 * S, 8 * S])
                bid = BASE + rng.randint(-20, 20) * tick
                ask = bid + rng.randint(1, 4) * tick
                if rng.random() < 0.08:
                    bid = None
                if rng.random() < 0.08:
                    ask = None
                quotes.append({"ts": ts, "iid": iid, "bid": bid, "ask": ask})
    rng.shuffle(quotes)  # file order is not time order
    last = {(3, DAY0): MIDNIGHT + 9 * 3600 * S + 200 * S}
    horizons = [1, 100 * MS, 1 * S, 10 * S, 60 * S]
    fills = []
    for _ in range(300):
        iid = rng.choice([3, 9])
        day = rng.choice([0, 1])
        ts = MIDNIGHT + day * DAY + 9 * 3600 * S + rng.randint(-5 * S, 260 * S)
        if rng.random() < 0.2:  # exactly on a quote timestamp
            ts = rng.choice([q["ts"] for q in quotes if q["iid"] == iid])
        px = BASE + rng.randint(-25, 25) * days[iid][0][2]
        fills.append({"iid": iid, "ts": ts, "d": rng.choice([1, -1]), "px": None if rng.random() < 0.05 else px})
    book = QuoteBook(_bbo_table(quotes), _instruments(days), last)
    mk = mid_markouts(
        book,
        np.array([f["iid"] for f in fills]),
        np.array([f["ts"] for f in fills]),
        np.array([f["d"] for f in fills]),
        np.array([0 if f["px"] is None else f["px"] for f in fills]),
        np.array([f["px"] is not None for f in fills]),
        horizons,
    )
    n_defined = 0
    for i, f in enumerate(fills):
        for k, h in enumerate(horizons):
            ref = ref_markouts(quotes, days, last, f, h)
            for name, want in ref.items():
                got = mk[name][i, k]
                if want is None:
                    assert np.isnan(got), (i, h, name)
                else:
                    n_defined += 1
                    assert got == pytest.approx(float(want), rel=1e-12, abs=1e-12), (i, h, name)
    assert 2000 < n_defined < 6000  # the random tape exercises defined and undefined cases alike

    # the scorer's markouts on the same quotes (fills with a valid price)
    from queuetruth.score.markouts import compute_markouts

    inst_rows = [
        {
            "instrument_id": iid,
            "session_date": date,
            "symbol": f"S{iid}",
            "venue": "nasdaq",
            "source_venue": "nasdaq",
            "price_exp": -9,
            "tick_size": tick,
            "lot_size": 1,
            "size_exp": 0,
            "contract_multiplier": 1.0,
            "currency": "USD",
            "session_tz": "UTC",
            "midnight_utc_ns": mn,
            "open_s": 0,
            "close_s": 86_400,
            "source": "test",
        }
        for iid, cal in days.items()
        for mn, date, tick in cal
    ]
    good = [i for i, f in enumerate(fills) if f["px"] is not None]
    score_fills = pa.table(
        {
            "order_key": [f"o{i}" for i in good],
            "arm": ["sim"] * len(good),
            "fill_seq": list(range(len(good))),
            "ts": [fills[i]["ts"] for i in good],
            "px": [fills[i]["px"] for i in good],
            "qty": [1] * len(good),
            "side": ["buy" if fills[i]["d"] > 0 else "sell" for i in good],
            "instrument_id": [fills[i]["iid"] for i in good],
        }
    )
    sc = compute_markouts(
        score_fills,
        _bbo_table(quotes),
        make_table("qt.instruments", inst_rows),
        horizons,
        ["mid"],
        last_event_ts=last,
    ).to_pylist()
    for j, i in enumerate(good):
        for k in range(len(horizons)):
            r = sc[j * len(horizons) + k]
            for name in ("mo_ticks", "mm_ticks", "mo_bps"):
                if r[name] is not None:
                    assert mk[name][i, k] == pytest.approx(r[name], rel=1e-12, abs=1e-12)
                elif name == "mo_ticks":
                    assert np.isnan(mk[name][i, k])


def test_quote_book_rejects_unknown_instruments() -> None:
    days = {1: [(MIDNIGHT, DAY0, TICK)]}
    book = QuoteBook(_bbo_table([{"ts": T0, "iid": 1, "bid": P(0), "ask": P(1)}]), _instruments(days))
    with pytest.raises(QtError) as e:
        mid_markouts(book, np.array([2]), np.array([T0]), np.array([1]), np.array([P(0)]), np.array([True]), [S])
    assert e.value.code == "QT-INPUT-002"
    out = mid_markouts(book, np.array([], dtype=np.int64), *(np.array([], dtype=np.int64),) * 3, np.array([]), [S])
    assert out["mo_ticks"].shape == (0, 1)


# ---- mechanisms on hand-built engine tapes -----------------------------------------------------------------------


class _Tape:
    """One instrument's MBO records (Nasdaq style: an execution is T, F and the removal at one sequence)."""

    def __init__(self) -> None:
        self.recs: list[tuple[int, str, int, int, int, int, int, int]] = []
        self.seq = 0

    def add(self, ts: int, side: int, px: int, size: int, oid: int) -> None:
        self.seq += 1
        self.recs.append((ts, "A", side, px, size, oid, LAST, self.seq))

    def execute(self, ts: int, resting: int, px: int, size: int, oid: int) -> None:
        self.seq += 1
        agg = A if resting == B else B
        self.recs += [
            (ts, "T", agg, px, size, 0, 0, self.seq),
            (ts, "F", resting, px, size, oid, 0, self.seq),
            (ts, "C", resting, px, size, oid, LAST, self.seq),
        ]

    def hidden(self, ts: int, px: int, size: int) -> None:
        self.seq += 1
        self.recs.append((ts, "T", N, px, size, 0, LAST, self.seq))

    def end(self, ts: int) -> None:
        self.add(ts, B, P(-50), 1, 999_999)


def _write_dataset(root: Path, tapes: dict[int, _Tape]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    inst, parts = [], []
    for iid, tape in tapes.items():
        inst.append(
            {
                "instrument_id": iid,
                "session_date": DAY0,
                "symbol": f"S{iid}",
                "venue": "nasdaq",
                "source_venue": "nasdaq",
                "price_exp": -9,
                "tick_size": TICK,
                "lot_size": 1,
                "size_exp": 0,
                "contract_multiplier": 1.0,
                "currency": "USD",
                "session_tz": "UTC",
                "midnight_utc_ns": MIDNIGHT,
                "open_s": 0,
                "close_s": 86_400,
                "source": "test",
            }
        )
        rows = [
            {
                "ts_event": ts,
                "ts_recv": ts,
                "instrument_id": iid,
                "action": ord(act),
                "side": side,
                "price": px,
                "size": sz,
                "order_id": oid,
                "flags": fl,
                "sequence": seq,
                "channel_id": 0,
                "publisher_id": 0,
            }
            for ts, act, side, px, sz, oid, fl, seq in tape.recs
        ]
        rel = f"mbo/date=2026-01-01/instrument_id={iid}/part-00000.parquet"
        fe = write_table(make_table("qt.mbo", rows), root / rel)
        parts.append(
            {
                "instrument_id": iid,
                "session_date": DAY0,
                "files": [{"path": rel, "rows": fe["rows"], "sha256": fe["sha256"]}],
                "rows": fe["rows"],
                "ts_min": rows[0]["ts_event"],
                "ts_max": rows[-1]["ts_event"],
            }
        )
    write_table(make_table("qt.instruments", inst), root / "instruments.parquet")
    man = {
        "schema": "qt.dataset_manifest",
        "schema_version": 1,
        "dataset_id": "1" * 64,
        "kind": "mbo",
        "source_format": "test",
        "venue": "nasdaq",
        "source_venue": "nasdaq",
        "inputs": [],
        "partitions": parts,
        "price_exp_by_instrument": {str(i): -9 for i in tapes},
        "created_by": "test",
        "validation_status": "synthetic_only",
    }
    (root / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return root


def _scenario_tapes() -> tuple[dict[int, _Tape], list[dict[str, Any]], dict[str, str]]:
    """One instrument per mechanism (plus a declined hidden print); a buy shadow of 10 joins each at T0 + 1 s."""
    tapes: dict[int, _Tape] = {}
    shadows: list[dict[str, Any]] = []
    want: dict[str, str] = {}

    def shadow(sid: str, iid: int, px: int, mech: str) -> None:
        shadows.append(
            {
                "shadow_id": sid,
                "instrument_id": iid,
                "side": B,
                "price_mode": "abs",
                "price": px,
                "offset_ticks": 0,
                "qty": 10,
                "ts_submit": T0 + S,
                "latency_ns": 0,
                "ts_cancel": None,
                "source": "user",
            }
        )
        want[sid] = mech

    # queue consumed by executions at the price: 100 ahead executed, then an order behind executes
    t = tapes.setdefault(1, _Tape())
    t.add(T0, B, P(0), 100, 11)
    t.add(T0 + 1, A, P(2), 100, 12)
    shadow("queue", 1, P(0), "queue")
    t.add(T0 + 2 * S, B, P(0), 50, 13)
    t.execute(T0 + 3 * S, B, P(0), 100, 11)
    t.execute(T0 + 4 * S, B, P(0), 30, 13)
    # the level swept: the order ahead executes, then a worse-priced bid executes (trade-through)
    t = tapes.setdefault(2, _Tape())
    t.add(T0, B, P(0), 100, 21)
    t.add(T0 + 1, B, P(-1), 100, 22)
    t.add(T0 + 2, A, P(2), 100, 23)
    shadow("swept", 2, P(0), "swept")
    t.execute(T0 + 3 * S, B, P(0), 100, 21)
    t.execute(T0 + 3 * S + 1, B, P(-1), 50, 22)
    # a hidden print at the shadow's price inside the displayed spread
    t = tapes.setdefault(3, _Tape())
    t.add(T0, B, P(0), 100, 31)
    t.add(T0 + 1, A, P(3), 100, 32)
    shadow("hidden_at_price", 3, P(1), "hidden_print")
    t.hidden(T0 + 3 * S, P(1), 20)
    # a hidden print strictly through the shadow's price
    t = tapes.setdefault(4, _Tape())
    t.add(T0, B, P(0), 100, 41)
    t.add(T0 + 1, A, P(3), 100, 42)
    shadow("hidden_through", 4, P(2), "hidden_print")
    t.hidden(T0 + 3 * S, P(1), 20)
    # a resting shadow crossed by an opposite arrival at its price
    t = tapes.setdefault(5, _Tape())
    t.add(T0, B, P(0), 100, 51)
    t.add(T0 + 1, A, P(3), 100, 52)
    shadow("crossed_resting", 5, P(1), "crossed_resting")
    t.add(T0 + 3 * S, A, P(1), 40, 53)
    # a shadow priced at the opposite best: taker fill on arrival
    t = tapes.setdefault(6, _Tape())
    t.add(T0, B, P(0), 100, 61)
    t.add(T0 + 1, A, P(1), 100, 62)
    shadow("cross_on_arrival", 6, P(1), "cross_on_arrival")
    # a hidden print at the shadow's price with displayed interest there is declined (A3d); the displayed
    # execution behind the shadow fills it: `queue`, not `hidden_print`
    t = tapes.setdefault(7, _Tape())
    t.add(T0, B, P(0), 100, 71)
    t.add(T0 + 1, A, P(3), 100, 72)
    shadow("declined_hidden", 7, P(0), "queue")
    t.add(T0 + 2 * S, B, P(0), 40, 73)
    t.execute(T0 + 3 * S, B, P(0), 100, 71)
    t.hidden(T0 + 3 * S + 5, P(0), 20)
    t.execute(T0 + 4 * S, B, P(0), 25, 73)
    for tape in tapes.values():
        tape.end(T0 + 600 * S)
    return tapes, shadows, want


@pytest.mark.requires_core
def test_every_exact_mechanism_on_hand_built_tapes(tmp_path: Path) -> None:
    from queuetruth import api

    tapes, shadows, want = _scenario_tapes()
    ds = _write_dataset(tmp_path / "ds", tapes)
    write_table(make_table("qt.shadow_spec", shadows), tmp_path / "shadows.parquet")
    summ = api.replay(
        {
            "dataset": {"path": str(ds)},
            "shadows": {"path": str(tmp_path / "shadows.parquet")},
            "out": str(tmp_path / "rp"),
            "bbo": "change",
            "trajectory": "none",
        }
    )
    assert summ.shadow_diagnostics is not None and summ.shadow_diagnostics.taker_fills == 1
    fills = pq.read_table(tmp_path / "rp" / "shadow_fills.parquet")
    trades = pq.read_table(tmp_path / "rp" / "trades.parquet")
    iid_of = {s["shadow_id"]: s["instrument_id"] for s in shadows}
    sid = fills.column("shadow_id").to_pylist()
    first = {s: i for i, s in reversed(list(enumerate(sid)))}
    assert set(first) == set(want)
    mech = exact_mechanism(
        np.array(fills.column("reason").cast(pa.utf8()).to_pylist(), dtype=object),
        np.array([iid_of[s] for s in sid]),
        np.array(fills.column("ts_event").cast(pa.int64()).to_pylist()),
        np.array(fills.column("sequence").to_pylist()),
        trades,
    )
    got = {s: str(mech[i]) for s, i in first.items()}
    assert got == want
    # every mechanism the exact arm can produce is covered (touch and particle are model-only)
    assert set(want.values()) == set(MECHANISMS) - {"touch", "particle"}


def test_exact_mechanism_hidden_rule_and_unknown_reasons() -> None:
    trades = make_table(
        "qt.trades",
        [
            {"ts_event": T0, "instrument_id": 1, "sequence": 5, "price": P(0), "size": 1, "aggressor_side": N,
             "kind": "hidden"},
            {"ts_event": T0, "instrument_id": 1, "sequence": 6, "price": P(0), "size": 1, "aggressor_side": N,
             "kind": "hidden"},
            {"ts_event": T0, "instrument_id": 1, "sequence": 6, "price": P(0), "size": 1, "aggressor_side": A,
             "kind": "displayed"},
            {"ts_event": T0, "instrument_id": 1, "sequence": 7, "price": P(0), "size": 1, "aggressor_side": N,
             "kind": "cross"},
        ],
    )  # fmt: skip
    reason = np.array(["queue_depleted", "trade_through", "queue_depleted", "queue_depleted", "crossed",
                       "crossed_at_live", "queue_depleted"], dtype=object)  # fmt: skip
    iid = np.array([1, 1, 2, 1, 1, 1, 1])
    seq = np.array([5, 5, 5, 6, 5, 5, 7])
    got = list(exact_mechanism(reason, iid, np.full(7, T0), seq, trades))
    # hidden print at (1, 5); other instrument; hidden and displayed at one sequence; crossed rows never hidden;
    # an ITCH cross print is not a hidden print
    assert got == ["hidden_print", "hidden_print", "queue", "queue", "crossed_resting", "cross_on_arrival", "queue"]
    with pytest.raises(QtError) as e:
        exact_mechanism(np.array(["bogus"], dtype=object), np.array([1]), np.array([T0]), np.array([1]), trades)
    assert e.value.code == "QT-INPUT-002"
    assert list(model_mechanism(np.array(["queue", "trade_through", "crossed", "crossed_at_live", "touch_draw"]))) == [
        "queue",
        "swept",
        "crossed_resting",
        "cross_on_arrival",
        "touch",
    ]
    with pytest.raises(QtError):
        model_mechanism(np.array(["nope"]))


def test_hidden_print_keys_keep_days_apart() -> None:
    """Sequence numbers restart every session (ITCH): a day-2 hidden print at the sequence of a day-1 displayed print
    is still a hidden print, because the key carries the print's timestamp."""
    d2 = T0 + DAY
    trades = make_table(
        "qt.trades",
        [
            {"ts_event": T0, "instrument_id": 1, "sequence": 5, "price": P(0), "size": 1, "aggressor_side": A,
             "kind": "displayed"},
            {"ts_event": d2, "instrument_id": 1, "sequence": 5, "price": P(0), "size": 1, "aggressor_side": N,
             "kind": "hidden"},
            {"ts_event": d2 + S, "instrument_id": 1, "sequence": 9, "price": P(0), "size": 1, "aggressor_side": N,
             "kind": "hidden"},
        ],
    )  # fmt: skip
    reason = np.array(["queue_depleted"] * 4, dtype=object)
    ts = np.array([d2, d2 + S, T0, d2 + 2 * S])
    seq = np.array([5, 9, 5, 9])
    # day 2 at 5 and 9: hidden; day 1 at 5: the displayed print; a fill at sequence 9 at another time: no print
    assert list(exact_mechanism(reason, np.ones(4, dtype=np.int64), ts, seq, trades)) == [
        "hidden_print",
        "hidden_print",
        "queue",
        "queue",
    ]


# ---- statuses and the PnL decomposition on hand-built zoo tables -------------------------------------------------

FQ = "front_of_queue[mode=corrected]"
PM = "databento_mc[k=-0.8;mode=corrected;particles=100]"
TT = "trade_through_only[mode=corrected]"
NT = "nt_prob_touch[mode=corrected;p=1]"
HB = "hbt_power3[exch=partial;mode=faithful;n=2]"
MODELS = [FQ, PM, TT, NT, HB]
W = 60 * S
HZ = [1 * S, 10 * S]


def L(k: int) -> int:
    """ts_live of shadow s<k>: 10 minutes apart from T0 (s0..s2 in the first half-hour block, s3, s4 in the next)."""
    return T0 + k * 600 * S


def _quotes() -> list[dict[str, Any]]:
    """Per shadow: mid P(1) until L + 1.5 s, P(-1) until L + 30 s, then P(3); s3 has its own (crossed) path."""
    q = []
    for k in range(10):
        path = [(0, -2, -1), (1500 * MS, -3, -1), (30 * S, -3, -1)] if k == 3 else [(0, 0, 2), (1500 * MS, -2, 0),
                                                                                     (30 * S, 2, 4)]  # fmt: skip
        for off, b, a in path:
            q.append({"ts": L(k) + off, "iid": 1, "bid": P(b), "ask": P(a)})
    return q


def _pred_rows() -> list[dict[str, Any]]:
    # (model, shadow) -> (p_fill at W, model_ts_first_fill)
    p: dict[tuple[str, int], tuple[float, int | None]] = {
        (FQ, 0): (1.0, L(0) + 1 * S),
        (FQ, 1): (1.0, L(1) + 25 * S),
        (FQ, 4): (1.0, L(4) + 1 * S),
        (FQ, 9): (1.0, L(9) + 1 * S),
        (PM, 0): (1.0, L(0) + 2 * S),
        (PM, 1): (0.3, None),
        (PM, 2): (0.6, L(2) + 25 * S),
        (TT, 3): (1.0, L(3) + 1 * S),
        (NT, 0): (1.0, L(0) + 1 * S),
        (NT, 3): (1.0, L(3) + 1 * S),
    }
    truth: dict[int, tuple[bool | None, int | None]] = {
        0: (True, L(0) + 2 * S),
        1: (False, L(1) + 90 * S),
        2: (True, L(2) + 40 * S),
        3: (False, None),
        4: (True, L(4) + 2 * S),
        5: (True, L(5) + 2 * S),
        6: (True, L(6) + 2 * S),
        7: (None, None),
        8: (True, L(8) + 2 * S),
        9: (True, L(9) + 2 * S),
    }
    rows = []
    for mid in MODELS:
        for k in range(10):
            pf, mts = p.get((mid, k), (0.0, None))
            y, yts = truth[k]
            for h in (*HZ[:1], W):
                rows.append(
                    {
                        "model_id": mid,
                        "shadow_id": f"s{k}",
                        "qty": 10,
                        "horizon_ns": h,
                        "p_fill": pf if h == W else 0.0,
                        "model_ts_first_fill": mts,
                        "truth_filled": None if y is None else (bool(y) if h == W else False),
                        "truth_ts_first_fill": yts,
                        "instrument_id": 1,
                        "session_date": DAY0,
                        "ts_live": L(k),
                        "status": "hbt_312_abort" if (mid == HB and k == 8 and h == W) else None,
                    }
                )
    return rows


def _predictions(predictions: Any) -> pa.Table:
    rows = _pred_rows()
    t = predictions([{k: v for k, v in r.items() if k != "status"} for r in rows])
    t = qa.upgrade(t, "qt.zoo_predictions")
    status = pa.array([r["status"] for r in rows], pa.utf8()).dictionary_encode()
    i = t.schema.get_field_index("status")
    return t.set_column(i, t.schema.field("status"), status.cast(t.schema.field("status").type))


def _planned() -> pa.Table:
    ids = [f"s{k}" for k in range(10)]
    cancel = [L(k) + 300 * S if k != 5 else L(5) + 30 * S for k in range(10)]
    admin = [None if k != 6 else L(6) + 30 * S for k in range(10)]
    return planned_life_table(ids, cancel, admin)


def _exact_fills() -> pa.Table:
    spec = [  # (shadow, ts, reason, sequence)
        (0, L(0) + 2 * S, "queue_depleted", 100),
        (1, L(1) + 90 * S, "queue_depleted", 101),
        (2, L(2) + 40 * S, "trade_through", 102),
        (4, L(4) + 2 * S, "queue_depleted", 400),
        (5, L(5) + 2 * S, "queue_depleted", 105),
        (6, L(6) + 2 * S, "queue_depleted", 106),
        (8, L(8) + 2 * S, "queue_depleted", 108),
        (9, L(9) + 3 * S, "queue_depleted", 109),  # differs from the zoo's truth (L + 2 s)
    ]
    return make_table(
        "qt.shadow_fills",
        [
            {
                "shadow_id": f"s{k}",
                "fill_seq": 0,
                "ts_event": ts,
                "sequence": seq,
                "fill_qty": 10,
                "cum_qty": 10,
                "leaves_qty": 0,
                "fill_px": P(0),
                "reason": reason,
                "queue_ahead_before": 0,
                "traded_at_px_event": 0,
                "trade_through_event": 0,
                "behind_volume_executed": 0,
                "liquidity": "maker",
            }
            for k, ts, reason, seq in spec
        ],
        full=True,
    )


def _model_fills() -> pa.Table:
    spec = [  # (model, shadow, fill_seq, ts, qty, reason, liquidity)
        (FQ, 0, 0, L(0) + 1 * S, 10.0, "queue", "maker"),
        (FQ, 1, 0, L(1) + 25 * S, 4.0, "queue", "maker"),
        (FQ, 1, 1, L(1) + 40 * S, 6.0, "trade_through", "maker"),
        (FQ, 4, 0, L(4) + 1 * S, 10.0, "queue", "maker"),
        (FQ, 9, 0, L(9) + 1 * S, 10.0, "queue", "maker"),
        (FQ, 3, 0, L(3) + 200 * S, 10.0, "queue", "maker"),  # outside the window
        (TT, 3, 0, L(3) + 1 * S, 10.0, "crossed_at_live", "taker"),
        (NT, 0, 0, L(0) + 1 * S, 10.0, "touch_draw", "maker"),
    ]
    return make_table(
        "qt.zoo_fills",
        [
            {
                "model_id": mid,
                "shadow_id": f"s{k}",
                "fill_seq": seq,
                "ts_event": ts,
                "fill_qty": q,
                "cum_qty": q,
                "queue_ahead_est_before": 0.0,
                "reason": reason,
                "quirk": "none",
                "liquidity": liq,
            }
            for mid, k, seq, ts, q, reason, liq in spec
        ],
        full=True,
    )


def _shadow_results(end_state: dict[int, str] | None = None) -> pa.Table:
    cols: dict[str, Any] = {
        "shadow_id": [f"s{k}" for k in range(10)],
        "instrument_id": pa.array([1] * 10, pa.uint32()),
        "side": pa.array([B] * 10, pa.uint8()),
        "price": pa.array([P(0)] * 10, pa.int64()),
    }
    if end_state is not None:
        cols["end_state"] = pa.array([end_state.get(k, "filled") for k in range(10)]).dictionary_encode()
    return pa.table(cols)


def _trades() -> pa.Table:
    return make_table(
        "qt.trades",
        [
            {"ts_event": L(4) + 2 * S, "instrument_id": 1, "sequence": 400, "price": P(0), "size": 20,
             "aggressor_side": N, "kind": "hidden"},
            {"ts_event": L(0) + 2 * S, "instrument_id": 1, "sequence": 100, "price": P(0), "size": 20,
             "aggressor_side": A, "kind": "displayed"},
        ],
    )  # fmt: skip


def _evaluate(predictions: Any, *, model_fills: pa.Table | None = None, results: pa.Table | None = None,
              **kw: Any) -> Any:  # fmt: skip
    args = {"window_ns": W, "horizons_ns": HZ, "rank_horizon_ns": 1 * S, "bootstrap_b": 400, "seed": 5,
            "allow_truth_mismatch": True}  # fmt: skip
    args.update(kw)
    return evaluate_toxicity(
        _predictions(predictions),
        _planned(),
        _model_fills() if model_fills is None else model_fills,
        _shadow_results() if results is None else results,
        _exact_fills(),
        _trades(),
        _bbo_table(_quotes()),
        _instruments({1: [(MIDNIGHT, DAY0, TICK)]}),
        last_event_ts={(1, DAY0): T0 + DAY // 2},
        **args,
    )


def _model(doc: dict[str, Any], mid: str) -> dict[str, Any]:
    return next(x for x in doc["models"] if x["model_id"] == mid)


def test_population_exclusions_and_statuses(predictions: Any) -> None:
    res = _evaluate(predictions)
    doc = res.doc
    assert doc["n_shadows"] == 5
    assert doc["n_excluded"] == {
        "hbt_312": 1,
        "no_exact_arm": 1,
        "cancel_bearing": 1,
        "no_label": 1,
        "truth_mismatch": 1,
        "never_live": 0,
    }
    assert doc["truth_mismatch_rate"] == pytest.approx(1 / 6)
    assert any(w.startswith("truth_mismatch: 1 of 6 shadow(s)") and "(first s9)" in w for w in res.warnings)
    assert doc["ci_block"] == "30min" and doc["n_blocks"] == 2 and doc["window_ns"] == W and doc["horizons_ns"] == HZ
    assert doc["exact"] == {"n_filled": 3, "n_fills": 3, "fills_by_mechanism": {"queue": 1, "swept": 1,
                                                                                 "hidden_print": 1}}  # fmt: skip
    st = {(r["model_id"], r["shadow_id"]): r["status"] for r in res.shadows.to_pylist()}
    expect = {
        FQ: ["confirmed", "phantom", "missed", "neither", "confirmed"],
        PM: ["confirmed", "neither", "confirmed", "neither", "missed"],
        TT: ["missed", "neither", "missed", "phantom", "missed"],
        NT: ["confirmed", "neither", "missed", "neither", "missed"],
        HB: ["missed", "neither", "missed", "neither", "missed"],
    }
    for mid, sts in expect.items():
        assert [st[(mid, f"s{k}")] for k in range(5)] == sts, mid
        d = _model(doc, mid)
        assert (d["n_confirmed"], d["n_phantom"], d["n_missed"]) == (
            sts.count("confirmed"),
            sts.count("phantom"),
            sts.count("missed"),
        )
        assert d["n_shadows"] == 5 and d["phantom_rate"] == sts.count("phantom") / 5
    assert set(st.values()) <= set(STATUSES)
    # the touch model's s3 forecast (p = 1) has no fill row: the rows decide (neither), the pair keeps its markout
    # and is counted as inconsistent
    assert _model(doc, NT)["n_inconsistent"] == 1 and _model(doc, NT)["horizons"]["1000000000"]["n"] == 5
    assert any(w.startswith(f"inconsistent: {NT}: 1 shadow(s) whose p_fill of 0 or 1") for w in res.warnings)
    assert all(_model(doc, x)["n_inconsistent"] == 0 for x in (FQ, PM, TT, HB))


def test_truth_mismatch_is_an_error_unless_allowed(predictions: Any) -> None:
    """The replay must reproduce the zoo's exact arm: s9's replayed first fill is 1 s after the zoo's."""
    with pytest.raises(QtError) as e:
        _evaluate(predictions, allow_truth_mismatch=False)
    assert e.value.code == "QT-INPUT-002" and "truth_mismatch: 1 of 6" in e.value.message
    assert "--allow-truth-mismatch" in (e.value.hint or "") and "--amends" in (e.value.hint or "")


def test_random_draw_models_take_their_status_from_the_fill_rows(predictions: Any) -> None:
    """A touch model with p < 1 books the fills its random draws produce: the analytic p_fill (0.3 with a fill row,
    0.7 without one) does not decide the status, nothing is inconsistent and every shadow keeps its markout."""
    rows = _pred_rows()
    for r in rows:
        if r["model_id"] == NT and r["horizon_ns"] == W:
            r["p_fill"] = {0: 0.3, 1: 0.7, 2: 0.7, 3: 0.3}.get(int(r["shadow_id"][1:]), 0.0)
    t = predictions([{k: v for k, v in r.items() if k != "status"} for r in rows])
    t = qa.upgrade(t, "qt.zoo_predictions")
    i = t.schema.get_field_index("status")
    status = pa.array([r["status"] for r in rows], pa.utf8()).dictionary_encode()
    t = t.set_column(i, t.schema.field("status"), status.cast(t.schema.field("status").type))
    draw = {"model_id": NT, "shadow_id": "s3", "fill_seq": 0, "ts_event": L(3) + 1 * S, "fill_qty": 10.0,
            "cum_qty": 10.0, "queue_ahead_est_before": 0.0, "reason": "touch_draw", "quirk": "none",
            "liquidity": "maker"}  # fmt: skip
    fills = pa.concat_tables([_model_fills(), make_table("qt.zoo_fills", [draw], full=True)])
    res = evaluate_toxicity(
        t, _planned(), fills, _shadow_results(), _exact_fills(), _trades(), _bbo_table(_quotes()),
        _instruments({1: [(MIDNIGHT, DAY0, TICK)]}), window_ns=W, horizons_ns=HZ, rank_horizon_ns=S,
        bootstrap_b=100, allow_truth_mismatch=True, last_event_ts={(1, DAY0): T0 + DAY // 2},
    )  # fmt: skip
    st = {r["shadow_id"]: r["status"] for r in res.shadows.to_pylist() if r["model_id"] == NT}
    # rows on s0 (touch_draw) and s3 (the draw above); s1 and s2 have p 0.7 but no draw
    assert [st[f"s{k}"] for k in range(5)] == ["confirmed", "neither", "missed", "phantom", "missed"]
    d = _model(res.doc, NT)
    assert d["n_inconsistent"] == 0 and not any(w.startswith(f"inconsistent: {NT}") for w in res.warnings)
    for hz in d["horizons"].values():
        assert hz["n"] == 5
        g, tm, dn = (hz[k]["value"] for k in ("grant_ticks", "timing_ticks", "deny_ticks"))
        assert hz["bias_ticks"]["value"] == pytest.approx(g + tm + dn, abs=1e-12)
        assert hz["bias_ticks"]["value"] == pytest.approx(
            hz["model_pnl_ticks"]["value"] - hz["exact_pnl_ticks"]["value"], abs=1e-12
        )


def test_never_live_shadows_leave_the_population_unless_a_model_fills_them(predictions: Any) -> None:
    """s3 never went live in the exact replay. With the taker model's fill on it, it stays (a genuine phantom);
    without any model fill it carries no information and is left out (`never_live`)."""
    results = _shadow_results({3: "rejected_would_cross"})
    kept = _evaluate(predictions, results=results).doc
    assert kept["n_shadows"] == 5 and kept["n_excluded"]["never_live"] == 0
    assert _model(kept, TT)["n_phantom"] == 1
    no_tt = _model_fills().filter(pc.not_equal(_model_fills().column("model_id"), TT))
    out = _evaluate(predictions, results=results, model_fills=no_tt).doc
    assert out["n_shadows"] == 4 and out["n_excluded"]["never_live"] == 1
    assert all(m["n_shadows"] == 4 and m["n_phantom"] + m["n_confirmed"] + m["n_missed"] <= 4 for m in out["models"])


def test_fill_rows_mechanisms_prices_and_window(predictions: Any) -> None:
    res = _evaluate(predictions)
    rows = res.fills.to_pylist()
    key = {(r["arm"], r["shadow_id"], r["fill_seq"]): r for r in rows}
    # exact rows within the window only (s1's fill at L + 90 s is outside it)
    assert [(r["shadow_id"], r["mechanism"]) for r in rows if r["arm"] == "exact"] == [
        ("s0", "queue"),
        ("s2", "swept"),
        ("s4", "hidden_print"),
    ]
    assert key[(FQ, "s1", 0)]["mechanism"] == "queue" and key[(FQ, "s1", 1)]["mechanism"] == "swept"
    assert key[(FQ, "s1", 0)]["status"] == "phantom" and key[(FQ, "s0", 0)]["status"] == "confirmed"
    assert (FQ, "s3", 0) not in key  # beyond the window
    # particle rows: one per filled shadow at the median first-fill time, whole quantity
    assert key[(PM, "s0", 0)]["mechanism"] == "particle" and key[(PM, "s0", 0)]["ts_event"].value == L(0) + 2 * S
    assert key[(PM, "s2", 0)]["fill_qty"] == 10.0 and (PM, "s1", 0) not in key
    assert key[(NT, "s0", 0)]["mechanism"] == "touch"
    # a taker row pays the prevailing opposite best (P(-1)), capped at the limit P(0); makers rest at the limit
    tk = key[(TT, "s3", 0)]
    assert tk["mechanism"] == "cross_on_arrival" and tk["liquidity"] == "taker" and tk["px"] == P(-1)
    assert key[(FQ, "s0", 0)]["px"] == P(0)
    assert res.fills.schema.equals(table_schema("qt.zoo_toxicity_fills"), check_metadata=True)
    assert res.markouts.schema.equals(table_schema("qt.zoo_toxicity_markouts"), check_metadata=True)
    assert res.shadows.schema.equals(table_schema("qt.zoo_toxicity_shadows"), check_metadata=True)
    assert res.markouts.num_rows == res.fills.num_rows * len(HZ)
    assert res.shadows.num_rows == 5 * len(MODELS)


def test_decomposition_matches_hand_computation(predictions: Any) -> None:
    """The front-of-queue model at 1 s, by hand: phantom s1 (fill L + 25 s at P(0), mid P(-1) at L + 26 s: -1 tick),
    missed s2 (exact fill L + 40 s, mid P(3) at L + 41 s: +3 ticks), confirmed s0 and s4 (model L + 1 s, exact
    L + 2 s, both -1 tick) over 5 shadows."""
    doc = _evaluate(predictions).doc
    h1 = _model(doc, FQ)["horizons"][str(S)]
    v = {k: x["value"] for k, x in h1.items() if isinstance(x, dict)}
    assert h1["n"] == 5
    assert v["grant_ticks"] == pytest.approx(-1 / 5)
    assert v["deny_ticks"] == pytest.approx(-3 / 5)
    assert v["timing_ticks"] == pytest.approx(0.0)
    assert v["bias_ticks"] == pytest.approx(-4 / 5)
    assert v["model_pnl_ticks"] == pytest.approx(-3 / 5) and v["exact_pnl_ticks"] == pytest.approx(1 / 5)
    assert v["phantom_mo_ticks"] == pytest.approx(-1.0) and v["missed_mo_ticks"] == pytest.approx(3.0)
    assert v["confirmed_mo_ticks"] == pytest.approx(-1.0) and v["confirmed_exact_mo_ticks"] == pytest.approx(-1.0)
    # mid-to-mid: the model's L + 1 s fills see P(1) -> P(-1) (-2 ticks), the exact L + 2 s fills see no move
    assert v["confirmed_mm_ticks"] == pytest.approx(-2.0) and v["confirmed_exact_mm_ticks"] == pytest.approx(0.0)
    assert v["phantom_mm_ticks"] == pytest.approx(0.0) and v["missed_mm_ticks"] == pytest.approx(0.0)
    # bps: one tick is 1 bp of P(0) = 100.00
    assert v["grant_bps"] == pytest.approx(-0.2) and v["bias_bps"] == pytest.approx(-0.8)
    # at 10 s the phantom fill (L + 25 s) sees P(3) at L + 35 s
    assert _model(doc, FQ)["horizons"][str(10 * S)]["grant_ticks"]["value"] == pytest.approx(3 / 5)
    # the identity bias = grant + timing + deny = model_pnl - exact_pnl, for every model and horizon
    for d in doc["models"]:
        for hz in d["horizons"].values():
            if hz["n"] == 0:
                continue
            g, t, dn = (hz[k]["value"] for k in ("grant_ticks", "timing_ticks", "deny_ticks"))
            assert hz["bias_ticks"]["value"] == pytest.approx(g + t + dn, abs=1e-12)
            mp, ep = hz["model_pnl_ticks"]["value"], hz["exact_pnl_ticks"]["value"]
            assert hz["bias_ticks"]["value"] == pytest.approx(mp - ep, abs=1e-12)
    # a model that never fills grants nothing and denies every exact fill: bias = -exact_pnl
    hb = _model(doc, HB)["horizons"][str(S)]
    assert hb["grant_ticks"]["value"] == 0.0 and hb["timing_ticks"]["value"] == 0.0 and hb["n"] == 5
    assert hb["deny_ticks"]["value"] == pytest.approx(-1 / 5) and hb["bias_ticks"]["value"] == pytest.approx(-1 / 5)


def test_pnl_terms_summed_in_blocks_give_the_documents_values(predictions: Any) -> None:
    """The W1 atlas sums `PnlTerms` per instrument-day and block (`blocks.pnl_block_stats`); summed over the blocks,
    every PnL statistic of the document comes back, so an atlas number is the `zoo-toxicity` number of its shadows."""
    from queuetruth.eval import blocks as B

    res = _evaluate(predictions)
    fields = {"pnl_grant": "grant_ticks", "pnl_deny": "deny_ticks", "pnl_timing": "timing_ticks",
              "pnl_model": "model_pnl_ticks", "pnl_exact": "exact_pnl_ticks", "pnl_bias": "bias_ticks"}  # fmt: skip
    for block_ns in (None, 1 * S, 30 * 60 * S):
        t = B.pnl_block_stats(res.pnl, symbols={1: "AAA"}, extra={"mode": "faithful"}, block_ns=block_ns)
        assert set(t.column("symbol").to_pylist()) == {"AAA"} and set(t.column("mode").to_pylist()) == {"faithful"}
        rows = t.to_pylist()
        for d in res.doc["models"]:
            for hz, h in d["horizons"].items():
                mine = [r for r in rows if r["model_id"] == d["model_id"] and r["horizon_ns"] == int(hz)]
                n = sum(r["pnl_n"] for r in mine)
                assert n == h["n"], (d["model_id"], hz)
                for f, name in fields.items():
                    got = sum(r[f] for r in mine) / n if n else None
                    assert got == pytest.approx(h[name]["value"], abs=1e-12), (d["model_id"], hz, name)
                nb = sum(r["pnl_n_bps"] for r in mine)
                got = sum(r["pnl_bias_bps"] for r in mine) / nb if nb else None
                assert got == pytest.approx(h["bias_bps"]["value"], abs=1e-12)
        # the cluster bootstrap reads the same sums: one instrument-day, so the value without an interval
        r = B.cluster_bootstrap(
            t, metric="pnl_bias_ticks", clusters=("session_date",), group=("model_id", "horizon_ns"), bootstrap_b=20
        )
        fq = next(x for x in r if x["model_id"] == FQ and x["horizon_ns"] == S)
        assert fq["value"] == pytest.approx(-4 / 5) and fq["ci_lo"] is None and fq["n"] == 5


def test_by_type_table_and_rankings(predictions: Any) -> None:
    doc = _evaluate(predictions).doc
    bt = {(e["arm"], e["status"], e["mechanism"]): e for e in doc["by_type"]}
    assert list(bt)[:4] == [("exact", None, "queue"), ("exact", None, "swept"), ("exact", None, "hidden_print"),
                            ("exact", None, "all")]  # fmt: skip
    # fill-qty weighted: FQ's phantom s1 rows are 4 @ L + 25 s (-1 tick at 1 s) and 6 @ L + 40 s (+3 ticks)
    ph = bt[(FQ, "phantom", "all")]
    assert ph["n_fills"] == 2 and ph["n_shadows"] == 1 and ph["qty"] == 10.0
    assert ph["markouts"][str(S)]["mo_ticks"]["value"] == pytest.approx((4 * -1 + 6 * 3) / 10)
    assert bt[(FQ, "phantom", "swept")]["markouts"][str(S)]["mo_ticks"]["value"] == pytest.approx(3.0)
    # missed rows are the exact rows of the missed shadows, with the exact mechanism
    assert bt[(HB, "missed", "hidden_print")]["n_fills"] == 1 and bt[(HB, "missed", "all")]["n_fills"] == 3
    assert (TT, "phantom", "cross_on_arrival") in bt and (PM, "confirmed", "particle") in bt
    assert all(e["mechanism"] in (*MECHANISMS, "all") for e in doc["by_type"])
    rk = doc["rankings"]
    assert rk["horizon_ns"] == S
    # every ranking is by |value| (the largest PnL distortion first), the signed value kept
    for name in ("grant", "deny", "bias"):
        mags = [abs(r["value"]) for r in rk[name]]
        assert mags == sorted(mags, reverse=True) and [r["rank"] for r in rk[name]] == [1, 2, 3, 4, 5]
    # FQ and TT both grant -1 tick per phantom over 5 shadows (TT: the taker fill at P(-1), mid P(-2) 1 s later);
    # ties keep model order, then PM, NT and HB grant 0
    assert [r["model_id"] for r in rk["grant"]] == [FQ, TT, PM, NT, HB]
    assert rk["grant"][0]["value"] == pytest.approx(-0.2) and rk["grant"][1]["value"] == pytest.approx(-0.2)
    # deny: FQ misses s2 (+3 ticks): -0.6; NT misses s2 and s4: -0.4; PM misses s4 (-1 tick): +0.2; TT and HB miss
    # s0, s2 and s4: -0.2
    assert [r["model_id"] for r in rk["deny"]] == [FQ, NT, PM, TT, HB]
    assert [r["value"] for r in rk["deny"]] == pytest.approx([-0.6, -0.4, 0.2, -0.2, -0.2])
    # rank intervals from the shared replicates: a model that grants 0 in every replicate shares its place with the
    # other zeros and with FQ or TT when one of them draws 0 (only one block drawn)
    hb = rk["grant"][4]
    assert (hb["rank_lo"], hb["rank_hi"]) == (2, 5)
    assert all(r["rank_lo"] <= r["rank"] <= r["rank_hi"] for name in ("grant", "deny", "bias") for r in rk[name])
    # paired gaps: equal magnitudes are not separated; the last place has no gap
    assert rk["grant"][0]["gap_to_next"]["value"] == pytest.approx(0.0)
    assert rk["grant"][0]["separated_from_next"] is False
    assert hb["gap_to_next"] is None and hb["separated_from_next"] is None
    gap = rk["deny"][0]["gap_to_next"]
    assert gap["value"] == pytest.approx(0.2) and gap["ci_lo"] <= gap["value"] <= gap["ci_hi"]


def test_sell_taker_price_and_the_no_quote_fallback(predictions: Any) -> None:
    """A sell taker fill gets the prevailing bid, floored at the limit: s3 as a sell limit P(-5) crossing at L + 1 s
    sells at the bid P(-2). A taker fill with no quote in its instrument-day yet is priced at the limit, with a
    warning."""
    t = _shadow_results()
    side = pa.array([A if k == 3 else B for k in range(10)], pa.uint8())
    px = pa.array([P(-5) if k == 3 else P(0) for k in range(10)], pa.int64())
    results = t.set_column(2, "side", side).set_column(3, "price", px)
    res = _evaluate(predictions, results=results)
    row = next(r for r in res.fills.to_pylist() if r["arm"] == TT)
    assert row["side"] == A and row["liquidity"] == "taker" and row["px"] == P(-2)
    # 1 s later the mid is P(-2): flat from the fill price; the mid fell half a tick from P(-1.5), a gain for a sell
    mo = next(r for r in res.markouts.to_pylist() if r["arm"] == TT and r["horizon_ns"] == S)
    assert mo["mo_ticks"] == pytest.approx(0.0) and mo["mm_ticks"] == pytest.approx(0.5)
    assert not any(w.startswith("taker_price") for w in res.warnings)
    # the same fill before the first quote of the day: the limit price
    mf = _model_fills()
    ts = pc.if_else(pc.equal(mf.column("model_id"), TT), pa.scalar(T0 - 5 * S, mf.schema.field("ts_event").type),
                    mf.column("ts_event"))  # fmt: skip
    early = mf.set_column(mf.schema.get_field_index("ts_event"), mf.schema.field("ts_event"), ts)
    res = _evaluate(predictions, results=results, model_fills=early)
    row = next(r for r in res.fills.to_pylist() if r["arm"] == TT)
    assert row["px"] == P(-5)
    assert f"taker_price: {TT}: 1 taker fill(s) without an opposite quote are priced at the limit" in res.warnings
    # 1 s later there is still no quote: no markout
    mo = next(r for r in res.markouts.to_pylist() if r["arm"] == TT and r["horizon_ns"] == S)
    assert mo["mo_ticks"] is None and mo["mm_ticks"] is None


def test_day_blocks_and_multi_day_markouts(predictions: Any) -> None:
    """Five session dates: the bootstrap blocks are days, and each fill is marked out on its own instrument-day's
    quotes (day d: mid P(1) at the open, P(1 - d) from 1.5 s, so a buy at P(0) 1 s after the open earns 1 - d)."""
    days = 5
    cal = [(MIDNIGHT + d * DAY, DAY0 + d, TICK) for d in range(days)]
    live = [T0 + d * DAY for d in range(days)]
    quotes = []
    for d in range(days):
        quotes += [{"ts": live[d], "iid": 1, "bid": P(0), "ask": P(2)},
                   {"ts": live[d] + 1500 * MS, "iid": 1, "bid": P(-d), "ask": P(2 - d)}]  # fmt: skip
    rows = [
        {"model_id": FQ, "shadow_id": f"d{d}", "qty": 10, "horizon_ns": W, "p_fill": 1.0,
         "model_ts_first_fill": live[d] + S, "truth_filled": True, "truth_ts_first_fill": live[d] + S,
         "instrument_id": 1, "session_date": DAY0 + d, "ts_live": live[d]}
        for d in range(days)
    ]  # fmt: skip
    exact = make_table("qt.shadow_fills", [
        {"shadow_id": f"d{d}", "fill_seq": 0, "ts_event": live[d] + S, "sequence": 10 + d, "fill_qty": 10,
         "cum_qty": 10, "leaves_qty": 0, "fill_px": P(0), "reason": "queue_depleted", "queue_ahead_before": 0,
         "traded_at_px_event": 0, "trade_through_event": 0, "behind_volume_executed": 0, "liquidity": "maker"}
        for d in range(days)], full=True)  # fmt: skip
    mfills = make_table("qt.zoo_fills", [
        {"model_id": FQ, "shadow_id": f"d{d}", "fill_seq": 0, "ts_event": live[d] + S, "fill_qty": 10.0,
         "cum_qty": 10.0, "queue_ahead_est_before": 0.0, "reason": "queue", "quirk": "none", "liquidity": "maker"}
        for d in range(days)], full=True)  # fmt: skip
    results = pa.table({
        "shadow_id": [f"d{d}" for d in range(days)], "instrument_id": pa.array([1] * days, pa.uint32()),
        "side": pa.array([B] * days, pa.uint8()), "price": pa.array([P(0)] * days, pa.int64())})  # fmt: skip
    res = evaluate_toxicity(
        predictions(rows), planned_life_table([f"d{d}" for d in range(days)], [None] * days, [None] * days),
        mfills, results, exact, _trades().slice(0, 0), _bbo_table(quotes), _instruments({1: cal}),
        window_ns=W, horizons_ns=[S], rank_horizon_ns=S, bootstrap_b=300, seed=2,
        last_event_ts={(1, DAY0 + d): live[d] + 3600 * S for d in range(days)},
    )  # fmt: skip
    doc = res.doc
    assert doc["ci_block"] == "day" and doc["n_blocks"] == days and doc["n_shadows"] == days
    mo = [r["mo_ticks"] for r in res.markouts.to_pylist() if r["arm"] == "exact"]
    assert mo == pytest.approx([1.0 - d for d in range(days)])
    h = _model(doc, FQ)["horizons"][str(S)]
    assert h["exact_pnl_ticks"]["value"] == pytest.approx(-1.0) and h["bias_ticks"]["value"] == 0.0
    # day blocks: the replicate of the exact PnL is a mean of whole days, so its CI lies within the days' range
    assert -3.0 <= h["exact_pnl_ticks"]["ci_lo"] < -1.0 < h["exact_pnl_ticks"]["ci_hi"] <= 1.0
    counts = block_counts(days, 300, bootstrap_seed(2, BOOTSTRAP_TEST_ID)).astype(float)
    reps = (counts @ np.array([1.0 - d for d in range(days)])) / counts.sum(axis=1)
    assert (h["exact_pnl_ticks"]["ci_lo"], h["exact_pnl_ticks"]["ci_hi"]) == pytest.approx(percentile_ci(reps))


def test_spec_canonical_horizons() -> None:
    """Equal horizon sets give one spec (and so one run_id): sorted, without duplicates; non-positive horizons and
    unknown fields are configuration errors."""
    from queuetruth import api

    base = {"zoo": {"path": "z"}, "dataset": {"path": "d"}}
    a = api._validate(ToxicitySpec, {**base, "horizons_ns": [S, S, 100 * MS]})
    b = api._validate(ToxicitySpec, {**base, "horizons_ns": [100 * MS, S]})
    assert a.horizons_ns == [100 * MS, S] and a.model_dump() == b.model_dump()
    for bad in ({"horizons_ns": [0, S]}, {"horizons_ns": []}, {"threads": 0}, {"bogus": 1}):
        with pytest.raises(QtError) as e:
            api._validate(ToxicitySpec, {**base, **bad})
        assert e.value.code == "QT-CONFIG-001"


def test_render_answer_first_view(predictions: Any) -> None:
    res = _evaluate(predictions)
    doc = {"run_id": "r", "zoo_run_id": "z", "zoo_mode": "corrected", **res.doc, "files": [], "warnings": []}
    text = render_toxicity(doc)
    assert text is not None
    lines = text.splitlines()
    assert lines[0].startswith("zoo-toxicity: 5 orders, 5 models (zoo run z, corrected mode")
    assert any(x.startswith("left out: 1 hbt 312, 1 no exact arm") for x in lines)
    # the exact arm's mechanisms, then the models ranked by |bias| with the starred cells
    table = lines[lines.index(next(x for x in lines if x.strip().startswith("model "))) :]
    order = [x.split()[0] for x in table[1:6]]
    assert order == [r["model_id"] for r in doc["rankings"]["bias"]]
    assert any(x.strip().startswith("hidden_print") for x in lines)
    plain = text[text.index("in plain words:") :]
    top = doc["rankings"]["bias"][0]["model_id"]
    assert f"{top} misstates PnL the most" in plain and "The bounds:" in plain
    assert f"{FQ} gains the most from fills it wrongly grants" in plain
    assert f"{FQ} loses the most PnL to fills it wrongly denies" in plain
    assert "n_inconsistent" in plain and "--allow-truth-mismatch" in plain
    assert render_toxicity({"models": []}) is None


def test_bootstrap_matches_explicit_replicates(predictions: Any) -> None:
    res = _evaluate(predictions)
    got = _model(res.doc, FQ)["horizons"][str(S)]["deny_ticks"]
    # shadows s0..s4: blocks 0, 0, 0, 1, 1; deny contribution -3 on s2 only
    counts = block_counts(2, 400, bootstrap_seed(5, BOOTSTRAP_TEST_ID)).astype(float)
    num = np.array([-3.0, 0.0])
    den = np.array([3.0, 2.0])
    reps = (counts @ num) / (counts @ den)
    lo, hi = percentile_ci(np.where(counts @ den > 0, reps, np.nan))
    assert got["ci_lo"] == pytest.approx(lo) and got["ci_hi"] == pytest.approx(hi) and got["n"] == 5
    again = _evaluate(predictions).doc
    assert again == res.doc
    other = _evaluate(predictions, seed=6).doc
    assert _model(other, FQ)["horizons"][str(S)]["deny_ticks"]["value"] == got["value"]


def test_configuration_errors(predictions: Any) -> None:
    with pytest.raises(QtError) as e:
        _evaluate(predictions, window_ns=30 * S)
    assert e.value.code == "QT-CONFIG-001" and "not a prediction horizon" in e.value.message
    with pytest.raises(QtError) as e:
        _evaluate(predictions, rank_horizon_ns=5 * S)
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        _evaluate(predictions, horizons_ns=[0, S], rank_horizon_ns=S)
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        evaluate_toxicity(
            _predictions(predictions),
            _planned(),
            _model_fills(),
            _shadow_results().slice(0, 3),
            _exact_fills(),
            _trades(),
            _bbo_table(_quotes()),
            _instruments({1: [(MIDNIGHT, DAY0, TICK)]}),
            window_ns=W,
            horizons_ns=HZ,
            rank_horizon_ns=S,
        )
    assert e.value.code == "QT-INPUT-002" and "shadow_results" in e.value.message
    with pytest.raises(QtError) as e:  # the planned life must cover every predicted shadow (A2, OC-7)
        evaluate_toxicity(
            _predictions(predictions),
            _planned().slice(0, 4),
            _model_fills(),
            _shadow_results(),
            _exact_fills(),
            _trades(),
            _bbo_table(_quotes()),
            _instruments({1: [(MIDNIGHT, DAY0, TICK)]}),
            window_ns=W,
            horizons_ns=HZ,
            rank_horizon_ns=S,
        )
    assert e.value.code == "QT-INPUT-002"


def test_empty_inputs_give_an_empty_valid_document(predictions: Any) -> None:
    empty = predictions([]).slice(0, 0)
    res = evaluate_toxicity(
        empty,
        planned_life_table([], [], []),
        _model_fills().slice(0, 0),
        _shadow_results().slice(0, 0),
        _exact_fills().slice(0, 0),
        _trades().slice(0, 0),
        _bbo_table([]),
        _instruments({1: [(MIDNIGHT, DAY0, TICK)]}),
        window_ns=W,
        horizons_ns=HZ,
        rank_horizon_ns=S,
    )
    assert res.doc["n_shadows"] == 0 and res.doc["models"] == [] and res.doc["by_type"] == []
    assert res.fills.num_rows == 0 and res.markouts.num_rows == 0 and res.shadows.num_rows == 0
    from queuetruth.eval import blocks as B

    pnl = B.pnl_block_stats(res.pnl)
    assert pnl.num_rows == 0 and set(B.PNL_SUMS) <= set(pnl.column_names)


def test_document_model_and_table_schemas_agree() -> None:
    lit = m.ToxMechanism.__args__  # type: ignore[attr-defined]
    assert tuple(lit) == (*MECHANISMS, "all")
    for name, cols in TABLES.items():
        sch = table_schema(name)
        assert sch.metadata[b"qt.schema"] == name.encode()
        assert [f.name for f in sch] == [c[0] for c in cols]
    schema = json.loads((REPO / "schemas" / "json" / "qt.zoo_toxicity.schema.json").read_text())
    assert schema["properties"]["schema"]["const"] == "qt.zoo_toxicity"
    assert m.DOC_MODELS["qt.zoo_toxicity"] is m.ZooToxicity


# ---- end to end on the synthetic Nasdaq fixture ------------------------------------------------------------------

E2E_MODELS = [
    "hbt_l3_fifo",
    "front_of_queue",
    "trade_through_only",
    "nt_prob_touch[p=1]",
    "nt_prob_touch[p=0.3]",
    "hbt_power3[n=2]",
    "databento_mc[k=-0.8]",
]


@pytest.fixture(scope="module")
def zoo_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from queuetruth import api

    if not FIXTURE.exists():
        pytest.skip("synth_small_nasdaq fixture missing")
    root = tmp_path_factory.mktemp("tox")
    api.sample_shadows({"dataset": {"path": str(FIXTURE)}, "out": str(root / "shadows.parquet"), "n": 240, "seed": 3})
    api.run_zoo(
        {
            "dataset": {"path": str(FIXTURE)},
            "shadows": {"path": str(root / "shadows.parquet")},
            "out": str(root / "zoo"),
            "models": E2E_MODELS,
            "mode": "corrected",
            "horizons_ns": [S, 10 * S, 60 * S],
            "h_max_ns": 300 * S,
            "bootstrap_b": 50,
        }
    )
    return root


@pytest.mark.requires_core
def test_runner_end_to_end_bounds_and_outputs(zoo_run: Path) -> None:
    out = zoo_run / "tox"
    doc = run_zoo_toxicity(
        {
            "zoo": {"path": str(zoo_run / "zoo")},
            "dataset": {"path": str(FIXTURE)},
            "out": str(out),
            "window_ns": 60 * S,
            "rank_horizon_ns": 10 * S,
            "bootstrap_b": 200,
        }
    )
    d = doc.model_dump(mode="json", by_alias=True)
    on_disk = json.loads((out / "summary.json").read_text())
    assert on_disk == d
    schema = json.loads((REPO / "schemas" / "json" / "qt.zoo_toxicity.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(on_disk)
    assert d["n_excluded"]["truth_mismatch"] == 0 and d["n_shadows"] > 100 and d["zoo_mode"] == "corrected"
    assert d["exact"]["n_filled"] > 0
    by_family = {x["family"]: x for x in d["models"]}
    # corrected hbt_l3_fifo is the exact arm (O-ZOO-4): nothing granted or denied, no timing error
    l3 = by_family["hbt_l3_fifo"]
    assert l3["n_phantom"] == 0 and l3["n_missed"] == 0
    for hz in l3["horizons"].values():
        assert hz["bias_ticks"]["value"] == 0.0 and hz["timing_ticks"]["value"] == 0.0
    # the bounds bracket the exact arm order by order (O-ZOO-6): the optimistic bound never misses, the pessimistic
    # one never grants
    assert by_family["front_of_queue"]["n_missed"] == 0
    assert by_family["trade_through_only"]["n_phantom"] == 0
    # every forecast agrees with the fill rows, also for the random-draw touch model (p = 0.3): its status is the
    # fill its draws booked, so every population order is scored and the identity holds
    assert all(x["n_inconsistent"] == 0 for x in d["models"])
    by_id = {x["model_id"]: x for x in d["models"]}
    p3 = next(x for x in d["models"] if x["model_id"].startswith("nt_prob_touch[") and "p=0.3" in x["model_id"])
    n_neither = len([r for r in pq.read_table(out / "zoo_toxicity_shadows.parquet").to_pylist()
                     if r["model_id"] == p3["model_id"] and r["status"] == "neither"])  # fmt: skip
    assert p3["n_confirmed"] + p3["n_phantom"] + p3["n_missed"] + n_neither == d["n_shadows"]
    assert p3["n_phantom"] > 0 and p3["n_missed"] > 0
    for x in d["models"]:
        for hz in x["horizons"].values():
            g, t, dn = (hz[k]["value"] for k in ("grant_ticks", "timing_ticks", "deny_ticks"))
            assert hz["bias_ticks"]["value"] == pytest.approx(g + t + dn, abs=1e-12)
    # the rankings name the bounds: the optimistic bound grants the most, the pessimistic one denies the most
    rk = d["rankings"]
    assert by_id[rk["grant"][0]["model_id"]]["family"] == "front_of_queue"
    assert by_id[rk["deny"][0]["model_id"]]["family"] == "trade_through_only"
    assert rk["deny"][0]["value"] < 0 and rk["deny"][-1]["value"] == 0.0
    files = {f["name"]: f for f in d["files"]}
    assert set(files) == {"zoo_toxicity_fills.parquet", "zoo_toxicity_markouts.parquet", "zoo_toxicity_shadows.parquet"}
    for name, tbl in (
        ("zoo_toxicity_fills.parquet", "qt.zoo_toxicity_fills"),
        ("zoo_toxicity_markouts.parquet", "qt.zoo_toxicity_markouts"),
        ("zoo_toxicity_shadows.parquet", "qt.zoo_toxicity_shadows"),
    ):
        t = pq.read_table(out / name)
        assert t.schema.equals(table_schema(tbl), check_metadata=True) and t.num_rows == files[name]["rows"]
    # the exact arm's rows on this Nasdaq tape (seeded) include every resting mechanism, a hidden print among them
    fills = pq.read_table(out / "zoo_toxicity_fills.parquet").to_pylist()
    assert {r["mechanism"] for r in fills if r["arm"] == "exact"} == {
        "queue",
        "swept",
        "crossed_resting",
        "hidden_print",
    }
    assert by_family["front_of_queue"]["n_phantom"] > 0 and by_family["trade_through_only"]["n_missed"] > 0
    # deterministic: the same inputs give the same document and run_id; another seed moves only the CIs' draws
    again = run_zoo_toxicity(
        {
            "zoo": {"path": str(zoo_run / "zoo")},
            "dataset": {"path": str(FIXTURE)},
            "window_ns": 60 * S,
            "rank_horizon_ns": 10 * S,
            "bootstrap_b": 200,
        }
    ).model_dump(mode="json", by_alias=True)
    assert again["run_id"] == d["run_id"] and again["files"] == []
    assert {k: v for k, v in again.items() if k not in ("files", "spec")} == {
        k: v for k, v in d.items() if k not in ("files", "spec")
    }


@pytest.mark.requires_core
def test_cli_zoo_toxicity(zoo_run: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from queuetruth.cli import main

    out = zoo_run / "tox-cli"
    argv = ["zoo-toxicity", "--zoo", str(zoo_run / "zoo"), "--data", str(FIXTURE), "--out", str(out),
            "--horizons", "10s,1s,1s", "--rank-horizon", "1s", "--bootstrap-b", "50", "--threads", "2"]  # fmt: skip
    assert main([*argv, "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.zoo_toxicity" and doc["horizons_ns"] == [S, 10 * S]
    assert doc["window_ns"] == 300 * S and doc["spec"]["window_ns"] == 300 * S  # the zoo's h_max, resolved
    assert doc["rankings"]["horizon_ns"] == S and (out / "summary.json").exists()
    assert "threads" not in doc["spec"]  # execution-only
    # the same run through the API on one replay thread, with the default window spelled out and the horizons
    # listed once: one run_id, one answer
    api_doc = run_zoo_toxicity(
        {"zoo": {"path": str(zoo_run / "zoo")}, "dataset": {"path": str(FIXTURE)}, "horizons_ns": [S, 10 * S],
         "window_ns": 300 * S, "rank_horizon_ns": S, "bootstrap_b": 50}
    ).model_dump(mode="json", by_alias=True)  # fmt: skip
    assert api_doc["run_id"] == doc["run_id"]
    assert {k: v for k, v in api_doc.items() if k != "files"} == {k: v for k, v in doc.items() if k != "files"}
    # the human view answers first
    assert main([*argv, "--format", "human"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("zoo-toxicity: ") and "each model's PnL error at the 1 s markout" in text
    assert "in plain words:" in text and "trade_through_only" in text
    assert main(["zoo-toxicity", "--zoo", str(zoo_run / "zoo"), "--data", str(FIXTURE), "--out", str(out),
                 "--window", "5s", "--format", "json"]) == 2  # fmt: skip
    assert json.loads(capsys.readouterr().out)["code"] == "QT-CONFIG-001"


@pytest.mark.requires_core
def test_runner_failure_modes(zoo_run: Path, tmp_path: Path) -> None:
    def run(**kw: Any) -> QtError:
        spec = {"zoo": {"path": str(zoo_run / "zoo")}, "dataset": {"path": str(FIXTURE)}, **kw}
        with pytest.raises(QtError) as e:
            run_zoo_toxicity(spec)
        return e.value

    assert run(zoo={"path": str(tmp_path / "nope")}).code == "QT-INPUT-001"
    assert run(bootstrap_b=0).code == "QT-CONFIG-001"
    no_fills = tmp_path / "zoo-no-fills"
    shutil.copytree(zoo_run / "zoo", no_fills)
    (no_fills / "zoo_fills.parquet").unlink()
    e = run(zoo={"path": str(no_fills)})
    assert e.code == "QT-INPUT-001" and "--no-fills" in (e.hint or "")
    moved = tmp_path / "zoo-moved"
    shutil.copytree(zoo_run / "zoo", moved)
    summ = json.loads((moved / "summary.json").read_text())
    summ["spec"]["shadows"] = {"path": str(tmp_path / "gone.parquet")}
    (moved / "summary.json").write_text(json.dumps(summ))
    e = run(zoo={"path": str(moved)})
    assert e.code == "QT-INPUT-001" and "--shadows" in (e.hint or "")
    l2 = tmp_path / "l2ds"
    l2.mkdir()
    (l2 / "manifest.json").write_text(json.dumps({"kind": "l2"}))
    assert run(dataset={"path": str(l2)}).code == "QT-INPUT-002"
    bad = tmp_path / "zoo-bad"
    shutil.copytree(zoo_run / "zoo", bad)
    (bad / "summary.json").write_text("{")
    assert run(zoo={"path": str(bad)}).code == "QT-INPUT-002"
    # a --data path with no dataset is not found
    e = run(dataset={"path": str(tmp_path / "no-such-dataset")})
    assert e.code == "QT-INPUT-001" and "manifest.json not found" in e.message
    # another dataset than the zoo's fails before any replay
    other = tmp_path / "other-ds"
    other.mkdir()
    (other / "manifest.json").write_text(json.dumps({"kind": "mbo", "dataset_id": "0" * 64}))
    e = run(dataset={"path": str(other)})
    assert e.code == "QT-INPUT-002" and "not the dataset the zoo ran on" in e.message
    # recorded amends that do not resolve from here: the hint names --amends
    moved = tmp_path / "zoo-amends-moved"
    shutil.copytree(zoo_run / "zoo", moved)
    summ = json.loads((moved / "summary.json").read_text())
    summ["spec"]["amends"] = {"path": str(tmp_path / "gone-amends.parquet")}
    (moved / "summary.json").write_text(json.dumps(summ))
    e = run(zoo={"path": str(moved)})
    assert e.code == "QT-INPUT-001" and "--amends" in (e.hint or "")
    assert run(amends={"path": str(tmp_path / "gone-amends.parquet")}).code == "QT-INPUT-001"


@pytest.mark.requires_core
def test_configuration_is_checked_before_the_replay(zoo_run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from queuetruth import api

    def no_replay(*_: Any, **__: Any) -> None:
        raise AssertionError("the replay must not run")

    monkeypatch.setattr(api, "replay", no_replay)
    base = {"zoo": {"path": str(zoo_run / "zoo")}, "dataset": {"path": str(FIXTURE)}}
    for bad in ({"window_ns": 5 * S}, {"rank_horizon_ns": 5 * S}, {"horizons_ns": [S], "rank_horizon_ns": 60 * S}):
        with pytest.raises(QtError) as e:
            run_zoo_toxicity({**base, **bad})
        assert e.value.code == "QT-CONFIG-001", bad


@pytest.mark.requires_core
def test_truth_mismatch_fails_the_run_unless_allowed(zoo_run: Path, tmp_path: Path) -> None:
    """A zoo summary whose recorded latency is not the one the zoo ran with: the replay no longer reproduces the
    exact arm. The run fails; with --allow-truth-mismatch it evaluates the rest and records the share."""
    tampered = tmp_path / "zoo-latency"
    shutil.copytree(zoo_run / "zoo", tampered)
    summ = json.loads((tampered / "summary.json").read_text())
    summ["spec"]["latency"] = {**(summ["spec"].get("latency") or {}), "feed_ns": 2_000_000_000}
    (tampered / "summary.json").write_text(json.dumps(summ))
    spec = {"zoo": {"path": str(tampered)}, "dataset": {"path": str(FIXTURE)}, "window_ns": 60 * S,
            "rank_horizon_ns": 10 * S, "bootstrap_b": 50}  # fmt: skip
    with pytest.raises(QtError) as e:
        run_zoo_toxicity(spec)
    assert e.value.code == "QT-INPUT-002" and e.value.message.startswith("truth_mismatch: ")
    doc = run_zoo_toxicity({**spec, "allow_truth_mismatch": True}).model_dump(mode="json", by_alias=True)
    n_bad = doc["n_excluded"]["truth_mismatch"]
    # the share of the checked shadows (the population before the never-live cut)
    checked = doc["n_shadows"] + n_bad + doc["n_excluded"]["never_live"]
    assert n_bad > 0 and doc["truth_mismatch_rate"] == pytest.approx(n_bad / checked)
    assert any(w.startswith("truth_mismatch: ") for w in doc["warnings"])


@pytest.fixture(scope="module")
def amended_zoo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A zoo run with amends, feed latency with jitter, a jitter seed and excluded real orders."""
    from queuetruth import api

    if not FIXTURE.exists():
        pytest.skip("synth_small_nasdaq fixture missing")
    root = tmp_path_factory.mktemp("tox-amend")
    api.sample_shadows({"dataset": {"path": str(FIXTURE)}, "out": str(root / "shadows.parquet"), "n": 120, "seed": 9})
    sh = pq.read_table(root / "shadows.parquet").to_pylist()
    amends = [
        {"shadow_id": r["shadow_id"], "ts_send": r["ts_submit"].value + 2 * S, "new_qty": int(r["qty"]) - 1}
        for r in sh
        if int(r["qty"]) >= 2
    ][:40]
    assert len(amends) >= 10
    write_table(make_table("qt.shadow_amend", amends), root / "amends.parquet")
    api.run_zoo(
        {
            "dataset": {"path": str(FIXTURE)},
            "shadows": {"path": str(root / "shadows.parquet")},
            "amends": {"path": str(root / "amends.parquet")},
            "latency": {"feed_ns": 1_000_000, "jitter_ns": 500_000},
            "seed": 7,
            "exclude_order_ids": [{"instrument_id": 1, "order_id": o} for o in (1001000000002, 1001000000003)],
            "out": str(root / "zoo"),
            "models": ["hbt_l3_fifo", "front_of_queue"],
            "mode": "corrected",
            "horizons_ns": [S, 10 * S],
            "h_max_ns": 60 * S,
            "bootstrap_b": 20,
        }
    )
    return root


@pytest.mark.requires_core
def test_replay_takes_the_zoos_amends_latency_and_exclusions(amended_zoo: Path, tmp_path: Path) -> None:
    """The replay reproduces an amended, latency-jittered, self-excluded zoo run exactly (truth_mismatch 0, else the
    run fails), corrected hbt_l3_fifo stays exact, and a zoo directory moved away from its inputs is evaluated with
    --shadows and --amends."""
    spec = {"zoo": {"path": str(amended_zoo / "zoo")}, "dataset": {"path": str(FIXTURE)}, "horizons_ns": [S, 10 * S],
            "rank_horizon_ns": S, "bootstrap_b": 50}  # fmt: skip
    doc = run_zoo_toxicity(spec).model_dump(mode="json", by_alias=True)
    assert doc["n_excluded"]["truth_mismatch"] == 0 and doc["truth_mismatch_rate"] == 0.0 and doc["n_shadows"] > 50
    l3 = next(m for m in doc["models"] if m["family"] == "hbt_l3_fifo")
    assert l3["n_phantom"] == 0 and l3["n_missed"] == 0
    assert all(hz["bias_ticks"]["value"] == 0.0 for hz in l3["horizons"].values())
    # the zoo directory alone, its recorded inputs gone from their paths: pass them explicitly
    moved = tmp_path / "zoo"
    shutil.copytree(amended_zoo / "zoo", moved)
    summ = json.loads((moved / "summary.json").read_text())
    summ["spec"]["shadows"] = {"path": str(tmp_path / "nowhere" / "shadows.parquet")}
    summ["spec"]["amends"] = {"path": str(tmp_path / "nowhere" / "amends.parquet")}
    (moved / "summary.json").write_text(json.dumps(summ))
    again = run_zoo_toxicity(
        {**spec, "zoo": {"path": str(moved)}, "shadows": {"path": str(amended_zoo / "shadows.parquet")},
         "amends": {"path": str(amended_zoo / "amends.parquet")}}
    ).model_dump(mode="json", by_alias=True)  # fmt: skip
    body = ("n_shadows", "n_excluded", "exact", "models", "by_type", "rankings")
    assert {k: again[k] for k in body} == {k: doc[k] for k in body}
