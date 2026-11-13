# Contributing to queuetruth

Thanks for helping. The most useful contributions right now are:

- **Reports from real data.** An importer or reader that breaks on your real export or feed, or a result that looks
  wrong on your venue. Use the "Data or venue support request" issue template. Share field layouts, error messages
  and aggregates, never your orders, fills or prices.
- **Bug reports** with a reproduction on the synthetic fixtures (`queuetruth fixtures synth --out synth`).
- **Fixes and features** that come with a test that would fail without them.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems go through the private process in
[SECURITY.md](SECURITY.md), not public issues.

## Development setup

Requirements:

- Rust via `rustup`. `rust-toolchain.toml` pins the toolchain (1.97.1 with clippy and rustfmt); rustup installs it on
  first use.
- [uv](https://docs.astral.sh/uv/) and CPython 3.13 for development (the package supports CPython 3.11 and later).
- For the service tests: PostgreSQL 16 binaries (`initdb`, `pg_ctl`). Point `QT_PG_BIN` at their directory, or set
  `QT_PG_DSN` to an existing server. Without either, those tests are skipped with a reason.
- Optional: Docker, for the compose smoke test (`make compose-smoke`; skipped without a running daemon).

On macOS arm64 (the primary development platform):

```sh
make venv sync develop     # arm64 .venv, locked dependencies, editable build of queuetruth._core
make env-check             # toolchain versions, venv architecture, free disk
```

`make venv` uses `PYTHON_BIN` (default `/opt/homebrew/bin/python3.13`; override with `make venv PYTHON_BIN=...`) and
checks that the venv is arm64. On Linux, create the venv yourself and continue as CI does:

```sh
uv venv --python python3.13 .venv
make sync develop
```

`make develop` builds the Rust extension with maturin (`maturin develop --release --uv`) into the venv and installs
the service package. Rebuild it after any change under `crates/`. Every Python command in the Makefile runs through
`uv run --frozen --no-sync`, so the lockfiles are never changed implicitly.

## Make targets

`make help` lists every target. The ones you will use most:

| Target | What it does |
|---|---|
| `make fmt` | format Rust and Python sources |
| `make lint` | `cargo fmt --check`, `cargo clippy -D warnings`, `ruff check`, `ruff format --check`, import layers (`tools/check_layers.py`) |
| `make test-rust` | all Rust tests |
| `make test-py` | Python package tests |
| `make test-service` | service tests on an ephemeral PostgreSQL 16 |
| `make test` | lint, Rust, extension build, Python and service tests |
| `make test-e2e` | end-to-end parity and determinism tests (builds `queuetruth-core`) |
| `make generate` / `make generate-check` | regenerate / verify every generated file, in dependency order (below) |
| `make schemas` / `make schemas-check` | regenerate / verify the committed JSON and Arrow schemas only |
| `make fixtures` | regenerate the committed synthetic fixtures and verify their manifests |
| `make openapi` | regenerate `service/openapi.json` |
| `make check-docs` | links, anchors, runnable blocks and generated reference pages in `docs/` |
| `make docs-build` | build the docs site with mkdocs-material in strict mode |
| `make check-placeholders` | fail on unfilled `{{PLACEHOLDERS}}` outside `docs/launch/` |
| `make examples` | run every script in `examples/` on the synthetic fixtures |
| `make release-check` | sdist and wheel, content checks, clean-venv smoke test (uploads nothing) |

`make realdata` and the `bench-*` targets need local copies of real market data and are not part of the normal
workflow.

## Test markers

Python tests use these pytest markers (declared in `pyproject.toml`). A test whose requirement is missing is skipped
with its reason printed.

| Marker | Needs |
|---|---|
| `requires_core` | the compiled extension (`make develop`) |
| `requires_postgres` | PostgreSQL binaries (`QT_PG_BIN`) or `QT_PG_DSN` |
| `requires_docker` | a running Docker daemon |
| `requires_hftbacktest` | the separate hftbacktest venv (`make hbt-crosscheck`, or `QT_HBT_PY` naming its python) |
| `requires_gray_reference` | `tests/fixtures/reference/gray_cmprsk.json` |
| `integration` | cross-component tests; deselected by default, run with `-m integration` |
| `realdata` | real market data under `.realdata/`; never run in CI |

Run one area with, for example, `uv run --frozen --no-sync pytest python/tests/score -k mcnemar`.

The integration suites run against the real engines in their own venvs, as the `integrations` CI job does:

```sh
make hbt-crosscheck PYTHON_BIN=python3.13   # .scratch/hbt-venv: hftbacktest 2.4.4 with the setup_venvs.sh pins
export QT_HBT_PY=$PWD/.scratch/hbt-venv/bin/python
make test-hftbacktest-plugin test-extra
# one venv per nautilus_trader release line, each with queuetruth installed (maturin build, then the wheel)
make test-nautilus QT_NT_PYTHON=$PWD/.scratch/nt-1.231.0/bin/python:$PWD/.scratch/nt-2.0.0rc9/bin/python
```

`QT_HBT_PY` names the hftbacktest venv's python (`QT_HBT_PYTHON` is the older name and still works). The exact
install commands and versions are in the `integrations` job of `.github/workflows/ci.yml`.

## Ground rules

- **No market data in the repository**, not even small excerpts or tables derived event by event from it. Fixtures
  are synthetic and generated by the project's own code. See [Data and licensing](docs/data-and-licensing.md).
- **Every claim has a test.** A new behaviour or a fixed bug comes with a test that would fail without it. Oracle
  tests carry a stable id in their name (`o_book_1_...`, `test_o_stat_1_...`); see
  [How queuetruth is tested](docs/testing.md).
- **Outputs stay deterministic.** Same inputs, spec and version give byte-identical outputs at any thread count, and
  every option that changes an output is a field of the command's spec.
- **Public names are a contract.** The CLI flags, Python API, schemas and HTTP endpoints are described in the
  [design specification](docs/design/spec.md) and the generated reference pages. A change to them needs a schema
  bump or a deprecation path; say so in the pull request.
- **Generated files are regenerated, not edited.** `make generate` runs every generator in order; `make
  generate-check` (in CI) fails when a committed file differs, names it and the command that rewrites it, and
  changes nothing. `uv run --frozen --no-sync python tools/generate.py --list` lists the steps: the importer
  registry (`LiveImportSpec.venue`, the CLI's live-import rules), `schemas/` and `crates/schema/src/live_venue.rs`,
  the Rust spec and document structs `crates/schema/src/specs_gen.rs` and `json_gen.rs` (step `rust-specs`,
  `scripts/gen_rust_specs.py`: generated from the JSON Schemas, so change the Pydantic model and regenerate; a hash
  on the first line of each makes a test fail when it is edited by hand), `service/openapi.json`,
  `crates/cli/src/python_commands.rs`, `crates/cli/src/flags_gen.rs` (step `cli-flags`, `crates/cli/gen_flags.py`:
  the flags of the advanced commands that map onto their spec, from the spec schemas; a new spec field becomes a
  flag, and a field that cannot needs an override or a reason in that script), `crates/cli/src/operations.rs` (step
  `operations`, `crates/cli/gen_operations.py`: the operation table for `queuetruth-core run`), the CLI golden files,
  `docs/reference/`, `docs/validated.md` and the README status block, the README chart, the prediction-market kit's
  schemas, and the synthetic zoo, tape, referee and NYSE fixtures. `--only STEP` runs one step. The steps that read
  the CLI grammar rebuild `queuetruth._core` first when
  an earlier step changed a Rust file.
- **Imports go down the layers.** In `python/queuetruth`: contracts, data adapters, engine, models, evaluation,
  operations, interfaces, products. No package imports another's private names. `tools/check_layers.py` (in
  `make lint`) enforces this; the exceptions that exist today are listed in `tools/check_layers_allow.txt`. Remove a
  line when you remove its import; do not add one.
- **One list per thing.** Source formats come from one registry (`crates/cli/src/formats.rs`, `_core.formats()`),
  importers (live venues, simulator outputs, trading bots) from the importer registry
  (`queuetruth.live.importers.registry()`): each importer module declares an `ImporterDef` with a typed options
  model, so a new venue is one Python file plus `make generate`. A copy kept by hand elsewhere needs a test that
  compares it with its source.
- **Dependencies.** Do not add or upgrade a dependency in a feature pull request. Propose it in an issue first; the
  maintainer updates `Cargo.lock` and `uv.lock` (`make lock`), which only admits releases at least two weeks old.
- **Third-party code.** hftbacktest is MIT and its ported logic is credited in `NOTICE`. NautilusTrader is LGPL-3.0:
  never copy its source; its behaviour is re-implemented from public documentation only.
- **Style.** Rust: `rustfmt`, `clippy -D warnings`, no `unsafe` outside the PyO3 bindings, no `unwrap` on
  input-derived data. Python: type hints on public functions, `ruff` with line length 120. Docs: plain Markdown that
  renders on GitHub, no em or en dashes (`check_docs.py` enforces this).

## Pull requests

1. Open an issue first for anything larger than a small fix, so the approach can be agreed before you write it.
2. Keep the pull request to one change. Fill in the template: what changed, why, and how it was tested.
   If users will notice the change, add a changelog fragment, `changelog.d/<slug>.<section>.md` (see
   [changelog.d/README.md](changelog.d/README.md)), instead of editing `CHANGELOG.md`: parallel pull requests then
   never conflict on the changelog. `python tools/changelog.py check` (in `make lint`) validates it.
3. Run `make lint test` (and `make test-e2e` or `make check-docs` when you touched those areas) before pushing.
4. Sign off every commit (see below).

## Developer Certificate of Origin

queuetruth uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/) (DCO) instead of a
contributor licence agreement. By signing off a commit you certify that you wrote the change, or otherwise have the
right to submit it under the project's licence. Add the sign-off with `git commit -s`, which appends a line such as:

```text
Signed-off-by: Your Name <you@example.com>
```

The name and email must match the commit author. To sign off commits you already made, use
`git rebase --signoff main` and force-push your branch. Pull requests with unsigned commits cannot be merged.

Contributions are licensed under the [Apache License 2.0](LICENSE), the same licence as the project.
