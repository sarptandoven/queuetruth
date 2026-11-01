# Run the nightly cross-checks

**Goal:** every night, on your own machine, check that queuetruth still agrees with the real engines it imitates
(NautilusTrader as pinned, the newest NautilusTrader 2.x release candidate, hftbacktest), that the public Nasdaq ITCH
reproduction still gives the published numbers, and that the quick benchmark has not regressed. Get a static HTML page,
a JSON history with the differences against earlier nights, and an exit code you can alert on.

**You need:** a checkout of queuetruth with its Rust toolchain, [uv](https://docs.astral.sh/uv/) and a CPython 3.13
interpreter (Homebrew's `/opt/homebrew/bin/python3.13` by default; `QT_NIGHTLY_PYTHON` or `--python` picks another).
The runner itself needs CPython 3.11 or newer; macOS's `/usr/bin/python3` is 3.9, so start it through the wrapper
`scripts/nightly/nightly.sh`, which picks the right interpreter. Network access to PyPI the first time each environment
is built. For the reproduction check, the public ITCH subsets of `scripts/repro/reproduce.py` on disk (checked against
the sha256 pinned in that script before anything runs), or `--repro-download` to stream them once. Everything runs
on your machine; the one exception is the daily [upstream compatibility](#upstream-compatibility-on-github) workflow,
which runs the engine checks on a GitHub runner against every new hftbacktest and NautilusTrader release.

## Run it once

<!-- check-docs: skip (builds four virtual environments and a release wheel; minutes, not seconds) -->
```sh
bash scripts/nightly/nightly.sh run --mode reduced --itch-dirs /path/to/itch-subsets
open .scratch/nightly/index.html
```

`reduced` is the quick shape for trying the runner (60 shadows per engine check, day one of the reproduction, two
benchmark cases). `full` (the default, and what `schedule` installs) is the nightly one; see
[Full mode](#full-mode) for what has and has not been verified about it. The terminal shows one line per step
(here a warm reduced run with `--skip bench` on 2026-10-24):

```text
[2026-10-24T19:31:59Z] [setup qt] pass
[2026-10-24T19:32:03Z] [setup dataset] pass
[2026-10-24T19:32:14Z] [hbt] pass in 10.9 s
[2026-10-24T19:32:25Z] [nt-pinned] pass in 10.9 s
[2026-10-24T19:32:35Z] [nt-rc] pass in 10.1 s
[2026-10-24T19:32:47Z] [repro] pass in 12.0 s
[2026-10-24T19:32:47Z] exit 0: ok: every check passed, nothing changed against the earlier runs; page ...
```

Without the subsets on disk and without `--repro-download`, `repro` errors with a message saying where it looked and
the run exits 2; the other checks still run. The first run builds the queuetruth venv and a release wheel (297 s on
the development laptop) and the first `bench` builds `queuetruth-core` in release mode (448 s); the engine venvs are
built once and reused while their pins do not change. Once warm, a reduced run takes about a minute (54 s to 67 s
measured on 2026-10-24, the bench check about 8 s on a quiet machine).

## What each check does

Each check runs in its own virtual environment under `<state>/venvs`. The queuetruth side is never the checkout's
`.venv`: the runner exports `uv.lock` into `venvs/qt`, builds a release wheel of the checkout with that venv's maturin
and installs it, then confirms that the venv imports queuetruth from itself. Engines only get their own pins. Each
check's `freeze.txt` (`uv pip freeze` of its venv) is kept with the run, and its sha256 is in `run.json`, so a change
in a transitive dependency can be traced.

| check | role | engine and venv | passes when |
|---|---|---|---|
| `hbt` | required | hftbacktest as pinned by `make hbt-crosscheck` (2.4.4), or `--hbt VERSION`, with numba and numpy | for both faithful `hbt_risk_adverse` and `hbt_power3[n=2]` (`no_partial`): every compared order has the same first fill timestamp; every order rested in hftbacktest (none expired or rejected on arrival); at least 90% of the orders were compared, and every hftbacktest error is the known #312 (error code 14) |
| `nt-pinned` | required | nautilus_trader as pinned in `scripts/nt_crosscheck/requirements.txt` (1.231.0) | the touch default and `queue_position=True` fill exactly the orders the faithful `nt_*` models fill at 0.1 / 1 / 10 / 60 s; each rule-set replica reproduces its reference; no disagreement is unexplained |
| `nt-rc` | advisory | the newest nautilus_trader 2.x on PyPI that has a wheel for this interpreter and platform, release candidates included (`--nt-rc` pins one) | the same, except the queue model, which 2.x changed on purpose (it caps queue ahead on book updates; [Results](../RESULTS.md#nautilustrader-cross-check)); its agreement is tracked, not gated |
| `repro` | required | `scripts/repro/reproduce.py` in the queuetruth venv | every step passes or records its numbers, the build did not change during the run |
| `bench` | required | `benches/bench.py --suite quick`, then `benches/check.py` against the baseline | no case errors; every case and every derived ratio with a SPEC 7.1 target meets it; no statistically significant slowdown over 20% against the baseline; errors instead when the machine is too loaded to measure |

Both engine cross-checks replay the same synthetic Nasdaq day (`queuetruth fixtures synth --venue nasdaq
--sessions 1`, seed 7), so they need no market data. The `hbt` check resolves every sampled shadow to an absolute
price on the zoo's L2 view at its live time (the NautilusTrader harness's rule), drops the ones that have no price or
would cross on arrival (each reason is counted separately under `hbt.dropped.*`), runs the zoo on exactly those
orders, and writes the hftbacktest event file (`export l2_npz`), an orders sidecar and the zoo's first fills per
instrument-day. That directory is also a valid `QT_ZOO3_DIR` for the optional
`python/tests/zoo/test_hbt_crosscheck.py`. In the reduced runs of 2026-10-24, 53 of 60 sampled shadows rested at
their live time (`hbt.dropped.improving_offset_crosses` = 7, every other reason 0), and both hbt models agreed with
hftbacktest on all 53, with 30 and 31 of them filled within 60 s. On the same day 2.0.0rc7's queue model also agreed
with `nt_queue_l2` on all 212 order-horizon pairs, so the 2.x cap did not bind on these 53 submitted orders.

| mode | engine shadows | NautilusTrader feeds | reproduction | benchmark |
|---|---|---|---|---|
| `full` | 400 | delta and depth-10 | both public days: subset, ingest, replay, headline, calib, hazard | the whole quick suite |
| `reduced` | 60 | delta | 2019-12-30: subset, ingest, replay | `core.synth2m.replay_book`, `bind.fixture.cli_replay` |

`bash scripts/nightly/nightly.sh list` prints the same. `--only hbt,nt-pinned` or `--skip bench` runs a subset; a
selection that leaves no check is a usage error.

### Full mode

Full mode has not been run end to end yet; every number and exit code on this page comes from reduced mode. Its
reproduction step alone runs `reproduce.py` over both days with the headline, calibration and hazard steps, which took
1,162 s and 1,387 s at 4 threads in the runs [Results](../RESULTS.md) records; the nightly uses 2 threads, so expect
longer, plus the 400-shadow engine checks and the whole quick suite. Its diff has been checked with unit tests on
results shaped like a full-mode reproduction (new wall times, paths and source hash every night give no tracked
change). Run `bash scripts/nightly/nightly.sh run --mode full` twice on a quiet machine before relying on it. When the
state directory is outside the checkout, `reproduce.py` writes under `<repo>/.scratch/nightly-repro/<nightly_id>`
(it records some artifacts relative to the checkout) and the runner moves the output into the run directory after.

## Read the results

Everything lives under the state directory (`--state`, default `<repo>/.scratch/nightly`, which git ignores):

- `index.html`: the latest run (verdict, one row per check, every gate with its detail, the tracked numbers next to
  the earlier ones, where they came from), the benchmark medians across runs, and the history table with a link to
  each run's own page while its directory is kept. Self-contained: no script, no network request, light and dark
  themes.
- `latest.json` and `runs/<nightly_id>/run.json`: the same content as a `qt.nightly.run` document.
- `history.json`: every run, oldest first (`--history-keep`, default 90).
- `runs/<nightly_id>/checks/<name>/`: `log.txt` with every command and its output, `freeze.txt`, and the harness's own
  report (`report.md` for NautilusTrader, `hbt_report.json`, `results.json` for the reproduction, `bench.json`).

Each check is diffed against its own baseline: the newest earlier run of the same mode in which that check produced
results. So a run with `--only` or `--skip`, or a night on which a check errored, never hides a change from the next
full night; a check that was not selected is not a change. Integers and strings must be identical and floats equal
within a relative 1e-12 (`math.isclose` with no absolute tolerance, so a tiny number that doubles is a change):
these checks are deterministic, so any change in a fill rate, a replay counter or an agreement count is real.

Three kinds of values are recorded but never count as a change:

- **Timings**: wall times, throughput, free disk, benchmark medians, and anything nested under such a key (for
  example the headline's `zoo_wall_s.faithful`). They are listed with their relative change; the benchmark has its
  own statistical gate.
- **Provenance**: the source tree hash, the queuetruth and engine versions, where a subset was found and whether it
  was streamed, artifact paths. A changed source tree hash means the code changed, which is context, not a result;
  it is listed under "context" on the page.
- **Paths inside this run's directories**: replaced by `<out>`, `<itch>`, `<state>` or `<repo>` before the comparison,
  so a number that embeds a path compares equal across nights.

## Exit codes

| code | meaning |
|---|---|
| `0` | every check passed and nothing changed against the earlier runs of the same mode |
| `1` | a required check failed a gate |
| `2` | a required check could not run (environment, download, crash, timeout, free disk below `--min-free-gib`, machine too loaded for the benchmark), or the runner could not proceed (another run holds the lock, `history.json` is unreadable, setup failed, the run was interrupted, unknown or empty check selection) |
| `3` | every check passed, but a tracked number or a check status changed against the earlier runs |
| `4` | only the advisory check (`nt-rc`) failed or errored |

When several apply the order is 2, 1, 4, 3, 0. `run.json` also lists every condition that held in `flags` (`error`,
`fail`, `advisory`, `changed`), and the verdict says when a higher code hides tracked changes, for example
`advisory: only the advisory check failed or errored (also 2 tracked change(s) against earlier runs)`. A new
NautilusTrader release candidate that changes a rule shows up as `4` with the failing replica gate on the page, while
the pinned version keeps the night at `0` or `3`.

## The benchmark baseline

`bench` compares the night with the newest earlier run of the same mode whose bench check passed with "no
regression" (or had no baseline itself). A night that regressed never becomes the baseline, so a slowdown keeps
failing every night until it is fixed. A slow drift below the 20% threshold per comparison is still absorbed one
passing night at a time; to catch it, pin a reference with `--bench-baseline PATH` (a `bench.json` from a run you
trust, or the file `make bench-baseline` writes). The gate detail names the baseline used.

The benchmark measures the machine as much as the code. When the 1-minute load average during `bench.py` is above
`--bench-max-load` (default: the number of CPUs; `0` disables), the check errors instead of judging the medians, and
that night is never a baseline. `bench.py` itself waits up to 30 s per round for the load to fall below the same
value, so on a busy machine the check takes longer before it errors (251 s in the reduced run below, against about
8 s on a quiet one). On 2026-10-24 the shared development laptop ran the benchmark at a 1-minute load average of 212, and the two
quick-suite medians fell 25% and 42% against the run just before (check.py slowdowns of 34% and 72%), which
is the case this guard exists for; with the guard, the next reduced run (load 119) reported `bench` as an error with
that reason and exited 2.

## Schedule it

The runner never installs anything. `schedule` prints the text for you to review and install yourself; both use the
wrapper `scripts/nightly/nightly.sh`, which sets a PATH with uv, cargo and Homebrew, runs the runner with CPython 3.11
or newer, keeps the Mac awake for the run (`caffeinate -i`) and passes the exit code through. Further `run` options go
after `--`, for example `-- --itch-dirs /data/itch --repro-download`; they are checked when you print the schedule.

**launchd (macOS).** Create the state directory (launchd opens its log file there before the job starts), print the
agent, save it, load it:

<!-- check-docs: skip (installs a user LaunchAgent; review the plist first) -->
```sh
mkdir -p .scratch/nightly
bash scripts/nightly/nightly.sh schedule launchd --hour 2 --minute 30 > ~/Library/LaunchAgents/io.github.queuetruth.nightly.plist
plutil -lint ~/Library/LaunchAgents/io.github.queuetruth.nightly.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.queuetruth.nightly.plist
launchctl kickstart gui/$(id -u)/io.github.queuetruth.nightly    # optional: run it now
```

launchd runs a missed job when the Mac wakes; stdout and stderr go to `<state>/launchd.log`. Remove it with
`launchctl bootout gui/$(id -u)/io.github.queuetruth.nightly` and delete the plist. Stopping the job (bootout, Ctrl-C
or a SIGTERM to the wrapper) stops the running check's whole process group, records a partial `run.json` with
`interrupted` and exits 2; the partial run is not added to the history and nothing is pruned.

**cron (Linux, or macOS if you prefer).** Print the line and add it with `crontab -e`:

<!-- check-docs: skip (prints a crontab line for you to install) -->
```sh
bash scripts/nightly/nightly.sh schedule cron --hour 2 --minute 30
```

```text
30 2 * * * mkdir -p <state> && /bin/bash <repo>/scripts/nightly/nightly.sh run --mode full --state <state> >> <state>/cron.log 2>&1
```

Every path is shell-quoted and every `%` escaped (cron reads a bare `%` as a newline). To be told about a bad night,
wrap the line so a non-zero exit sends you something, for example
`... || osascript -e 'display notification "queuetruth nightly failed"'`.

## Options worth knowing

- `--hbt VERSION`: run the `hbt` check against another hftbacktest release (default: the pinned one). The 14-day
  cutoff below is lifted for hftbacktest itself, so a release is installable on the day it is published.
- `--offline`: no network. Reuses existing venvs and wheels, picks the newest `nt-2.*` venv already built, and errors
  instead of downloading.
- `--refresh-envs`: rebuild every venv (otherwise a venv is rebuilt only when what it should hold changes). When a
  newer release candidate is installed, `nt-2.*` venvs other than it and the one before are deleted (about 330 MB
  each).
- `--qt-python PATH`: skip building the queuetruth venv and use an interpreter that already imports queuetruth, for
  example while iterating on the runner itself. Bench then uses that interpreter's venv.
- `--wheel-dirs DIR,...`: directories searched for the retagged NautilusTrader 1.231.0 macOS wheel before
  downloading it (its file is checked against PyPI's sha256 either way).
- `--itch-dirs DIR,...` and `--repro-download`: where the public subsets are looked for, and permission to stream them
  from emi.nasdaq.com (several GB of traffic; streamed subsets are kept in `<state>/itch` for later nights). A subset
  whose content does not match the pinned sha256 is ignored, never silently re-streamed.
- `--bench-baseline PATH` and `--bench-max-load N`: see [The benchmark baseline](#the-benchmark-baseline).
- `--min-free-gib` (default 10): a check does not start below this much free disk. `--keep-runs` (default 14) run
  directories of each mode are kept on disk, plus each mode's benchmark baseline; `--keep-work` keeps each check's
  work directory for debugging.
- `render`: rebuild `index.html` from `history.json` without running anything.

Environments installed with an open version (`nt-rc`'s pyarrow, numpy and pandas; hftbacktest's numba and numpy) use
a cutoff 14 days back (`--exclude-newer`), the repository's supply-chain rule, lifted only for the release candidate
under test; only the release candidate itself may be a pre-release. The NautilusTrader 1.231.0 macOS arm64 wheel is
tagged for macOS 26 although its modules are built for macOS 11, so on older macOS the runner installs it under a
`macosx_11_0` name, as `scripts/nt_crosscheck/requirements.txt` documents.

## Upstream compatibility on GitHub

`.github/workflows/compat.yml` runs every day at 04:17 UTC (and on demand from the Actions tab) and answers one
question: do the newest hftbacktest and NautilusTrader releases still fill orders exactly as queuetruth's faithful
models of their rules say? It runs `scripts/nightly/compat.py`, which drives this runner:

1. `compat.py plan` asks PyPI for every release of both projects and lists the versions to check: the pinned ones
   (`make hbt-crosscheck`, `scripts/nt_crosscheck/requirements.txt`) and the newest release of each major line from
   the pinned one up, release candidates included, that has a wheel for the interpreter and platform (the workflow
   asks for Linux x86_64 and CPython 3.13; run by hand, the default is your own host). A yanked or dev release, or
   one without such a wheel, is skipped; a newest release equal to the pin is one row with both
   roles. So a NautilusTrader 1.x maintenance release and a 2.x release candidate are both checked.
2. `compat.py run` makes one `nightly.py run --mode reduced --only <check>` per version, each with its own state
   directory and the checkout's `.venv` as `--qt-python`: `hbt` with `--hbt VERSION` for hftbacktest, `nt-pinned`
   for the pinned NautilusTrader and `nt-rc --nt-rc VERSION` for any other (its 2.x queue model is tracked, not
   gated, as in the table above). It writes `compat-<date>.json` (schema `qt.compat.result`: per version the check,
   its status, every gate with its detail, the log's URL, the run's URL) and one log per version. Each version has
   a time limit (`--target-timeout`, 40 minutes in the workflow) and the result is rewritten after every version,
   so one that hangs is recorded as an error and the others are kept.
3. The publish job uploads only what `compat.py select` accepts from the check job's artifact: that day's result,
   when it is valid and carries that date, and the logs it names. It keeps them as assets of the `compat-results`
   release (results older than 90 days are deleted), uploads a preview `compat.html`, and fails when a pinned
   version did not pass. A newer release that disagrees is
   a warning annotation, not a failure: finding it is what the page is for.
4. The site job dispatches `.github/workflows/atlas.yml` with `site_only`. A repository has one GitHub Pages site,
   so the atlas workflow owns it: every site build downloads the kept results, renders `compat.html` next to the
   atlas index with `scripts/nightly/compat_site.py` and links it from the index. A site-only build runs no IEX day
   and has its own concurrency group, so it never displaces a pending IEX run. The compatibility page is best
   effort: when it cannot be built the atlas deploys without it and without the link. Before the first IEX day the
   compatibility page is the whole site.

The page lists, per library and version (newest first), the latest result with its date, the gates passed, why it
did not pass and links to the log and the run, then every kept result. Like the atlas it is static, runs no script,
uses the atlas's styles and is byte-identical for the same result files.

The check job installs packages published hours ago, so it gets a read-only token, its checkout keeps no
credentials and it saves no uv or Rust cache (the atlas workflow restores those caches while it can write, so a
poisoned cache would carry the new packages' code there). Its artifact is treated as untrusted data, as described in
step 3. Only the publish job (release assets) and the site job (one dispatch) can write. Release assets are listed
through the paged REST API (`scripts/nightly/release_assets.sh`), so a release with hundreds of them is read in
full. To try the same steps
on your machine (needs the queuetruth `.venv` from `make sync develop` and network access to PyPI):

<!-- check-docs: skip (builds one virtual environment per version and needs network access to PyPI) -->
```sh
python3.13 scripts/nightly/compat.py plan --out .scratch/compat/targets.json
python3.13 scripts/nightly/compat.py run --targets .scratch/compat/targets.json --out .scratch/compat \
  --python "$(command -v python3.13)" --qt-python "$PWD/.venv/bin/python"
python3.13 scripts/nightly/compat_site.py --results .scratch/compat/upload --out .scratch/compat/compat.html
```

## Run the runner's own tests

<!-- check-docs: skip (needs pytest; outside the default testpaths) -->
```sh
uv run --frozen --no-sync pytest scripts/nightly/tests
```
