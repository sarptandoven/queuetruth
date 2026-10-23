"""Report fixes: calibration profile on the train rows it is captioned with (review E4),
"not computable" McNemar (E5), labels (E10), the leaderboard sort (trader review C1), the calibration verdict,
the learned-model comparison on the same rows (F6) and the why section's use of `api.why_explain` /
`api.why_suggestion`."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from queuetruth.errors import QtError
from queuetruth.report import render, why
from queuetruth.report.compute import alloc_profile
from queuetruth.report.load import load_inputs
from queuetruth.report.sections_models import _crossing
from queuetruth.report.sections_score import _shadow_liquidity
from queuetruth.schemas.arrow import FULL_SCHEMAS, SCHEMAS
from queuetruth.schemas.io import write_table
from queuetruth.schemas.models import CalibrationResult, WhyTrace
from report import synth as rc

S = 1_000_000_000
D1, D2 = "2019-12-30", "2019-12-31"


def _render(tmp_path: Path, inputs: list[Any], **kw: Any) -> tuple[Any, str]:
    out = tmp_path / "out" / "report.html"
    extra = {k: kw.pop(k) for k in list(kw) if k == "leaderboard_sort_horizon_ns"}
    summary = render({"inputs": [str(i) for i in inputs], "out": str(out), **kw}, **extra)
    return summary, out.read_text(encoding="utf-8")


def _section(html: str, sid: str) -> str:
    m = re.search(rf'<section id="{sid}">(.*?)</section>', html, re.S)
    assert m, f"section {sid} missing"
    return m.group(1)


# ---- E4: calibration profile ---------------------------------------------------------------------------------------


def _events(rng: np.random.Generator, m: int, n_true: float, day: int, t0: int, sid0: int) -> dict[str, list]:
    front, back = rng.uniform(1, 500, m), rng.uniform(1, 500, m)
    tot = rng.integers(1, 20, m)
    behind = rng.binomial(tot, 1.0 - (front / (front + back)) ** n_true).astype(float)
    return {
        "shadow_id": [f"s{sid0 + i % 40:05d}" for i in range(m)],
        "instrument_id": [1] * m,
        "session_date": [day] * m,
        "ts_event": (t0 + np.arange(m) * 10**8).tolist(),
        "front": front.tolist(),
        "back": back.tolist(),
        "ahead_cancel": (tot - behind).tolist(),
        "behind_cancel": behind.tolist(),
        "l2_prev": [None] * m,
        "l2_new": [None] * m,
        "l2_trades": [None] * m,
    }


def _published_ci(ev: dict[str, list], grid: tuple[float, float, int]) -> tuple[float, float, float]:
    prof = alloc_profile(
        "power3", ev["front"], ev["back"], ev["ahead_cancel"], ev["behind_cancel"], (grid[0], grid[1], 7001)
    )
    lmax = max(v for _, v in prof)
    lo, hi = _crossing([(t, v - lmax) for t, v in prof], -3.841459 / 2)
    t_hat = max(prof, key=lambda tv: tv[1])[0]
    assert lo is not None and hi is not None
    return math.exp(t_hat), math.exp(lo), math.exp(hi)


def _cal_dir(root: Path, ev_tr: dict, ev_te: dict, ci_profile: tuple[float, float], est: float, **window: Any) -> None:
    root.mkdir(parents=True, exist_ok=True)
    ev = {k: ev_tr[k] + ev_te[k] for k in ev_tr}
    rc.write(root, "alloc_events.parquet", "qt.alloc_events", ev)
    cal = CalibrationResult(
        run_id="1111222233334444",
        spec={"kind": "alloc", "family": "power3", "grid": {"alloc": {"lo": -3.0, "hi": 4.0, "n": 141}}}
        | window.pop("spec_extra", {}),
        kind="alloc",
        family="power3",
        mode="faithful",
        param_name="n",
        estimate=est,
        ci_profile=ci_profile,
        ci_bootstrap=(ci_profile[0] * 0.98, ci_profile[1] * 1.02),
        loglik=-1.0,
        n_events=len(ev_tr["front"]),
        n_shadows=40,
        train=window.get("train", {"dates": [D1]}),
        test=window.get("test", {"dates": [D2]}),
        held_out={},
        baseline_held_out={},
        stratified=[],
        exports={},
        data_sources=[],
        warnings=[],
    )
    rc.write_doc(root / "calibration.json", cal)


def test_e4_profile_uses_train_rows_and_matches_published_ci(tmp_path: Path) -> None:
    rng = np.random.default_rng(11)
    train = _events(rng, 3000, 2.0, rc.DAY, rc.OPEN, 0)
    test = _events(rng, 3000, 4.0, rc.DAY + 1, rc.OPEN + 86_400 * S, 100)  # another n on the test day
    est, lo, hi = _published_ci(train, (-3.0, 4.0))
    _, lo_all, hi_all = _published_ci({k: train[k] + test[k] for k in train}, (-3.0, 4.0))
    assert lo_all > hi  # recomputing on every row (the old chart) gives a disjoint interval
    _cal_dir(tmp_path / "cal", train, test, (lo, hi), est)
    _, html = _render(tmp_path, [tmp_path / "cal"])
    s = _section(html, "calibration")
    assert "3,000 of 6,000 events, train-window events (session dates 2019-12-30)" in s
    assert "matches the published profile CI" in s and "does NOT match" not in s
    # a published CI the rows cannot reproduce is called out, not silently charted
    _cal_dir(tmp_path / "cal2", train, test, (lo_all, hi_all), est)
    _, html2 = _render(tmp_path / "b", [tmp_path / "cal2"])
    assert "does NOT match the published profile CI" in _section(html2, "calibration")


def test_e4_profile_block_window_uses_dataset_sessions(tmp_path: Path) -> None:
    rng = np.random.default_rng(12)
    day_len = 6.5 * 3600 * S
    # two blocks of the 09:30-16:00 session: train = block 0 (first half), test = block 1
    train = _events(rng, 2000, 2.0, rc.DAY, rc.OPEN + 10 * S, 0)
    test = _events(rng, 2000, 4.0, rc.DAY, int(rc.OPEN + day_len / 2 + 10 * S), 100)
    est, lo, hi = _published_ci(train, (-3.0, 4.0))
    ds = tmp_path / "ds"
    ds.mkdir()
    inst = {k: [] for k in SCHEMAS["qt.instruments"].names}
    for k, v in {
        "instrument_id": 1,
        "session_date": rc.DAY,
        "symbol": "X",
        "venue": "nasdaq",
        "source_venue": "nasdaq",
        "price_exp": -9,
        "tick_size": 10**7,
        "lot_size": 1,
        "size_exp": 0,
        "contract_multiplier": 1.0,
        "currency": "USD",
        "session_tz": "America/New_York",
        "midnight_utc_ns": rc.OPEN - 34_200 * S,
        "open_s": 34_200,
        "close_s": 57_600,
        "source": "synthetic",
    }.items():
        inst[k].append(v)
    rc.write(ds, "instruments.parquet", "qt.instruments", inst)
    blocks = {"train": {"blocks": {"index": [0], "n_blocks": 2}}, "test": {"blocks": {"index": [1], "n_blocks": 2}}}
    _cal_dir(tmp_path / "cal", train, test, (lo, hi), est, spec_extra={"dataset": {"path": str(ds)}}, **blocks)
    _, html = _render(tmp_path, [tmp_path / "cal"])
    s = _section(html, "calibration")
    assert "2,000 of 4,000 events, train-window events (blocks [0] of 2" in s
    assert "matches the published profile CI" in s
    head = re.search(r'<header id="header">(.*?)</header>', html, re.S).group(1)
    assert "instruments.parquet" in head  # the file the report read is listed with its hash


# ---- calibration verdict ---------------------------------------------------------------------------------------------


def test_calibration_verdict_card(tmp_path: Path) -> None:
    rc.make_cal_dir(tmp_path / "ok")
    _, html = _render(tmp_path, [tmp_path / "ok"])
    s = _section(html, "calibration")
    assert "Exportable: the fit is identified and beats the default out of sample" in s
    assert "Held-out comparison with the library default" in s
    # worse than the default on held-out data, fitted at the grid edge: refused, with the reasons
    doc = json.loads((tmp_path / "ok" / "calibration.json").read_text())
    for ho in doc["held_out"].values():
        ho["brier"] = {k: v + 1.0 for k, v in ho["brier"].items()}
    doc["warnings"] = ["trust: at_grid_edge: the maximum is at the upper end of the grid (n = 54.6)"]
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "calibration.json").write_text(json.dumps(doc), encoding="utf-8")
    _, html = _render(tmp_path / "b", [bad])
    s = _section(html, "calibration")
    assert "Not exportable" in s and "at_grid_edge" in s and "not_beating_default" in s
    assert "Calibration warnings" not in s  # trust lines move into the verdict card
    assert "Verdict source: queuetruth.export.trust" in s
    # 1.1: the native `trust` field wins (here a refusal the warnings do not carry: own orders in the L2 depth)
    doc = json.loads((tmp_path / "ok" / "calibration.json").read_text())
    doc["trust"] = {
        "verdict": "refused",
        "reasons": ["own_orders_in_depth"],
        "flags": ["own_orders_in_depth", "plateau"],
        "beats_default": {str(h): "better" for h in rc.HORIZONS},
    }
    native = tmp_path / "native"
    native.mkdir()
    (native / "calibration.json").write_text(json.dumps(doc), encoding="utf-8")
    _, html = _render(tmp_path / "c", [native])
    s = _section(html, "calibration")
    assert "Not exportable" in s and "own_orders_in_depth" in s and "Other diagnostics: plateau" in s
    assert "Verdict source: trust field of the calibration result" in s


# ---- zoo leaderboard -------------------------------------------------------------------------------------------------


def test_e5_mcnemar_not_computable_and_sort_horizon_keyword(tmp_path: Path) -> None:
    rc.make_zoo_dir(tmp_path / "zoo", n=80)
    doc = rc.json_load(tmp_path / "zoo" / "summary.json")
    first = doc["leaderboard"][0]
    first["mcnemar"] = {"b": 0, "c": 0, "p_exact": None}  # nothing labelled at h_max
    (tmp_path / "zoo" / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    _, html = _render(tmp_path, [tmp_path / "zoo"])
    s = _section(html, "zoo")
    row = re.search(rf"<tr><td>{re.escape(rc.esc(first['model_id']))}</td>(.*?)</tr>", s).group(1)
    assert row.count("not computable") == 2 and "0 / 0" not in row
    # configurable sort horizon (default 60 s); the order follows |bias| at that horizon
    h = 10 * S
    _, html = _render(tmp_path / "b", [tmp_path / "zoo"], leaderboard_sort_horizon_ns=h)
    s = _section(html, "zoo")
    assert "sorted by absolute fill-rate bias at 10 s" in s
    want = min(
        doc["leaderboard"],
        key=lambda e: (abs(e["fill_rate_bias"][str(h)]["value"]), e["brier"][str(h)], e["model_id"]),
    )["model_id"]
    rows = re.findall(r"<tr><td>([^<]+)</td>", s.split("<tbody>", 1)[1])
    assert rows[0] == "exact fill rate (truth)" and rows[1] == rc.esc(want)
    with pytest.raises(QtError):
        _render(tmp_path / "c", [tmp_path / "zoo"], leaderboard_sort_horizon_ns=0)


# ---- learned model (F6) ----------------------------------------------------------------------------------------------


def test_learned_model_comparison_on_the_same_rows(tmp_path: Path) -> None:
    rc.make_hazard_dir(tmp_path / "hz")
    p = tmp_path / "hz" / "model.json"
    doc = json.loads(p.read_text())
    doc["zoo_comparison"][doc["model_id"]] = {"brier": {k: 0.05 for k in doc["zoo_comparison"][rc.MODELS[1]]["brier"]}}
    p.write_text(json.dumps(doc), encoding="utf-8")
    _, html = _render(tmp_path, [tmp_path / "hz"])
    s = _section(html, "learned_model")
    assert "best zoo: " + rc.esc(rc.MODELS[1]) in s  # the model's own entry is not a zoo model
    assert "best zoo: learned_hazard" not in s
    assert "scored against their exact never-cancel fill" in s
    assert "IPCW-weighted" in s
    # an older card (no own entry) is labelled as not directly comparable
    _, html = _render(tmp_path / "b", [_old_card(tmp_path)])
    assert "Older model card" in _section(html, "learned_model")


def _old_card(tmp_path: Path) -> Path:
    rc.make_hazard_dir(tmp_path / "hz_old")
    return tmp_path / "hz_old"


# ---- E10 labels ------------------------------------------------------------------------------------------------------


def test_e10_labels(tmp_path: Path) -> None:
    rc.make_score_dir(tmp_path / "score", n=200)
    _, html = _render(tmp_path, [tmp_path / "score"])
    assert "605-style, not NBBO" in _section(html, "markouts")
    s = _section(html, "two_by_two")
    assert "day-block bootstrap" not in s and "95% block-bootstrap CI (" in s


def test_e10_taker_walk_counts_fills_not_rows(tmp_path: Path) -> None:
    root = tmp_path / "replay"
    root.mkdir()
    cols: dict[str, list] = {k: [] for k in SCHEMAS["qt.shadow_fills"].names}

    def add(sid: str, seq: int, ts: int, qty: int, reason: str) -> None:
        for k, v in {
            "shadow_id": sid,
            "fill_seq": len([x for x in cols["shadow_id"] if x == sid]) + 1,
            "ts_event": ts,
            "sequence": seq,
            "fill_qty": qty,
            "cum_qty": qty,
            "leaves_qty": 0,
            "fill_px": 100,
            "reason": reason,
            "queue_ahead_before": 0,
            "traded_at_px_event": 0,
            "trade_through_event": 0,
            "behind_volume_executed": 0,
        }.items():
            cols[k].append(v)

    add("a", 10, rc.OPEN + S, 5, "queue_depleted")
    add("c", 30, rc.OPEN + 3 * S, 1, "crossed")  # a later cross is a maker fill (binding A5 refinement)
    for _lvl in range(3):  # one taker fill walking three price levels
        add("b", 20, rc.OPEN + 2 * S, 2, "crossed_at_live")
    rc.write(root, "shadow_fills.parquet", "qt.shadow_fills", cols)
    out = "".join(_shadow_liquidity(load_inputs([str(root)]).runs[0]))
    assert re.search(r"taker fills \(crossed at live\)</dt><dd>1 \(3 rows, one per price level walked\)", out)
    assert re.search(r"maker fills \(queue, trade-through, crossed\)</dt><dd>2</dd>", out)
    # a 1.1 `liquidity` column wins over the reason
    root2 = tmp_path / "replay2"
    root2.mkdir()
    full = FULL_SCHEMAS["qt.shadow_fills"]
    data = {**cols, "liquidity": ["maker"] + ["taker"] * 4}
    tab = pa.Table.from_arrays([pa.array(data[f.name]).cast(f.type) for f in full], schema=full)
    write_table(tab, root2 / "shadow_fills.parquet", full)
    out2 = "".join(_shadow_liquidity(load_inputs([str(root2)]).runs[0]))
    assert re.search(r"taker fills \(crossed at live\)</dt><dd>2 \(4 rows", out2)


# ---- why section: api.why_explain / api.why_suggestion ---------------------------------------------------------------


def _zoo_with_paths(tmp_path: Path, kind: str) -> tuple[Path, Path, Path]:
    zoo = tmp_path / "zoo"
    rc.make_zoo_dir(zoo, n=60)
    ds, sh = tmp_path / "ds", tmp_path / "shadows.parquet"
    ds.mkdir()
    (ds / "manifest.json").write_text(json.dumps({"kind": kind}), encoding="utf-8")
    sh.write_bytes(b"")
    doc = rc.json_load(zoo / "summary.json")
    doc["spec"]["dataset"] = {"path": str(ds)}
    doc["spec"]["shadows"] = {"path": str(sh)}
    (zoo / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    return zoo, ds, sh


def _trace(subject: str, **extra: Any) -> WhyTrace:
    notes = extra.pop("notes", [None] * 4)
    return WhyTrace(
        subject={"shadow_id": subject},
        instrument_id=1,
        side="B",
        price=1,
        qty=1,
        ts_live=rc.OPEN,
        outcome={"filled_qty": 0, "end_state": "cancelled"},
        events=[
            {
                "ts_event": rc.OPEN + k,
                "sequence": k,
                "action": "C",
                "order_id": k,
                "side": "B",
                "price": 1,
                "size": 1,
                "effect": "remove",
                "is_ahead": True,
                "queue_ahead_after": 5 - k,
                "rank_after": 5 - k,
                "level_qty_after": 9,
                "fill_qty": 0,
                "note": notes[k],
            }
            for k in range(4)
        ],
        truncated=False,
        **extra,
    )


def test_why_uses_why_explain_with_the_zoo_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    zoo, ds, sh = _zoo_with_paths(tmp_path, "mbo")
    calls: list[tuple] = []

    def fake_explain(spec: dict[str, Any], zoo_dir: str | None, model: str | None) -> Any:
        calls.append((spec, zoo_dir, model))
        tr = _trace(
            spec["shadow_id"],
            model_id=model,
            model_queue_ahead=[None, 4.0, 4.0, 1.5],
            first_divergence={"index": 2, "ts_event": rc.OPEN + 2},
            conclusion="The model kept 4 ahead while the exact queue shrank to 3.",
        )
        return SimpleNamespace(trace=tr, notes=["model: from the zoo run"])

    def no_why(spec: dict[str, Any]) -> None:
        raise AssertionError("api.why must not be used when why_explain answers")

    monkeypatch.setattr(why, "WHY_EXPLAIN_PROVIDER", fake_explain)
    monkeypatch.setattr(why, "WHY_PROVIDER", no_why)
    _, html = _render(tmp_path, [zoo], max_why=2)
    s = _section(html, "why")
    assert len(calls) == 2 and all(c[1] == str(zoo) and c[2] in rc.MODELS for c in calls)
    assert all(c[0]["dataset"] == {"path": str(ds)} and c[0]["shadows"] == {"path": str(sh)} for c in calls)
    assert "queuetruth.api.why_explain" in s
    assert "<strong>Conclusion:</strong> The model kept 4 ahead while the exact queue shrank to 3." in s
    assert "model queue ahead (" in s and "◀ first divergence" in s
    assert '<tr class="hl">' in s
    assert "model estimate (" in s  # the step chart gets the model series from the trace


def test_why_reads_v01_note_fields_and_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    zoo, _, _ = _zoo_with_paths(tmp_path, "mbo")
    notes = [None, "model_ahead=4", "model_ahead=4; first_divergence: m 4 vs exact 3", "model_ahead=1.5"]

    def explain_notes(spec: dict[str, Any], zoo_dir: str | None, model: str | None) -> Any:
        return SimpleNamespace(trace=_trace(spec["shadow_id"], notes=list(notes)), model_id="m")

    monkeypatch.setattr(why, "WHY_EXPLAIN_PROVIDER", explain_notes)
    monkeypatch.setattr(why, "WHY_PROVIDER", None)
    _, html = _render(tmp_path, [zoo], max_why=1)
    s = _section(html, "why")
    assert "model queue ahead (m)" in s and "◀ first divergence" in s
    assert "model_ahead=" not in s  # moved to its own column
    # why_explain unavailable -> the plain exact trace of api.why
    monkeypatch.setattr(why, "WHY_EXPLAIN_PROVIDER", lambda *a: None)
    monkeypatch.setattr(why, "WHY_PROVIDER", lambda spec: _trace(spec["shadow_id"]))
    _, html = _render(tmp_path / "b", [zoo], max_why=1)
    s = _section(html, "why")
    assert "exact engine (queuetruth why)" in s and "model queue ahead" not in s


def test_why_suggestion_never_suggests_why_on_l2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(why, "WHY_EXPLAIN_PROVIDER", None)
    monkeypatch.setattr(why, "WHY_PROVIDER", None)
    zoo, _, _ = _zoo_with_paths(tmp_path / "l2", "l2")
    _, html = _render(tmp_path / "l2", [zoo], max_why=1)
    s = _section(html, "why")
    assert "No event-level trace in the inputs" in s
    assert "--write-trajectory" in s and "queuetruth why --data" not in s
    zoo2, _, _ = _zoo_with_paths(tmp_path / "mbo", "mbo")
    _, html = _render(tmp_path / "mbo", [zoo2], max_why=1)
    assert "queuetruth why --data" in _section(html, "why")
