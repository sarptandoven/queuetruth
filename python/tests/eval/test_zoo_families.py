"""Drift tests of the zoo family registry (`queuetruth._core.zoo_families()`): every list of families or models kept
in Python equals the registry, and `zoo --list-models` prints the same registry."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.requires_core


def test_registry_shape():
    from queuetruth.eval.zoo import zoo_all_ids, zoo_families

    fams = zoo_families()
    names = [f["name"] for f in fams]
    assert len(names) == len(set(names)) == 13
    assert len(zoo_all_ids()) == 16
    for f in fams:
        assert f["param_keys"] == [p["key"] for p in f["params"]]
        assert f["input"] in {"l2", "mbo"}
        for s in f["in_all"]:
            assert set(s) <= set(f["param_keys"])
    by = {f["name"]: f for f in fams}
    assert by["nt_prob_touch"]["library_default"] == {"p": 1.0}
    assert by["databento_mc"]["calibrations"] == ["alloc", "survival", "live"]
    assert by["front_of_queue"]["library"] is None


def test_list_models_reads_the_registry():
    from queuetruth import _core
    from queuetruth.eval.zoo import zoo_all_ids, zoo_families

    with pytest.raises(_core.CliExit) as e:
        _core.cli_parse(["queuetruth", "--format", "json", "zoo", "--list-models"])
    doc = json.loads(e.value.args[1])
    assert [r["id"] for r in doc["all"]] == zoo_all_ids()
    listed = {r["family"] for r in (*doc["all"], *doc["other"])}
    assert listed == {f["name"] for f in zoo_families()}
    summaries = {f["name"]: f["summary"] for f in zoo_families()}
    assert all(r["summary"] == summaries[r["family"]] for r in (*doc["all"], *doc["other"]))


def test_python_particle_lists_equal_the_registry():
    from queuetruth.eval import zoo

    particle = tuple(f["name"] for f in zoo.zoo_families() if f["particle"])
    assert zoo.PARTICLE_FAMILIES == zoo.particle_families() == particle
    assert zoo._PARTICLE_FALLBACK == particle  # the literal used without the extension


def test_calibration_families_match_the_cli_choices():
    """`--family` choices of `calibrate alloc`, `calibrate survival` and `calibrate-live` are the registry's
    `calibration_family` of the families each calibration fits."""
    from queuetruth import _core
    from queuetruth.eval.zoo import zoo_families

    def registry(kind: str) -> set[str]:
        return {f["calibration_family"].replace("_", "-") for f in zoo_families() if kind in f["calibrations"]}

    for argv, kind in (
        (["calibrate", "alloc"], "alloc"),
        (["calibrate", "survival"], "survival"),
        (["calibrate-live"], "live"),
    ):
        with pytest.raises(_core.CliExit) as e:
            _core.cli_parse(["queuetruth", *argv, "--help"])
        after = e.value.args[1].split("--family <family>", 1)[1]
        listed = after.split("[possible values: ", 1)[1].split("]", 1)[0].split(", ")
        assert set(listed) == registry(kind), (argv, listed)
