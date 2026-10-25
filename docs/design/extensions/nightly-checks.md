# Extension: local nightly cross-checks (`scripts/nightly/`)

Proposed SPEC text for a section on maintenance tooling. The runner is a developer tool, not part of the installed
package: it has no `QT-*` error codes and its documents are not published schemas.

## Scope

`scripts/nightly/nightly.py run` runs, on the maintainer's machine and never on GitHub Actions or a cloud runner,
five checks against the checkout it lives in:

| check | role | what it compares | gates |
|---|---|---|---|
| `hbt` | required | hftbacktest (the version `make hbt-crosscheck` pins) against faithful `hbt_risk_adverse` and `hbt_power3[n=2]` on `no_partial`, order by order | per model: every compared order's first fill timestamp is equal and at least one order is compared; no order expired or was rejected on arrival (`not_resting_in_hbt` = 0); every hftbacktest error has error code 14 (hftbacktest #312) and at least 90% of the orders are compared |
| `nt-pinned` | required | `scripts/nt_crosscheck/run.py all` on the NautilusTrader version pinned in `scripts/nt_crosscheck/requirements.txt` | report is for that version; `touch_p1` (and `d10_touch_p1` when run) and, on 1.x, `queue_p1` agree with the faithful zoo model on every order at 0.1 / 1 / 10 / 60 s; each rule-set replica reproduces its reference on every order and horizon; no unexplained disagreement |
| `nt-rc` | advisory | the same harness on the newest NautilusTrader 2.x release on PyPI (pre-releases included) that has a wheel for the host interpreter and platform | as `nt-pinned`, without the `queue_p1` agreement gate (2.x caps queue ahead on book updates) |
| `repro` | required | `scripts/repro/reproduce.py` on the public Nasdaq ITCH subsets (sha256 pinned in the script) | exit 0; every expected step `pass` or `recorded`; build unchanged during the run; not aborted |
| `bench` | required | `benches/bench.py --suite quick --max-load L`, then `benches/check.py` against the baseline: `--bench-baseline`, else the newest earlier run of the mode whose bench check passed with "no regression" or "no previous run" | exit 0; every case and every derived ratio (`results.derived`) with a SPEC 7.1 target meets it; no regression (`check.py` exit 1) against the baseline; an environment that is not comparable (exit 3) is reported, not failed. Status `error` when the 1-minute load average during the benchmark exceeds `--bench-max-load` L (default the CPU count) |

Modes: `full` (400 shadows per engine check with the depth-10 configs, both public days with the headline,
calibration and hazard steps, the whole quick suite) and `reduced` (60 shadows, delta feed only, day one subset,
ingest and replay steps, two quick-suite cases). Runs of one mode are diffed only against runs of the same mode.
Full mode has not been run end to end; its diff is covered by unit tests on full-mode-shaped results only.

## Environments

Each engine runs in its own venv under `<state>/venvs/<name>` built with uv (`--no-config`). The queuetruth venv
holds the dependencies exported from `uv.lock` (`--frozen`, dev group and all extras, workspace members excluded) and
a release wheel of the checkout built with that venv's maturin each run; the runner verifies that the venv imports
queuetruth from itself. Engine venvs: the pinned NautilusTrader requirement file as is; the release candidate with
pyarrow, numpy and pandas under a 14-day `--exclude-newer` cutoff lifted for `nautilus-trader` only; hftbacktest with
`--no-deps` plus numba and numpy under the same cutoff. A venv is reused while the sha256 of its specification (the
interpreter, the install arguments and a fingerprint of the wheel digests or `uv.lock`) is unchanged; the cutoff is
not part of the digest. A macOS wheel whose platform tag is newer than the host while its extension modules target
older systems (nautilus_trader 1.231.0) is downloaded once, checked against PyPI's sha256 and installed under a
`macosx_11_0` file name. No install passes `--prerelease=allow`: only the exact release-candidate pin may be a
pre-release. Every child process runs without `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV` and with
`PYTHONNOUSERSITE=1`. After each environment is ensured, `uv pip freeze` of it is written to the check directory
(`freeze.txt`, the queuetruth venv's to `setup/qt-freeze.txt`) and its sha256 recorded under `versions`. Release
candidate venvs other than the current one and the newest older one are deleted.

## Documents

`<state>/runs/<nightly_id>/run.json` (`schema: qt.nightly.run`, `schema_version: 1`), with `nightly_id` the UTC start
time `YYYYMMDDTHHMMSSZ` (not a content hash: a nightly is an observation of the checkout at a time, and two runs of the
same tree must stay distinct; a run that would reuse the id of one that finished within the same second waits for the
next second):

- `mode`, `started_at`, `finished_at`, `wall_s`, `checks_selected`, `partial` (not every check selected), `git`
  (`head`, `branch`, `dirty`: tracked changes under the source directories, `untracked_sources`: untracked files
  there), `host`.
- `setup.qt`, `setup.dataset`: status, reason when not `pass`, the wheel, its sha256, the loaded `_core` sha256, the
  synthetic dataset's `dataset_id`.
- `checks.<name>`: `title`, `required`, `status` (`pass` when every gate is ok and there is at least one gate, `fail`
  otherwise, `error` when the check could not run), `reason`, `gates` (`name`, `ok`, `detail`), `metrics` (a flat map of
  scalars; strings have this run's directories replaced by `<out>`, `<itch>`, `<state>`, `<repo>`), `noisy` (metric
  names or prefixes that are timings: any key segment matching wall time, throughput, memory, load or free disk, and
  the benchmark medians), `provenance` (a flat map of what the numbers were computed from: build identity, engine
  versions, input paths, `streamed_now`, the bench baseline), `versions`, `artifacts` (paths relative to the run
  directory), `wall_s`.
- `diff`, per check against its own baseline among the earlier runs of the same mode: `vs` (the newest earlier run or
  null), `baselines` (per check: `status`, the newest earlier run that ran it; `metrics`, the newest earlier run in
  which it passed or failed), `status_changes` (checks run now whose status differs from their status baseline),
  `changed` (tracked metrics that differ from the metrics baseline: integers and strings exactly, floats unless
  `math.isclose(a, b, rel_tol=1e-12, abs_tol=0)`, plus metrics added or removed; not computed for a check that errored
  now), `noisy` (timings that moved, with their relative change), `context` (provenance that changed), `new_checks`
  (checks with no earlier run). Only `status_changes` and `changed` count.
- `exit_code`, `flags` (every condition that held: `error`, `fail`, `advisory`, `changed`), `verdict` (the exit
  code's meaning, plus the number of tracked changes when a higher code hides them), `runner_error` when the run could
  not proceed, `interrupted` (`signal`, `during`) when a stop signal ended it.

`<state>/history.json` (`schema: qt.nightly.history`, `schema_version: 1`): `runs`, the run documents oldest first,
trimmed to `--history-keep` (default 90); written atomically. An unreadable history or one of another schema is left
untouched, no run directory is pruned, and the run exits 2. A run with `runner_error` (unreadable history,
interrupted) is not appended. `<state>/latest.json` is the newest run document; `<state>/index.html` and
`<state>/runs/<nightly_id>/index.html` are the static page (inline CSS only, light and dark).

## Exit codes

Precedence 2 > 1 > 4 > 3 > 0: `0` every check passed and nothing changed against the earlier runs of the same mode;
`1` a required check failed a gate; `2` a required check could not run (environment, download, crash, timeout, disk
below `--min-free-gib`, load above `--bench-max-load`) or the runner could not proceed (lock held by another run,
unreadable history, setup failed, interrupted, unknown check or empty selection);
`3` every check passed but a tracked number or a status changed; `4` only the advisory check failed or errored.

## Safety

The runner needs CPython 3.11 or newer and exits 2 with a message on an older interpreter. One run per state
directory (`fcntl.flock` on `<state>/.lock`). A check does not start below `--min-free-gib` (default 10). Every
subprocess runs in its own process group, which is killed (SIGTERM, then SIGKILL after a grace period) on its
per-mode timeout and whenever the runner is stopped: SIGTERM, SIGHUP and SIGINT raise in the runner, the child group
is stopped, a partial `run.json` is written and the run exits 2. Run directories beyond the newest `--keep-runs`
(default 14) of each mode are deleted, except each mode's benchmark baseline, and only directories named like a
`nightly_id`; nothing is pruned when the history could not be loaded or the run was interrupted. Before
`reproduce.py` runs, each subset found is checked against the framed sha256 pinned in `PINNED_SUBSETS` (read from
the script as a literal) and only verified subsets are shown to it, so a stale subset is never re-streamed unasked.
Public ITCH subsets are streamed only with `--repro-download`; streamed subsets are kept in `<state>/itch`.
`--offline` reuses existing venvs and wheels and refuses every download. When the state directory is outside the
checkout, `reproduce.py` writes under `<repo>/.scratch/nightly-repro/<nightly_id>` and the output is moved into the
run directory. `schedule launchd|cron` prints a plist (built with `plistlib`) or a crontab line (shell-quoted, `%`
escaped, creating the state directory first) and installs nothing; run options after `--` are validated and passed
through.
