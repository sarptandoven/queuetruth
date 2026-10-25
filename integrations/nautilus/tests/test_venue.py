"""Objects built for the installed NautilusTrader (skipped without nautilus_trader)."""

from __future__ import annotations

import re
import warnings

import pytest

nautilus_trader = pytest.importorskip("nautilus_trader")

from conftest import FIXTURES, exportable, variant  # noqa: E402

import queuetruth_nautilus as qtn  # noqa: E402
from queuetruth_nautilus import compat  # noqa: E402

NT2 = int(nautilus_trader.__version__.split(".")[0]) >= 2


def fill_params(fm) -> tuple[float, float, int | None]:
    """(prob_fill_on_limit, prob_slippage, random_seed) of a fill model; 2.x exposes them only in its repr, 1.x does
    not expose the seed (None is returned for it)."""
    if NT2:
        r = repr(fm)
        p = float(re.search(r"prob_fill_on_limit: ([0-9.e-]+)", r).group(1))
        s = float(re.search(r"prob_slippage: ([0-9.e-]+)", r).group(1))
        m = re.search(r"random_seed: Some\((\d+)\)", r)
        return p, s, int(m.group(1)) if m else None
    return fm.prob_fill_on_limit, fm.prob_slippage, None


def lat_params(lm) -> tuple[int, int, int, int]:
    if NT2:
        r = repr(lm)
        vals = [int(x) for x in re.findall(r"_latency_nanos: \w+\((\d+)\)", r)]
        assert len(vals) == 4, r
        return tuple(vals)  # base, insert, update, delete(=cancel)
    return lm.base_latency_nanos, lm.insert_latency_nanos, lm.update_latency_nanos, lm.cancel_latency_nanos


def test_installed_version_is_verified():
    v = compat.installed_version()
    assert v.verified
    assert v.text == nautilus_trader.__version__


def test_fill_model_class_and_parameters(touch_doc):
    cal = qtn.from_document(touch_doc)
    fm = qtn.fill_model(cal, random_seed=42, prob_slippage=0.25)
    assert type(fm).__name__ == ("DefaultFillModel" if NT2 else "FillModel")
    p, s, seed = fill_params(fm)
    assert p == 0.28662597114317423  # full precision, not the export's 6 digits
    assert s == 0.25
    if NT2:
        assert seed == 42
    assert "prob_fill_on_limit=0.28662597114317423" in compat.describe(fm) or NT2


def test_fill_model_from_snippet(touch_doc):
    cal = qtn.load_calibration(FIXTURES / "cal_touch.export.txt")
    assert fill_params(qtn.fill_model(cal))[0] == 0.286626


@pytest.mark.parametrize("seed", [-1, 2**64, 1.5, True, "3"])
def test_bad_seed(touch_doc, seed):
    with pytest.raises(qtn.QtNautilusError, match="random_seed") as ei:
        qtn.fill_model(qtn.from_document(touch_doc), random_seed=seed)
    assert ei.value.code == "QT-CONFIG-001"


def test_seed_bounds_accepted(touch_doc):
    cal = qtn.from_document(touch_doc)
    qtn.fill_model(cal, random_seed=0)
    qtn.fill_model(cal, random_seed=2**64 - 1)


@pytest.mark.parametrize("slip", [-0.1, 1.1, float("nan"), "0.1", None])
def test_bad_slippage(touch_doc, slip):
    with pytest.raises(qtn.QtNautilusError, match="prob_slippage") as ei:
        qtn.fill_model(qtn.from_document(touch_doc), prob_slippage=slip)
    assert ei.value.code == "QT-CONFIG-001"


def test_venue_options_touch(touch_doc):
    o = qtn.venue_options(qtn.from_document(touch_doc), random_seed=1)
    assert set(o) == {"fill_model", "book_type", "trade_execution", "queue_position"}
    assert o["queue_position"] is False and o["trade_execution"] is True
    assert o["book_type"] == compat.book_type_l2()


def test_venue_options_latency(latency_doc):
    cal = qtn.from_document(latency_doc)
    with pytest.warns(qtn.CalibrationWarning, match="update_latency_nanos not fitted"):
        o = qtn.venue_options(cal)
    assert set(o) == {"fill_model", "latency_model", "book_type", "trade_execution", "queue_position"}
    assert type(o["latency_model"]).__name__ == ("StaticLatencyModel" if NT2 else "LatencyModel")
    assert lat_params(o["latency_model"]) == (0, 250400, 0, 180000)


def test_latency_only_calibration(latency_doc):
    cal = qtn.from_document(variant(latency_doc, family="latency", kind="latency", estimate=None))
    assert cal.prob_fill_on_limit is None
    with pytest.warns(qtn.CalibrationWarning):
        o = qtn.venue_options(cal)
    assert set(o) == {"latency_model"}


def test_core_export_latency_line_fails_but_package_builds(latency_doc):
    """queuetruth 0.1.1's `LatencyModel(..., delete_latency_nanos=...)` is rejected by both release lines."""
    cls = compat.latency_model_cls()
    with pytest.raises(TypeError, match="delete_latency_nanos"):
        cls(base_latency_nanos=0, insert_latency_nanos=250400, delete_latency_nanos=180000)
    cal = qtn.load_calibration(FIXTURES / "cal_touch_latency.export.txt")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", qtn.CalibrationWarning)
        lm = qtn.latency_model(cal)
    assert lat_params(lm) == (0, 250400, 0, 180000)


def test_nothing_usable(touch_doc):
    cal = qtn.from_document(variant(touch_doc, family="hbt_power3", kind="alloc", estimate=3.1))
    with pytest.raises(qtn.QtNautilusError, match="nothing NautilusTrader can use") as ei:
        qtn.venue_options(cal)
    assert ei.value.code == "QT-CONFIG-001"
    assert qtn.latency_model(cal) is None
    with pytest.raises(qtn.QtNautilusError, match="fits no NautilusTrader prob_fill_on_limit"):
        qtn.fill_model(cal)


def test_corrected_mode_warns(touch_doc):
    cal = qtn.from_document(variant(touch_doc, mode="corrected"))
    with pytest.warns(qtn.CalibrationWarning, match="'corrected' mode"):
        qtn.fill_model(cal)


def test_queue_gated_warns_on_2x_only(queue_doc):
    cal = qtn.from_document(exportable(queue_doc))
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        qtn.fill_model(cal)
    msgs = [str(x.message) for x in w if issubclass(x.category, qtn.CalibrationWarning)]
    assert any("1.231.0 queue rules" in m for m in msgs) == NT2


def zero_fee() -> dict:
    """`fee_model` for a 2.x venue (required there); nothing on 1.x."""
    if not NT2:
        return {}
    from nautilus_trader.execution import FixedFeeModel
    from nautilus_trader.model import Currency, Money

    return {"fee_model": FixedFeeModel(Money(0, Currency.from_str("USD")))}


def test_venue_config(touch_doc, latency_doc):
    cal = qtn.from_document(latency_doc)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", qtn.CalibrationWarning)
        cfg = qtn.venue_config(
            cal, "XNAS", "NETTING", "MARGIN", ["1000000 USD"], random_seed=9, bar_execution=False, **zero_fee()
        )
    assert cfg.queue_position is False and cfg.trade_execution is True and cfg.bar_execution is False
    if NT2:
        assert "prob_fill_on_limit: 0.28662597114317423" in repr(cfg.fill_model)
        assert lat_params(cfg.latency_model) == (0, 250400, 0, 180000)
    else:
        from nautilus_trader.backtest.config import FillModelFactory, LatencyModelFactory

        fm = FillModelFactory.create(cfg.fill_model)
        assert (fm.prob_fill_on_limit, fm.prob_slippage) == (0.28662597114317423, 0.0)
        assert lat_params(LatencyModelFactory.create(cfg.latency_model)) == (0, 250400, 0, 180000)
        assert str(cfg.book_type).endswith("L2_MBP")


def test_venue_config_rejects_calibrated_keywords(touch_doc):
    with pytest.raises(qtn.QtNautilusError, match="queue_position") as ei:
        qtn.venue_config(qtn.from_document(touch_doc), "XNAS", "NETTING", "MARGIN", ["1 USD"], queue_position=True)
    assert ei.value.code == "QT-CONFIG-001"


@pytest.mark.skipif(not NT2, reason="1.x venues need no fee model")
def test_venue_config_2x_requires_fee_model(touch_doc):
    """A 2.x backtest venue refuses to start without a fee model (checked by `test_venue_config_runs_in_node`)."""
    with pytest.raises(qtn.QtNautilusError, match="explicit fee_model") as ei:
        qtn.venue_config(qtn.from_document(touch_doc), "XNAS", "NETTING", "MARGIN", ["1 USD"], random_seed=1)
    assert ei.value.code == "QT-CONFIG-001" and "FixedFeeModel" in ei.value.hint


def test_venue_config_runs_in_node(tmp_path, latency_doc):
    """The config drives a real `BacktestNode` run (venue, fill, latency and fee models built by the engine)."""
    from ntkit import run_node

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", qtn.CalibrationWarning)
        cfg = qtn.venue_config(
            qtn.from_document(latency_doc), "XNAS", "NETTING", "MARGIN", ["1000000 USD"], random_seed=3, **zero_fee()
        )
    results = run_node(cfg, str(tmp_path / "catalog"))
    assert len(results) == 1


def _requires_fee_model() -> bool:
    """nautilus_trader 2.0.0rc7 and later refuse a backtest venue without a fee model (rc4 and rc5 accept one)."""
    v = compat.installed_version()
    return v.major >= 2 and (v.pre is None or (v.major, v.minor, v.patch) > (2, 0, 0) or v.pre >= ("rc", 7))


@pytest.mark.skipif(not _requires_fee_model(), reason="this release accepts a venue without a fee model")
def test_node_without_fee_model_fails_on_2x(tmp_path, touch_doc):
    """Why `venue_config` insists on `fee_model` on 2.x: from 2.0.0rc7 the engine refuses the venue at run time
    (the package requires it on every 2.x release, so code written for one release candidate runs on the next)."""
    from nautilus_trader.backtest import BacktestVenueConfig
    from ntkit import run_node

    fm = qtn.fill_model(qtn.from_document(touch_doc), random_seed=1)
    cfg = BacktestVenueConfig(
        name="XNAS", oms_type="NETTING", account_type="MARGIN", starting_balances=["1 USD"], fill_model=fm
    )
    with pytest.raises(Exception, match="explicit fee_model"):
        run_node(cfg, str(tmp_path / "catalog"))


def caught(fn, *args, **kwargs) -> list[warnings.WarningMessage]:
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        fn(*args, **kwargs)
    return [x for x in w if issubclass(x.category, qtn.CalibrationWarning)]


def test_forced_latency_calibration_warns_once_at_the_caller(latency_doc):
    """One trust check per public call: a forced calibration with a latency fit warns NOT VALIDATED once, and every
    warning points at this file (the caller), not at the package."""
    d = variant(latency_doc, trust={"verdict": "refused", "reasons": ["few_events"], "flags": [], "beats_default": {}})
    cal = qtn.from_document(d)
    for fn, kw in (
        (qtn.venue_options, {"random_seed": 1}),
        (qtn.venue_config, {"random_seed": 1, **zero_fee()}),
    ):
        args = (cal,) if fn is qtn.venue_options else (cal, "XNAS", "NETTING", "MARGIN", ["1 USD"])
        w = caught(fn, *args, force=True, **kw)
        assert sum("NOT VALIDATED" in str(x.message) for x in w) == 1, [str(x.message) for x in w]
        assert sum("update_latency_nanos not fitted" in str(x.message) for x in w) == 1
        assert {x.filename for x in w} == {__file__}


def test_seed_warning(touch_doc):
    cal = qtn.from_document(touch_doc)
    assert any("random_seed not set" in str(x.message) for x in caught(qtn.fill_model, cal))
    assert not caught(qtn.fill_model, cal, random_seed=1)
    assert not caught(qtn.fill_model, qtn.from_document(exportable(touch_doc, estimate=1.0)))  # deterministic


def test_latency_only_on_unverified_version_warns(monkeypatch, latency_doc):
    monkeypatch.setattr(nautilus_trader, "__version__", "2.1.0" if NT2 else "1.232.0")
    cal = qtn.from_document(variant(latency_doc, family="latency", kind="latency", estimate=None))
    w = caught(qtn.latency_model, cal)
    assert any("has not been cross-checked" in str(x.message) for x in w)


def test_snippet_mode_unknown_warns(touch_doc):
    text = (FIXTURES / "cal_touch.export.txt").read_text()
    assert qtn.from_snippet(text).mode == "faithful"  # read from the baseline the export names
    cal = qtn.from_snippet("\n".join(ln for ln in text.splitlines() if not ln.startswith("# held-out:")))
    assert cal.mode == "unknown"
    w = caught(qtn.fill_model, cal, random_seed=1)
    assert any("does not name the fit's mode" in str(x.message) for x in w)


def test_venue_config_refused(queue_doc):
    with pytest.raises(qtn.CalibrationRefused):
        qtn.venue_config(qtn.from_document(queue_doc), "XNAS", "NETTING", "MARGIN", ["1 USD"])


@pytest.mark.parametrize("fixture", ["cal_touch.json", "cal_touch_latency.json"])
def test_rendered_snippet_runs(fixture):
    """The code `render_snippet` writes for the installed major version executes, builds the same objects, and its
    `venue_options` go through `BacktestEngine.add_venue` into a run that fills orders."""
    from ntkit import run_episodes

    cal = qtn.load_calibration(FIXTURES / fixture)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", qtn.CalibrationWarning)
        text = qtn.render_snippet(cal, compat.installed_version().major, random_seed=5)
    ns: dict = {}
    exec(compile(text, fixture, "exec"), ns)
    assert fill_params(ns["fill_model"])[0] == cal.prob_fill_on_limit
    assert ns["venue_options"]["queue_position"] is False
    assert ns["venue_options"]["book_type"] == compat.book_type_l2()
    if cal.latency is not None:
        assert lat_params(ns["latency_model"]) == (0, 250400, 0, 180000)
    # trade-throughs fill every order; 10 ms episodes leave room for the fitted 250 us insert latency
    filled = run_episodes(ns["venue_options"], 40, [(-1, 100)], step=10_000_000)
    assert filled == set(range(40))


def _readme_use_block() -> str:
    readme = (FIXTURES.parents[1] / "README.md").read_text(encoding="utf-8")
    use = readme[readme.index("## Use") :]
    return use[use.index("```python") + len("```python") : use.index("```", use.index("```python") + 3)]


def test_readme_example_runs(touch_doc, tmp_path):
    """The README's `add_venue` example, verbatim on 2.x (on 1.x as its `# 1.x:` comments say), runs a backtest in
    which orders fill."""
    from ntkit import run_episodes

    code = _readme_use_block()
    path = tmp_path / "cal" / "calibration.json"
    path.parent.mkdir()
    path.write_text((FIXTURES / "cal_touch.json").read_text())
    code = code.replace('"cal/calibration.json"', repr(str(path)))
    if not NT2:
        out = []
        for ln in code.splitlines():
            if ln.rstrip().endswith("# 1.x: leave out"):
                continue
            if ln.startswith("from nautilus_trader.model import "):
                ln = (
                    "from nautilus_trader.model.enums import AccountType, OmsType\n"
                    "from nautilus_trader.model.objects import Currency, Money"
                )
            out.append(ln)
        code = "\n".join(out)

    def add_venue(engine, venue):
        exec(compile(code, "README.md", "exec"), {"engine": engine, "venue": venue})

    assert run_episodes({}, 40, [(-1, 100)], add_venue=add_venue) == set(range(40))


def test_other_major_snippet_does_not_import_here(touch_doc):
    other = 1 if NT2 else 2
    text = qtn.render_snippet(qtn.from_document(touch_doc), other)
    with pytest.raises(ImportError):
        exec(compile(text, "other", "exec"), {})


def test_unverified_version_warns(monkeypatch, touch_doc):
    monkeypatch.setattr(nautilus_trader, "__version__", "2.1.0" if NT2 else "1.232.0")
    with pytest.warns(qtn.CalibrationWarning, match="has not been cross-checked"):
        qtn.fill_model(qtn.from_document(touch_doc))


def test_old_version_refused(monkeypatch, touch_doc):
    monkeypatch.setattr(nautilus_trader, "__version__", "1.230.0")
    with pytest.raises(qtn.QtNautilusError, match="older than") as ei:
        qtn.fill_model(qtn.from_document(touch_doc))
    assert ei.value.code == "QT-USAGE-001"
