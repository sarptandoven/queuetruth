"""The streaming capture reader (`queuetruth.pm.capture`): wrappers, documents, arrays, gzip, heartbeats, errors."""

from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from queuetruth.errors import QtError
from queuetruth.pm.capture import read_frames


def test_wrapped_raw_and_request_frames(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    lines = [
        json.dumps({"recv_ns": 5, "msg": {"type": "a", "x": 1.25}}),
        "PING",
        "",
        json.dumps({"recv_ns": "6", "msg": '{"type": "b"}'}),
        json.dumps({"recv_ns": 7, "msg": "PONG"}),
        json.dumps({"dir": "out", "local_ns": 10, "msg": {"method": "GET", "path": "/x/queue_position"}}),
        json.dumps({"dir": "in", "local_ns": 20, "order_id": "o-1", "msg": {"queue_position_fp": "3.00"}}),
        json.dumps({"dir": "in", "local_ns": 30, "msg": {"after": True}}),
        json.dumps({"type": "unwrapped"}),
        json.dumps({"recv_ns": 40, "msg": [{"e": 1}, {"e": 2}]}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    frames, st = read_frames([p])
    assert st.heartbeats == 2 and st.requests == 1
    kinds = [(f.direction, f.recv_ns, f.send_ns) for f in frames]
    assert kinds == [
        ("in", 5, None),
        ("in", 6, None),
        ("out", None, 10),
        ("in", 20, 10),  # the response takes the request's send time
        ("in", 30, None),  # only the next in-record gets the request
        ("in", None, None),
        ("in", 40, None),
        ("in", 40, None),
    ]
    assert frames[0].obj["x"] == Decimal("1.25")  # never a binary float
    assert frames[3].request == {"method": "GET", "path": "/x/queue_position", "send_ns": 10}
    assert frames[3].context == {"order_id": "o-1"}
    assert [f.obj for f in frames[-2:]] == [{"e": 1}, {"e": 2}]
    assert frames[-1].location.endswith("[1]") and frames[-1].pos == (0, 10, 1)


def test_multiline_json_document_array_and_gzip(tmp_path: Path) -> None:
    doc = tmp_path / "doc.json"
    doc.write_text(json.dumps({"result": [{"a": 1}, {"a": 2}]}, indent=2), encoding="utf-8")
    frames, _ = read_frames([doc])
    assert len(frames) == 1 and frames[0].obj["result"][1] == {"a": 2}
    arr = tmp_path / "arr.json"
    arr.write_text(json.dumps([{"a": 1}, {"a": 2}, {"a": 3}], indent=1), encoding="utf-8")
    frames, _ = read_frames([arr])
    assert [f.obj["a"] for f in frames] == [1, 2, 3]
    gz = tmp_path / "c.jsonl.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"recv_ns": 1, "msg": {"k": 1}}) + "\n")
    frames, st = read_frames([gz])
    assert frames[0].obj == {"k": 1} and st.files == 1


@pytest.mark.parametrize(
    ("content", "where"),
    [
        ('{"recv_ns": 1, "msg": {"a": 1}}\nnot json\n', ":2"),
        ('{"recv_ns": 1.5, "msg": {}}\n', "recv_ns"),
        ('{"dir": "sideways", "local_ns": 1, "msg": {}}\n', "dir"),
        ('{"recv_ns": 1, "msg": "{broken"}\n', "wrapped msg"),
        ("[1, 2,\n", "invalid JSON document"),
    ],
)
def test_bad_lines_name_the_file_and_line(tmp_path: Path, content: str, where: str) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(QtError) as e:
        read_frames([p])
    assert e.value.code == "QT-INPUT-002" and where in e.value.message and "bad.jsonl" in e.value.message


def test_missing_input_is_qt_input_001(tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        read_frames([tmp_path / "nope.jsonl"])
    assert e.value.code == "QT-INPUT-001"
