"""Builds the example notebooks from their percent-format sources, and optionally executes them.

Each `src/NN_name.py` is a plain Python script whose cells are separated by `# %%` lines (`# %% [markdown]` cells
hold Markdown as `# ` comment lines). `build.py` turns each source into `NN_name.ipynb` next to this file, inserts
the "Open in Colab" badge under the title and a setup cell that installs queuetruth from PyPI when it is missing
(Colab), and with `--execute` runs the notebook top to bottom with nbclient and stores the outputs. One of
`--execute` and `--no-outputs` is required, so a plain `build.py` never silently replaces the stored outputs with
empty ones.

Executing needs `nbclient`, `nbformat` and `ipykernel` (plus pandas and matplotlib, which the notebooks use). They
are not queuetruth dependencies: install them in a throwaway virtual environment, for example

    uv venv .scratch/nbvenv && uv pip install --python .scratch/nbvenv/bin/python \
        nbclient nbformat ipykernel pandas matplotlib pyarrow numpy scipy scikit-learn pydantic
    PYTHONPATH=$PWD/python .scratch/nbvenv/bin/python examples/notebooks/build.py --execute \
        --workdir .scratch/nbrun

The kernel is an ipykernel started by this interpreter (an installed `python3` kernelspec cannot substitute another
Python) and runs in `--workdir` (default: a fresh temporary directory), so the notebooks' outputs never land in the
repository; that is also why `PYTHONPATH` must be absolute. After execution the build fails if a notebook is larger
than `--max-kb` or if any output contains the working directory, the home directory or the repository path (outputs
must be portable and must not leak local paths). Execution timing metadata is not stored, so a rebuild only changes
outputs that really changed.

Environment passed through to the kernel: `QT_NB_DATASET` (notebook 02: an order-level dataset directory to use
instead of the synthetic one).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
REPO_URL = "github/sarptandoven/queuetruth/blob/main/examples/notebooks"
BADGE = (
    "[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)]"
    "(https://colab.research.google.com/{repo}/{name})"
)
SETUP = """# Setup: on Colab (or anywhere queuetruth is missing) this installs it from PyPI; elsewhere it does nothing.
import importlib
import importlib.util
import subprocess
import sys

if importlib.util.find_spec("queuetruth") is None:
    try:
        from IPython import get_ipython

        shell = get_ipython()
    except ImportError:
        shell = None
    if shell is not None:  # %pip lets Colab offer a restart when an already-imported package was upgraded
        shell.run_line_magic("pip", "install -q queuetruth pandas matplotlib")
    else:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "queuetruth", "pandas", "matplotlib"])
    importlib.invalidate_caches()
if importlib.util.find_spec("queuetruth") is None or importlib.util.find_spec("queuetruth.frames") is None:
    raise RuntimeError(
        "this notebook needs queuetruth.frames (first released after queuetruth 0.1.1): pip install -U queuetruth"
    )"""

CELL_RE = re.compile(r"^# %%(?P<kind> \[markdown\])?\s*$")


def parse(path: Path) -> list[tuple[str, str]]:
    """`(kind, source)` cells of one percent-format file; `kind` is `markdown` or `code`."""
    cells: list[tuple[str, list[str]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = CELL_RE.match(line)
        if m:
            cells.append(("markdown" if m.group("kind") else "code", []))
            continue
        if not cells:
            if line.strip():
                raise SystemExit(f"{path}: text before the first '# %%' line")
            continue
        kind, lines = cells[-1]
        if kind == "markdown":
            if line and not line.startswith("#"):
                raise SystemExit(f"{path}: markdown cell line without '#': {line!r}")
            line = line[2:] if line.startswith("# ") else line[1:]
        lines.append(line)
    out = []
    for kind, lines in cells:
        text = "\n".join(lines).strip("\n")
        if text:
            out.append((kind, text))
    return out


def build(path: Path) -> Any:
    """The notebook (nbformat v4) for one source file, with badge and setup cell, outputs empty."""
    import nbformat

    cells = parse(path)
    if not cells or cells[0][0] != "markdown" or not cells[0][1].startswith("# "):
        raise SystemExit(f"{path}: the first cell must be markdown starting with a '# ' title")
    name = path.with_suffix(".ipynb").name
    title, _, rest = cells[0][1].partition("\n")
    badge = BADGE.format(repo=REPO_URL, name=name)
    nb = nbformat.v4.new_notebook()
    nb.metadata = {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python"},
        "colab": {"provenance": []},
    }
    first = f"{title}\n\n{badge}\n\n{rest.strip()}".rstrip()
    nb.cells.append(nbformat.v4.new_markdown_cell(first))
    nb.cells.append(nbformat.v4.new_code_cell(SETUP))
    for kind, text in cells[1:]:
        cell = nbformat.v4.new_markdown_cell(text) if kind == "markdown" else nbformat.v4.new_code_cell(text)
        nb.cells.append(cell)
    for i, cell in enumerate(nb.cells):
        cell["id"] = f"{path.stem[:2]}-{i:02d}"
    return nb


def execute(nb: Any, workdir: Path, timeout: int) -> None:
    """Runs the notebook in `workdir` with an ipykernel started by this interpreter (`sys.executable`).

    The kernel spec manager searches no kernel directories, so only ipykernel's built-in `python3` spec (which
    launches `sys.executable`) exists; a user- or system-installed `python3` kernelspec cannot substitute another
    Python."""
    from jupyter_client.kernelspec import KernelSpecManager
    from jupyter_client.manager import KernelManager
    from nbclient import NotebookClient

    workdir.mkdir(parents=True, exist_ok=True)
    ksm = KernelSpecManager(kernel_dirs=[])
    spec = ksm.get_kernel_spec("python3")
    if not spec.argv or Path(spec.argv[0]).resolve() != Path(sys.executable).resolve():
        raise SystemExit(f"the python3 kernel would run {spec.argv[:1]}, not this interpreter {sys.executable}")
    km = KernelManager(kernel_name="python3", kernel_spec_manager=ksm)
    client = NotebookClient(
        nb, km=km, timeout=timeout, resources={"metadata": {"path": str(workdir)}}, record_timing=False
    )
    client.execute()


def check(nb: Any, text: str, name: str, forbidden: list[str], max_kb: int) -> int:
    """Size in bytes of the serialized notebook `text`; exits on a cell error or a forbidden path in any output of
    `nb` (a notebook node or plain dict), or when `text` is above `max_kb`."""
    for cell in nb["cells"]:
        for out in cell.get("outputs", []):
            blob = str(out)
            for f in forbidden:
                if f and f in blob:
                    i = blob.index(f)
                    raise SystemExit(
                        f"{name}: output of cell {cell.get('id')} contains the local path {f!r} "
                        f"(...{blob[max(0, i - 80) : i + 80]}...); print relative paths only"
                    )
            if out.get("output_type") == "error":
                raise SystemExit(f"{name}: a cell raised {out.get('ename')}: {out.get('evalue')}")
    size = len(text.encode("utf-8"))
    if size > max_kb * 1024:
        raise SystemExit(f"{name}: {size / 1024:.0f} KB is above --max-kb {max_kb}")
    return size


def main(argv: list[str] | None = None) -> None:
    """Builds (and with `--execute` runs) every notebook, or the ones named."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="source stems to build (default: all), e.g. 01_quickstart")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--execute", action="store_true", help="run each notebook and store its outputs")
    mode.add_argument(
        "--no-outputs",
        action="store_true",
        help="write the notebooks without running them (replaces the stored outputs with empty ones)",
    )
    ap.add_argument("--workdir", type=Path, help="kernel working directory (default: a fresh temporary directory)")
    ap.add_argument("--timeout", type=int, default=1200, help="per-cell timeout in seconds")
    ap.add_argument("--max-kb", type=int, default=200, help="largest allowed notebook size")
    a = ap.parse_args(argv)
    import nbformat

    sources = sorted(SRC.glob("[0-9][0-9]_*.py"))
    if a.names:
        sources = [s for s in sources if s.stem in set(a.names)]
        missing = set(a.names) - {s.stem for s in sources}
        if missing:
            raise SystemExit(f"no source for {sorted(missing)} in {SRC}")
    tmp = None
    if a.execute and a.workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="qt-notebooks-")
        a.workdir = Path(tmp.name)
    forbidden = [str(HERE.parents[1]), str(Path.home())]
    if a.workdir is not None:
        forbidden.append(str(a.workdir.resolve()))
    try:
        for src in sources:
            nb = build(src)
            if a.execute:
                print(f"executing {src.stem} ...", file=sys.stderr, flush=True)
                execute(nb, a.workdir.resolve(), a.timeout)
            text = nbformat.writes(nb) + "\n"
            size = check(nb, text, src.stem, forbidden, a.max_kb)
            out = HERE / f"{src.stem}.ipynb"
            out.write_text(text, encoding="utf-8")
            print(f"{out.relative_to(HERE.parents[1])}: {len(nb.cells)} cells, {size / 1024:.1f} KB")
    finally:
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    os.environ.setdefault("PYDEVD_DISABLE_FILE_VALIDATION", "1")
    main()
