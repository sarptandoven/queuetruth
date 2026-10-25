"""`hbt_driver.py` in the hftbacktest venvs: fills, stops, GTX expiry, cancels, and parity with the O-ZOO-3 driver.

The O-ZOO-3 driver (`python/tests/zoo/hbt_crosscheck.py`, validated against a Rust build of hftbacktest) steps
through every feed row in plain Python; `hbt_driver.py` jumps between actions with a numba loop. On random feeds
both must report the same fills (per order and timestamp) and the same stop code.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import hbt_config as hc
import numpy as np
import pytest

INTEG = Path(__file__).resolve().parents[1]
REPO = INTEG.parents[1]
DRIVER = INTEG / "hbt_driver.py"
OZOO3 = REPO / "python" / "tests" / "zoo" / "hbt_crosscheck.py"
EVENT = np.dtype(
    [
        ("ev", "<u8"),
        ("exch_ts", "<i8"),
        ("local_ts", "<i8"),
        ("px", "<f8"),
        ("qty", "<f8"),
        ("order_id", "<u8"),
        ("ival", "<i8"),
        ("fval", "<f8"),
    ]
)
EXCH, LOCAL, BUY, SELL, DEPTH, TRADE = 1 << 31, 1 << 30, 1 << 29, 1 << 28, 1, 2


def _npz(path: Path, rows: list[tuple[int, int, float, float]]) -> None:
    a = np.zeros(len(rows), EVENT)
    for i, (ev, ts, px, qty) in enumerate(rows):
        a[i] = (EXCH | LOCAL | ev, ts, ts, px, qty, 0, 0, 0.0)
    np.savez_compressed(path, data=a)


def _config(path: Path, entries: list[dict]) -> Path:
    path.write_text(hc.dumps(hc.document(entries, "test")))
    return path


def _drive(py: Path, tmp: Path, rows: list, actions: list, entries: list[dict], lot: float = 1.0) -> dict:
    _npz(tmp / "d.npz", rows)
    (tmp / "d.orders.json").write_text(
        json.dumps({"tick_size": 0.01, "lot_size": lot, "actions": actions, "shadow_ids": {"1": "sh1"}})
    )
    cfg = _config(tmp / "cfg.json", entries)
    out = tmp / "out.json"
    subprocess.run(
        [
            str(py),
            "-I",
            str(DRIVER),
            "--npz",
            str(tmp / "d.npz"),
            "--orders",
            str(tmp / "d.orders.json"),
            "--config",
            str(cfg),
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return json.loads(out.read_text())


RA = hc.make_entry("risk_adverse", "partial", [], "fixed")
# one lot ahead; a buy of 2 fills 1 then exactly the remaining 1 (hbt #312), then another trade arrives
ROWS_312 = [
    (DEPTH | BUY, 1, 100.0, 1.0),
    (DEPTH | SELL, 1, 100.01, 5.0),
    (TRADE | SELL, 3, 100.0, 2.0),
    (TRADE | SELL, 4, 100.0, 1.0),
    (TRADE | SELL, 5, 100.0, 1.0),
    (DEPTH | BUY, 6, 100.0, 3.0),
]
BUY_2 = [{"ts": 1, "kind": "submit", "order_id": 1, "side": "B", "px": 100.0, "qty": 2.0, "tif": "GTX"}]


def test_stock_build_stops_at_the_312_abort(hbt_stock_py: Path, tmp_path: Path) -> None:
    doc = _drive(hbt_stock_py, tmp_path, ROWS_312, BUY_2, [RA])
    (run,) = doc["runs"]
    assert doc["schema"] == "qtint.hbt_driver_run"
    assert run["error"] == {"code": 14, "ts": 5, "live_order_ids": []}
    assert [(f["order_id"], f["ts"], f["qty"], f["status"]) for f in run["fills"]] == [
        (1, 3, 1.0, "partially_filled"),
        (1, 4, 1.0, "filled"),
    ]
    assert run["fills"][0]["shadow_id"] == "sh1"


def test_patched_build_runs_to_the_end(hbt_patched_py: Path, tmp_path: Path) -> None:
    doc = _drive(hbt_patched_py, tmp_path, ROWS_312, BUY_2, [RA])
    (run,) = doc["runs"]
    assert run["error"] is None
    assert [(f["ts"], f["qty"]) for f in run["fills"]] == [(3, 1.0), (4, 1.0)]
    assert run["orders"] == [
        {"order_id": 1, "shadow_id": "sh1", "status": "filled", "leaves_qty": 0.0, "filled_qty": 2.0}
        | {"first_fill_ts": 3}
    ]


def test_gtx_cross_expires_and_cancel_paths(hbt_stock_py: Path, tmp_path: Path) -> None:
    rows = [
        (DEPTH | BUY, 1, 100.0, 1.0),
        (DEPTH | SELL, 1, 100.01, 5.0),
        (TRADE | SELL, 3, 100.0, 1.0),
        (TRADE | SELL, 8, 100.0, 4.0),
        (DEPTH | BUY, 9, 100.0, 2.0),
    ]
    actions = [
        # 1: crosses the ask on arrival -> GTX expires, never fills
        {"ts": 1, "kind": "submit", "order_id": 1, "side": "B", "px": 100.01, "qty": 1.0, "tif": "GTX"},
        # 2: cancelled at 5, before the trade at 8 that would fill it
        {"ts": 2, "kind": "submit", "order_id": 2, "side": "B", "px": 100.0, "qty": 1.0, "tif": "GTX"},
        {"ts": 5, "kind": "cancel", "order_id": 2},
        # 3: fills completely at 8 (4 > 1 ahead + 1); its cancel at 9 is skipped, not an error
        {"ts": 6, "kind": "submit", "order_id": 3, "side": "B", "px": 100.0, "qty": 1.0, "tif": "GTX"},
        {"ts": 9, "kind": "cancel", "order_id": 3},
        # a cancel of an order that was never submitted is skipped as well
        {"ts": 9, "kind": "cancel", "order_id": 1},
    ]
    doc = _drive(hbt_stock_py, tmp_path, rows, actions, [RA])
    (run,) = doc["runs"]
    assert run["error"] is None
    status = {o["order_id"]: o["status"] for o in run["orders"]}
    assert status == {1: "expired", 2: "canceled", 3: "filled"}
    assert [(f["order_id"], f["ts"], f["qty"]) for f in run["fills"]] == [(3, 8, 1.0)]


def test_bad_inputs_are_reported(hbt_stock_py: Path, tmp_path: Path) -> None:
    _npz(tmp_path / "d.npz", ROWS_312)
    (tmp_path / "o.json").write_text(json.dumps({"tick_size": 0.01, "lot_size": 1.0, "actions": BUY_2 + BUY_2}))
    cfg = _config(tmp_path / "cfg.json", [RA])
    r = subprocess.run(
        [
            str(hbt_stock_py),
            "-I",
            str(DRIVER),
            "--npz",
            str(tmp_path / "d.npz"),
            "--orders",
            str(tmp_path / "o.json"),
            "--config",
            str(cfg),
            "--out",
            str(tmp_path / "x.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "QT-INPUT-003" in r.stderr and "submitted twice" in r.stderr


def test_identity_and_compile_time_are_recorded(hbt_stock_py: Path, tmp_path: Path) -> None:
    r = subprocess.run([str(hbt_stock_py), "-I", str(DRIVER), "--identify"], capture_output=True, text=True, check=True)
    ident = json.loads(r.stdout)
    assert ident["hftbacktest"] == "2.4.4"
    assert set(ident) == {"hftbacktest", "native_sha256", "numba", "llvmlite", "numpy", "python"}
    ((name, digest),) = ident["native_sha256"].items()
    assert name.startswith("_hftbacktest") and len(digest) == 64
    doc = _drive(hbt_stock_py, tmp_path, ROWS_312, BUY_2, [RA, hc.make_entry("log", "partial", [], "fixed")])
    assert doc["engine_identity"] == ident
    assert doc["compile_s"] > 0 and doc["npz_load_s"] >= 0
    # compilation happens once before the timed runs: the first model's loop time is not the compile time
    assert all(r["wall_s"] < doc["compile_s"] for r in doc["runs"])


def test_missing_arguments_are_a_usage_error(hbt_stock_py: Path) -> None:
    r = subprocess.run([str(hbt_stock_py), "-I", str(DRIVER), "--npz", "x.npz"], capture_output=True, text=True)
    assert r.returncode == 2
    assert "QT-USAGE-001" in r.stderr


def _random_scenario(seed: int) -> tuple[list, list, float]:
    """A random two-sided book around 100.00 with depth updates, at-price trades and trade-throughs."""
    rng = np.random.default_rng(seed)
    lot = 1.0 if seed % 2 else 0.5
    rows: list = []
    ts = 1
    bid, ask = 100.0, 100.02
    for px in (bid, bid - 0.01, bid - 0.02):
        rows.append((DEPTH | BUY, ts, round(px, 2), float(rng.integers(1, 8)) * lot))
    for px in (ask, ask + 0.01, ask + 0.02):
        rows.append((DEPTH | SELL, ts, round(px, 2), float(rng.integers(1, 8)) * lot))
    for _ in range(120):
        ts += int(rng.integers(1, 4))
        r = rng.random()
        side_buy = rng.random() < 0.5
        if r < 0.55:
            px = (bid - 0.01 * int(rng.integers(0, 3))) if side_buy else (ask + 0.01 * int(rng.integers(0, 3)))
            rows.append((DEPTH | (BUY if side_buy else SELL), ts, round(px, 2), float(rng.integers(0, 9)) * lot))
        else:
            # aggressor sells hit bids (SELL trade), buys hit asks; sometimes through the touch
            through = 0.01 * int(rng.random() < 0.15)
            if side_buy:
                rows.append((TRADE | BUY, ts, round(ask + through, 2), float(rng.integers(1, 5)) * lot))
            else:
                rows.append((TRADE | SELL, ts, round(bid - through, 2), float(rng.integers(1, 5)) * lot))
    actions = []
    for oid in range(1, 7):
        t = int(rng.integers(2, ts // 2))
        buy = oid % 2 == 1
        px = round((bid if buy else ask) - (0.01 if buy else -0.01) * int(rng.integers(0, 2)), 2)
        qty = float(rng.integers(1, 4)) * lot
        actions.append({"ts": t, "kind": "submit", "order_id": oid, "side": "B" if buy else "A", "px": px, "qty": qty})
        actions[-1]["tif"] = "GTX"
        if rng.random() < 0.5:
            actions.append({"ts": t + int(rng.integers(5, 60)), "kind": "cancel", "order_id": oid})
    return rows, actions, lot


@pytest.mark.parametrize("which", ["stock", "patched"])
def test_parity_with_the_o_zoo_3_driver_on_random_feeds(
    which: str, hbt_stock_py: Path, hbt_patched_py: Path, tmp_path: Path
) -> None:
    py = hbt_stock_py if which == "stock" else hbt_patched_py
    entries = [hc.make_entry("risk_adverse", exch, [], "fixed") for exch in ("partial", "no_partial")] + [
        hc.make_entry(f, "partial", a, "fixed") for f, a in (("power3", [2.0]), ("log", []), ("power2", [0.5]))
    ]
    ozoo3_models = "hbt_risk_adverse,hbt_power3[n=2],hbt_log,hbt_power2[n=0.5]"
    scen = tmp_path / "scen"
    scen.mkdir()
    n_cmp = n_full = 0
    for seed in range(12):
        rows, actions, lot = _random_scenario(seed)
        _npz(scen / f"r{seed:02d}.npz", rows)
        (scen / f"r{seed:02d}.orders.json").write_text(
            json.dumps({"tick_size": 0.01, "lot_size": lot, "actions": actions})
        )
    ref_out = tmp_path / "ref.json"
    subprocess.run(
        [str(py), "-I", str(OZOO3), str(scen), "--out", str(ref_out), "--models", ozoo3_models],
        check=True,
        capture_output=True,
    )
    ref = {(r["scenario"], r["model_id"]): r for r in json.loads(ref_out.read_text())["runs"]}
    cfg = _config(tmp_path / "cfg.json", entries)
    for seed in range(12):
        out = tmp_path / f"o{seed}.json"
        subprocess.run(
            [
                str(py),
                "-I",
                str(DRIVER),
                "--npz",
                str(scen / f"r{seed:02d}.npz"),
                "--orders",
                str(scen / f"r{seed:02d}.orders.json"),
                "--config",
                str(cfg),
                "--out",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        for run in json.loads(out.read_text())["runs"]:
            r = ref.get((f"r{seed:02d}", run["model_id"]))
            if r is None:
                continue
            # the O-ZOO-3 driver stops at a cancel of an order that is no longer active (hbt returns an error
            # code); hbt_driver.py skips such a cancel. Compare up to that action then.
            stop = r["error"]["ts"] if r["error"] and r["error"].get("in") == "action" else None
            want: dict = {}
            for f in r["fills"]:
                want[(f["order_id"], f["ts"])] = want.get((f["order_id"], f["ts"]), 0.0) + f["fill_qty"]
            got: dict = {}
            for f in run["fills"]:
                if stop is None or f["ts"] <= stop:
                    got[(f["order_id"], f["ts"])] = got.get((f["order_id"], f["ts"]), 0.0) + f["qty"]
            assert got == want, (seed, run["model_id"])
            if stop is None:
                assert (run["error"] or {}).get("code") == (r["error"] or {}).get("error_code"), (seed, run["model_id"])
                n_full += 1
            n_cmp += 1
    assert n_cmp == 12 * 5
    assert n_full >= 30, n_full
