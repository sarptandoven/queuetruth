"""Property-based fuzzing of the Python parsers and importers that untrusted uploads reach: FIX wire and data
dictionaries, QuickFIX logs and stores, the JSON venue importers, IBKR Flex, the instrument-map CSV, the simulator
importers and the `dq` dataset loader (the extension doc lists what is not covered). See `fuzz._harness` for the
contract, the budget (`QT_FUZZ_EXAMPLES`, `QT_FUZZ_PROFILE=deep`) and the statistics table this conftest prints at
the end of the session. Every test here carries the `fuzz` marker (`-m "not fuzz"` deselects the suite)."""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path

import pytest
from fuzz._harness import stats_report

from queuetruth.live import import_venue

_HERE = Path(__file__).resolve().parent


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "fuzz: parser fuzz properties and their regression tests (python/tests/fuzz)")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if Path(str(item.path)).resolve().is_relative_to(_HERE):
            item.add_marker(pytest.mark.fuzz)


@pytest.fixture(scope="session", autouse=True)
def _warm_imports() -> None:
    """One untimed import before the first property, so lazy imports (pyarrow, pydantic schemas) do not show up as
    the slowest example of whichever target runs first."""
    with tempfile.TemporaryDirectory() as d, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        (Path(d) / "x.jsonl").write_text("")
        import_venue({"venue": "binance", "inputs": [{"path": str(Path(d) / "x.jsonl")}], "out": str(Path(d) / "o")})


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Prints the per-target example counts, outcomes, times and memory peaks of the fuzz properties that ran."""
    lines = stats_report()
    if lines:
        terminalreporter.section("parser fuzz statistics")
        for line in lines:
            terminalreporter.write_line(line)
