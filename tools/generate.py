"""Regenerate every generated file of the repository, in dependency order (`make generate`), or check that the
committed files are current (`make generate-check`). Standard library only; each step runs its own generator.

    uv run --frozen --no-sync python tools/generate.py                 # write every generated file
    uv run --frozen --no-sync python tools/generate.py --check         # exit 1 and list the stale files
    uv run --frozen --no-sync python tools/generate.py --only schemas  # one step (repeatable); --list names them

`--check` runs each generator on the outputs of the steps before it, compares what it writes with the files on disk,
and puts every file back at the end, so it changes nothing. A stale file is listed with its step and the command that
rewrites it; a generator that fails is reported and the other steps still run.

The steps, in order (a later step reads what an earlier one writes):

1. importers: the importer registry -> `LiveImportSpec.venue` values and the CLI's live-import rules
2. schemas: JSON Schemas, Arrow schemas, samples, run id known answers and the Rust `LiveVenue`
3. rust-specs: the Rust spec and document structs (`crates/schema/src/specs_gen.rs`, `json_gen.rs`) from the JSON
   Schemas and samples
4. openapi: service/openapi.json
5. python-commands: the clap rows of the Python-only command groups
6. extension: rebuilds `queuetruth._core` (`make develop`) when a step above changed a Rust file; the steps
   below read the CLI grammar through it (write mode only)
7. cli-golden: the CLI parse golden files (`QT_BLESS=1 cargo test -p queuetruth-cli --test cli_parse`)
8. reference: docs/reference/*.md (CLI, Python API, HTTP API, MCP, schemas, importers)
9. validated: docs/validated.md and the README status block
10. readme-chart: docs/assets/fill-bias-1s.svg from docs/RESULTS.md
11. zoo-fixtures: the O-ZOO-2 / O-ZOO-4 parity fixtures (python/tests/zoo/gen_fixtures.py)
12. synth-fixtures: the synthetic tapes, own orders and referee fixtures (`make fixtures`)
13. nyse-fixtures: the synthetic NYSE Integrated Feed days (`QT_BLESS=1 cargo test -p queuetruth-ingest`)

Not run here, on purpose: scripts/reference/gen_all.py (reference values from SciPy, statsmodels and R, frozen and
recorded with their versions in PROVENANCE.json), and the one-off fixture writers that need an outside tool
(tests/fixtures/live/hummingbot/make_fixtures.py, crates/crypto/tests/fixtures/kraken_l3/gen.py,
fuzz/examples/gen_seeds.rs).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CARGO = os.environ.get("CARGO", "cargo")


@dataclass(frozen=True)
class Step:
    name: str
    what: str
    command: tuple[str, ...]
    #: files the step writes (globs relative to the repository root)
    outputs: tuple[str, ...]
    #: write the command's standard output to this file instead
    stdout: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    #: the command a person runs to fix a stale file
    fix: str = ""

    def fix_command(self) -> str:
        if self.fix:
            return self.fix
        cmd = ["uv run --frozen --no-sync python" if c == PY else c for c in self.command]
        return " ".join([*(f"{k}={v}" for k, v in self.env.items()), *cmd])


def _py(*args: str) -> tuple[str, ...]:
    return (PY, *args)


STEPS: tuple[Step, ...] = (
    Step(
        "importers",
        "the importer registry -> LiveImportSpec.venue values and the CLI's live-import rules",
        _py("scripts/gen_importers.py"),
        ("python/queuetruth/schemas/_live_venues.py", "crates/cli/src/live_importers.rs"),
    ),
    Step(
        "schemas",
        "JSON and Arrow schemas, samples, run id known answers, crates/schema/src/live_venue.rs",
        _py("scripts/gen_schemas.py"),
        (
            "schemas/json/*.json",
            "schemas/arrow/*.json",
            "schemas/samples/*.json",
            "schemas/run_id_kat.json",
            "crates/schema/src/live_venue.rs",
        ),
        fix="make schemas",
    ),
    Step(
        "pm-schemas",
        "the prediction-market kit's JSON and Arrow schemas (python/queuetruth/pm/schemas)",
        _py("-c", "from queuetruth.pm.schemas import export; export()"),
        ("python/queuetruth/pm/schemas/*.json",),
    ),
    Step(
        "rust-specs",
        "Rust spec and document structs from the JSON Schemas (crates/schema/src/specs_gen.rs, json_gen.rs)",
        _py("scripts/gen_rust_specs.py"),
        ("crates/schema/src/specs_gen.rs", "crates/schema/src/json_gen.rs"),
    ),
    Step(
        "openapi",
        "service/openapi.json",
        _py("-m", "queuetruth_service.export_openapi"),
        ("service/openapi.json",),
        stdout="service/openapi.json",
        fix="make openapi",
    ),
    Step(
        "python-commands",
        "clap rows of the Python-only command groups",
        _py("crates/cli/gen_python_commands.py"),
        ("crates/cli/src/python_commands.rs",),
    ),
    Step(
        "extension",
        "rebuild queuetruth._core when a step above changed a Rust file",
        ("make", "develop"),
        (),
    ),
    Step(
        "cli-golden",
        "CLI parse golden files",
        (CARGO, "test", "--locked", "-q", "-p", "queuetruth-cli", "--test", "cli_parse"),
        ("crates/cli/tests/golden/*.json",),
        env={"QT_BLESS": "1"},
    ),
    Step(
        "reference",
        "docs/reference pages",
        _py("docs/gen_reference.py"),
        tuple(
            f"docs/reference/{p}"
            for p in ("cli.md", "python-api.md", "http-api.md", "mcp.md", "schemas.md", "importers.md")
        ),
    ),
    Step(
        "validated",
        "docs/validated.md and the README status block",
        _py("docs/gen_validated.py"),
        ("docs/validated.md", "README.md"),
    ),
    Step(
        "readme-chart",
        "the README chart from docs/RESULTS.md",
        _py("docs/assets/readme_chart.py"),
        ("docs/assets/fill-bias-1s.svg",),
    ),
    Step(
        "zoo-fixtures",
        "zoo parity fixtures",
        _py("python/tests/zoo/gen_fixtures.py", "--write"),
        (
            "tests/fixtures/zoo/hbt_parity/*",
            "tests/fixtures/zoo/l3_faithful.json",
            "tests/fixtures/zoo/MANIFEST.sha256",
        ),
    ),
    Step(
        "synth-fixtures",
        "synthetic tapes, own orders and referee fixtures",
        (CARGO, "run", "--locked", "-q", "--release", "-p", "queuetruth-testkit", "--example", "make_fixtures"),
        (
            "tests/fixtures/synth_small/**/*",
            "tests/fixtures/synth_small_nasdaq/**/*",
            "tests/fixtures/referee/**/*",
        ),
        fix="make fixtures",
    ),
    Step(
        "nyse-fixtures",
        "synthetic NYSE Integrated Feed days",
        (CARGO, "test", "--locked", "-q", "-p", "queuetruth-ingest", "--lib", "synthetic_fixtures_are_current"),
        ("tests/fixtures/nyse/*.csv",),
        env={"QT_BLESS": "1"},
    ),
)
_BY_NAME = {s.name: s for s in STEPS}


def _files(globs: tuple[str, ...]) -> set[Path]:
    out: set[Path] = set()
    for g in globs:
        out |= {p for p in ROOT.glob(g) if p.is_file()}
    return out


def _snapshot(step: Step) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in _files(step.outputs)}


def _restore(before: dict[Path, bytes], step: Step) -> None:
    for p in _files(step.outputs) - set(before):
        p.unlink()
    for p, data in before.items():
        if not p.exists() or p.read_bytes() != data:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)


def _run(step: Step) -> bool:
    """Runs one step; False (with its output on stderr) when it fails."""
    env = {**os.environ, **step.env}
    t0 = time.monotonic()
    r = subprocess.run(step.command, cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode == 0 and step.stdout is not None:
        (ROOT / step.stdout).write_text(r.stdout, encoding="utf-8")
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-4000:] + r.stderr[-4000:])
        print(f"  {step.name}: FAILED ({' '.join(step.command)}, exit {r.returncode})", file=sys.stderr, flush=True)
        return False
    print(f"  {step.name}: {step.what} ({time.monotonic() - t0:.0f}s)", flush=True)
    return True


def _changed(before: dict[Path, bytes], step: Step) -> list[str]:
    after = _snapshot(step)
    paths = set(before) | set(after)
    return sorted(str(p.relative_to(ROOT)) for p in paths if before.get(p) != after.get(p))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="regenerate (or check) every generated file")
    ap.add_argument("--check", action="store_true", help="exit 1 when a committed file differs; change nothing")
    ap.add_argument("--only", action="append", choices=list(_BY_NAME), metavar="STEP", help="run only these steps")
    ap.add_argument("--list", action="store_true", help="list the steps and exit")
    a = ap.parse_args(argv)
    if a.list:
        for i, s in enumerate(STEPS, 1):
            print(f"{i:2}. {s.name}: {s.what}")
        return 0
    steps = [s for s in STEPS if not a.only or s.name in a.only]
    print("generate-check:" if a.check else "generate:", ", ".join(s.name for s in steps), flush=True)
    stale: list[tuple[Step, list[str]]] = []
    failed: list[Step] = []
    # In check mode every step runs on the outputs of the steps before it (as `make generate` would), and the files
    # are put back at the end.
    saved: list[tuple[Step, dict[Path, bytes]]] = []
    rust_changed = False
    try:
        for step in steps:
            if step.name == "extension":
                if rust_changed and not a.check and not _run(step):
                    return 1
                continue
            before = _snapshot(step)
            saved.append((step, before))
            if not _run(step):
                if not a.check:
                    return 1
                failed.append(step)
                continue
            changed = _changed(before, step)
            if changed:
                stale.append((step, changed))
                rust_changed |= any(c.endswith(".rs") for c in changed)
                if not a.check:
                    for c in changed:
                        print(f"    wrote {c}")
    finally:
        if a.check:
            for step, before in reversed(saved):
                _restore(before, step)
    if not a.check:
        return 0
    if stale:
        print("\nstale generated files (the committed copy differs from the generator):", file=sys.stderr)
        for step, files in stale:
            print(f"- {step.name}: run `{step.fix_command().strip()}`, or `make generate`", file=sys.stderr)
            for f in files:
                print(f"    {f}", file=sys.stderr)
    for step in failed:
        print(f"- {step.name}: the generator failed (output above)", file=sys.stderr)
    if stale or failed:
        return 1
    print("every generated file is current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
