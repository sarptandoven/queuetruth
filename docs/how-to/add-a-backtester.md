# Add a backtester as a plugin

**Goal:** let `queuetruth check`, `queuetruth.live.import_sim` and `queuetruth reconcile` read your backtester's
output, from a package of your own, with no change to queuetruth.

queuetruth keeps one registry of backtesters, `queuetruth.backtesters`. The built-ins (hftbacktest, NautilusTrader,
Backtrader, VectorBT, LEAN, Freqtrade, Hummingbot) are entries in it, and a package adds one through the
`queuetruth.backtesters` entry-point group.

## 1. Write the entry

A backtester needs a name, a label and an importer of its output. The importer turns the output files into the
rows of a canonical live log (`queuetruth.live.importers.Importer`: `(paths, ImportOptions) -> ImportResult`;
`queuetruth.live.builder` builds the rows). Reading the output of a format queuetruth already knows and passing it
to that importer is enough, as this sketch does:

<!-- check-docs: skip (a sketch of a third-party package) -->
```python
# mysim_qt/__init__.py
from queuetruth.backtesters import BacktesterEntry, Probe


def detect(probe: Probe) -> bool:
    """True for MySim's order files (read from their columns, never from the file name alone)."""
    return {"mysim_order_id", "mysim_status"} <= probe.columns


def import_mysim(paths, options):
    from queuetruth.live.importers.sim import import_hftbacktest

    snapshots = convert_to_hftbacktest_snapshots(paths)  # your code
    return import_hftbacktest(snapshots, options)


BACKTESTER = BacktesterEntry(name="mysim", label="MySim", import_sim=import_mysim, detect=detect)
```

The other parts are optional: `import_live` (a bot's live run), `replay` (`export replay-schedule`), `snippet` (the
code to paste for a calibration), `zoo_models` (the zoo families that replicate its fill models), `reconcile` (its
run against a live run, order by order) and `detect_priority` (the order `detect` is tried in; built-ins come first).
The `queuetruth.backtesters.Backtester` protocol lists their signatures.

## 2. Register it

<!-- check-docs: skip (a sketch of a third-party pyproject.toml) -->
```toml
[project.entry-points."queuetruth.backtesters"]
mysim = "mysim_qt:BACKTESTER"
```

After `pip install` of the package:

- `queuetruth check mysim_orders.csv --data DATASET` detects the file and replays its orders;
  `--backtester mysim` names it when `detect` cannot tell.
- `queuetruth.live.import_sim("mysim", [...], OUT)` writes a simulator log with venue `sim:mysim`.
- `queuetruth reconcile --kind mysim ...` works when the entry has a `reconcile`.

A plugin cannot replace a built-in, and a plugin that fails to load is skipped with a `BacktesterWarning`.
`queuetruth live import --venue sim-NAME` lists the built-ins only, since its venues are part of the
`qt.live_import_spec` schema; plugins use `import_sim`.

queuetruth's own test of this path is
[python/tests/backtesters/test_registry.py](https://github.com/sarptandoven/queuetruth/blob/main/python/tests/backtesters/test_registry.py),
with a toy plugin in
[python/tests/backtesters/toy_plugin](https://github.com/sarptandoven/queuetruth/blob/main/python/tests/backtesters/toy_plugin).
