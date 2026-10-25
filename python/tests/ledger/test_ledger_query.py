"""Queries: filters, trial counts, variants, statistics, selection inputs, exports and document schemas."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator

from queuetruth import ledger as L
from queuetruth.errors import QtError
from queuetruth.ledger import _query as q
from queuetruth.ledger.models import DOCUMENTS, LedgerFilter


def _validate(doc: dict) -> None:
    schema = L.json_schemas()[doc["schema"]]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(doc)


@pytest.fixture
def grid(ledger_dir: Path) -> dict[str, str]:
    """A study `mom` of 3 lookbacks (one registered twice), a study `mr` of 2, a variant without a series and a
    failed run; returns name -> trial_id."""
    ids = {}
    with L.study("mom"):
        for lb, rets in ((10, [0.01, -0.01, 0.02]), (20, [0.0, 0.01, 0.01]), (40, [-0.02, 0.03, 0.0])):
            r = L.register_variant("mom", {"lb": lb}, returns=rets, periods_per_year=252, dataset="es-2024")
            ids[f"mom{lb}"] = r.trial_id
        again = L.register_variant(
            "mom", {"lb": 20}, returns=[0.0, 0.01, 0.01], periods_per_year=252, dataset="es-2024"
        )
        assert again.trial_id == ids["mom20"]
    for z in (1.5, 2.0):
        r = L.register_variant(
            "mr", {"z": z}, scores=[0.2, 0.25], higher_is_better=False, study="mr", run_ids=["a" * 16]
        )
        ids[f"mr{z}"] = r.trial_id
    ids["bare"] = L.register_variant("bare", {"k": 1}, study="mom").trial_id
    L.record("zoo", spec={"dataset": {"path": "missing"}, "shadows": {"path": "x.parquet"}},
             error=QtError("QT-INPUT-001", "missing/manifest.json: not found"), study="mom")  # fmt: skip
    return ids


def test_n_trials_counts_distinct_trials(grid: dict[str, str]) -> None:
    assert L.n_trials() == 6
    assert L.n_trials(study="mom") == 4  # 3 lookbacks (one registered twice) + bare; the failed run is no trial
    assert L.n_trials({"study": "m*"}) == 6
    assert L.n_trials(study="mr") == 2
    assert L.n_trials(kinds=["zoo"]) == 0
    assert L.n_trials(name="mom") == 3
    assert L.n_trials(dataset="es-") == 3
    assert L.n_trials(run_id="aaaa") == 2
    assert L.n_trials(status="error") == 0
    assert L.n_trials(LedgerFilter(study="mom"), name="bare") == 1


def test_code_version_and_dataset_make_new_trials(ledger_dir: Path) -> None:
    a = L.register_variant("v", {"x": 1})
    b = L.register_variant("v", {"x": 1}, code_version="abc123")
    c = L.register_variant("v", {"x": 1}, dataset="other")
    d = L.register_variant("v", {"x": 1.0})  # 1 and 1.0 are the same JSON number
    assert len({a.trial_id, b.trial_id, c.trial_id}) == 3 and d.trial_id == a.trial_id
    assert a.spec_hash == b.spec_hash == c.spec_hash  # one configuration
    assert L.n_trials() == 3 and L.stats().n_configs == 1


def test_list_records_filters_and_limit(grid: dict[str, str]) -> None:
    every = L.list_records()
    assert [r.seq for r in every] == list(range(1, 9)) and every[-1].status == "error"
    assert [r.seq for r in L.list_records(limit=2)] == [7, 8]
    assert [r.status for r in L.list_records(status="error")] == ["error"]
    first, last = every[0].recorded_at_ns, every[-1].recorded_at_ns
    assert [r.seq for r in L.list_records(since_ns=last)] == [8]
    assert len(L.list_records(until_ns=first + 1)) == 1
    assert L.list_records(spec_hash=every[0].spec_hash[:6])[0].name == "mom"
    for bad in ({"limit": -1}, {"limit": True}):
        with pytest.raises(QtError, match="QT-CONFIG-001"):
            L.list_records(**bad)
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        L.list_records(kinds=["nope"])
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        L.list_records({"unknown": 1})
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        L.list_records(filter=["study"])  # type: ignore[arg-type]
    assert L.as_filter(kinds="variant").kinds == ["variant"]


def test_stats(grid: dict[str, str]) -> None:
    st = L.stats()
    assert (st.n_records, st.n_trials, st.n_configs, st.n_errors, st.n_with_series) == (8, 6, 7, 1, 5)
    assert (st.n_conflicts, st.n_linked) == (0, 0)
    assert st.by_kind["variant"].model_dump() == {"records": 7, "trials": 6, "configs": 6, "errors": 0}
    assert st.by_kind["zoo"].model_dump() == {"records": 1, "trials": 0, "configs": 1, "errors": 1}
    assert set(st.by_study) == {"mom", "mr"} and st.by_study["mom"].trials == 4
    assert st.first_recorded_at_ns is not None and st.first_recorded_at_ns <= st.last_recorded_at_ns
    _validate(st.model_dump(mode="json"))
    empty = L.stats(study="nothing")
    assert (empty.n_records, empty.n_trials, empty.first_recorded_at_ns, empty.by_kind) == (0, 0, None, {})


def test_variants(grid: dict[str, str]) -> None:
    vs = L.variants(study="mom")
    assert [v["params"] for v in vs] == [{"lb": 10}, {"lb": 20}, {"lb": 40}, {"k": 1}]
    v20 = vs[1]
    assert v20["trial_id"] == grid["mom20"] and v20["n_records"] == 2 and v20["dataset"] == "es-2024"
    assert (v20["series_kind"], v20["n_obs"]) == ("returns", 3)
    assert vs[3]["series_kind"] is None and vs[3]["n_obs"] == 0
    assert L.variants(kinds=["zoo"]) == L.variants()  # the kind is forced to variant
    assert json.loads(json.dumps(vs)) == vs


def test_selection_inputs_plain_data(grid: dict[str, str]) -> None:
    sel = L.selection_inputs(study="mom")
    assert json.loads(json.dumps(sel, allow_nan=False)) == sel  # JSON types only
    _validate(sel)
    assert sel["n_trials"] == 4 and sel["n_with_series"] == 3 and sel["conflicts"] == []
    assert sel["chain_verified"] is True and sel["series_kind"] == "returns"
    assert [t["trial_id"] for t in sel["trials"]] == [grid["mom10"], grid["mom20"], grid["mom40"], grid["bare"]]
    t = sel["trials"][0]
    assert t["series"] == [0.01, -0.01, 0.02] and t["n_obs"] == 3 and t["periods_per_year"] == 252
    assert sel["trials"][1]["n_records"] == 2
    assert sel["trials"][3]["series"] is None  # counted, without a series
    scores = L.selection_inputs(study="mr", series="scores")
    assert scores["n_with_series"] == 2 and scores["trials"][0]["higher_is_better"] is False
    assert L.selection_inputs(study="mr")["n_with_series"] == 0  # no returns series there
    head = L.verify_chain().head
    assert sel["head"] == head.model_dump()
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        L.selection_inputs(series="pnl")


def test_iterating_the_code_without_a_version_is_a_new_trial_each_time(ledger_dir: Path) -> None:
    """The common workflow: edit the strategy, re-run, re-register the same name, params and data with no
    code_version. Each different result is a trial; re-registering the same result is not."""
    ids = [
        L.register_variant("mm", {"spread": 2}, returns=r, dataset="BTC").trial_id
        for r in ([0.1, 0.2], [0.1, 0.3], [0.2, 0.3], [0.2, 0.3])
    ]
    assert len(set(ids)) == 3 and ids[2] == ids[3]
    assert L.n_trials() == 3 and L.stats().n_conflicts == 0 and L.stats().n_configs == 1
    sel = L.selection_inputs()
    assert sel["n_trials"] == 3 and [t["series"] for t in sel["trials"]] == [[0.1, 0.2], [0.1, 0.3], [0.2, 0.3]]
    # without a series the metrics are the outcome
    a = L.register_variant("m", {"k": 1}, metrics={"sharpe": 1.2})
    b = L.register_variant("m", {"k": 1}, metrics={"sharpe": 0.4})
    c = L.register_variant("m", {"k": 1}, metrics={"sharpe": 0.4})
    assert a.trial_id != b.trial_id == c.trial_id


def test_a_run_whose_attached_series_disagree_counts_each_series(ledger_dir: Path) -> None:
    summ = {"run_id": "c" * 16, "spec": {"seed": 1}, "models": [], "n_shadows": 1}
    a = L.record("zoo", summary=summ, scores=[0.1, 0.2])
    L.record("zoo", summary=summ, scores=[0.1, 0.3])
    L.record("zoo", summary=summ)  # a later record without a series
    assert a.trial_id == "c" * 16 and L.n_trials() == 2  # one run, two outcomes: counted twice, not hidden
    st = L.stats()
    assert (st.n_trials, st.n_conflicts, st.n_with_series) == (2, 1, 2)
    sel = L.selection_inputs(kinds=["zoo"], series="scores")
    assert sel["conflicts"] == [a.trial_id] and sel["n_trials"] == 2
    assert [(t["conflict"], t["series"], t["n_records"]) for t in sel["trials"]] == [
        (True, [0.1, 0.2], 3),
        (True, [0.1, 0.3], 3),
    ]
    assert q.list_document({"kinds": ["zoo"]}).n_conflicts == 1


def test_selection_is_the_count_of_one_search(ledger_dir: Path) -> None:
    """selection_inputs counts the strategy variants of one study unless told otherwise: zoo, calibrate and other
    runs are not strategy trials, and a run a variant was evaluated with is part of that variant's trial."""
    zoo = {"spec": {"seed": 1}, "models": [], "n_shadows": 1}
    with L.study("s1"):
        runs = [L.record("zoo", summary={**zoo, "run_id": f"{i:016x}"}) for i in range(3)]
        v = L.register_variant("mm", {"k": 1}, returns=[0.1, 0.2], run_ids=[runs[0].run_id])
    assert L.n_trials(study="s1") == 3  # every kind: 2 unlinked zoo runs + the variant (with its zoo run)
    assert L.n_trials(study="s1", kinds=["variant"]) == 1
    sel = L.selection_inputs(study="s1")
    assert sel["filter"]["kinds"] == ["variant"] and sel["n_trials"] == 1 and sel["by_kind"] == {"variant": 1}
    with pytest.raises(QtError) as e:
        L.selection_inputs(study="s1", kinds=["zoo", "variant"])
    assert e.value.code == "QT-CONFIG-001" and "2 kinds" in e.value.message
    mixed = L.selection_inputs(study="s1", kinds=["zoo", "variant"], allow_mixed=True)
    assert mixed["n_trials"] == 3 and mixed["linked_run_ids"] == [runs[0].run_id]
    assert mixed["by_kind"] == {"zoo": 2, "variant": 1}
    st = L.stats(study="s1")
    assert (st.n_trials, st.n_linked, st.by_kind["zoo"].trials) == (3, 1, 2)
    L.register_variant("other", {"k": 2}, study="s2")
    with pytest.raises(QtError, match="2 studies"):
        L.selection_inputs()
    assert L.selection_inputs(allow_mixed=True)["studies"] == ["s1", "s2"]
    assert v.trial_id in {t["trial_id"] for t in sel["trials"]}


def test_selection_reports_alignment_configs_and_data(ledger_dir: Path) -> None:
    ix = [1_000, 2_000, 3_000]
    L.register_variant("a", {"k": 1}, returns=[0.1, 0.2, 0.3], index=ix, dataset="es")
    L.register_variant("a", {"k": 2}, returns=[0.0, 0.1, 0.1], index=ix, dataset="es")
    L.register_variant("a", {"k": 2}, returns=[0.0, 0.2, 0.1], index=ix, dataset="nq")
    sel = L.selection_inputs()
    assert sel["aligned"] is True and sel["index"] == ix and sel["trials"][0]["index"] == ix
    assert (sel["n_trials"], sel["n_configs"]) == (3, 2)  # (k=2 on es) and (k=2 on nq): one configuration
    assert sel["trials"][1]["dataset_hash"] != sel["trials"][2]["dataset_hash"]
    L.register_variant("a", {"k": 3}, returns=[0.0, 0.1, 0.1], index=[1_000, 2_000, 4_000])
    assert L.selection_inputs()["aligned"] is False and L.selection_inputs()["index"] is None
    L.register_variant("b", {"k": 1}, returns=[0.1], study="other")
    one = L.selection_inputs(study="other")
    assert one["aligned"] is False and one["trials"][0]["index"] is None  # no index: alignment unknown
    for bad, msg in (([1, 1, 2], "does not follow"), ([1, 2], "entries for 3 values"), (["x", 1, 2], "not a time")):
        with pytest.raises(QtError, match=msg):
            L.register_variant("c", returns=[0.1, 0.2, 0.3], index=bad)
    import datetime as dt

    r = L.register_variant("d", returns=[0.1, 0.2], index=[dt.date(2024, 1, 2), dt.datetime(2024, 1, 3, 12)])
    assert L.selection_inputs(name="d")["index"] == [1704153600 * 10**9, (1704240000 + 43200) * 10**9]
    assert r.series is not None and r.series.indexed


def test_dataset_is_identified_by_content_when_it_is_a_path(ledger_dir: Path, tmp_path: Path) -> None:
    ds = tmp_path / "ds"
    ds.mkdir()
    (ds / "manifest.json").write_text('{"schema": "qt.dataset_manifest", "dataset_id": "' + "d" * 64 + '"}')
    f = tmp_path / "bars.csv"
    f.write_text("1,2\n")
    assert L.register_variant("v", {"k": 1}, dataset=ds).inputs == {"dataset": "d" * 64}
    import hashlib

    assert L.register_variant("v", {"k": 1}, dataset=f"@{f}").inputs == {
        "dataset": hashlib.sha256(b"1,2\n").hexdigest()
    }
    assert L.register_variant("v", {"k": 1}, dataset="ES 2024H1").inputs == {"dataset_label": "ES 2024H1"}
    assert L.n_trials(dataset="dddd") == 1
    with pytest.raises(QtError) as e:
        L.register_variant("v", dataset=tmp_path / "missing")
    assert e.value.code == "QT-INPUT-001"
    assert [v["dataset"] for v in L.variants()] == ["d" * 64, hashlib.sha256(b"1,2\n").hexdigest(), "ES 2024H1"]


def test_register_variants_in_one_batch(ledger_dir: Path) -> None:
    recs = L.register_variants(
        [{"name": "g", "params": {"k": k}, "returns": [0.01 * k, 0.02], "code_version": "v1"} for k in range(5)]
    )
    assert [r.seq for r in recs] == [1, 2, 3, 4, 5] and L.n_trials() == 5
    assert recs[0].code is not None and recs[0].code.user == "v1"
    for bad in ([{"params": {}}], [{"name": "x", "ledger": "y"}], [{"name": "x", "nope": 1}], [{"name": "has space"}]):
        with pytest.raises(QtError, match="QT-CONFIG-001"):
            L.register_variants([{"name": "fine"}, *bad])
    assert L.n_trials() == 5  # a refused batch writes nothing


def test_reads_refuse_a_tampered_ledger(grid: dict[str, str], ledger_dir: Path) -> None:
    p = ledger_dir / "ledger.jsonl"
    assert p.read_bytes().count(b'"params":{"lb":10}') == 1
    p.write_bytes(p.read_bytes().replace(b'"params":{"lb":10}', b'"params":{"lb":11}'))
    for fn in (L.n_trials, L.list_records, L.stats, L.variants, L.selection_inputs):
        with pytest.raises(QtError) as e:
            fn()
        assert e.value.code == "QT-LEDGER-001"
    v = L.verify_chain()
    assert not v.ok and [p.reason for p in v.problems] == ["hash_mismatch"]


def test_export_formats(grid: dict[str, str], ledger_dir: Path, tmp_path: Path) -> None:
    src = ledger_dir / "ledger.jsonl"
    ex = L.export(tmp_path / "out" / "copy.jsonl")
    assert (
        ex.format == "jsonl"
        and ex.n_records == 9
        and (tmp_path / "out" / "copy.jsonl").read_bytes() == src.read_bytes()
    )
    copy = L.Ledger(tmp_path / "out" / "copy.jsonl")
    assert copy.verify().ok  # verifiable on its own, series files included
    assert len(list((tmp_path / "out" / "series").iterdir())) == 4  # the two mr variants share one series
    assert L.selection_inputs(study="mom", ledger=copy) == L.selection_inputs(study="mom")
    _validate(ex.model_dump(mode="json"))

    ex = L.export(tmp_path / "mom.json", study="mom")
    dump = json.loads((tmp_path / "mom.json").read_text())
    _validate(dump)
    assert ex.n_records == len(dump["entries"]) == 6 and dump["filter"]["study"] == "mom"
    first = dump["entries"][0]["record"]["series"]
    assert dump["series"][first["sha256"]] == {"values": [0.01, -0.01, 0.02], "index": None}
    hashes = L.Ledger(src).scan().hashes
    assert all(e["hash"] in hashes for e in dump["entries"])

    ex = L.export(tmp_path / "all.parquet")
    t = pq.read_table(tmp_path / "all.parquet")
    assert ex.n_records == t.num_rows == 8
    assert t.schema.metadata[b"qt.schema"] == b"qt.ledger"
    assert t.schema.equals(L.LEDGER_TABLE, check_metadata=False)
    rows = t.to_pylist()
    assert rows[0]["series"] == [0.01, -0.01, 0.02] and json.loads(rows[0]["params_json"]) == {"lb": 10}
    assert rows[0]["series_sha256"] == first["sha256"] and rows[0]["series_index"] is None
    assert rows[-1]["error_code"] == "QT-INPUT-001" and rows[-1]["series"] is None
    assert rows[0]["recorded_at"].tzinfo is not None

    ex = L.export(tmp_path / "sel.json", selection=True, study="mom")
    assert ex.format == "selection" and ex.n_records == 4
    sel = json.loads((tmp_path / "sel.json").read_text())
    assert sel == L.selection_inputs(study="mom") and sel["head"] == ex.head.model_dump()

    # determinism: the same ledger exports to the same bytes
    L.export(tmp_path / "all2.parquet")
    L.export(tmp_path / "mom2.json", study="mom")
    assert (tmp_path / "mom2.json").read_bytes() == (tmp_path / "mom.json").read_bytes()
    assert (tmp_path / "all2.parquet").read_bytes() == (tmp_path / "all.parquet").read_bytes()


@pytest.mark.parametrize(
    ("out", "kw", "msg"),
    [
        ("x.csv", {}, "must end in"),
        ("x.jsonl", {"study": "mom"}, "copies the whole chain"),
        ("x.parquet", {"selection": True}, "selection inputs are written as .json"),
    ],
)
def test_export_refusals(grid: dict[str, str], tmp_path: Path, out: str, kw: dict, msg: str) -> None:
    with pytest.raises(QtError) as e:
        L.export(tmp_path / out, **kw)
    assert e.value.code == "QT-CONFIG-001" and msg in e.value.message
    assert not (tmp_path / out).exists()


def test_export_onto_the_ledger_itself_is_refused(grid: dict[str, str], ledger_dir: Path) -> None:
    before = (ledger_dir / "ledger.jsonl").read_bytes()
    with pytest.raises(QtError, match="is the ledger itself"):
        L.export(ledger_dir / "ledger.jsonl")
    assert (ledger_dir / "ledger.jsonl").read_bytes() == before


def test_records_and_documents_match_their_json_schemas(grid: dict[str, str], ledger_dir: Path) -> None:
    schemas = L.json_schemas()
    assert set(schemas) == set(DOCUMENTS)
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        assert schema["title"] == name and {"schema", "schema_version"} <= set(schema["required"])
    for line in (ledger_dir / "ledger.jsonl").read_bytes().splitlines():
        _validate(json.loads(line)["record"])
    _validate(L.verify_chain().model_dump(mode="json"))


def test_series_validation(ledger_dir: Path) -> None:
    import numpy as np

    r = L.register_variant("np", {"w": np.float64(0.5), "grid": np.array([1, 2])}, returns=np.array([0.1, -0.2]))
    assert r.params == {"w": 0.5, "grid": [1, 2]} and L.selection_inputs()["trials"][0]["series"] == [0.1, -0.2]
    cases = [
        ({"returns": [0.1, math.nan]}, "not finite"),
        ({"returns": [0.1, math.inf]}, "not finite"),
        ({"returns": []}, "at least 1"),
        ({"returns": "0.1"}, "sequence of numbers"),
        ({"returns": [0.1, "x"]}, "is not a number"),
        ({"returns": [True]}, "is not a number"),
        ({"returns": [0.1], "scores": [0.2]}, "not both"),
        ({"periods_per_year": 252}, "applies to a returns series"),
        ({"returns": [0.1], "periods_per_year": 0}, "greater than 0"),
        ({"params": {"x": math.nan}}, "not JSON"),
        ({"params": {"x": object()}}, "is not JSON"),
        ({"dataset": " "}, "dataset"),
        ({"study": "has space"}, "study"),
        ({"note": "n" * 5000}, "note"),
        ({"run_ids": ["nothex"]}, "run_ids"),
    ]
    for kw, msg in cases:
        params = kw.pop("params", {"a": 1})
        with pytest.raises(QtError) as e:
            L.register_variant("v", params, **kw)
        assert e.value.code == "QT-CONFIG-001" and msg in e.value.message, (kw, e.value.message)
    for bad in ("", "has space", "x" * 129, None):
        with pytest.raises(QtError, match="QT-CONFIG-001"):
            L.register_variant(bad, {})  # type: ignore[arg-type]
    assert len(L.list_records()) == 1  # nothing invalid was written


def test_series_length_cap(ledger_dir: Path) -> None:
    from queuetruth.ledger import models

    big = [0.0] * (models.MAX_SERIES + 1)
    with pytest.raises(QtError, match="at most"):
        L.register_variant("big", returns=big)


def test_off_switch_writes_nothing_and_says_so(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setenv("QT_LEDGER", "off")
    with caplog.at_level(logging.WARNING, logger="queuetruth.ledger"):
        assert L.register_variant("v", {"x": 1}, returns=[0.1]) is None
        assert L.record("zoo", error=QtError("QT-INPUT-001", "x")) is None
        assert L.register_variants([{"name": "a"}]) == []
    assert not ledger_dir.exists()
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 3 and all("QT_LEDGER=off" in m and "not recorded" in m for m in msgs)
