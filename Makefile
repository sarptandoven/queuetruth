# queuetruth: the single source of truth for build, test and gate commands.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ROOT := $(CURDIR)
export CARGO_TARGET_DIR := $(ROOT)/target
export CARGO_INCREMENTAL := 0
export UV_PYTHON_DOWNLOADS := never
export PYO3_PYTHON := $(ROOT)/.venv/bin/python

PYTHON_BIN ?= /opt/homebrew/bin/python3.13
VENV_PY := $(ROOT)/.venv/bin/python
RUN := uv run --frozen --no-sync
CARGO := cargo
PY_DIRS := $(wildcard python service scripts tests benches tools integrations) actions
# Test directories outside python/ and service/ (also in pyproject testpaths). Their optional dependencies
# (the hftbacktest venvs, queuetruth._core, real data) skip with a reason when absent.
EXTRA_TEST_DIRS := $(wildcard tools/livefill/tests scripts/nightly/tests scripts/scale/tests \
	integrations/hftbacktest/tests)
FIXTURE_DIRS := synth_small synth_small_nasdaq referee
# Local checkout of the author's earlier private et prototype (its out/ and data/ artifacts). Needed only by
# realdata, bench-et and the realdata bench suite; defaults to $QT_ET_ROOT, empty when unset.
ET_ROOT ?= $(QT_ET_ROOT)

.PHONY: help env-check venv lock sync develop fmt lint test-rust test-py test-service test-e2e test schemas \
	schemas-check fixtures openapi gate-0 gate-1 gate-2 gate-3 realdata bench bench-check compose-config \
	lint-py test-extra test-nautilus fuzz-py fuzz-rust nightly nightly-reduced \
	hbt-crosscheck clean-realdata check-placeholders check-docs docs-build examples wheel-smoke release-check compose-smoke dbn-samples bench-real bench-baseline bench-et test-bench reproduce reproduce-breadth

help: ## list targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | sort

env-check: ## print toolchain versions, venv arch and free disk; fail if the venv is not arm64
	@echo "machine: $$(uname -m)"
	@rustc --version && $(CARGO) --version && uv --version
	@echo "python: $$($(PYTHON_BIN) --version 2>&1) ($(PYTHON_BIN))"
	@test -x "$(VENV_PY)" || { echo "missing .venv (run make venv)"; exit 1; }
	@$(VENV_PY) -c "import platform, sys; m = platform.machine(); print('venv:', sys.version.split()[0], m); sys.exit(0 if m == 'arm64' else 1)"
	@df -h / | tail -1

venv: ## create the arm64 venv
	uv venv --python $(PYTHON_BIN) .venv
	$(VENV_PY) -c "import platform, sys; assert platform.machine() == 'arm64', platform.machine(); print(sys.version)"

lock: ## re-resolve uv.lock and Cargo.lock (maintainers only; review the diff)
	uv lock
	$(CARGO) +nightly generate-lockfile -Zmin-publish-age --config 'registries.crates-io.min-publish-age="14 days"'

sync: ## install locked Python dependencies (not the workspace packages)
	uv sync --frozen --all-packages --all-extras --group dev --no-install-workspace

develop: ## build crates/py into queuetruth._core (editable) and install the service package
	$(RUN) maturin develop --release --uv
	uv pip install --python .venv --no-deps -e service

fmt: ## format Rust and Python sources
	$(CARGO) fmt --all
	$(RUN) ruff format $(PY_DIRS)

lint: ## rustfmt check, clippy -D warnings, ruff check and format check
	$(CARGO) fmt --all --check
	$(CARGO) clippy --workspace --all-targets --locked -- -D warnings
	$(RUN) ruff check $(PY_DIRS)
	$(RUN) ruff format --check $(PY_DIRS)

lint-py: ## ruff check and format check only (the Python half of lint)
	$(RUN) ruff check $(PY_DIRS)
	$(RUN) ruff format --check $(PY_DIRS)

test-rust: ## all Rust tests including the cross-crate gate tests
	$(CARGO) test --workspace --locked --exclude queuetruth-py --features gate

test-py: ## Python package tests
	$(RUN) pytest python/tests

test-service: ## service tests
	$(RUN) pytest service/tests

test-extra: ## tests of tools/, integrations/ and scripts/ (livefill kit, nightly runner, scale tracing, hbt)
	$(RUN) pytest $(EXTRA_TEST_DIRS)

# A separate session under the plugin's own pytest config: its tests import their conftest by module name, which
# resolves to another directory's conftest.py in the root session. Engine tests skip without nautilus_trader.
test-nautilus: ## queuetruth-nautilus plugin tests (integrations/nautilus, its own pytest session)
	$(RUN) pytest integrations/nautilus/tests

test-e2e: ## end-to-end tests (integration included); builds queuetruth-core for the O-PARITY-1 binary arm
	$(CARGO) build --locked -p queuetruth-cli --bin queuetruth-core
	$(RUN) pytest tests/e2e -m "integration or not integration"

test: lint test-rust develop test-py test-service test-extra test-nautilus ## everything except e2e

FUZZ_EXAMPLES ?= 400
FUZZ_STATS ?= .scratch/fuzz-py-stats.json

fuzz-py: ## deep random campaign of the Python parser fuzz suite (FUZZ_EXAMPLES per property; stats to FUZZ_STATS)
	mkdir -p $(dir $(FUZZ_STATS))
	QT_FUZZ_PROFILE=deep QT_FUZZ_EXAMPLES=$(FUZZ_EXAMPLES) QT_FUZZ_STATS=$(FUZZ_STATS) \
		$(RUN) pytest python/tests/fuzz -m fuzz

fuzz-rust: ## Rust fuzz harness tests on stable: seeds, regressions and known crashes still reproduce (no libFuzzer)
	cd fuzz && $(CARGO) +stable test --locked

NIGHTLY_ARGS ?=

nightly: ## local nightly cross-checks, full mode (hbt, NT, repro, bench; venvs and results in .scratch/nightly)
	bash scripts/nightly/nightly.sh run --mode full $(NIGHTLY_ARGS)

nightly-reduced: ## the nightly cross-checks in reduced mode (fewer shadows, one repro day, two bench cases)
	bash scripts/nightly/nightly.sh run --mode reduced $(NIGHTLY_ARGS)

schemas: ## regenerate schemas/json, schemas/arrow, schemas/samples, schemas/run_id_kat.json
	$(RUN) python scripts/gen_schemas.py

schemas-check: ## fail if the committed schema files differ from the generated ones
	$(RUN) python scripts/gen_schemas.py --check

fixtures: ## regenerate the committed synthetic fixtures and verify their manifests
	$(CARGO) run --locked --release -p queuetruth-testkit --example make_fixtures
	for d in $(FIXTURE_DIRS); do (cd tests/fixtures/$$d && shasum -a 256 -c MANIFEST.sha256); done

openapi: ## regenerate service/openapi.json
	$(RUN) python -m queuetruth_service.export_openapi > service/openapi.json

gate-0: ## scaffold must-pass list (workspace, types, schemas, golden values)
	$(CARGO) build --workspace --locked
	$(CARGO) test --locked -p queuetruth-types -p queuetruth-schema -p queuetruth-zoo
	$(CARGO) clippy --workspace --all-targets --locked -- -D warnings && $(CARGO) fmt --all --check
	$(MAKE) develop
	$(RUN) pytest python/tests/test_schemas.py python/tests/reference -q
	$(RUN) ruff check python scripts && $(RUN) ruff format --check python scripts

gate-1: ## foundation gate (book, replay, ingest, L2, crypto, testkit, stats)
	$(CARGO) test --locked --features gate -p queuetruth-types -p queuetruth-schema -p queuetruth-book \
		-p queuetruth-replay -p queuetruth-ingest -p queuetruth-l2 -p queuetruth-crypto -p queuetruth-testkit
	$(MAKE) fixtures
	$(CARGO) clippy --locked --all-targets -p queuetruth-book -p queuetruth-replay -p queuetruth-ingest \
		-p queuetruth-l2 -p queuetruth-crypto -p queuetruth-testkit -- -D warnings
	$(CARGO) fmt -p queuetruth-book -p queuetruth-replay -p queuetruth-ingest -p queuetruth-l2 \
		-p queuetruth-crypto -p queuetruth-testkit -- --check
	$(RUN) pytest python/tests/stats python/tests/eval python/tests/model python/tests/live \
		-m "integration or not integration"

gate-2: ## main-build gate (everything, integration tests, schemas)
	$(MAKE) test
	$(RUN) pytest python/tests -m integration
	$(MAKE) schemas-check

gate-3: ## integration gate (e2e, real data, benches, release, docs, compose)
	$(MAKE) test
	$(MAKE) test-e2e
	$(MAKE) fuzz-rust
	$(MAKE) realdata
	$(MAKE) bench
	$(MAKE) release-check
	$(MAKE) examples
	$(MAKE) check-placeholders
	$(MAKE) check-docs
	$(MAKE) docs-build
	$(MAKE) compose-config
	$(MAKE) compose-smoke

CHECK_DOCS := $(firstword $(wildcard scripts/check_docs.py docs/check_docs.py))
DIST_DIR := .scratch/dist
SMOKE_VENV := .scratch/smoke-venv

check-docs: ## docs link and runnable-block check
	@test -n "$(CHECK_DOCS)" || { echo "check_docs.py missing (scripts/ or docs/)"; exit 1; }
	$(RUN) python $(CHECK_DOCS)
	$(RUN) python docs/assets/readme_chart.py --self-test
	$(RUN) python docs/assets/readme_chart.py --check

check-placeholders: ## fail on unfilled {{PLACEHOLDERS}} in README.md and docs/ (docs/launch/ templates excepted)
	@! grep -rnE --include="*.md" "\{\{[A-Z0-9_]+\}\}" README.md docs --exclude-dir=launch

docs-build: ## build the docs site with mkdocs-material (strict) into .scratch/site
	$(RUN) mkdocs build --strict --site-dir .scratch/site

RELEASE_DIR := .scratch/release

release-check: ## sdist + dist wheel, content checks against the tree, clean-venv smoke test (uploads nothing)
	rm -rf $(RELEASE_DIR)
	$(RUN) maturin sdist --out $(RELEASE_DIR)/dist
	$(RUN) maturin build --profile dist --locked --out $(RELEASE_DIR)/dist
	$(PYTHON_BIN) -I scripts/release/check_dist.py $(RELEASE_DIR)/dist --source . --require-sdist
	bash scripts/release/smoke.sh $(PYTHON_BIN) "$$(ls $(RELEASE_DIR)/dist/queuetruth-*.whl | head -1)" $(RELEASE_DIR)/smoke
	rm -rf $(RELEASE_DIR)

examples: ## run every example script on the synthetic fixtures
	for f in examples/0*.py; do echo "== $$f"; $(RUN) python $$f; done

wheel-smoke: ## dist wheel + clean arm64 venv (no repo on PYTHONPATH): doctor, fixtures synth, example 01
	rm -rf $(DIST_DIR) $(SMOKE_VENV)
	$(RUN) maturin build --profile dist --out $(DIST_DIR)
	uv venv --python $(PYTHON_BIN) $(SMOKE_VENV)
	uv pip install --python $(SMOKE_VENV) $(DIST_DIR)/queuetruth-*.whl
	cd $(SMOKE_VENV) && env -u PYTHONPATH bin/queuetruth doctor
	cd $(SMOKE_VENV) && env -u PYTHONPATH bin/queuetruth fixtures synth --out synth
	cd $(SMOKE_VENV) && env -u PYTHONPATH QT_EXAMPLE_DATA=synth QT_EXAMPLE_OUT=ex-out bin/python $(ROOT)/examples/01_replay_shadows.py
	rm -rf $(DIST_DIR) $(SMOKE_VENV)

reproduce: ## reproduce every published real-data number from the public Nasdaq ITCH files (streams, writes .realdata/repro/)
	$(RUN) python scripts/repro/reproduce.py

reproduce-breadth: ## the 36-symbol, 5-tier breadth study from the public Nasdaq ITCH files (writes .realdata/repro/breadth/)
	$(RUN) python scripts/repro/breadth.py

realdata: ## real-data validation against et prototype artifacts (needs ET_ROOT; writes .realdata/ only)
	@test -n "$(ET_ROOT)" || { echo "set ET_ROOT (or QT_ET_ROOT) to the et checkout"; exit 1; }
	$(RUN) python scripts/realdata/validate_et.py --et-root "$(ET_ROOT)" --out .realdata

BENCH_SUITE ?= gate
BENCH_OUT ?= .realdata/bench.json

bench: ## SPEC 7.1 harness: suite $(BENCH_SUITE) (quick|full|realdata|gate) -> $(BENCH_OUT)
	$(CARGO) build --locked --release -p queuetruth-cli --bin queuetruth-core
	test -f "$$($(VENV_PY) -c 'import queuetruth._core as m; print(m.__file__)')" || $(MAKE) develop
	$(RUN) python benches/bench.py --suite $(BENCH_SUITE) --core $(CARGO_TARGET_DIR)/release/queuetruth-core \
		$(if $(ET_ROOT),--et-root "$(ET_ROOT)") --out $(BENCH_OUT)

dbn-samples: ## opt-in: fetch Databento's Apache-2.0 DBN MBO test files (pinned commit) and check ingest + replay
	$(RUN) python scripts/realdata/dbn_samples.py --out .realdata/dbn-samples

bench-real: ## RD-13 only: day-one cases (outputs stay in .realdata/)
	$(MAKE) bench BENCH_SUITE=realdata

bench-check: ## regression gate vs benches/baseline.json (exit 1 regression, 2 error, 3 env not comparable)
	$(RUN) python benches/check.py --baseline benches/baseline.json --current $(BENCH_OUT) \
		--json .realdata/bench-check.json

bench-baseline: ## promote the last results to benches/baseline.json (review the diff before committing)
	$(RUN) python benches/check.py --promote $(BENCH_OUT) --out benches/baseline.json

bench-et: ## re-measure the et prototype reference (needs ET_ROOT; read-only on et; ~15 min) -> benches/reference_et.json
	@test -n "$(ET_ROOT)" || { echo "set ET_ROOT (or QT_ET_ROOT) to the et checkout"; exit 1; }
	$(RUN) python scripts/bench/et_baseline.py --et-root "$(ET_ROOT)" \
		--scratch .scratch/bench-et --out benches/reference_et.json
	rm -rf .scratch/bench-et

test-bench: ## unit tests of the harness statistics and gate
	$(RUN) pytest tests/e2e/test_bench_check.py

compose-smoke: ## compose up, tenant, replay job, verified downloads, compose down (skips without Docker)
	bash scripts/compose_smoke.sh

compose-config: ## validate the compose file (needs the Docker CLI, not the daemon)
	docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env.example config -q

hbt-crosscheck: ## optional O-ZOO-3 in a separate venv (hftbacktest 2.4.4)
	uv venv .scratch/hbt-venv --python $(PYTHON_BIN)
	uv pip install --python .scratch/hbt-venv numba numpy
	uv pip install --python .scratch/hbt-venv --no-deps hftbacktest==2.4.4  # its polars pin does not resolve offline
	.scratch/hbt-venv/bin/python python/tests/zoo/hbt_crosscheck.py tests/fixtures/zoo/npz

clean-realdata: ## remove real-data outputs and scratch
	rm -rf .realdata .scratch
