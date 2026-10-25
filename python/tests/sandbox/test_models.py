"""Sandbox documents and spec: JSON Schemas in the repo's conventions, real documents validating against them, spec
validation and normalization, and the exact helpers (durations, times, fixed point, rendering)."""

from __future__ import annotations

import random
from decimal import Decimal

import jsonschema
import pytest

from queuetruth.errors import QtError
from queuetruth.sandbox import (
    SANDBOX_DOC_MODELS,
    Sandbox,
    SandboxSpec,
    json_schemas,
    normalize_spec,
    parse_sandbox_document,
    render,
    verify,
)
from queuetruth.sandbox._util import (
    fmt_duration,
    fmt_fixed,
    local_time,
    money,
    parse_duration,
    parse_time,
    tick_decimals,
    to_fixed,
    wire,
)
from queuetruth.sandbox.scorecard import finish_summary
from sandbox.sbx_helpers import FIXTURES, SYNTH, random_agent

MID = 1_781_481_600_000_000_000


def test_schemas_follow_the_document_conventions() -> None:
    schemas = json_schemas()
    assert set(schemas) == {*SANDBOX_DOC_MODELS, "qt.sandbox_spec"}
    for name in SANDBOX_DOC_MODELS:
        s = schemas[name]
        jsonschema.Draft202012Validator.check_schema(s)
        assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema" and s["title"] == name
        assert s["required"][:2] == ["schema", "schema_version"]
        assert list(s["properties"])[:2] == ["schema", "schema_version"]
        assert s["properties"]["schema"]["const"] == name and s["additionalProperties"] is False
    spec = schemas["qt.sandbox_spec"]
    assert spec["required"] == ["dataset"] and spec["additionalProperties"] is False


@pytest.mark.requires_core
def test_real_documents_validate_against_their_schemas(tmp_path) -> None:
    schemas = json_schemas()
    docs = []
    with Sandbox.open(
        {
            "dataset": {"path": str(SYNTH)},
            "instrument_id": 1,
            "session_date": "2026-06-16",
            "start": "14:00",
            "end": "14:05",
            "latency": "1ms",
        }
    ) as sb:
        docs.append(sb.session.model_dump(mode="json"))
        docs.append(sb.submit("buy", 1, peg_offset_ticks=0).model_dump(mode="json"))
        docs.extend(o.model_dump(mode="json") for o in random_agent(sb, random.Random(1), steps=10))
        docs.append(sb.cancel("o1").model_dump(mode="json") if not sb.orders[0].final else docs[-1])
        card = sb.finish(out=tmp_path / "o")
        docs.append(card.model_dump(mode="json"))
        docs.append(sb.order_log())
    docs.append(verify(tmp_path / "o" / "order_log.json").model_dump(mode="json"))
    for detail in ("summary", "full"):
        docs.append(finish_summary(card, session_id=None, out_dir=None, detail=detail).model_dump(mode="json"))
    from queuetruth.mcp.sandbox_tools import SandboxTools

    tools = SandboxTools(root=FIXTURES)
    docs.append(tools.list_doc())
    seen = set()
    for d in docs:
        jsonschema.Draft202012Validator(schemas[d["schema"]]).validate(d)
        assert parse_sandbox_document(d).model_dump(mode="json") == d
        seen.add(d["schema"])
    assert seen == set(SANDBOX_DOC_MODELS)
    for bad in ({**docs[0], "schema": "qt.other"}, {**docs[0], "extra": 1}, {**docs[0], "schema_version": 2}):
        assert not jsonschema.Draft202012Validator(schemas["qt.sandbox_session"]).is_valid(bad)
    with pytest.raises(ValueError):
        parse_sandbox_document({"schema": "qt.nope"})


@pytest.mark.requires_core
def test_price_conversions_of_a_session() -> None:
    spec = {"dataset": {"path": str(SYNTH)}, "instrument_id": 1, "session_date": "2026-06-15", "start": "13:35"}
    with Sandbox.open({**spec, "end": "13:36"}) as sb:
        assert sb.to_raw("5001.25") == 5_001_250_000_000 and sb.to_raw(Decimal("5001.25")) == 5_001_250_000_000
        assert sb.to_price(5_001_250_000_000) == Decimal("5001.25") and sb.to_raw(42) == 42
        with pytest.raises(QtError):
            sb.to_raw(5001.25)


def test_spec_defaults_and_validation() -> None:
    s = SandboxSpec.model_validate({"dataset": {"path": "d"}})
    assert (s.latency_ns, s.depth_levels, s.max_trades, s.markout_horizons_ns) == (
        0,
        5,
        20,
        [10**9, 10**10, 6 * 10**10],
    )
    assert s.self_trade_prevention == "reject_new" and s.fees.maker_bps == 0.0
    for bad in (
        {"bogus": 1},
        {"latency_ns": -1},
        {"depth_levels": 0},
        {"markout_horizons_ns": []},
        {"markout_horizons_ns": [5, 5]},
        {"start_ns": 10, "end_ns": 10},
        {"market_collar_bps": 0},
        {"fees": {"taker_bps": float("inf")}},
        {"fees": {"rebate": 1}},
        {"session_date": "2026-6-1"},
        {"self_trade_prevention": "cancel_both"},
    ):
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            SandboxSpec.model_validate({"dataset": {"path": "d"}, **bad})


def test_normalize_spec() -> None:
    raw = {
        "dataset": {"path": "d"},
        "start": "09:30",
        "end_ns": "16:00:00.5",
        "latency": "250us",
        "jitter_ns": "1ms",
        "feed_latency": 7,
        "markout_horizons": ["1s", 5],
    }
    out = normalize_spec(raw, MID)
    assert out == {
        "dataset": {"path": "d"},
        "start_ns": MID + 34_200 * 10**9,
        "end_ns": MID + 57_600 * 10**9 + 5 * 10**8,
        "latency_ns": 250_000,
        "jitter_ns": 1_000_000,
        "feed_latency_ns": 7,
        "markout_horizons_ns": [10**9, 5],
    }
    assert normalize_spec({"start": "10:00"}, None) == {"start_ns": None}
    for bad in (
        {"jitter": "1ms", "jitter_ns": 2},
        {"start": "1", "start_ns": 2},
        {"markout_horizons": [], "markout_horizons_ns": []},
    ):
        with pytest.raises(QtError) as e:
            normalize_spec(bad, MID)
        assert e.value.code == "QT-CONFIG-001"


@pytest.mark.parametrize(
    ("value", "ns"),
    [
        (0, 0),
        (7, 7),
        ("7ns", 7),
        ("250us", 250_000),
        ("100ms", 10**8),
        ("2s", 2 * 10**9),
        ("5m", 3 * 10**11),
        (" 1h ", 3600 * 10**9),
    ],
)
def test_parse_duration(value, ns) -> None:
    assert parse_duration(value, "x") == ns


@pytest.mark.parametrize("value", ["5", "1.5s", "-1s", "1d", "", True, 1.5, -1, None, "9" * 30 + "h"])
def test_parse_duration_rejects(value) -> None:
    with pytest.raises(QtError) as e:
        parse_duration(value, "x")
    assert e.value.code == "QT-CONFIG-001"


def test_parse_time_and_local_time() -> None:
    assert parse_time("09:30", MID, "t") == MID + 34_200 * 10**9
    assert parse_time("9:30:01.25", MID, "t") == MID + 34_201_250_000_000
    assert parse_time(123, MID, "t") == 123
    for bad in ("9.30", "24:60", "10:00:61", True, 1.0):
        with pytest.raises(QtError):
            parse_time(bad, MID, "t")
    assert local_time(MID + 34_201_250_000_000, MID) == "09:30:01.250000"
    assert local_time(MID - 10**9, MID) == "-00:00:01.000000"


def test_fixed_point_helpers() -> None:
    assert to_fixed("291.65", -9, "p") == 291_650_000_000
    assert to_fixed(" 5000.25 ", -9, "p") == 5_000_250_000_000
    assert to_fixed(Decimal("0.000000001"), -9, "p") == 1
    assert to_fixed(42, -9, "p") == 42  # integers are already fixed point
    assert to_fixed("-1.5", -1, "p") == -15
    with pytest.raises(QtError) as e:
        to_fixed("0.0000000001", -9, "p")
    assert e.value.code == "QT-INPUT-004"
    for bad in ("1e999999999", "nan", "abc", 1.5, True):
        with pytest.raises(QtError):
            to_fixed(bad, -9, "p")
    assert fmt_fixed(291_650_000_000, -9, 2) == "291.65" and fmt_fixed(5_000_000_000_000, -9) == "5000"
    assert fmt_fixed(None, -9) == "-"
    assert tick_decimals(10_000_000, -9) == 2 and tick_decimals(250_000_000, -9) == 2 and tick_decimals(1, -8) == 8
    assert tick_decimals(10**9, -9) == 0 and tick_decimals(0, -4) == 4
    assert money(20_000_000, -9, 10.0) == Decimal("0.2")
    assert float(money(Decimal(-25_000_000) / 2, -9, 50.0)) == -0.625


@pytest.mark.requires_core
def test_text_renderings(tmp_path) -> None:
    with Sandbox.open(
        {
            "dataset": {"path": str(SYNTH)},
            "instrument_id": 1,
            "session_date": "2026-06-15",
            "start": "13:35",
            "end": "13:40",
            "latency": "1ms",
        }
    ) as sb:
        text = render.session_text(sb.session.model_dump(mode="json"))
        assert "QTAU6" in text and "13:35:00.000000" in text and "5001.00" in text and "rules:" in text
        assert text.startswith("sandbox session on QTAU6") and "latency 1ms, feed latency 0" in text
        ack = sb.submit("buy", 2, price="5001.00")
        assert "o1 BUY 2 @ 5001.00 day pending" in render.ack_text(ack.model_dump(mode="json"), sb.instrument)
        sb.submit("buy", 1, type="market")
        obs = sb.advance(by="30s").model_dump(mode="json")
        text = render.observation_text(obs, sb.instrument, sb.end_ns)
        assert text.startswith("step 1 | 13:35:30.000000") and "book: bid" in text and "position" in text
        assert "orders: o1 BUY 2 @ 5001.00 day filled; o2 BUY 1 @ MKT ioc filled\n" in text  # no "filled filled"
        assert "vwap 5001.1204" in text
        card_model = sb.finish()
        card = card_model.model_dump(mode="json")
    text = render.scorecard_text(card)
    assert card["run_id"] in text and "consistency with one full exact replay: EQUAL" in text
    summary = finish_summary(card_model, session_id="sbx_0123456789abcdef", out_dir="/out/x").model_dump(mode="json")
    assert render.scorecard_text(summary).splitlines()[:4] == text.splitlines()[:4]
    assert "written to /out/x" in render.scorecard_text(summary)


def test_wire_forms_and_price_helpers() -> None:
    import numpy as np

    assert [wire(v) for v in (None, True, 3, "x", 2.0, Decimal("1.50"))] == [None, True, 3, "x", 2.0, "1.50"]
    assert [wire(v) for v in (float("nan"), float("inf"), -float("inf"))] == ["nan", "inf", "-inf"]
    assert wire(np.int64(7)) == 7 and type(wire(np.int64(7))) is int and type(wire(np.float32(0.5))) is float
    assert wire(np.bool_(True)) == repr(np.bool_(True)) and wire([1]) == "[1]"
    assert [fmt_duration(x) for x in (0, 1, 1_000, 250_000, 10**6, 1_500_000, 5 * 10**9, 60 * 10**9)] == [
        "0",
        "1ns",
        "1us",
        "250us",
        "1ms",
        "1500us",
        "5s",
        "1m",
    ]
