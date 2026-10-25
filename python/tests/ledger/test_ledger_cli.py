"""`queuetruth ledger ...` through `queuetruth.cli.main`: commands, output formats, exit codes and errors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from queuetruth import cli
from queuetruth import ledger as L
from queuetruth.errors import QtError
from queuetruth.ledger import _capture as cap
from queuetruth.ledger import cli as lcli

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


@pytest.fixture
def run_json(run: Any) -> Any:
    """`--format json`: exactly one JSON document on stdout, valid against its schema (ledger or qt.error)."""

    def _run(*argv: str) -> tuple[int, dict[str, Any], str]:
        code, out, err = run("--format", "json", *argv)
        lines = [x for x in out.splitlines() if x.strip()]
        assert len(lines) == 1, (out, err)
        doc = json.loads(lines[0])
        schemas = L.json_schemas()
        if doc["schema"] == "qt.error":
            schema = json.loads((REPO / "schemas" / "json" / "qt.error.schema.json").read_text())
        else:
            schema = schemas[doc["schema"]]
        Draft202012Validator(schema).validate(doc)
        return code, doc, err

    return _run


@pytest.fixture
def filled(ledger_dir: Path) -> Path:
    for lb in (10, 20):
        L.register_variant("mom", {"lb": lb}, returns=[0.01, -0.02], study="mom")
    L.register_variant("mr", {"z": 2}, scores=[0.3], study="mr")
    return ledger_dir


def test_command_detection() -> None:
    assert lcli.is_ledger_command(["ledger", "list"])
    assert lcli.is_ledger_command(["--format", "json", "ledger"])
    assert lcli.is_ledger_command(["--format=json", "--quiet", "--seed", "3", "ledger", "stats"])
    assert not lcli.is_ledger_command(["zoo", "--data", "ledger"])
    assert not lcli.is_ledger_command(["--format", "ledger"])
    assert not lcli.is_ledger_command([])
    assert lcli.main_if_ledger(["doctor"]) is None
    g, rest = lcli.split_globals(["ledger", "list", "--output-format=json", "--verbose", "--log-level", "info"])
    assert rest == ["ledger", "list"] and g["format"] == "json" and g["verbose"] and g["log_level"] == "info"


def test_list(filled: Path, run: Any, run_json: Any) -> None:
    code, doc, _ = run_json("ledger", "list")
    assert code == 0 and doc["n_matching"] == 3 and [r["name"] for r in doc["records"]] == ["mom", "mom", "mr"]
    code, doc, _ = run_json("ledger", "list", "--limit", "1", "--study", "mo*")
    assert doc["n_matching"] == 2 and [r["params"] for r in doc["records"]] == [{"lb": 20}]
    code, doc, _ = run_json("ledger", "list", "--kind", "zoo", "--kind", "score")
    assert doc["n_matching"] == 0
    code, out, err = run("ledger", "list")
    assert code == 0 and err == ""
    assert out.splitlines()[0].startswith("3 of 3 matching record(s) in ")
    assert "returns n=2" in out and "mom" in out and out.splitlines()[-1].startswith("head: 3:")
    code, out, _ = run("ledger", "list", "--name", "nothing")
    assert out.startswith("0 of 0 matching")


def test_stats(filled: Path, run: Any, run_json: Any) -> None:
    code, doc, _ = run_json("ledger", "stats")
    assert code == 0 and (doc["n_trials"], doc["n_records"], doc["n_with_series"]) == (3, 3, 3)
    assert set(doc["by_study"]) == {"mom", "mr"}
    code, doc, _ = run_json("--format", "json", "ledger", "stats", "--study", "mr", "--since", "2000-01-01")
    assert doc["n_trials"] == 1
    code, doc, _ = run_json("ledger", "stats", "--until", "2000-01-01T00:00:00+00:00")
    assert doc["n_trials"] == 0
    code, out, _ = run("ledger", "stats")
    assert out.startswith("3 trial(s) (3 configuration(s), 3 record(s), 0 failed run(s), 3 with a series)")
    assert "by kind:" in out and "by study:" in out


def test_time_parsing() -> None:
    assert lcli._time_ns("1970-01-02", "--since") == 86_400 * 10**9
    assert lcli._time_ns("1970-01-01T00:00:01.5+01:00", "--since") == -3_598_500_000_000
    assert lcli._time_ns("123", "--since") == 123
    with pytest.raises(QtError, match="QT-USAGE-001"):
        lcli._time_ns("yesterday", "--since")


def test_verify_ok_and_anchor(filled: Path, run: Any, run_json: Any) -> None:
    code, doc, _ = run_json("ledger", "verify")
    assert code == 0 and doc["ok"] and doc["n_records"] == 4
    anchor = f"{doc['head']['seq']}:{doc['head']['hash']}"
    L.register_variant("later", {})
    code, doc, _ = run_json("ledger", "verify", "--anchor", anchor)
    assert code == 0 and doc["anchor_ok"] is True
    code, out, _ = run("ledger", "verify", "--anchor", anchor)
    assert out.startswith("ledger OK: 5 record(s)") and f"anchor {anchor}: present and unchanged" in out


def test_verify_detects_tampering(filled: Path, run: Any, run_json: Any) -> None:
    p = filled / "ledger.jsonl"
    p.write_bytes(p.read_bytes().replace(b'"lb":20', b'"lb":25'))
    code, doc, _ = run_json("ledger", "verify")
    assert code == 2 and doc["code"] == "QT-LEDGER-001"
    v = doc["details"]["verification"]
    assert not v["ok"] and [(x["line"], x["reason"]) for x in v["problems"]] == [(3, "hash_mismatch")]
    code, out, err = run("ledger", "verify")
    assert code == 2 and out == "" and err.startswith("QT-LEDGER-001: ") and "line 3: hash_mismatch" in err
    assert "hint: " in err
    for sub in ("list", "stats"):
        code, doc, _ = run_json("ledger", sub)
        assert code == 2 and doc["code"] == "QT-LEDGER-001" and doc["details"]["problem"]["reason"] == "hash_mismatch"


def test_verify_warns_about_an_uncommitted_tail(filled: Path, run: Any) -> None:
    with (filled / "ledger.jsonl").open("ab") as fh:
        fh.write(b'{"hash":"')
    code, _, err = run("ledger", "verify")
    assert code == 0 and "warning: 9 byte(s) after the last record" in err
    code, _, err = run("--quiet", "ledger", "verify")
    assert code == 0 and err == ""


def test_export(filled: Path, tmp_path: Path, run: Any, run_json: Any) -> None:
    code, doc, _ = run_json("ledger", "export", "--out", str(tmp_path / "x.jsonl"))
    assert code == 0 and doc["format"] == "jsonl" and doc["n_records"] == 4
    assert (tmp_path / "x.jsonl").read_bytes() == (filled / "ledger.jsonl").read_bytes()
    code, doc, _ = run_json("ledger", "verify", "--ledger", str(tmp_path / "x.jsonl"))
    assert code == 0 and doc["ok"] and doc["ledger"] == str(tmp_path / "x.jsonl")
    code, doc, _ = run_json("ledger", "export", "--out", str(tmp_path / "s.json"), "--selection", "--study", "mom")
    assert doc["format"] == "selection" and json.loads((tmp_path / "s.json").read_text())["n_trials"] == 2
    code, doc, _ = run_json("ledger", "export", "--out", str(tmp_path / "s2.json"), "--selection", "--series", "scores")
    assert code == 2 and doc["code"] == "QT-CONFIG-001" and "2 studies" in doc["message"]  # one search at a time
    code, doc, _ = run_json(
        "ledger", "export", "--out", str(tmp_path / "s2.json"), "--selection", "--series", "scores", "--allow-mixed"
    )
    sel = json.loads((tmp_path / "s2.json").read_text())
    assert code == 0 and (sel["n_trials"], sel["n_with_series"], sel["studies"]) == (3, 1, ["mom", "mr"])
    code, doc, _ = run_json("ledger", "export", "--out", str(tmp_path / "s3.json"), "--allow-mixed")
    assert code == 2 and doc["code"] == "QT-USAGE-001"
    code, out, _ = run("ledger", "export", "--out", str(tmp_path / "x.parquet"))
    assert code == 0 and out.startswith(f"wrote 3 record(s) to {tmp_path / 'x.parquet'} (parquet, ")
    code, doc, _ = run_json("ledger", "export", "--out", str(tmp_path / "y.jsonl"), "--study", "mom")
    assert code == 2 and doc["code"] == "QT-CONFIG-001"
    code, doc, _ = run_json("ledger", "export", "--out", str(tmp_path / "y.json"), "--series", "scores")
    assert code == 2 and doc["code"] == "QT-USAGE-001"


def test_add(ledger_dir: Path, tmp_path: Path, run: Any, run_json: Any) -> None:
    rets = tmp_path / "r.csv"
    rets.write_text("return\n0.01\n\n-0.005\n0.002\n")
    params = tmp_path / "p.json"
    params.write_text('{"lb": 20, "z": [1, 2]}')
    code, doc, _ = run_json(
        "ledger", "add", "--name", "mom", "--params", f"@{params}", "--returns", str(rets), "--periods-per-year",
        "252", "--study", "s1", "--dataset", "es-2024", "--code-version", "abc", "--run-id", "a" * 16, "--note", "n",
    )  # fmt: skip
    assert code == 0 and doc["schema"] == "qt.ledger_record" and doc["kind"] == "variant"
    want = {"kind": "returns", "n_obs": 3, "indexed": False, "periods_per_year": 252.0, "higher_is_better": True}
    assert {k: v for k, v in doc["series"].items() if k != "sha256"} == want
    assert doc["inputs"] == {"dataset_label": "es-2024"}
    assert L.selection_inputs(study="s1")["trials"][0]["series"] == [0.01, -0.005, 0.002]
    assert doc["params"] == {"lb": 20, "z": [1, 2]} and doc["code"]["user"] == "abc" and doc["run_ids"] == ["a" * 16]
    sc = tmp_path / "s.json"
    sc.write_text('{"values": [0.2, 0.1]}')
    code, out, _ = run(
        "ledger", "add", "--name", "m2", "--params", '{"a": 1}', "--scores", str(sc), "--lower-is-better"
    )
    assert code == 0 and out.startswith("recorded variant m2 as trial ") and "scores n=2" in out
    assert L.selection_inputs(series="scores", study="default")["trials"][0]["higher_is_better"] is False


@pytest.mark.parametrize(
    ("argv", "code_"),
    [
        (["ledger"], "QT-USAGE-001"),
        (["ledger", "frobnicate"], "QT-USAGE-001"),
        (["ledger", "list", "--limit", "-1"], "QT-USAGE-001"),
        (["ledger", "list", "--status", "maybe"], "QT-USAGE-001"),
        (["ledger", "list", "--since", "tomorrow-ish"], "QT-USAGE-001"),
        (["ledger", "add"], "QT-USAGE-001"),
        (["ledger", "add", "--name", "x", "--params", "[1]"], "QT-USAGE-001"),
        (["ledger", "add", "--name", "x", "--params", "{bad"], "QT-USAGE-001"),
        (["ledger", "add", "--name", "x", "--params", "@/nonexistent/p.json"], "QT-INPUT-001"),
        (["ledger", "add", "--name", "x", "--returns", "/nonexistent/r.csv"], "QT-INPUT-001"),
        (["ledger", "add", "--name", "x", "--lower-is-better"], "QT-USAGE-001"),
        (["ledger", "add", "--name", "x", "--periods-per-year", "12"], "QT-USAGE-001"),
        (["ledger", "add", "--name", "bad name"], "QT-CONFIG-001"),
        (["ledger", "verify", "--anchor", "nope"], "QT-CONFIG-001"),
        (["ledger", "list", "--format", "xml"], "QT-USAGE-001"),
        (["ledger", "list", "--threads"], "QT-USAGE-001"),
    ],
)
def test_errors(filled: Path, run: Any, argv: list[str], code_: str) -> None:
    code, out, err = run("--format", "json", *argv) if "--format" not in argv else run(*argv)
    assert code == 2
    if out.strip():
        assert json.loads(out)["code"] == code_
    else:
        assert err.startswith(code_)


def test_series_files(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("1.0\nx\n")
    with pytest.raises(QtError, match="line 2"):
        lcli.read_series(str(bad))
    # a first line that starts like a number is data, never a header that is silently dropped
    pct = tmp_path / "pct.csv"
    pct.write_text("0.01%\n0.02\n0.03\n")
    with pytest.raises(QtError) as e:
        lcli.read_series(str(pct))
    assert e.value.code == "QT-INPUT-002" and "line 1" in e.value.message
    hdr = tmp_path / "hdr.csv"
    hdr.write_text("ret_1d\n0.01\n0.02\n")
    assert lcli.read_series(str(hdr)) == ([0.01, 0.02], None)
    # the usual date,return CSV: the last column, or one named; dates as the index
    csv = tmp_path / "daily.csv"
    csv.write_text("date,pnl,ret\n2024-01-02,5,0.01\n2024-01-03,-2,-0.004\n")
    day = 86_400 * 10**9
    assert lcli.read_series(str(csv)) == ([0.01, -0.004], None)
    assert lcli.read_series(str(csv), column="pnl", index_column="date") == ([5.0, -2.0], [19724 * day, 19725 * day])
    assert lcli.read_series(str(csv), column="1")[0] == [5.0, -2.0]
    with pytest.raises(QtError, match="--column"):
        lcli.read_series(str(csv), column="nope")
    ragged = tmp_path / "ragged.csv"
    ragged.write_text("1,2\n3\n")
    with pytest.raises(QtError, match="line 2: 1 column"):
        lcli.read_series(str(ragged))
    with pytest.raises(QtError, match="not an ISO 8601"):
        lcli.read_series(str(csv), index_column="ret")
    tsv = tmp_path / "x.txt"
    tsv.write_text("1700000000000000000\t0.5\n1700000060000000000\t0.25\n")
    assert lcli.read_series(str(tsv), index_column="0") == ([0.5, 0.25], [1700000000000000000, 1700000060000000000])
    # parquet: a numeric column, and a timestamp index
    import datetime as dt

    import pyarrow as pa
    import pyarrow.parquet as pq

    pqf = tmp_path / "r.parquet"
    ts = [dt.datetime(2024, 1, 2, tzinfo=dt.UTC), dt.datetime(2024, 1, 3, tzinfo=dt.UTC)]
    pq.write_table(pa.table({"t": pa.array(ts, pa.timestamp("us", tz="UTC")), "r": [0.1, 0.2]}), pqf)
    assert lcli.read_series(str(pqf), index_column="t") == ([0.1, 0.2], [19724 * day, 19725 * day])
    pq.write_table(pa.table({"r": pa.array(["a", "b"])}), pqf)
    with pytest.raises(QtError, match="not numbers"):
        lcli.read_series(str(pqf))
    jsx = tmp_path / "ix.json"
    jsx.write_text('{"values": [0.1, 0.2], "index": [5, 6]}')
    assert lcli.read_series(str(jsx)) == ([0.1, 0.2], [5, 6])
    js = tmp_path / "bad.json"
    js.write_text('["a"]')
    with pytest.raises(QtError, match="array of numbers"):
        lcli.read_series(str(js))
    js.write_text("{nope")
    with pytest.raises(QtError, match="invalid JSON"):
        lcli.read_series(str(js))


def test_missing_ledger(tmp_path: Path, run_json: Any) -> None:
    for sub in ("list", "stats", "verify"):
        code, doc, _ = run_json("ledger", sub, "--ledger", str(tmp_path / "nothing"))
        assert code == 2 and doc["code"] == "QT-INPUT-001"


def test_help(run: Any) -> None:
    code, out, _ = run("ledger", "--help")
    assert code == 0 and "list" in out and "verify" in out and "add" in out
    code, out, _ = run("ledger", "verify", "--help")
    assert code == 0 and "--anchor" in out
    code, out2, _ = run("help", "ledger")  # the Rust grammar does not know `ledger`; `help ledger` still works
    assert code == 0 and "verify" in out2 and "add" in out2
    code, out3, _ = run("help", "ledger", "verify")
    assert code == 0 and out3 == out


def test_add_with_an_index_and_a_dataset_path(ledger_dir: Path, tmp_path: Path, run_json: Any) -> None:
    csv = tmp_path / "d.csv"
    csv.write_text("date,ret\n2024-01-02,0.01\n2024-01-03,0.02\n")
    code, doc, _ = run_json(
        "ledger", "add", "--name", "a", "--returns", str(csv), "--index-column", "date", "--dataset", f"@{csv}"
    )
    assert (
        code == 0
        and doc["series"]["indexed"] is True
        and doc["inputs"]["dataset"] == cap.dataset_identity(csv)["dataset"]
    )
    assert L.selection_inputs()["index"] == [19724 * 86_400 * 10**9, 19725 * 86_400 * 10**9]
    code, doc, _ = run_json("ledger", "add", "--name", "b", "--column", "ret")
    assert code == 2 and doc["code"] == "QT-USAGE-001"


def test_off_switch_add(monkeypatch: pytest.MonkeyPatch, run_json: Any) -> None:
    monkeypatch.setenv("QT_LEDGER", "off")
    code, doc, _ = run_json("ledger", "add", "--name", "x")
    assert code == 2 and doc["code"] == "QT-USAGE-001" and "QT_LEDGER=off" in doc["message"]
