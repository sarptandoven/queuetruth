"""Claim text: references, literal braces and rendering (`queuetruth.cert.certify`)."""

from __future__ import annotations

import pytest

from queuetruth.cert.certify import parse_claim, parse_run_args, ref_text, render_claim
from queuetruth.errors import QtError


def test_parse_claim_references_and_literals() -> None:
    parts = parse_claim("bias {score#/bias/fill_rate_bias} at {cal:fit.json#/held_out/x~1y} {{literal}}")
    assert parts == [
        "bias ",
        ("score", None, "/bias/fill_rate_bias"),
        " at ",
        ("cal", "fit.json", "/held_out/x~1y"),
        " {literal}",
    ]
    assert render_claim(parts, [0.0125, "z"]) == "bias 0.0125 at z {literal}"


def test_parse_claim_without_references() -> None:
    assert parse_claim("plain text") == ["plain text"]
    assert render_claim(parse_claim("a}}b{{c"), []) == "a}b{c"


@pytest.mark.parametrize(
    "text",
    [
        "bad {score}",  # no pointer
        "bad {score#bias}",  # pointer without '/'
        "bad {sc ore#/x}",  # label with a space
        "unclosed {score#/x",
        "stray } brace",
        "empty {}",
    ],
)
def test_parse_claim_rejects_malformed_text(text: str) -> None:
    with pytest.raises(QtError) as e:
        parse_claim(text)
    assert e.value.code == "QT-USAGE-001"


def test_ref_text() -> None:
    assert ref_text("r", None, "/a") == "r#/a"
    assert ref_text("r", "f.json", "/a") == "r:f.json#/a"


def test_parse_run_args_labels() -> None:
    got = parse_run_args(["out/replay", "zoo=runs/zoo/", "x.y-z_1=/abs/dir"])
    assert [(r.label, r.path) for r in got] == [("replay", "out/replay"), ("zoo", "runs/zoo/"), ("x.y-z_1", "/abs/dir")]
    # an '=' that does not start with a valid label is part of the path
    got = parse_run_args(["a b=c/d"])
    assert (got[0].label, got[0].path) == ("d", "a b=c/d")


def test_parse_run_args_errors() -> None:
    with pytest.raises(QtError, match="names both"):
        parse_run_args(["a/run", "b/run"])
    with pytest.raises(QtError, match="at least one"):
        parse_run_args([])
    with pytest.raises(QtError, match="must be 1 to 64"):
        parse_run_args(["dir with space/run dir"])
    # a label names the directory a re-execution is written to: never '.', '..' or a hidden name
    for bad in ("..", ".", "../..", "..=replay", ".hidden=replay"):
        with pytest.raises(QtError, match="must be 1 to 64"):
            parse_run_args([bad])
