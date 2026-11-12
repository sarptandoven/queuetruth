"""The importer registry: built-ins declared next to their code, typed options, every consumer reading the registry
(drift tests for the lists that remain), and a third-party importer registered through the `queuetruth.importers`
entry point, listed and usable with no change to queuetruth."""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import pytest

import queuetruth as qt
from queuetruth import backtesters
from queuetruth import live as qlive
from queuetruth.errors import QtError
from queuetruth.live import importers
from queuetruth.live.importers import ImporterDef, ImporterOptions, ImportOptions
from queuetruth.schemas import specs as sp

REPO = Path(__file__).resolve().parents[3]
BINANCE = Path(__file__).resolve().parent / "fixtures" / "binance" / "01_ws_execution_report_lifecycle"


# ---- built-ins -------------------------------------------------------------------------------------------------------


def test_builtins_are_declared_next_to_their_code() -> None:
    defs = importers.builtins()
    assert [d.venue for d in defs] == list(importers.live_venues(builtin=True))
    for d in defs:
        mod = sys.modules[d.run.__module__]
        declared = getattr(mod, "IMPORTER", None) or getattr(mod, "IMPORTERS", ())
        assert d is declared or d in tuple(declared), d.venue
        assert d.builtin and d.description and d.formats and d.patterns and d.docs, d.venue
        assert issubclass(d.options, ImporterOptions)
        page, _, _anchor = d.docs.partition("#")
        assert (REPO / "docs" / page).is_file(), d.docs
    assert importers.names("venue") == tuple(qlive.IMPORTERS)
    assert importers.get("hftbacktest", "sim").venue == "sim_hftbacktest"
    assert importers.by_venue("ibkr-flex").name == "ibkr_flex"
    with pytest.raises(QtError, match="no importer for venue 'nope'"):
        importers.by_venue("nope")


def test_compatibility_views_are_read_only_and_match_the_registry() -> None:
    views = {"venue": importers.VENUE_IMPORTERS, "sim": importers.SIM_IMPORTERS, "bot": importers.BOT_IMPORTERS}
    for kind, view in views.items():
        assert dict(view) == {d.name: d.run for d in importers.importers(kind)}
        with pytest.raises(TypeError):
            view["x"] = print  # type: ignore[index]
    assert importers.import_binance is importers.get("binance").run  # the old package-level names still resolve
    assert qlive.SIM_VENUES == {d.venue: d.name for d in importers.importers("sim")}
    assert qlive.BOT_VENUES == {d.venue for d in importers.importers("bot")}
    assert qlive.TZ_VENUES == {"ibkr_flex", "fix44"}


# ---- drift: every list of venues is read from the registry or generated from it ---------------------------------


def test_generated_files_match_the_registry() -> None:
    for rel, text in importers.generated().items():
        assert (REPO / rel).read_text(encoding="utf-8") == text, f"{rel} is stale; run `make generate`"


def test_spec_schema_and_rust_venues_come_from_the_registry() -> None:
    venues = list(importers.live_venues(builtin=True))
    assert list(get_args(sp.LiveImportSpec.model_fields["venue"].annotation)) == venues
    schema = json.loads((REPO / "schemas/json/qt.live_import_spec.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["venue"]["enum"] == venues
    rs = (REPO / "crates/schema/src/live_venue.rs").read_text(encoding="utf-8")
    assert [line.strip().strip('",') for line in rs.split("LIVE_VENUE_CLI_NAMES")[1].splitlines()[1:-1]] == [
        v.replace("_", "-") for v in venues
    ]


def test_backtesters_import_with_the_registry_importers() -> None:
    """Each built-in backtester's `import_sim` is the registry's sim importer of the same name (the backtester
    registry names it by a lazy `module:function` string, so the two cannot drift apart)."""
    sims = {d.name: d for d in importers.importers("sim", builtin=True)}
    assert set(backtesters.builtin_names()) == set(sims)
    for name, d in sims.items():
        assert backtesters.get(name).import_sim.__name__ == d.run.__name__
    for bt in backtesters.builtin_names():
        has_live = backtesters.get(bt).import_live is not None
        assert has_live == (bt in importers.names("bot")), bt


def test_reconcile_kinds_are_backtesters_with_an_importer() -> None:
    from queuetruth.schemas.reconcile import ReconcileReport

    kinds = backtesters.builtin_names(reconcile=True)
    for kind in kinds:
        assert kind in importers.names("sim")
    # `qt.bot_reconcile.kind` is a published enum: it lists exactly the built-ins with a reconciler
    assert set(get_args(ReconcileReport.model_fields["kind"].annotation)) == set(kinds)


def test_cli_plugin_stand_in_takes_options_but_not_fix_flags() -> None:
    from queuetruth.cli import _PLUGIN_STAND_IN

    d = importers.by_venue(_PLUGIN_STAND_IN)
    assert d.builtin and d.options.option_names() - {"fix", "tz"} and "fix" not in d.options.option_names()


@pytest.mark.requires_core
def test_cli_venue_rules_come_from_the_registry() -> None:
    from queuetruth import _core

    base = ["queuetruth", "live", "import", "--in", "x", "--out", "o", "--venue"]
    for d in importers.builtins():
        has_opts = bool(d.options.option_names() - {"fix", "tz"})
        argv = [*base, d.cli_venue, "--option", "k=v"]
        if has_opts:
            assert json.loads(_core.cli_parse(argv))["args"]["venue"] == d.venue
        else:
            with pytest.raises(_core.CliExit):
                _core.cli_parse(argv)
        fix = [*base, d.cli_venue, "--fix-delimiter", "|"]
        if "fix" in d.options.option_names():
            _core.cli_parse(fix)
        else:
            with pytest.raises(_core.CliExit):
                _core.cli_parse(fix)


# ---- typed options -----------------------------------------------------------------------------------------------


def test_each_importer_has_its_own_options() -> None:
    assert importers.get("binance").options.option_names() == frozenset()
    assert importers.get("ibkr_flex").options.option_names() == {"tz", "flex_date_order"}
    assert "order_key_map" in importers.get("hftbacktest", "sim").options.option_names()
    assert "order_key_map" not in importers.get("nautilus", "sim").options.option_names()
    bot, sim = importers.get("freqtrade", "bot"), importers.get("freqtrade", "sim")
    assert sim.options.option_names() - bot.options.option_names() == {"pair_with", "live_dry_run", "live_venue"}
    for d in importers.builtins():  # the shared fields are inherited, never options of their own
        assert {"account", "imap"} <= set(d.options.model_fields)
        assert not d.options.option_names() & ImporterOptions.SHARED


def test_options_pass_through_as_given() -> None:
    d = importers.get("lean", "sim")
    opts = d.import_options("acct", None, timeframe="5m", price_exp="-2", size_exp=-3)
    assert isinstance(opts, ImportOptions) and opts.account == "acct"
    assert opts.extra == {"timeframe": "5m", "price_exp": "-2", "size_exp": -3}
    fix = importers.get("fix44").import_options(fix={"delimiter": "|"})
    assert fix.extra["fix"]["delimiter"] == "|" and fix.extra["fix"]["checksum"] == "exclude"


def test_wrong_options_fail_with_a_clear_error() -> None:
    d = importers.get("vectorbt", "sim")
    with pytest.raises(QtError, match=r"vectorbt importer: unknown option\(s\) \['colum'\]; known: \[.*'column'") as e:
        d.validate(colum="a")
    assert e.value.code == "QT-CONFIG-001" and "docs/reference/importers.md" in (e.value.hint or "")
    with pytest.raises(QtError, match="vectorbt importer: option price_exp: expected integer or text, got True"):
        d.validate(price_exp=True)
    with pytest.raises(QtError, match="binance importer: unknown option"):
        importers.get("binance").validate(tz="UTC")


def test_api_validates_options_against_the_model(tmp_path: Path) -> None:
    spec = {"venue": "binance", "inputs": [{"path": str(BINANCE / "user_data_stream.jsonl")}], "out": str(tmp_path)}
    with pytest.raises(QtError, match=r"binance importer: unknown option\(s\) \['region'\]; known: \[\]"):
        qt.import_live(spec, region="eu")
    sim = {**spec, "venue": "sim_nautilus"}
    with pytest.raises(QtError, match=r"nautilus importer: unknown option\(s\) \['bogus'\]"):
        qt.import_live(sim, bogus=1)


# ---- a third-party importer through the entry point ------------------------------------------------------------

PLUGIN = '''
"""A third-party importer: Binance-shaped fills under a venue of its own, with an option of its own."""

from pydantic import Field

from queuetruth.live import importers
from queuetruth.live.importers import ImporterDef, ImporterOptions, ImportOptions


class ToyVenueOptions(ImporterOptions):
    region: str | None = Field(None, description="the venue's region code")


SEEN = []


def import_toyvenue(paths, opts):
    SEEN.append(dict(opts.extra))
    binance = importers.get("binance")
    return binance.run(paths, ImportOptions(account=opts.account, imap=opts.imap))


IMPORTER = ImporterDef(
    name="toyvenue",
    kind="venue",
    run=import_toyvenue,
    options=ToyVenueOptions,
    formats=("toy fills JSON lines",),
    patterns=("*.jsonl",),
    description="Toy venue: fills of your own account",
)
'''


@pytest.fixture
def toy_importer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Installs the package above into a site directory with the dist-info that declares its entry point."""
    site = tmp_path / "site"
    (site / "qt_toy_importer").mkdir(parents=True)
    (site / "qt_toy_importer" / "__init__.py").write_text(textwrap.dedent(PLUGIN))
    info = site / "qt_toy_importer-0.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: qt-toy-importer\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text("[queuetruth.importers]\ntoyvenue = qt_toy_importer:IMPORTER\n")
    monkeypatch.syspath_prepend(str(site))
    importers.reload()
    yield tmp_path
    sys.modules.pop("qt_toy_importer", None)
    monkeypatch.undo()
    importers.reload()


def test_plugin_is_listed(toy_importer: Path) -> None:
    d = importers.by_venue("toyvenue")
    assert d.origin == "qt-toy-importer:toyvenue" and not d.builtin
    assert importers.live_venues()[-1] == "toyvenue"
    assert "toyvenue" not in importers.live_venues(builtin=True)  # the spec enum and CLI choices stay fixed
    assert importers.VENUE_IMPORTERS["toyvenue"] is d.run
    assert "toyvenue" not in importers.names("venue", builtin=True)


def test_plugin_runs_from_python_with_typed_options(toy_importer: Path) -> None:
    from qt_toy_importer import SEEN  # type: ignore[import-not-found]

    spec = {"venue": "toyvenue", "inputs": [{"path": str(BINANCE / "user_data_stream.jsonl")}]}
    s = qt.import_live({**spec, "out": str(toy_importer / "a")}, region="eu")
    assert s.n_orders > 0 and SEEN[-1] == {"region": "eu"}
    s2 = qlive.import_venue({**spec, "out": str(toy_importer / "b")})
    assert s2.n_orders == s.n_orders
    with pytest.raises(QtError, match=r"toyvenue importer: unknown option\(s\) \['regoin'\]; known: \['region'\]"):
        qt.import_live({**spec, "out": str(toy_importer / "c")}, regoin="eu")
    with pytest.raises(QtError, match="tz: the toyvenue importer reads UTC"):
        qt.import_live({**spec, "out": str(toy_importer / "d"), "tz": "UTC"})


@pytest.mark.requires_core
def test_plugin_runs_from_the_cli(toy_importer: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from queuetruth.cli import main

    out = toy_importer / "cli"
    argv = [
        "--format",
        "json",
        "live",
        "import",
        "--venue",
        "toyvenue",
        "--in",
        str(BINANCE / "user_data_stream.jsonl"),
    ]
    rc = main([*argv, "--out", str(out), "--option", "region=eu"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0, doc
    assert doc["n_orders"] > 0 and (out / "manifest.json").is_file()
    rc = main([*argv, "--out", str(toy_importer / "bad"), "--option", "nope=1"])
    err = capsys.readouterr()
    assert rc == 2 and "unknown option(s) ['nope']" in err.out + err.err


def test_plugin_cannot_replace_a_builtin(tmp_path: Path) -> None:
    d = ImporterDef(name="binance", kind="venue", run=lambda p, o: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(QtError, match="already registered"):
        importers.register(d)


def test_register_and_invalid_entries() -> None:
    d = ImporterDef(name="inline_toy", kind="sim", run=lambda p, o: None)  # type: ignore[arg-type,return-value]
    importers.register(d)
    try:
        assert importers.by_venue("sim_inline_toy").origin == "register"
        assert "inline_toy" in importers.SIM_IMPORTERS
    finally:
        importers.unregister("sim_inline_toy")
    assert "sim_inline_toy" not in importers.registry()
    with pytest.raises(TypeError, match="lowercase"):
        importers.register(ImporterDef(name="Bad-Name", kind="venue", run=print))
    with pytest.raises(TypeError, match="kind must be one of"):
        importers.register(ImporterDef(name="x", kind="exchange", run=print))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="options must be an ImporterOptions subclass"):
        importers.register(ImporterDef(name="x", kind="venue", run=print, options=dict))  # type: ignore[arg-type]


def test_broken_plugin_is_skipped_with_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = tmp_path / "site"
    info = site / "qt_broken-0.1.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: qt-broken\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text("[queuetruth.importers]\nbroken = qt_no_such_module:IMPORTER\n")
    monkeypatch.syspath_prepend(str(site))
    importers.reload()
    try:
        with pytest.warns(importers.ImporterWarning, match="not loaded"):
            reg = importers.registry()
        assert "broken" not in reg and "binance" in reg
    finally:
        monkeypatch.undo()
        importers.reload()
