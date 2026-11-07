"""queuetruth.frames: backend selection, exact conversions, run directories and the flattened summaries."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import queuetruth as qt
from queuetruth import frames
from queuetruth.errors import QtError
from queuetruth.schemas import models as m

pd = pytest.importorskip("pandas")
try:
    import polars as pl
except ImportError:  # the pandas-only paths are still tested
    pl = None

needs_polars = pytest.mark.skipif(pl is None, reason="polars is not installed")
BOTH = ["pandas", pytest.param("polars", marks=needs_polars)]
BIG = 2**62 + 1  # not representable as float64
H1, H10 = "1000000000", "10000000000"


def rows(df: Any) -> list[dict[str, Any]]:
    """A frame of either backend as a list of plain dicts (pandas NA / NaT -> None)."""
    if pl is not None and isinstance(df, pl.DataFrame):
        return df.to_dicts()
    out = []
    for r in df.astype(object).to_dict("records"):
        out.append(
            {k: (None if v is pd.NA or v is pd.NaT or (isinstance(v, float) and v != v) else v) for k, v in r.items()}
        )
    return out


def sample_table() -> pa.Table:
    ts = pa.array([1_781_530_000_000_000_001, None, 5], pa.timestamp("ns", tz="UTC"))
    t = pa.table(
        {
            "big": pa.array([BIG, None, -1], pa.int64()),
            "u": pa.array([2**64 - 1, 0, None], pa.uint64()),
            "flag": pa.array([True, None, False]),
            "x": pa.array([0.5, None, 1.5], pa.float64()),
            "s": pa.array(["a", None, "c"]),
            "d": pa.array(["filled", "live", "filled"]).dictionary_encode(),
            "ts": ts,
        }
    )
    return t.replace_schema_metadata({b"qt.schema": b"qt.example", b"qt.schema_version": b"1"})


# ---- backends ---------------------------------------------------------------------------------------------------


def test_available_backends_in_order() -> None:
    assert frames.available_backends() == ["pandas"] + ([] if pl is None else ["polars"])


@needs_polars
def test_resolve_backend_explicit_env_and_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(frames.ENV_BACKEND, raising=False)
    assert frames.resolve_backend() == "pandas"
    assert frames.resolve_backend("auto") == "pandas"
    assert frames.resolve_backend(" Polars ") == "polars"
    monkeypatch.setenv(frames.ENV_BACKEND, "polars")
    assert frames.resolve_backend() == "polars"
    assert frames.resolve_backend("pandas") == "pandas"  # the argument wins over the environment


def test_resolve_backend_unknown_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(frames.ENV_BACKEND, raising=False)
    with pytest.raises(QtError) as e:
        frames.resolve_backend("arrow")
    assert e.value.code == "QT-CONFIG-001" and "backend" in e.value.message
    monkeypatch.setenv(frames.ENV_BACKEND, "duckdb")
    with pytest.raises(QtError) as e:
        frames.resolve_backend()
    assert e.value.code == "QT-CONFIG-001" and frames.ENV_BACKEND in e.value.message


@needs_polars
def test_auto_falls_back_to_polars_and_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(frames.ENV_BACKEND, raising=False)
    monkeypatch.setattr(frames, "_importable", lambda name: name == "polars")
    assert frames.resolve_backend() == "polars"
    assert isinstance(frames.to_frame(sample_table()), pl.DataFrame)
    with pytest.raises(QtError) as e:
        frames.resolve_backend("pandas")
    assert e.value.code == "QT-USAGE-001" and e.value.hint == "pip install pandas"
    monkeypatch.setattr(frames, "_importable", lambda name: False)
    with pytest.raises(QtError) as e:
        frames.to_frame(sample_table())
    assert e.value.code == "QT-USAGE-001" and "pandas or polars" in e.value.message
    assert "queuetruth[polars]" in (e.value.hint or "")


def test_importable_handles_broken_finders(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(name: str) -> None:
        raise ValueError(name)

    monkeypatch.setattr(frames.importlib.util, "find_spec", boom)
    assert frames.available_backends() == []


# ---- conversions ------------------------------------------------------------------------------------------------


def test_to_frame_pandas_is_exact() -> None:
    df = frames.to_frame(sample_table(), "pandas")
    assert isinstance(df, pd.DataFrame)
    assert (
        str(df["big"].dtype) == "Int64"
        and df["big"].iloc[0] == BIG
        and df["big"].isna().tolist() == [False, True, False]
    )
    assert str(df["u"].dtype) == "UInt64" and int(df["u"].iloc[0]) == 2**64 - 1
    assert str(df["flag"].dtype) == "boolean" and df["flag"].isna().iloc[1]
    assert str(df["d"].dtype) == "category" and df["d"].tolist() == ["filled", "live", "filled"]
    assert str(df["ts"].dtype) == "datetime64[ns, UTC]"
    assert df["ts"].iloc[0].value == 1_781_530_000_000_000_001 and df["ts"].isna().iloc[1]
    assert df.attrs["qt.schema"] == "qt.example"


@needs_polars
def test_to_frame_polars_is_exact() -> None:
    df = frames.to_frame(sample_table(), "polars")
    assert isinstance(df, pl.DataFrame)
    assert df["big"].to_list() == [BIG, None, -1] and df["big"].dtype == pl.Int64
    assert df["u"].to_list() == [2**64 - 1, 0, None] and df["u"].dtype == pl.UInt64
    assert df["ts"].dtype == pl.Datetime("ns", "UTC")
    assert df["d"].to_list() == ["filled", "live", "filled"]


def test_to_frame_without_schema_metadata_has_no_attr() -> None:
    df = frames.to_frame(pa.table({"a": [1]}), "pandas")
    assert "qt.schema" not in df.attrs


@pytest.mark.parametrize("backend", BOTH)
def test_to_frame_accepts_batches_and_row_dicts(backend: str) -> None:
    batch = pa.record_batch({"a": [1, 2]})
    assert rows(frames.to_frame(batch, backend)) == [{"a": 1}, {"a": 2}]
    assert rows(frames.to_frame([{"a": 1, "b": "x"}, {"a": None, "b": "y"}], backend)) == [
        {"a": 1, "b": "x"},
        {"a": None, "b": "y"},
    ]
    assert len(frames.to_frame([], backend)) == 0


@pytest.mark.parametrize("backend", BOTH)
def test_to_frame_row_dicts_take_the_union_of_keys(backend: str) -> None:
    """Columns come from every row, in first-seen order, not from the first row only."""
    assert rows(frames.to_frame([{"a": 1}, {"a": 2, "b": "x"}], backend)) == [{"a": 1, "b": None}, {"a": 2, "b": "x"}]
    assert rows(frames.to_frame([{"b": "x"}, {"a": 2}], backend)) == [{"b": "x", "a": None}, {"b": None, "a": 2}]
    with pytest.raises(QtError) as e:
        frames.to_frame([{"a": 1}, {"a": "x"}], backend)
    assert e.value.code == "QT-INPUT-002" and "incompatible" in e.value.message


def test_to_frame_pandas_nested_columns_keep_arrow_types() -> None:
    """List and struct columns become ArrowDtype columns, so an int64 inside a list with a null is not a float."""
    t = pa.table(
        {
            "xs": pa.array([[BIG, None], [], None], pa.list_(pa.int64())),
            "st": pa.array([{"k": BIG}, None, {"k": None}], pa.struct([("k", pa.int64())])),
            "f": pa.array([0.5, None, 2.0]),
        }
    )
    df = frames.to_frame(t, "pandas")
    assert isinstance(df["xs"].dtype, pd.ArrowDtype) and isinstance(df["st"].dtype, pd.ArrowDtype)
    assert df["xs"].iloc[0] == [BIG, None] and df["st"].iloc[0] == {"k": BIG}
    assert pa.Table.from_pandas(df, preserve_index=False).cast(t.schema).equals(t)
    # Documented: a float null reads as NaN on pandas.
    assert str(df["f"].dtype) == "float64" and df["f"].isna().tolist() == [False, True, False]


@pytest.mark.parametrize("bad", ["text", 3, [1, 2], {"a": 1}])
def test_to_frame_rejects_other_inputs(bad: Any) -> None:
    with pytest.raises(QtError) as e:
        frames.to_frame(bad, "pandas")
    assert e.value.code == "QT-INPUT-002"


# ---- flattened summaries (plain documents, no engine) ----------------------------------------------------------


def ci(v: float | None, lo: float | None = None, hi: float | None = None) -> dict[str, Any]:
    return {"value": v, "ci_lo": lo, "ci_hi": hi}


def zoo_doc(leaderboard: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entry = {
        "model_id": "nt_prob_touch[mode=faithful;p=1]",
        "brier": {H10: 0.2, H1: 0.1},
        "logloss": {H1: 0.3, H10: 0.6},
        "fill_rate_bias": {H10: ci(0.03, 0.02, 0.04), H1: ci(0.0313, 0.0253, 0.0377)},
        "overfill_bias": ci(0.05, 0.04, 0.06),
        "qty_bias": 1.5,
        "ttf_iad": None,
        "mcnemar": {"b": 12, "c": 0, "p_exact": 0.0005},
        "exact_fill_rate": {H1: 0.1103, H10: 0.48},
        "model_fill_rate": {H1: 0.1417, H10: 0.51},
    }
    second = {
        **entry,
        "model_id": "trade_through_only[mode=faithful]",
        "fill_rate_bias": {H1: ci(None)},
        "brier": {H1: None},
        "logloss": {},
        "exact_fill_rate": {},
        "model_fill_rate": {},
        "overfill_bias": ci(None),
        "qty_bias": None,
        "mcnemar": {"b": 0, "c": 3, "p_exact": None},
    }
    return {
        "schema": "qt.zoo_summary",
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "spec": {},
        "n_shadows": 10,
        "models": [entry["model_id"], second["model_id"]],
        "leaderboard": [entry, second] if leaderboard is None else leaderboard,
        "files": [],
        "warnings": [],
    }


@pytest.mark.parametrize("backend", BOTH)
def test_leaderboard_long_rows(backend: str) -> None:
    got = rows(frames.leaderboard(zoo_doc(), backend))
    assert [(r["model_id"][:8], r["horizon_ns"]) for r in got] == [
        ("nt_prob_", 1_000_000_000),
        ("nt_prob_", 10_000_000_000),
        ("trade_th", 1_000_000_000),
    ]
    first = got[0]
    assert first["fill_rate_bias"] == 0.0313 and (first["ci_lo"], first["ci_hi"]) == (0.0253, 0.0377)
    assert (first["exact_fill_rate"], first["model_fill_rate"], first["brier"], first["logloss"]) == (
        0.1103,
        0.1417,
        0.1,
        0.3,
    )
    assert (first["overfill_bias"], first["qty_bias"], first["mcnemar_b"], first["mcnemar_c"]) == (0.05, 1.5, 12, 0)
    last = got[2]
    assert last["fill_rate_bias"] is None and last["brier"] is None and last["logloss"] is None
    assert last["mcnemar_p_exact"] is None and last["mcnemar_c"] == 3
    assert list(rows(frames.leaderboard(zoo_doc(), backend))[0]) == frames.LEADERBOARD_SCHEMA.names
    # The scoring population: null / 0 when the summary predates the fields, else repeated on every row.
    assert {(r["n_leaderboard"], r["n_excluded_hbt_312"]) for r in got} == {(None, 0)}
    doc = {**zoo_doc(), "n_leaderboard": 8, "n_excluded_hbt_312": 2}
    assert {(r["n_leaderboard"], r["n_excluded_hbt_312"]) for r in rows(frames.leaderboard(doc, backend))} == {(8, 2)}


@pytest.mark.parametrize("backend", BOTH)
def test_leaderboard_empty_keeps_columns(backend: str) -> None:
    df = frames.leaderboard(zoo_doc(leaderboard=[]), backend)
    assert len(df) == 0 and list(df.columns) == frames.LEADERBOARD_SCHEMA.names


def test_leaderboard_accepts_model_and_summary_path(tmp_path: Path) -> None:
    doc = zoo_doc()
    model = m.ZooSummary.model_validate(doc)
    (tmp_path / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    want = rows(frames.leaderboard(doc, "pandas"))
    assert rows(frames.leaderboard(model, "pandas")) == want
    assert rows(frames.leaderboard(tmp_path / "summary.json", "pandas")) == want
    assert rows(frames.leaderboard(str(tmp_path), "pandas")) == want


def test_flatteners_reject_the_wrong_document(tmp_path: Path) -> None:
    for call in (frames.held_out, frames.scorecards):
        with pytest.raises(QtError) as e:
            call(zoo_doc(), "pandas")
        assert e.value.code == "QT-INPUT-002" and "qt.zoo_summary" in e.value.message
    with pytest.raises(QtError) as e:
        frames.leaderboard(42, "pandas")
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        frames.leaderboard(tmp_path / "nope.json", "pandas")
    assert e.value.code == "QT-INPUT-001"
    (tmp_path / "x.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(QtError) as e:
        frames.leaderboard(tmp_path / "x.json", "pandas")
    assert e.value.code == "QT-INPUT-002" and "not a JSON document" in e.value.message
    (tmp_path / "y.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    with pytest.raises(QtError) as e:
        frames.leaderboard(tmp_path / "y.json", "pandas")
    assert e.value.code == "QT-INPUT-002" and "not a queuetruth document" in e.value.message


def cal_doc(trust: bool = True) -> dict[str, Any]:
    held = {"brier": {H1: 0.1, H10: 0.2}, "logloss": {H1: 0.3, H10: 0.4}, "fill_rate_bias": {H1: -0.01, H10: None}}
    base = {"brier": {H1: 0.11}, "logloss": {H1: 0.31}, "fill_rate_bias": {H1: 0.02}}
    return {
        "schema": "qt.calibration_result",
        "schema_version": 1,
        "held_out": {"hbt_power3[n=0.5]": held},
        "baseline_held_out": {"hbt_power3[n=2]": base},
        "trust": {"verdict": "exportable", "reasons": [], "flags": [], "beats_default": {H1: "better", H10: "tie"}}
        if trust
        else None,
    }


@pytest.mark.parametrize("backend", BOTH)
def test_held_out_rows_and_trust(backend: str) -> None:
    got = rows(frames.held_out(cal_doc(), backend))
    assert [(r["arm"], r["model_id"], r["horizon_ns"], r["beats_default"]) for r in got] == [
        ("fitted", "hbt_power3[n=0.5]", 1_000_000_000, "better"),
        ("fitted", "hbt_power3[n=0.5]", 10_000_000_000, "tie"),
        ("default", "hbt_power3[n=2]", 1_000_000_000, "better"),
    ]
    assert (got[0]["brier"], got[0]["logloss"], got[0]["fill_rate_bias"]) == (0.1, 0.3, -0.01)
    assert got[1]["fill_rate_bias"] is None
    assert all(r["beats_default"] is None for r in rows(frames.held_out(cal_doc(trust=False), backend)))


def score_doc(kind: str, model_id: str | None, live_only: int, sim_only: int, dc: bool = True) -> dict[str, Any]:
    return {
        "schema": "qt.scorecard_summary",
        "schema_version": 1,
        "run_id": f"{live_only:016x}",
        "sim_source": {"kind": kind, "model_id": model_id, "path": None},
        "n_orders": 200,
        "n_matched": 184,
        "two_by_two": {"both": 100, "neither": 50, "live_only": live_only, "sim_only": sim_only},
        "bias": {"fill_rate_bias": 0.1, "ci_lo": 0.05, "ci_hi": 0.15, "qty_bias": 2.0, "direction": "sim_overfills"},
        "mcnemar": {"b": live_only, "c": sim_only, "chi2_cc": 1.0, "p_chi2": 0.3, "p_exact": 0.25},
        "discordance_clustered": {"p_value": 0.01} if dc else None,
        "verdict": {"headline": "sim overfills", "signed_bias": 0.1, "gates_passed": False, "failed_gates": ["x"]},
    }


@pytest.mark.parametrize("backend", BOTH)
def test_scorecards_mapping_sequence_and_single(backend: str) -> None:
    a = score_doc("external", None, 1, 20)
    b = score_doc("zoo", "hbt_power3[n=2]", 3, 4, dc=False)
    got = rows(frames.scorecards({"naive": a, "power3": b}, backend))
    assert [r["label"] for r in got] == ["naive", "power3"]
    assert (got[0]["both"], got[0]["neither"], got[0]["live_only"], got[0]["sim_only"]) == (100, 50, 1, 20)
    assert got[0]["discordance_p"] == 0.01 and got[1]["discordance_p"] is None
    assert got[0]["gates_passed"] is False and got[0]["direction"] == "sim_overfills"
    assert [r["label"] for r in rows(frames.scorecards([a, b], backend))] == ["external", "hbt_power3[n=2]"]
    single = rows(frames.scorecards(a, backend))
    assert len(single) == 1 and single[0]["run_id"] == a["run_id"]
    with pytest.raises(QtError) as e:
        frames.scorecards(7, backend)
    assert e.value.code == "QT-INPUT-002"


@pytest.mark.parametrize("backend", BOTH)
def test_records_flattens_nested_objects(backend: str) -> None:
    doc = {
        "schema": "qt.x",
        "items": [{"a": 1, "b": {"value": 0.5, "ci": {"lo": 0.1}}}, {"a": 2, "b": {"value": None}}],
    }
    got = rows(frames.records(doc, "items", backend))
    assert got[0] == {"a": 1, "b.value": 0.5, "b.ci.lo": 0.1}
    assert got[1]["a"] == 2 and got[1]["b.value"] is None and got[1]["b.ci.lo"] is None
    assert len(frames.records({"schema": "qt.x", "items": []}, "items", backend)) == 0


@pytest.mark.parametrize("backend", BOTH)
@pytest.mark.parametrize("reverse", [False, True])
def test_records_columns_are_the_union_over_rows(backend: str, reverse: bool) -> None:
    """A field that only a later row has, or a nested object that is null in the first row, is never dropped."""
    items: list[dict[str, Any]] = [
        {"a": 2, "b": {"value": None}},
        {"a": 1, "b": {"value": 0.5, "ci": {"lo": 0.1}}},
        {"a": 3, "b": None, "c": 7},
    ]
    if reverse:
        items = items[::-1]
    got = {r["a"]: r for r in rows(frames.records({"schema": "qt.x", "items": items}, "items", backend))}
    assert got[1] == {"a": 1, "b.value": 0.5, "b.ci.lo": 0.1, "c": None}
    assert got[2] == {"a": 2, "b.value": None, "b.ci.lo": None, "c": None}
    assert got[3] == {"a": 3, "b.value": None, "b.ci.lo": None, "c": 7}
    order = ["a", "c", "b.value", "b.ci.lo"] if reverse else ["a", "b.value", "b.ci.lo", "c"]
    assert list(got[1]) == order  # first-seen order over all rows


def test_records_rejects_conflicting_shapes() -> None:
    for items, text in (
        ([{"b": 1}, {"b": {"value": 2}}], "object in one row and a value"),
        ([{"b": {"value": 2}}, {"b": [1]}], "object in one row and a value"),
        ([{"a.b": 1}, {"a": {"b": 2}}], "same column"),
        ([{"a": 1}, {"a": "x"}], "incompatible"),
    ):
        with pytest.raises(QtError) as e:
            frames.records({"schema": "qt.x", "items": items}, "items", "pandas")
        assert e.value.code == "QT-INPUT-002" and text in e.value.message, items


def test_records_errors(tmp_path: Path) -> None:
    for doc, field, text in (
        ({"schema": "qt.x"}, "items", "absent"),
        ({"schema": "qt.x", "items": None}, "items", "NoneType"),
        ({"schema": "qt.x", "items": [1]}, "items", "non-object"),
    ):
        with pytest.raises(QtError) as e:
            frames.records(doc, field, "pandas")
        assert e.value.code == "QT-INPUT-002" and text in e.value.message
    with pytest.raises(QtError) as e:
        frames.records(tmp_path / "missing", "items", "pandas")
    assert e.value.code == "QT-INPUT-001"
    with pytest.raises(QtError) as e:
        frames.records(3.5, "items", "pandas")
    assert e.value.code == "QT-INPUT-002"


# ---- run directories (real runs on the synthetic fixture; needs the compiled extension) ------------------------


@pytest.fixture(scope="module")
def runs(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    pytest.importorskip("queuetruth._core")
    synth = repo_root / "tests" / "fixtures" / "synth_small"
    if not (synth / "manifest.json").exists():
        pytest.fail("tests/fixtures/synth_small is missing (run `make fixtures`)")
    base = tmp_path_factory.mktemp("frames")
    shadows = base / "shadows.parquet"
    qt.sample_shadows({"dataset": {"path": str(synth)}, "out": str(shadows), "n": 120, "seed": 3})
    ds = {"path": str(synth)}
    zoo = qt.run_zoo(
        {
            "dataset": ds,
            "shadows": {"path": str(shadows)},
            "out": str(base / "zoo"),
            "models": ["hbt_power3[n=2]", "front_of_queue"],
            "bootstrap_b": 50,
        }
    )
    cal = qt.calibrate(
        {
            "kind": "alloc",
            "family": "power3",
            "dataset": ds,
            "shadows": {"path": str(shadows)},
            "out": str(base / "cal"),
            "train": {"dates": ["2026-06-15"]},
            "test": {"dates": ["2026-06-16"]},
            "bootstrap": {"b": 20},
        }
    )
    score = qt.api.score(
        {
            "live": {"path": str(synth / "own_orders" / "live")},
            "sim": {"kind": "exact"},
            "dataset": ds,
            "out": str(base / "score"),
            "bootstrap": {"b": 50},
            "perm_b": 50,
        }
    )
    return {"base": base, "shadows": shadows, "zoo": zoo, "cal": cal, "score": score}


def copy_run(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


@pytest.mark.requires_core
@pytest.mark.parametrize("backend", BOTH)
def test_load_run_zoo(runs: dict[str, Any], backend: str) -> None:
    run = frames.load_run(runs["base"] / "zoo", backend=backend, verify=True)
    assert run.schema == "qt.zoo_summary" and run.run_id == runs["zoo"].run_id and run.backend == backend
    assert list(run) == ["zoo_errors", "zoo_fills", "zoo_predictions", "zoo_shadows", "zoo_trajectory"]
    assert len(run) == 5 and "zoo_errors" in run and run.summary_path.name == "summary.json"
    assert set(run.files) == {f.name for f in runs["zoo"].files}
    errors = run["zoo_errors"]
    assert run["zoo_errors"] is errors  # cached
    assert len(errors) == pq.read_metadata(run.file_path("zoo_errors")).num_rows
    assert run.table("zoo_errors").schema.metadata[b"qt.schema"] == b"qt.zoo_errors"
    assert isinstance(run.model(), m.ZooSummary)
    assert run.verify() == len(runs["zoo"].files)
    assert "qt.zoo_summary" in repr(run) and runs["zoo"].run_id in repr(run)
    # The flattened leaderboard is the same from the model, the RunFrames and the directory.
    want = rows(frames.leaderboard(runs["zoo"], backend))
    assert want and rows(frames.leaderboard(run, backend)) == want
    assert rows(frames.leaderboard(runs["base"] / "zoo", backend)) == want
    models = {r["model_id"] for r in want}
    assert models == set(runs["zoo"].models)
    for e in runs["zoo"].leaderboard:
        mine = [r for r in want if r["model_id"] == e.model_id]
        assert [r["horizon_ns"] for r in mine] == sorted(int(h) for h in e.fill_rate_bias)
        for r in mine:
            assert r["fill_rate_bias"] == e.fill_rate_bias[str(r["horizon_ns"])].value


@needs_polars
@pytest.mark.requires_core
@pytest.mark.parametrize("name", ["zoo_shadows", "zoo_predictions", "zoo_fills"])
def test_both_backends_round_trip_a_run_table_exactly(runs: dict[str, Any], name: str) -> None:
    orig = frames.load_run(runs["base"] / "zoo").table(name)
    a = frames.load_run(runs["base"] / "zoo", backend="pandas")[name]
    b = frames.load_run(runs["base"] / "zoo", backend="polars")[name]
    assert list(a.columns) == b.columns == orig.column_names
    back_a = pa.Table.from_pandas(a, preserve_index=False).cast(orig.schema)
    back_b = b.to_arrow().cast(orig.schema)
    assert back_a.equals(orig) and back_b.equals(orig)


@pytest.mark.requires_core
def test_load_run_calibration_finds_nested_tables(runs: dict[str, Any]) -> None:
    run = frames.load_run(runs["base"] / "cal")
    assert run.schema == "qt.calibration_result" and run.summary_path.name == "calibration.json"
    # calibration.json lists no files; the nested held-out zoo summary does, and those are verified.
    nested = json.loads((runs["base"] / "cal" / "held_out" / "summary.json").read_text(encoding="utf-8"))["files"]
    assert set(run.files) == {f"held_out/{f['name']}" for f in nested} and nested
    assert "alloc_events.parquet" in run.unlisted and "touch_records.parquet" in run.unlisted
    with pytest.warns(UserWarning, match="not verified"):
        assert run.verify() == len(nested)
    assert {"alloc_events", "touch_records", "held_out/zoo_predictions"} <= set(run)
    assert len(run["held_out/zoo_predictions"]) > 0
    got = rows(frames.held_out(run))
    assert {r["arm"] for r in got} == {"fitted", "default"}
    assert rows(frames.held_out(runs["cal"])) == got
    beats = runs["cal"].trust.beats_default if runs["cal"].trust else {}
    for r in got:
        assert r["beats_default"] == beats.get(str(r["horizon_ns"]))


@pytest.mark.requires_core
@pytest.mark.parametrize("backend", BOTH)
def test_scorecard_run_and_records(runs: dict[str, Any], backend: str) -> None:
    run = frames.load_run(runs["base"] / "score", backend=backend, verify=True)
    assert run.schema == "qt.scorecard_summary" and "score_orders" in run
    s = runs["score"]
    got = rows(frames.scorecards({"exact": run}))
    assert got[0]["label"] == "exact" and got[0]["n_orders"] == s.n_orders
    assert (got[0]["live_only"], got[0]["sim_only"]) == (s.two_by_two.live_only, s.two_by_two.sim_only)
    assert got[0]["headline"] == s.verdict.headline
    mk = rows(frames.records(run, "markouts", backend))
    assert len(mk) == len(s.markouts) and mk[0]["horizon_ns"] == s.markouts[0].horizon_ns
    assert len(frames.records(s, "markouts", backend)) == len(s.markouts)
    assert len(frames.records(runs["base"] / "score", "markouts", backend)) == len(s.markouts)


@pytest.mark.requires_core
def test_load_run_from_summary_file(runs: dict[str, Any]) -> None:
    run = frames.load_run(runs["base"] / "zoo" / "summary.json")
    assert run.path == runs["base"] / "zoo" and run.run_id == runs["zoo"].run_id


@pytest.mark.requires_core
def test_verify_catches_tampering(runs: dict[str, Any], tmp_path: Path) -> None:
    d = copy_run(runs["base"] / "zoo", tmp_path / "zoo")
    entry = next(f for f in runs["zoo"].files if f.name == "zoo_fills.parquet")
    # Same size, different bytes, same table (one letter of the footer's `created_by`): only the sha256 can catch it.
    p = d / entry.name
    data = bytearray(p.read_bytes())
    data[data.rindex(b"queuetruth ")] ^= 0x20
    p.write_bytes(bytes(data))
    with pytest.raises(QtError) as e:
        frames.load_run(d, verify=True)
    assert e.value.code == "QT-INPUT-002" and "zoo_fills.parquet: sha256" in e.value.message
    run = frames.load_run(d)  # without verify the directory still opens
    assert "zoo_fills" in run
    # A different size is reported as such.
    p.write_bytes(bytes(data) + b"\0")
    with pytest.raises(QtError) as e:
        run.verify()
    assert "bytes, summary records" in e.value.message


@pytest.mark.requires_core
def test_verify_catches_row_count(runs: dict[str, Any], tmp_path: Path) -> None:
    d = copy_run(runs["base"] / "zoo", tmp_path / "zoo")
    doc = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    for f in doc["files"]:
        if f["name"] == "zoo_errors.parquet":
            f["rows"] += 1
    (d / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(QtError) as e:
        frames.load_run(d, verify=True)
    assert e.value.code == "QT-INPUT-002" and "zoo_errors.parquet:" in e.value.message and "rows" in e.value.message


@pytest.mark.requires_core
def test_load_run_failures(runs: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        frames.load_run(tmp_path / "missing")
    assert e.value.code == "QT-INPUT-001"
    (tmp_path / "empty").mkdir()
    with pytest.raises(QtError) as e:
        frames.load_run(tmp_path / "empty")
    assert e.value.code == "QT-INPUT-002" and "no run summary" in e.value.message
    with pytest.raises(QtError) as e:
        frames.load_run(runs["shadows"])
    assert e.value.code == "QT-INPUT-002" and "frames.read" in (e.value.hint or "")
    d = copy_run(runs["base"] / "zoo", tmp_path / "zoo")
    (d / "zoo_trajectory.parquet").unlink()
    with pytest.raises(QtError) as e:
        frames.load_run(d)
    assert e.value.code == "QT-INPUT-001" and "zoo_trajectory.parquet" in e.value.message
    run = frames.load_run(runs["base"] / "zoo")
    for call in (lambda: run["nope"], lambda: run.table("nope"), lambda: run.file_path("nope")):
        with pytest.raises(KeyError, match="nope"):
            call()


@pytest.mark.requires_core
def test_load_run_ignores_and_reports_stray_parquet(runs: dict[str, Any], tmp_path: Path) -> None:
    d = copy_run(runs["base"] / "zoo", tmp_path / "zoo")
    shutil.copy(d / "zoo_fills.parquet", d / "zoo_fills_old.parquet")
    run = frames.load_run(d)
    assert "zoo_fills_old" not in run and run.unlisted == ["zoo_fills_old.parquet"]
    with pytest.warns(UserWarning, match="ignored"):
        assert run.verify() == len(runs["zoo"].files)


@pytest.mark.requires_core
@pytest.mark.parametrize("name", ["../outside.parquet", "/tmp/abs.parquet", "a/../../x.parquet", "C:\\x.parquet", ""])
def test_load_run_rejects_names_outside_the_run(runs: dict[str, Any], tmp_path: Path, name: str) -> None:
    d = copy_run(runs["base"] / "zoo", tmp_path / "zoo")
    pq.write_table(pa.table({"a": [1]}), tmp_path / "outside.parquet")
    doc = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    doc["files"].append({"name": name, "bytes": 1, "sha256": "0" * 64})
    (d / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(QtError) as e:
        frames.load_run(d)
    assert e.value.code == "QT-INPUT-002" and "run directory" in e.value.message


@pytest.mark.requires_core
def test_verify_after_a_file_disappears_and_without_a_listing(runs: dict[str, Any], tmp_path: Path) -> None:
    d = copy_run(runs["base"] / "zoo", tmp_path / "zoo")
    run = frames.load_run(d)
    (d / "zoo_fills.parquet").unlink()
    with pytest.raises(QtError) as e:
        run.verify()
    assert e.value.code == "QT-INPUT-001" and "zoo_fills.parquet" in e.value.message
    bare = copy_run(runs["base"] / "zoo", tmp_path / "bare")
    doc = json.loads((bare / "summary.json").read_text(encoding="utf-8"))
    doc["files"] = []
    (bare / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.warns(UserWarning) as caught:
        assert frames.load_run(bare, verify=True).verify() == 0
    assert any("nothing was verified" in str(w.message) for w in caught)


@pytest.mark.requires_core
def test_model_rejects_an_invalid_summary(runs: dict[str, Any], tmp_path: Path) -> None:
    d = copy_run(runs["base"] / "zoo", tmp_path / "zoo")
    doc = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    doc["run_id"] = "not-a-run-id"
    (d / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    run = frames.load_run(d)
    assert run.run_id == "not-a-run-id"
    with pytest.raises(QtError) as e:
        run.model()
    assert e.value.code == "QT-INPUT-002"


@pytest.mark.requires_core
@pytest.mark.parametrize("backend", BOTH)
def test_read_one_table(runs: dict[str, Any], backend: str) -> None:
    df = frames.read(runs["shadows"], expect="qt.shadow_spec", backend=backend)
    assert len(df) == 120
    sub = frames.read(runs["shadows"], columns=["qty", "shadow_id"], backend=backend)
    assert list(sub.columns) == ["qty", "shadow_id"]
    with pytest.raises(QtError) as e:
        frames.read(runs["shadows"], columns=["nope"], backend=backend)
    assert e.value.code == "QT-INPUT-002" and "nope" in e.value.message
    with pytest.raises(QtError) as e:
        frames.read(runs["shadows"], expect="qt.zoo_errors", backend=backend)
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        frames.read(runs["base"] / "missing.parquet", backend=backend)
    assert e.value.code == "QT-INPUT-001"


@pytest.mark.requires_core
@pytest.mark.parametrize("backend", BOTH)
def test_read_pushes_columns_and_filters_down(runs: dict[str, Any], backend: str, tmp_path: Path) -> None:
    full = pq.read_table(runs["shadows"])
    iid = full["instrument_id"][0].as_py()
    want = full.filter(pa.compute.equal(full["instrument_id"], iid)).select(["shadow_id", "qty"])
    got = frames.read(
        runs["shadows"], columns=["shadow_id", "qty"], filters=[("instrument_id", "==", iid)], backend=backend
    )
    assert rows(got) == want.to_pylist() and 0 < len(got) < full.num_rows
    if backend == "pandas":
        assert got.attrs["qt.schema"] == "qt.shadow_spec"
    run = frames.load_run(runs["base"] / "zoo", backend=backend)
    part = run.read("zoo_shadows", columns=["shadow_id"])
    assert list(part.columns) == ["shadow_id"] and len(part) == len(run["zoo_shadows"])
    for bad in (
        {"columns": ["nope"]},
        {"filters": [("nope", "==", 1)]},
        {"expect": "qt.zoo_errors", "columns": ["qty"]},
    ):
        with pytest.raises(QtError) as e:
            frames.read(runs["shadows"], backend=backend, **bad)
        assert e.value.code == "QT-INPUT-002", bad
    plain = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"a": [1]}), plain)
    with pytest.raises(QtError) as e:
        frames.read(plain, columns=["a"], backend=backend)
    assert e.value.code == "QT-INPUT-002" and "qt.schema" in e.value.message


# ---- the example notebooks built on these helpers -----------------------------------------------------------------


def _load_build(repo_root: Path) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("qt_notebooks_build", repo_root / "examples/notebooks/build.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_notebook_source_parser(repo_root: Path, tmp_path: Path) -> None:
    build = _load_build(repo_root)
    src = tmp_path / "99_x.py"
    src.write_text("# %% [markdown]\n# # Title\n#\n# text\n\n# %%\nx = 1\n\n\n# %%\n\n# %% [markdown]\n#\n", "utf-8")
    assert build.parse(src) == [("markdown", "# Title\n\ntext"), ("code", "x = 1")]
    src.write_text("x = 1\n# %%\ny = 2\n", "utf-8")
    with pytest.raises(SystemExit, match="before the first"):
        build.parse(src)
    src.write_text("# %% [markdown]\nnot a comment\n", "utf-8")
    with pytest.raises(SystemExit, match="without '#'"):
        build.parse(src)


def _tiny_notebook(outputs: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    nb = {
        "cells": [
            {"cell_type": "markdown", "id": "t-00", "metadata": {}, "source": "# t"},
            {"cell_type": "code", "id": "t-01", "metadata": {}, "source": "x = 1", "outputs": outputs},
        ]
    }
    return nb, json.dumps(nb, indent=1)


def test_notebook_check_rejects_paths_errors_and_size(repo_root: Path) -> None:
    build = _load_build(repo_root)
    ok, text = _tiny_notebook([{"output_type": "stream", "name": "stdout", "text": "qt-notebooks/01/zoo\n"}])
    assert build.check(ok, text, "ok", ["/work/dir", ""], 200) == len(text.encode("utf-8"))
    leak = _tiny_notebook([{"output_type": "stream", "name": "stdout", "text": "wrote /work/dir/zoo\n"}])
    with pytest.raises(SystemExit, match="local path '/work/dir'"):
        build.check(*leak, "leak", ["/work/dir"], 200)
    err = _tiny_notebook([{"output_type": "error", "ename": "ValueError", "evalue": "boom", "traceback": []}])
    with pytest.raises(SystemExit, match="raised ValueError: boom"):
        build.check(*err, "err", [], 200)
    big = _tiny_notebook([{"output_type": "stream", "name": "stdout", "text": "x" * 3000}])
    with pytest.raises(SystemExit, match="above --max-kb 2"):
        build.check(*big, "big", [], 2)


def test_notebook_build_needs_an_explicit_mode(repo_root: Path) -> None:
    """A plain `build.py` must not silently replace the stored executed notebooks with empty ones."""
    build = _load_build(repo_root)
    with pytest.raises(SystemExit) as e:
        build.main([])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        build.main(["--execute", "--no-outputs"])
    assert e.value.code == 2


def test_stored_notebooks_match_their_sources(repo_root: Path) -> None:
    """Every stored notebook is the build of its source (rebuild with build.py after editing a source), executed
    without errors, small, with the Colab badge and the setup cell, and with no local path in any output."""
    build = _load_build(repo_root)
    nb_dir = repo_root / "examples" / "notebooks"
    sources = sorted((nb_dir / "src").glob("[0-9][0-9]_*.py"))
    assert [s.stem for s in sources] == [p.stem for p in sorted(nb_dir.glob("[0-9][0-9]_*.ipynb"))]
    assert len(sources) == 4
    for src in sources:
        path = nb_dir / f"{src.stem}.ipynb"
        assert path.stat().st_size < 200 * 1024, path.name
        nb = json.loads(path.read_text(encoding="utf-8"))
        cells = [("".join(c["source"]), c["cell_type"]) for c in nb["cells"]]
        parsed = build.parse(src)
        title, _, rest = parsed[0][1].partition("\n")
        badge = build.BADGE.format(repo=build.REPO_URL, name=path.name)
        assert cells[0] == (f"{title}\n\n{badge}\n\n{rest.strip()}".rstrip(), "markdown"), path.name
        assert cells[1] == (build.SETUP, "code"), path.name
        assert cells[2:] == [(text, kind) for kind, text in parsed[1:]], f"{path.name} is stale: run build.py"
        code = [c for c in nb["cells"] if c["cell_type"] == "code"]
        assert all(c["execution_count"] is not None for c in code), f"{path.name} is not executed"
        for c in code:
            for out in c.get("outputs", []):
                assert out["output_type"] != "error", path.name
                assert "/Users/" not in json.dumps(out) and "/home/" not in json.dumps(out), path.name
