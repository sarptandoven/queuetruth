"""Fixtures of the certificate tests: small synthetic runs (replay, referee, dq) on a writable copy of the committed
referee fixture, made from a base directory with relative paths exactly as the CLI would make them."""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from queuetruth.cert.keys import generate

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "referee" / "02_level_shrink_caps_queue"


def make_runs(base: Path) -> Path:
    """Copies the fixture to `base/fx` and runs `shadows sample`, `replay`, `referee run` and `dq` there."""
    from queuetruth import api

    shutil.copytree(FIXTURE, base / "fx")
    with contextlib.chdir(base):
        api.sample_shadows(
            {
                "dataset": {"path": "fx/dataset"},
                "n": 20,
                "seed": 1,
                "out": "shadows.parquet",
                "window_open_s": 0,
                "window_close_s": 0,
            }
        )
        api.replay({"dataset": {"path": "fx/dataset"}, "shadows": {"path": "shadows.parquet"}, "out": "replay"})
        api.referee(
            {
                "dataset": {"path": "fx/dataset"},
                "schedule": {"path": "fx/schedule.parquet"},
                "sim_fills": {"path": "fx/expected_fills.parquet"},
                "out": "referee",
            }
        )
        api.dq({"dataset": {"path": "fx/dataset"}, "out": "dq"})
    return base


@pytest.fixture(scope="session")
def pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The runs, made once per session (never modified: tests copy it with `work`)."""
    return make_runs(tmp_path_factory.mktemp("cert_runs"))


@pytest.fixture
def work(pristine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private copy of the runs; the test runs in it (the base directory)."""
    w = tmp_path / "w"
    shutil.copytree(pristine, w)
    monkeypatch.chdir(w)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    monkeypatch.delenv("QT_CERT_KEY_PASSPHRASE", raising=False)
    yield w


@pytest.fixture(scope="session")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, str]:
    """(private PEM, public PEM, fingerprint) of an unencrypted test key."""
    d = tmp_path_factory.mktemp("keys")
    key, pub, _ = generate(d / "signer.pem")
    return d / "signer.pem", pub, key.fingerprint
