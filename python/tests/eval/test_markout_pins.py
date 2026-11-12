"""Pinned markouts of every caller that used to compute them on its own, now routed to `queuetruth.eval.markouts`.

`markout_pins.json` holds the outputs (as `float.hex`, bitwise) computed before the callers moved: the scorer's
`compute_markouts` (mid and microprice, censored and `last_quote` past the end, fees), the toxicity evaluation's
`mid_markouts` and its markout table, the prediction-market score's adverse-selection rows and the sandbox's
in-session markouts and scorecard rows. The arithmetic is elementwise IEEE (integer differences, then one rounding per
operation), so the record holds on every platform. Never regenerate it: a change here is a change of a published
number. `python tests/eval/test_markout_pins.py --record` exists only to make the first record.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

PINS_PATH = Path(__file__).parent / "markout_pins.json"
S = 1_000_000_000
MS = 1_000_000
DAY = 86_400 * S
DAY0 = 20_454
MIDNIGHT = DAY0 * DAY


def h(x: Any) -> Any:
    """Bitwise text of an output (floats as `float.hex`, NaN as "nan")."""
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        return [h(v) for v in x]
    if isinstance(x, np.ndarray):
        return [h(v) for v in x.tolist()]
    if isinstance(x, dict):
        return {str(k): h(v) for k, v in x.items()}
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return "nan" if x != x else float(x).hex()
    return x


def _digest(x: Any) -> str:
    """sha256 of the bitwise text of `x` (for outputs whose values another pin already holds)."""
    import hashlib

    return hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()


def _table(t: pa.Table) -> dict[str, Any]:
    from queuetruth.score._io import rows

    rs = rows(t)
    cols = {c: [r[c] for r in rs] for c in t.column_names}
    return {"schema": t.schema.metadata.get(b"qt.schema", b"").decode(), "columns": h(cols)}


# ---- the scorer -----------------------------------------------------------------------------------------------------


def _scorer_tape(seed: int) -> tuple[pa.Table, pa.Table, pa.Table, dict]:
    """Two instruments over two days at price_exp -9: quotes in shuffled file order with equal timestamps, empty sides
    and odd sizes, fills before the first quote, on quote timestamps, past the end of data and of an unknown
    instrument, with fees in and out of the instrument currency."""
    rng = random.Random(seed)
    inst = []
    for iid, tick, ccy in ((3, 10_000_000, "USD"), (9, 5_000_000, "EUR")):
        for day in (0, 1):
            inst.append(
                {
                    "instrument_id": iid,
                    "session_date": DAY0 + day,
                    "midnight_utc_ns": MIDNIGHT + day * DAY,
                    "tick_size": tick,
                    "price_exp": -9,
                    "size_exp": -2 if iid == 9 else 0,
                    "contract_multiplier": 1.0 if iid == 3 else 10.0,
                    "currency": ccy,
                }
            )
    quotes = []
    for r in inst:
        t = r["midnight_utc_ns"] + 9 * 3600 * S
        mid = 101_230_000_000 if r["instrument_id"] == 3 else 4_567_895_000_000
        tick = r["tick_size"]
        for _ in range(120):
            t += rng.choice([0, 1, MS, 250 * MS, 3 * S])
            mid += rng.randint(-3, 3) * tick
            spread = rng.randint(1, 4) * tick
            bid = mid - spread // 2 - (tick // 2 if rng.random() < 0.3 else 0)
            ask = bid + spread
            quotes.append(
                {
                    "instrument_id": r["instrument_id"],
                    "ts_event": t,
                    "bid_px": None if rng.random() < 0.05 else bid,
                    "bid_sz": rng.randint(0, 900),
                    "ask_px": None if rng.random() < 0.05 else ask,
                    "ask_sz": rng.randint(0, 900),
                }
            )
    rng.shuffle(quotes)
    fills = []
    for i in range(90):
        iid = rng.choice([3, 9, 9, 3, 4])
        day = rng.choice([0, 1])
        ts = MIDNIGHT + day * DAY + 9 * 3600 * S + rng.randint(-3 * S, 400 * S)
        same = [q["ts_event"] for q in quotes if q["instrument_id"] == iid]
        if same and rng.random() < 0.2:
            ts = rng.choice(same)
        tick = 10_000_000 if iid == 3 else 5_000_000
        base = 101_230_000_000 if iid == 3 else 4_567_895_000_000
        fee_ccy = rng.choice([None, "USD", "EUR", "usd"])
        fills.append(
            {
                "order_key": f"k{i // 3}",
                "arm": rng.choice(["live", "sim"]),
                "fill_seq": i % 3 + 1,
                "ts": ts,
                "qty": rng.randint(1, 50),
                "px": base + rng.randint(-20, 20) * tick + (tick // 2 if rng.random() < 0.2 else 0),
                "liquidity": rng.choice(["maker", "taker"]),
                "side": rng.choice(["buy", "sell", "B", "A"]),
                "instrument_id": iid,
                "fee": None if rng.random() < 0.3 else rng.uniform(-0.05, 0.2),
                "fee_ccy": fee_ccy,
            }
        )
    last = {(3, DAY0): max(q["ts_event"] for q in quotes if q["instrument_id"] == 3) - 10 * S}
    instruments = pa.table(
        {
            "instrument_id": [r["instrument_id"] for r in inst],
            "session_date": [r["session_date"] for r in inst],
            "midnight_utc_ns": [r["midnight_utc_ns"] for r in inst],
            "tick_size": [r["tick_size"] for r in inst],
            "price_exp": [r["price_exp"] for r in inst],
            "size_exp": [r["size_exp"] for r in inst],
            "contract_multiplier": [r["contract_multiplier"] for r in inst],
            "currency": [r["currency"] for r in inst],
        }
    )
    bbo = pa.table(
        {
            "instrument_id": pa.array([q["instrument_id"] for q in quotes], pa.uint32()),
            "ts_event": pa.array([q["ts_event"] for q in quotes], pa.int64()),
            "bid_px": pa.array([q["bid_px"] for q in quotes], pa.int64()),
            "bid_sz": pa.array([q["bid_sz"] for q in quotes], pa.uint64()),
            "ask_px": pa.array([q["ask_px"] for q in quotes], pa.int64()),
            "ask_sz": pa.array([q["ask_sz"] for q in quotes], pa.uint64()),
        }
    )
    return pa.Table.from_pylist(fills), bbo, instruments, last


def scorer_outputs() -> dict[str, Any]:
    from queuetruth.score import compute_markouts

    repo = Path(__file__).resolve().parents[3]
    ref = json.loads((repo / "tests" / "fixtures" / "reference" / "markouts.json").read_text())
    ins = ref["instrument"]
    instruments = pa.table(
        {
            "instrument_id": [1],
            "session_date": [DAY0],
            "midnight_utc_ns": [MIDNIGHT],
            "tick_size": [ins["tick_size"]],
            "price_exp": [ins["price_exp"]],
            "size_exp": [ins["size_exp"]],
            "contract_multiplier": [ins["contract_multiplier"]],
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
    out: dict[str, Any] = {}
    for past_end in (None, "last_quote"):
        t = compute_markouts(
            fills,
            bbo,
            instruments,
            ref["horizons_ns"],
            ["mid", "microprice"],
            last_event_ts={(1, DAY0): ins["last_event_ts"]},
            past_end=past_end,
        )
        out[f"reference:{past_end}"] = _table(t)
    fills2, bbo2, inst2, last2 = _scorer_tape(5)
    hs = [0, 1, 100 * MS, 1 * S, 10 * S, 60 * S, 600 * S]
    for past_end in (None, "last_quote"):
        extras: dict[str, list] = {}
        t = compute_markouts(
            fills2, bbo2, inst2, hs, ["microprice", "mid"], last_event_ts=last2, past_end=past_end, extras=extras
        )
        out[f"tape:{past_end}"] = tab = _table(t)
        out[f"tape:{past_end}:extras_are_columns"] = h(extras) == {k: tab["columns"][k] for k in extras}
    out["tape:default_end"] = _digest(_table(compute_markouts(fills2, bbo2, inst2, hs, ["mid"])))
    return out


def shortfall_outputs() -> dict[str, Any]:
    """The shortfall decomposition's exact marks (`2 M(t-)`, `2 M(t+h)`) of every fill of the scorer tape."""
    from queuetruth.score.shortfall import Marks

    fills, bbo, inst, last = _scorer_tape(5)
    f = fills.to_pylist()
    ts = np.array([r["ts"] for r in f], dtype=np.int64)
    out: dict[str, Any] = {}
    for past_end in (None, "last_quote"):
        m = Marks(bbo, inst, last_event_ts=last, past_end=past_end)
        book = m.locate([r["instrument_id"] for r in f], ts)
        out[f"{past_end}"] = {
            "book": h(book),
            "pre": h(m.pre_many(book, ts)),
            "post": [h(m.post_many(book, ts, hz)) for hz in (0, 1, 100 * MS, 1 * S, 60 * S, 600 * S)],
            "one": [h(m.pre(r["instrument_id"], r["ts"])) for r in f[:10]]
            + [h(m.post(r["instrument_id"], r["ts"], 1 * S)) for r in f[:10]],
        }
    return out


def atlas_touch_outputs() -> dict[str, Any]:
    """The atlas touch tables' last-print markout (`scripts/scale/atlas_touch.py`) on random prints and orders."""
    scale = Path(__file__).resolve().parents[3] / "scripts" / "scale"
    if str(scale) not in sys.path:
        sys.path.insert(0, str(scale))
    import atlas_touch as AT

    rng = np.random.default_rng(7)
    out: dict[str, Any] = {}
    for case, n_prints in (("prints", 300), ("one_print", 1), ("no_prints", 0)):
        ts = np.sort(rng.integers(0, 600 * S, n_prints)).astype(np.int64)
        px = (100_000 + rng.integers(-40, 40, n_prints)).astype(np.int64)
        n = 250
        lim = (100_000 + rng.integers(-30, 30, n)).astype(np.int64)
        sign = rng.choice([-1, 1], n).astype(np.int64)
        t = rng.integers(-60 * S, 620 * S, n).astype(np.int64)
        have = rng.random(n) < 0.8
        out[case] = {str(bar): h(AT.markout(ts, px, lim, sign, 5, t, bar, have)) for bar in (1 * S, 60 * S, 300 * S)}
    return out


# ---- the toxicity evaluation ----------------------------------------------------------------------------------------


def toxicity_outputs() -> dict[str, Any]:
    from queuetruth.eval import toxicity as TX
    from queuetruth.score._io import make_table

    rng = random.Random(23)
    days = {3: [(MIDNIGHT + d * DAY, DAY0 + d, 10_000_000) for d in (0, 1)], 9: [(MIDNIGHT, DAY0, 5_000_000)]}
    quotes = []
    for iid, cal in days.items():
        for mn, _, tick in cal:
            t = mn + 9 * 3600 * S
            bid = 99_990_000_000
            for _ in range(150):
                t += rng.choice([0, 1, 10 * MS, 2 * S])
                bid += rng.randint(-2, 2) * tick
                ask = bid + rng.randint(1, 3) * tick
                quotes.append(
                    {
                        "ts": t,
                        "iid": iid,
                        "bid": None if rng.random() < 0.04 else bid,
                        "ask": None if rng.random() < 0.04 else ask,
                    }
                )
    rng.shuffle(quotes)
    bbo = make_table(
        "qt.bbo",
        [
            {
                "ts_event": q["ts"],
                "instrument_id": q["iid"],
                "sequence": i + 1,
                "bid_px": q["bid"],
                "bid_sz": 0,
                "bid_ct": 0,
                "ask_px": q["ask"],
                "ask_sz": 0,
                "ask_ct": 0,
            }
            for i, q in enumerate(quotes)
        ],
    )
    inst_rows = [(iid, mn, date, tick) for iid, cal in days.items() for mn, date, tick in cal]
    instruments = pa.table(
        {
            "instrument_id": pa.array([r[0] for r in inst_rows], pa.uint32()),
            "session_date": pa.array([r[2] for r in inst_rows], pa.int32()).cast(pa.date32()),
            "midnight_utc_ns": pa.array([r[1] for r in inst_rows], pa.int64()),
            "tick_size": pa.array([r[3] for r in inst_rows], pa.int64()),
        }
    )
    last = {(3, DAY0): MIDNIGHT + 9 * 3600 * S + 150 * S}
    book = TX.QuoteBook(bbo, instruments, last)
    n = 200
    iid = np.array([rng.choice([3, 9]) for _ in range(n)])
    ts = np.array(
        [MIDNIGHT + rng.choice([0, DAY]) * (i == 3) + 9 * 3600 * S + rng.randint(-5 * S, 320 * S) for i in iid]
    )
    d = np.array([rng.choice([1, -1]) for _ in range(n)])
    tick = np.where(iid == 3, 10_000_000, 5_000_000)
    px = np.array([99_990_000_000 + rng.randint(-15, 15) * int(t) + (int(t) // 2) * (rng.random() < 0.2) for t in tick])
    valid = np.array([rng.random() > 0.05 for _ in range(n)])
    hs = [1, 100 * MS, 1 * S, 10 * S, 60 * S]
    mk = TX.mid_markouts(book, iid, ts, d, np.where(valid, px, 0), valid, hs)
    fills = pa.table(
        {
            "arm": [f"m{i % 3}" for i in range(n)],
            "shadow_id": [f"s{i}" for i in range(n)],
            "fill_seq": pa.array([i % 4 for i in range(n)], pa.uint32()),
        }
    )
    tab = TX._markouts_table(fills, mk, hs)
    quote = book.quote(iid, book.day(iid, ts)[0], ts, strict=False, end_check=True)
    return {
        "mid_markouts": {k: h(v.reshape(-1)) for k, v in mk.items()},
        "markouts_table": _digest(h(tab.to_pydict())),
        "quote": h([q.tolist() for q in quote]),
    }


# ---- the prediction-market score ------------------------------------------------------------------------------------


def pm_outputs(tmp: Path) -> dict[str, Any]:
    tests = Path(__file__).resolve().parents[1]
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    from pm.conftest import run_pipeline

    out = {}
    for venue in ("kalshi", "polymarket"):
        run = run_pipeline(venue, tmp / venue)
        out[venue] = h([m.model_dump() if hasattr(m, "model_dump") else dict(m) for m in run.score.markouts])
    return out


# ---- the sandbox ----------------------------------------------------------------------------------------------------


def sandbox_outputs(tmp: Path) -> dict[str, Any]:
    tests = Path(__file__).resolve().parents[1]
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    import pyarrow.parquet as pq
    from sandbox.sbx_helpers import SYNTH, random_agent

    from queuetruth.sandbox import Sandbox

    with Sandbox.open(
        {
            "dataset": {"path": str(SYNTH)},
            "instrument_id": 2,
            "session_date": "2026-06-15",
            "start": "15:00",
            "end": "15:15",
            "latency": "300us",
            "markout_horizons": ["500ms", "5s", "30s"],
        }
    ) as sb:
        seen = random_agent(sb, random.Random(21), steps=70)
        card = sb.finish(out=tmp / "sbx")
    in_session = [
        [m.order_id, m.fill_seq, m.horizon_ns, m.mo_ticks, m.mo_bps, m.mo_ccy] for o in seen for m in o.markouts
    ]
    return {
        "in_session": h(in_session),
        "card_markouts": h([m.model_dump() for m in card.markouts]),
        "card_adverse": h([m.model_dump() for m in card.adverse_selection]),
        "markouts_parquet": h(pq.read_table(tmp / "sbx" / "markouts.parquet").to_pydict()),
    }


def outputs(tmp: Path, *, core: bool) -> dict[str, Any]:
    out = {
        "scorer": scorer_outputs(),
        "shortfall": shortfall_outputs(),
        "atlas_touch": atlas_touch_outputs(),
        "toxicity": toxicity_outputs(),
        "pm": pm_outputs(tmp),
    }
    if core:
        out["sandbox"] = sandbox_outputs(tmp)
    return out


# ---- tests ----------------------------------------------------------------------------------------------------------


def _pins() -> dict[str, Any]:
    return json.loads(PINS_PATH.read_text(encoding="utf-8"))


def test_scorer_markouts_are_pinned() -> None:
    assert scorer_outputs() == _pins()["scorer"]


def test_shortfall_marks_are_pinned() -> None:
    assert shortfall_outputs() == _pins()["shortfall"]


def test_atlas_touch_markouts_are_pinned() -> None:
    assert atlas_touch_outputs() == _pins()["atlas_touch"]


def test_toxicity_markouts_are_pinned() -> None:
    assert toxicity_outputs() == _pins()["toxicity"]


def test_prediction_market_markouts_are_pinned(tmp_path: Path) -> None:
    assert pm_outputs(tmp_path) == _pins()["pm"]


@pytest.mark.requires_core
def test_sandbox_markouts_are_pinned(tmp_path: Path) -> None:
    assert sandbox_outputs(tmp_path) == _pins()["sandbox"]


if __name__ == "__main__":  # first record only (module docstring)
    import tempfile

    if "--record" not in sys.argv:
        raise SystemExit("usage: python tests/eval/test_markout_pins.py --record")
    with tempfile.TemporaryDirectory() as d:
        PINS_PATH.write_text(json.dumps(outputs(Path(d), core=True), sort_keys=True, separators=(",", ":")) + "\n")
