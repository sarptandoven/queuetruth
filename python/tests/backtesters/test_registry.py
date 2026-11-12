"""The `Backtester` plugin interface: built-ins, CLI names, detection, and a third-party plugin found through the
`queuetruth.backtesters` entry point and run by `check`, `import_sim` and the CLI with no change to queuetruth."""

from __future__ import annotations

import shutil
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import queuetruth as qt
from queuetruth import backtesters
from queuetruth.errors import QtError

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DATA = REPO / "tests" / "fixtures" / "synth_small"


def test_builtins_and_their_parts() -> None:
    reg = backtesters.registry()
    assert list(reg)[:7] == ["hftbacktest", "nautilus", "backtrader", "vectorbt", "lean", "freqtrade", "hummingbot"]
    for b in reg.values():
        assert isinstance(b, backtesters.Backtester)
    assert backtesters.get("lean").label == "QuantConnect LEAN"
    assert backtesters.get("hftbacktest").replay is not None and backtesters.get("nautilus").snippet is not None
    assert backtesters.get("backtrader").replay is None
    assert "nt_prob_touch" in backtesters.get("nautilus").zoo_models
    assert ("hftbacktest", "hbt_l3_fifo") in set(backtesters.zoo_models())
    assert backtesters.get("freqtrade").import_live is not None and backtesters.get("lean").import_live is None
    assert list(reg)[7] == "backtesting_py" and backtesters.get("backtesting_py").label == "backtesting.py"
    with pytest.raises(QtError, match="no backtester 'nope'"):
        backtesters.get("nope")
    assert qt.backtesters is backtesters


def test_cli_names_are_unchanged() -> None:
    from queuetruth.check.detect import BACKTESTERS
    from queuetruth.live import SIM_VENUES
    from queuetruth.live.importers import SIM_IMPORTERS

    assert list(BACKTESTERS) == [
        "hftbacktest", "nautilus", "backtrader", "vectorbt", "lean", "freqtrade", "backtesting_py"
    ]  # fmt: skip
    assert backtesters.builtin_names(reconcile=True) == ("freqtrade", "hummingbot", "backtrader", "vectorbt", "lean")
    assert SIM_VENUES == {f"sim_{k}": k for k in SIM_IMPORTERS}


def test_every_builtin_imports_through_the_registry(tmp_path: Path) -> None:
    """The registry's importers are the importers of `queuetruth.live.importers` (lazily bound)."""
    from queuetruth.live.importers import SIM_IMPORTERS

    for name, fn in SIM_IMPORTERS.items():
        entry = backtesters.get(name).import_sim
        assert entry.__name__ == fn.__name__


def test_names_accept_registered_plugins() -> None:
    b = backtesters.BacktesterEntry(name="inline_toy", label="Inline", import_sim=lambda p, o: None, detect=None)
    backtesters.register(b)
    try:
        assert "inline_toy" in backtesters.Names()
        assert "inline_toy" not in backtesters.Names(check=True)
        assert "inline_toy" not in list(backtesters.Names())  # help text lists the built-ins only
        with pytest.raises(QtError, match="already registered"):
            backtesters.register(b)
    finally:
        backtesters.unregister("inline_toy")
    assert "inline_toy" not in backtesters.registry()


def test_invalid_entries_are_refused() -> None:
    with pytest.raises(TypeError, match="missing import_sim"):
        backtesters.register(backtesters.BacktesterEntry(name="x", label="x", import_sim=None))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="lowercase"):
        backtesters.register(backtesters.BacktesterEntry(name="Bad-Name", label="x", import_sim=print))


# ---- a third-party plugin through the entry point ----------------------------------------------------------------


def _install_plugin(root: Path, value: str = "qt_toy_backtester:BACKTESTER", name: str = "toy") -> Path:
    """Copies the toy plugin package to `root` and writes the dist-info that declares its entry point."""
    shutil.copytree(HERE / "toy_plugin" / "qt_toy_backtester", root / "qt_toy_backtester")
    info = root / "qt_toy_backtester-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: qt-toy-backtester\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text(f"[queuetruth.backtesters]\n{name} = {value}\n")
    return root


@pytest.fixture
def toy_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.syspath_prepend(str(_install_plugin(tmp_path / "site")))
    backtesters.reload()
    yield tmp_path
    sys.modules.pop("qt_toy_backtester", None)
    monkeypatch.undo()
    backtesters.reload()


def _write_toy(path: Path) -> Path:
    import sys as _sys

    _sys.path.insert(0, str(REPO / "python" / "tests" / "check"))
    try:
        import synth_backtests as sb
    finally:
        _sys.path.pop(0)
    rows = ["toy_ref,toy_symbol,toy_side,toy_price,toy_qty,toy_tick,toy_placed_ns,toy_done_ns,toy_filled"]
    for o in sb.orders(DATA):
        side = "B" if o.side == 1 else "S"
        rows.append(f"{o.oid},{o.symbol},{side},{o.price:.2f},{o.qty},0.25,{o.ts},{o.ts_end},{int(o.filled)}")
    path.write_text("\n".join(rows) + "\n")
    return path


def test_plugin_is_found_through_the_entry_point(toy_plugin: Path) -> None:
    b = backtesters.get("toy")
    assert b.label == "Toy backtester"
    assert backtesters.names(check=True)[-1] == "toy"
    assert "toy" in backtesters.Names(check=True)
    toy = _write_toy(toy_plugin / "toy_orders.csv")
    assert backtesters.detect(toy) == "toy"


@pytest.mark.requires_core
def test_check_runs_a_plugin_with_no_core_edit(toy_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from queuetruth import cli

    toy = _write_toy(toy_plugin / "toy_orders.csv")
    res = qt.check(toy, data=DATA, out=toy_plugin / "out", window="10s")
    assert res.doc["backtester"] == "toy"
    assert res.doc["n_limit"] == 40
    ref = qt.check(_hbt(toy_plugin), data=DATA, out=toy_plugin / "ref", window="10s")
    assert res.summary == ref.summary  # the same orders as hftbacktest snapshots give the same answer

    code = cli.main(["check", str(toy), "--data", str(DATA), "--backtester", "toy", "--out", str(toy_plugin / "c")])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.splitlines()[0] == qt.check(toy, data=DATA, out=toy_plugin / "d").summary

    from queuetruth.live import import_sim

    summary = import_sim("toy", [str(toy)], toy_plugin / "log")
    assert summary.venue == "sim:toy"


def _hbt(root: Path) -> Path:
    sys.path.insert(0, str(REPO / "python" / "tests" / "check"))
    try:
        import synth_backtests as sb
    finally:
        sys.path.pop(0)
    return sb.write_hftbacktest(root / "hbt.jsonl", sb.orders(DATA))


def test_broken_or_clashing_plugins_warn_and_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = tmp_path / "site"
    site.mkdir()
    for dist, line in (("qt_broken", "broken = qt_no_such_module:X"), ("qt_clash", "lean = qt_toy_backtester:LEAN")):
        info = site / f"{dist}-0.1.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1\n")
        (info / "entry_points.txt").write_text(f"[queuetruth.backtesters]\n{line}\n")
    shutil.copytree(HERE / "toy_plugin" / "qt_toy_backtester", site / "qt_toy_backtester")
    (site / "qt_toy_backtester" / "__init__.py").open("a").write(
        "\nLEAN = BacktesterEntry(name='lean', label='Not LEAN', import_sim=import_toy)\n"
    )
    monkeypatch.syspath_prepend(str(site))
    backtesters.reload()
    try:
        with warnings.catch_warnings(record=True) as got:
            warnings.simplefilter("always")
            reg = backtesters.registry()
        msgs = [str(w.message) for w in got if issubclass(w.category, backtesters.BacktesterWarning)]
        assert any("broken" in m and "not loaded" in m for m in msgs), msgs
        assert any("'lean' is already registered" in m for m in msgs), msgs
        assert reg["lean"].label == "QuantConnect LEAN"
        assert "broken" not in reg
    finally:
        sys.modules.pop("qt_toy_backtester", None)
        monkeypatch.undo()
        backtesters.reload()


def test_reconcile_dispatch_goes_through_the_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`reconcile --kind NAME` calls the registered reconciler (a plugin's as well as a built-in's)."""
    from queuetruth.live import reconcile_cli

    seen: dict[str, Any] = {}

    class Report:
        orders: tuple[Any, ...] = ()

        def model_dump(self, **_: Any) -> dict[str, Any]:
            return {"schema": "qt.bot_reconcile", "warnings": []}

    def reconcile(sim: list[str], live: list[str], out: str, **kw: Any) -> Report:
        seen.update(sim=sim, live=live, out=out, kw=kw)
        return Report()

    monkeypatch.setattr("queuetruth.live.importers.freqtrade.render_reconcile", lambda r, max_rows: "rendered")
    backtesters.register(
        backtesters.BacktesterEntry(name="recon_toy", label="R", import_sim=print, reconcile=reconcile)
    )
    try:
        args = reconcile_cli.parser().parse_args(
            ["reconcile", "--kind", "recon_toy", "--sim", "a", "--live", "b", "--out", str(tmp_path), "--no-score"]
        )
        _, text = reconcile_cli.run(args)
    finally:
        backtesters.unregister("recon_toy")
    assert text == "rendered" and seen["sim"] == ["a"] and seen["kw"]["score"] is False
    with pytest.raises(QtError, match="invalid choice"):
        reconcile_cli.parser().parse_args(
            ["reconcile", "--kind", "recon_toy", "--sim", "a", "--live", "b", "--out", "x"]
        )
