"""Schema 1.1 `LiveCalibrationSpec.self_exclude` / `match_window_ns`: `calibrate_live` reads them from the
spec (defaults true and 1 ms, which keep the run_id); the keyword arguments still override and are recorded in the
spec echo and the run_id; the module's spec is the registered `queuetruth.schemas.specs.LiveCalibrationSpec`."""

from __future__ import annotations

from queuetruth.schemas import specs as s
from queuetruth.score import LiveCalibrationSpec, calibrate_live
from score.test_score_calibrate_live import S, ZooByParam, _dataset, _log


def _spec(tmp_path, **kw):
    return {
        "dataset": {"path": str(_dataset(tmp_path))},
        "live": {"path": str(_log(tmp_path))},
        "family": "power3",
        "horizons_ns": [1 * S, 2 * S, 4 * S, 8 * S, 16 * S],
        "bootstrap": {"b": 10},
        **kw,
    }


def test_one_registered_spec() -> None:
    assert LiveCalibrationSpec is s.LiveCalibrationSpec
    d = s.LiveCalibrationSpec.model_validate({"dataset": {"path": "d"}, "live": {"path": "l"}, "family": "power"})
    assert (d.self_exclude, d.match_window_ns) == (True, 1_000_000)


def test_self_exclude_is_read_from_the_spec(tmp_path) -> None:
    spec = _spec(tmp_path, self_exclude=False)
    by_spec = calibrate_live(spec, runner=ZooByParam())
    by_kw = calibrate_live(
        {k: v for k, v in spec.items() if k != "self_exclude"}, runner=ZooByParam(), self_exclude=False
    )
    for cal in (by_spec, by_kw):
        assert any(w.startswith("trust: own_orders_in_depth: ") and "self_exclude is false" in w for w in cal.warnings)
        assert cal.spec["self_exclude"] is False and cal.spec["match_window_ns"] == 1_000_000
    # The keyword is folded into the spec: same echo, same run_id as the spec field.
    assert by_kw.run_id == by_spec.run_id
    default = calibrate_live({k: v for k, v in spec.items() if k != "self_exclude"}, runner=ZooByParam())
    assert not any(w.startswith("trust: own_orders_in_depth: ") for w in default.warnings)
    assert default.spec["self_exclude"] is True and default.run_id != by_spec.run_id
    # An explicit keyword wins over the spec.
    back = calibrate_live(spec, runner=ZooByParam(), self_exclude=True)
    assert back.run_id == default.run_id


def test_match_window_is_read_from_the_spec(tmp_path) -> None:
    spec = _spec(tmp_path)
    cal = calibrate_live({**spec, "match_window_ns": 2_000_000}, runner=ZooByParam())
    assert cal.spec["match_window_ns"] == 2_000_000
    assert cal.run_id == calibrate_live(spec, runner=ZooByParam(), match_window_ns=2_000_000).run_id
    assert cal.run_id != calibrate_live(spec, runner=ZooByParam()).run_id
