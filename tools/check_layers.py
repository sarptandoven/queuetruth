"""Check the import layering of `python/queuetruth` (plan section 5.1). Standard library only.

Rules, read from every import in the source, function-local ones included:

1. layer: a package imports only its own layer or a lower one. The layers, from the bottom: contracts, data
   adapters, engine, models, evaluation, operations, interfaces, products (`LAYERS` below).
2. cycle: two packages of the same layer do not import each other.
3. private: no import of another package's private name (`_name`, `pkg._module`, or anything below them). A
   package's own private names are fine. `queuetruth._core` and `queuetruth._version` are public.
4. tests: code that ships or runs outside the test suites does not import test code (including by adding a tests
   directory to `sys.path`).

Rules 1 and 2 cover `python/queuetruth`. Rules 3 and 4 also cover `service`, `tools`, `scripts`, `integrations`,
`benches` and `actions`; test files (a `tests` directory, `test_*.py`, `conftest.py`) are exempt from both.

Today's violations are listed in `tools/check_layers_allow.txt`, one per line. A new violation fails, and so does a
listed one that is gone (remove its line). Run `python tools/check_layers.py --write-allow` only to record an
intended change of the list, and say why in the pull request.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO / "python" / "queuetruth"
ALLOW_FILE = REPO / "tools" / "check_layers_allow.txt"
OTHER_ROOTS = ("service", "tools", "scripts", "integrations", "benches", "actions")

#: The layers from the bottom, and the top-level modules or subpackages of `queuetruth` in each. `queuetruth`
#: itself (the root `__init__.py`, which re-exports `api`) is an interface.
LAYERS: dict[str, tuple[str, ...]] = {
    "contracts": ("_version", "errors", "schemas", "stats"),
    "data adapters": ("live", "frames", "backtesters"),
    "engine": ("_core",),
    "models": ("model",),
    "evaluation": ("eval", "score", "report", "dq"),
    "operations": ("api", "export", "ledger", "cert", "check", "fit"),
    "interfaces": ("queuetruth", "cli", "client", "mcp"),
    "products": ("pm", "forecast", "sandbox", "demo", "atlas", "backtrader", "lean"),
}
LAYER_OF = {pkg: i for i, names in enumerate(LAYERS.values()) for pkg in names}
LAYER_NAME = list(LAYERS)
PUBLIC_UNDERSCORE = {"_core", "_version"}


@dataclass(frozen=True)
class Violation:
    source: str  # importing module, dotted (`queuetruth.score.runner`) or a repo path for files outside the package
    target: str  # the imported module or name, dotted
    rule: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.source} -> {self.target}"


def is_test_file(path: Path) -> bool:
    rel = path.relative_to(REPO)
    return "tests" in rel.parts or path.name.startswith("test_") or path.name == "conftest.py"


def module_name(path: Path) -> str:
    parts = list(path.relative_to(PKG_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def top_package(dotted: str) -> str:
    """`queuetruth.score.runner` -> `score`; `queuetruth` -> `queuetruth`."""
    parts = dotted.split(".")
    return parts[1] if len(parts) > 1 else "queuetruth"


def is_module(dotted: str) -> bool:
    """Whether a dotted `queuetruth...` name is a module or package on disk (or the compiled extension)."""
    if dotted == "queuetruth._core":
        return True
    rel = Path(*dotted.split(".")[1:]) if "." in dotted else Path()
    base = PKG_ROOT / rel
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def imported_names(tree: ast.AST, path: Path, own_module: str | None) -> list[tuple[str, int]]:
    """Every `queuetruth...` name imported in `tree`, as dotted names (a module, or a module plus an attribute)."""
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names if a.name.split(".")[0] == "queuetruth"]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if own_module is None:
                    continue
                base = own_module.split(".")
                if path.name != "__init__.py":
                    base = base[:-1]
                base = base[: len(base) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if mod.split(".")[0] != "queuetruth":
                continue
            for alias in node.names:
                full = f"{mod}.{alias.name}"
                out.append((full, node.lineno) if alias.name != "*" else (mod, node.lineno))
    return out


def resolve(full: str) -> tuple[str, str | None]:
    """Split an imported dotted name into (module, attribute or None)."""
    if is_module(full):
        return full, None
    mod, _, attr = full.rpartition(".")
    return mod, attr


def private_part(full: str) -> str | None:
    """The private component of a dotted name below its top package, if any (`api._validate` -> `_validate`)."""
    for part in full.split(".")[2:]:
        if part.startswith("_") and not part.startswith("__"):
            return part
    if full.count(".") == 1:
        last = full.split(".")[1]
        if last.startswith("_") and last not in PUBLIC_UNDERSCORE and not last.startswith("__"):
            return last
    return None


def private_attribute_uses(tree: ast.AST, path: Path, own_module: str | None) -> set[str]:
    """`alias._name` where `alias` is bound to a `queuetruth` module by an import in this file (any scope)."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "queuetruth" and a.asname:
                    aliases[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom):
            for (full, _line), alias in zip(
                imported_names(ast.Module(body=[node], type_ignores=[]), path, own_module), node.names, strict=False
            ):
                if alias.name != "*" and is_module(full):
                    aliases[alias.asname or alias.name] = full
    uses = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and node.attr.startswith("_")
            and not node.attr.startswith("__")
        ):
            uses.add(f"{aliases[node.value.id]}.{node.attr}")
    return uses


def tests_on_sys_path(tree: ast.AST) -> list[int]:
    """Lines that add a directory named `tests` to `sys.path` (`sys.path.insert(0, str(REPO / "python" / "tests"))`)."""
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append"}
            and ast.unparse(node.func.value) == "sys.path"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "tests" in sub.value.split("/"):
                    lines.append(node.lineno)
                    break
    return lines


def collect() -> tuple[list[Violation], list[str]]:
    violations: list[Violation] = []
    errors: list[str] = []
    # package-level edges inside one layer, for the cycle rule: (src pkg, dst pkg) -> [(source module, target)]
    same_layer: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)

    files = sorted(PKG_ROOT.rglob("*.py"))
    for root in OTHER_ROOTS:
        files += sorted((REPO / root).rglob("*.py"))
    for path in files:
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        in_pkg = PKG_ROOT in path.parents
        if not in_pkg and is_test_file(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(REPO)}: cannot parse: {exc}")
            continue
        own = module_name(path) if in_pkg else None
        source = own or str(path.relative_to(REPO))
        own_pkg = top_package(own) if own else None

        for full, line in imported_names(tree, path, own):
            mod, attr = resolve(full)
            target_pkg = top_package(mod) if mod != "queuetruth" or attr is None else "queuetruth"
            if mod == "queuetruth" and attr is not None and is_module(f"queuetruth.{attr}"):
                target_pkg = attr
            if own_pkg is not None and target_pkg != own_pkg:
                if own_pkg not in LAYER_OF or target_pkg not in LAYER_OF:
                    missing = own_pkg if own_pkg not in LAYER_OF else target_pkg
                    errors.append(f"{path.relative_to(REPO)}:{line}: `{missing}` has no layer; add it to LAYERS")
                    continue
                lo, lt = LAYER_OF[own_pkg], LAYER_OF[target_pkg]
                target = mod if attr is None or mod != "queuetruth" else f"queuetruth.{attr}"
                if lt > lo:
                    violations.append(
                        Violation(
                            source,
                            target,
                            "layer",
                            f"{LAYER_NAME[lo]} ({own_pkg}) imports {LAYER_NAME[lt]} ({target_pkg})",
                        )
                    )
                elif lt == lo:
                    same_layer[(own_pkg, target_pkg)].append((source, target))
            priv = private_part(full)
            if priv is not None and (own_pkg is None or top_package(full) != own_pkg):
                violations.append(Violation(source, full, "private", f"imports the private name `{priv}`"))

        for full in private_attribute_uses(tree, path, own):
            if own_pkg is None or top_package(full) != own_pkg:
                violations.append(Violation(source, full, "private", "uses a private name of an imported module"))

        if not in_pkg:
            for line in tests_on_sys_path(tree):
                violations.append(
                    Violation(source, "sys.path += tests", "tests", f"line {line} puts a tests directory on sys.path")
                )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) or (isinstance(node, ast.ImportFrom) and not node.level):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    if name.split(".")[0] in {"tests", "conftest"}:
                        violations.append(Violation(source, name, "tests", "imports test code"))

    # cycles between packages of one layer
    graph: dict[str, set[str]] = defaultdict(set)
    for a, b in same_layer:
        graph[a].add(b)

    def reaches(start: str, goal: str) -> bool:
        seen, todo = {start}, [start]
        while todo:
            for nxt in graph[todo.pop()]:
                if nxt == goal:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return False

    for (a, b), uses in same_layer.items():
        if reaches(b, a):
            for source, target in uses:
                violations.append(Violation(source, target, "cycle", f"{a} and {b} import each other"))
    return violations, errors


def read_allow() -> dict[str, int]:
    allow: dict[str, int] = {}
    if not ALLOW_FILE.is_file():
        return allow
    for n, raw in enumerate(ALLOW_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if line:
            allow[line] = n
    return allow


def write_allow(violations: list[Violation]) -> None:
    by_key: dict[str, set[str]] = defaultdict(set)
    for v in violations:
        by_key[v.key].add(v.rule)
    lines = [
        "# Import-layer violations recorded on purpose (tools/check_layers.py). One `source -> target` per line;",
        "# the comment names the rules it breaks. Remove a line when its import is gone; never add one to pass CI",
        "# without saying why in the pull request.",
    ]
    lines += [f"{k}  # {', '.join(sorted(by_key[k]))}" for k in sorted(by_key)]
    ALLOW_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--write-allow", action="store_true", help="rewrite the allow-list from today's violations")
    args = ap.parse_args(argv)

    violations, errors = collect()
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if args.write_allow:
        write_allow(violations)
        print(f"wrote {len({v.key for v in violations})} entries to {ALLOW_FILE.relative_to(REPO)}")
        return 1 if errors else 0

    allow = read_allow()
    new = sorted({v.key: v for v in violations if v.key not in allow}.values(), key=lambda v: v.key)
    found = {v.key for v in violations}
    stale = sorted(k for k in allow if k not in found)
    for v in new:
        print(f"new violation ({v.rule}): {v.key}: {v.detail}", file=sys.stderr)
    for k in stale:
        print(f"fixed, remove line {allow[k]} of {ALLOW_FILE.relative_to(REPO)}: {k}", file=sys.stderr)
    if new or stale or errors:
        print(
            f"check_layers: {len(new)} new, {len(stale)} stale, {len(errors)} errors "
            f"(rules and how to fix: the docstring of tools/check_layers.py)",
            file=sys.stderr,
        )
        return 1
    print(f"check_layers: ok ({len(found)} recorded exceptions, no new ones)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
