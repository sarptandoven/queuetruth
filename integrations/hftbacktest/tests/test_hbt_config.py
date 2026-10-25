"""`hbt_config`: the qtint.hbt_queue_config format, its validation and the BacktestAsset builder calls."""

from __future__ import annotations

import json
import math
import random
import struct
from pathlib import Path

import hbt_config as hc
import numpy as np
import pytest


def test_fmt_param_matches_numpy_shortest_positional() -> None:
    rng = random.Random(20261024)
    xs = [2.0, 3.165744804349019, 0.1633981666582094, 1e-7, 1e16, 0.5, 123456789.125, 5e-324, 1.7976931348623157e308]
    xs += [struct.unpack("d", struct.pack("Q", rng.getrandbits(64)))[0] for _ in range(5000)]
    for x in xs:
        if math.isfinite(x):
            assert hc.fmt_param(x) == np.format_float_positional(x, unique=True, trim="-"), repr(x)


def test_fmt_param_integral_and_non_finite() -> None:
    assert hc.fmt_param(2.0) == "2"
    assert hc.fmt_param(1e16) == "10000000000000000"
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(hc.ConfigError, match="QT-CONFIG-001"):
            hc.fmt_param(bad)


def test_model_id_follows_the_queuetruth_grammar() -> None:
    assert hc.model_id("power3", "partial", [2.0]) == "hbt_power3[exch=partial;mode=faithful;n=2]"
    assert hc.model_id("log2", "no_partial", []) == "hbt_log2[exch=no_partial;mode=faithful]"
    assert (
        hc.model_id("power2", "partial", [0.1633981666582094])
        == "hbt_power2[exch=partial;mode=faithful;n=0.1633981666582094]"
    )


@pytest.mark.parametrize(
    ("family", "builder", "args"),
    [
        ("power", "power_prob_queue_model", [1.5]),
        ("power2", "power_prob_queue_model2", [2.0]),
        ("power3", "power_prob_queue_model3", [3.165744804349019]),
        ("log", "log_prob_queue_model", []),
        ("log2", "log_prob_queue_model2", []),
        ("risk_adverse", "risk_adverse_queue_model", []),
    ],
)
def test_make_entry_maps_every_family(family: str, builder: str, args: list[float]) -> None:
    e = hc.make_entry(family, "partial", args, "fitted")
    assert e["builder"] == builder
    assert e["exchange_builder"] == "partial_fill_exchange"
    assert e["args"] == args
    hc.validate_entry(e)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"family": "databento", "args": [0.5]}, "no hftbacktest queue model"),
        ({"family": "power3", "args": []}, "takes 1 argument"),
        ({"family": "log", "args": [1.0]}, "takes 0 argument"),
        ({"family": "power3", "args": [0.0]}, "must be > 0"),
        ({"family": "power3", "args": [-2.0]}, "must be > 0"),
        ({"family": "power3", "args": [math.nan]}, "not a finite number"),
        ({"family": "power3", "args": [math.inf]}, "not a finite number"),
        ({"family": "power3", "args": [2.0], "exchange": "fifo"}, "exchange 'fifo'"),
        ({"family": "power3", "args": [2.0], "role": "best"}, "role 'best'"),
    ],
)
def test_make_entry_rejects_invalid_input(kwargs: dict, match: str) -> None:
    kw = {"exchange": "partial", "role": "fitted"} | kwargs
    with pytest.raises(hc.ConfigError, match=match):
        hc.make_entry(kw["family"], kw["exchange"], kw["args"], kw["role"])


def test_validate_entry_checks_builder_and_model_id() -> None:
    e = hc.make_entry("power3", "partial", [2.0], "default")
    with pytest.raises(hc.ConfigError, match="does not match"):
        hc.validate_entry(e | {"model_id": "hbt_power3[exch=partial;mode=faithful;n=3]"})
    with pytest.raises(hc.ConfigError, match="uses builder"):
        hc.validate_entry(e | {"builder": "power_prob_queue_model"})
    with pytest.raises(hc.ConfigError, match="uses no_partial_fill_exchange"):
        hc.validate_entry(e | {"exchange": "no_partial"})
    with pytest.raises(hc.ConfigError, match="not a finite number"):
        hc.validate_entry(e | {"args": [True]})


def _doc() -> dict:
    return hc.document(
        [hc.make_entry("power3", "partial", [3.25], "fitted"), hc.make_entry("log2", "partial", [], "fixed")], "0.1.1"
    )


def test_document_rejects_duplicates_and_dumps_canonically(tmp_path: Path) -> None:
    e = hc.make_entry("log", "partial", [], "fixed")
    with pytest.raises(hc.ConfigError, match="duplicate model_id"):
        hc.document([e, e], "0.1.1")
    doc = _doc()
    text = hc.dumps(doc)
    assert text == hc.dumps(json.loads(text))
    assert text.endswith("}\n")
    p = tmp_path / "cfg.json"
    p.write_text(text)
    assert hc.load(p) == doc


@pytest.mark.parametrize(
    ("content", "code", "match"),
    [
        (None, "QT-INPUT-001", "not found"),
        ("{not json", "QT-CONFIG-001", "is not JSON"),
        ('{"schema": "qt.other"}', "QT-CONFIG-001", "schema is not"),
        ('{"schema": "qtint.hbt_queue_config", "schema_version": 2}', "QT-CONFIG-001", "schema_version"),
        ('{"schema": "qtint.hbt_queue_config", "schema_version": 1, "models": []}', "QT-CONFIG-001", "non-empty"),
    ],
)
def test_load_failure_modes(tmp_path: Path, content: str | None, code: str, match: str) -> None:
    p = tmp_path / "cfg.json"
    if content is not None:
        p.write_text(content)
    with pytest.raises(hc.ConfigError, match=match) as ei:
        hc.load(p)
    assert str(ei.value).startswith(code)


def test_select_by_id_and_unknown_id() -> None:
    doc = _doc()
    assert [m["family"] for m in hc.select(doc)] == ["power3", "log2"]
    assert hc.select(doc, ["hbt_log2[exch=partial;mode=faithful]"])[0]["family"] == "log2"
    with pytest.raises(hc.ConfigError, match="not in the config"):
        hc.select(doc, ["hbt_log[exch=partial;mode=faithful]"])


class _Asset:
    """Records builder calls like hftbacktest's chainable BacktestAsset."""

    def __init__(self, calls: list | None = None) -> None:
        self.calls = calls if calls is not None else []

    def __getattr__(self, name: str):
        def call(*args):
            return _Asset([*self.calls, (name, args)])

        return call


def test_apply_calls_the_queue_model_then_the_exchange() -> None:
    a = hc.apply(_Asset(), hc.make_entry("power2", "no_partial", [0.1633981666582094], "fitted"))
    assert a.calls == [("power_prob_queue_model2", (0.1633981666582094,)), ("no_partial_fill_exchange", ())]
    b = hc.apply(_Asset(), hc.make_entry("log", "partial", [], "fixed"))
    assert b.calls == [("log_prob_queue_model", ()), ("partial_fill_exchange", ())]
    assert (
        hc.builder_chain(hc.make_entry("power3", "partial", [2.0], "default"))
        == ".power_prob_queue_model3(2.0).partial_fill_exchange()"
    )
