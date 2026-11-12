"""Writes crates/cli/src/flags_gen.rs: the clap flags of the advanced commands that map field by field onto their
spec, generated from the spec's published JSON Schema (`schemas/json`).

For every field of the spec, the flag takes from the schema:
- its name: the field name with `_` as `-` (`window_open_s` is `--window-open-s`);
- its kind: a dataset or file reference is a path, an integer field named `*_ns` a duration, another integer an
  integer, a number a number, a string a string, an enum a choice of its values (with `-` for `_`), a boolean that
  defaults to false a switch, one that defaults to true a `--no-` switch;
- whether it is required (the schema's `required`);
- its help: the field's `description`, as one lower-case phrase. A field without a description is an error: add one
  to the Pydantic model, which is the contract. Defaults are added to the help from the schema by the grammar.

What does not map one to one is in `COMMANDS` below, by hand: the order of the flags (it is also the order of the
spec fields in the parsed arguments, which the golden files pin), renamed flags, kinds the rules cannot see
(`--placements O:W,...`), help of a negated switch, flags the command line requires although the spec does not, and
the fields that are not flags (each with its reason). A field the schema has and `COMMANDS` does not name becomes a
flag by the rules above, after the named ones, so a new spec field reaches the command line through `make
generate`; a field the rules cannot turn into a flag stops the generator until it has an override or a reason.

    uv run --frozen --no-sync python crates/cli/gen_flags.py           # write the file
    uv run --frozen --no-sync python crates/cli/gen_flags.py --check   # exit 1 if it would change
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "src" / "flags_gen.rs"
REPO = HERE.parent.parent
SCHEMAS = REPO / "schemas" / "json"


@dataclass(frozen=True)
class Override:
    """What the rules do not give for one field."""

    long: str | None = None
    kind: str | None = None
    help: str | None = None
    required: bool | None = None


@dataclass(frozen=True)
class Command:
    """One command: its spec schema, its fields in flag order, overrides, and the fields that are not flags."""

    const: str
    schema: str
    order: tuple[str, ...]
    overrides: dict[str, Override] = field(default_factory=dict)
    not_flags: dict[str, str] = field(default_factory=dict)


COMMANDS: dict[tuple[str, ...], Command] = {
    ("shadows", "sample"): Command(
        "SHADOWS_SAMPLE",
        "qt.shadow_sample_spec",
        (
            "dataset",
            "n",
            "out",
            "placements",
            "qty",
            "lifetime_ns",
            "latency_ns",
            "window_open_s",
            "window_close_s",
            "clamp_to_data_end",
            "allocation",
            "sides",
        ),
        {
            "dataset": Override(long="data"),
            "out": Override(required=True),
            "placements": Override(kind="Placements"),
            "qty": Override(kind="Qty"),
            "lifetime_ns": Override(long="lifetime"),
            "clamp_to_data_end": Override(
                long="allow-past-end",
                help="do not shorten the placement window so that every life fits in the data",
            ),
            "sides": Override(kind="StrList"),
        },
        {
            "seed": "the global --seed",
            "partition_quotas": "a map by instrument-day; give it in a spec (queuetruth advanced run shadows)",
        },
    ),
    ("shadows", "mirror-real"): Command(
        "SHADOWS_MIRROR_REAL",
        "qt.mirror_spec",
        ("dataset", "out", "n", "stratify"),
        {
            "dataset": Override(long="data"),
            "out": Override(required=True),
            "stratify": Override(long="no-stratify", help="do not stratify by instrument and outcome"),
        },
        {
            "seed": "the global --seed",
            "placement": "one value only (behind_real)",
        },
    ),
}

#: `$defs` names of references that are given as a path on the command line.
PATH_REFS = {"PathRef", "DatasetIdRef", "UploadRef", "JobFileRef"}


def _branches(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The non-null branches of an `anyOf` (or the node itself)."""
    if "anyOf" in node:
        return [b for b in node["anyOf"] if b.get("type") != "null"]
    return [node]


def _ref_name(node: dict[str, Any]) -> str | None:
    ref = node.get("$ref")
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None


def _is_path(node: dict[str, Any], defs: dict[str, Any]) -> bool:
    for b in _branches(node):
        name = _ref_name(b)
        if name is None:
            return False
        target = defs.get(name, {})
        if name not in PATH_REFS and not all(_ref_name(x) in PATH_REFS for x in target.get("anyOf", [{}])):
            return False
    return True


def kind_of(name: str, node: dict[str, Any], defs: dict[str, Any]) -> str | None:
    """The `Kind` of a field by the rules of the module docstring, or None when they do not apply."""
    if _is_path(node, defs):
        return "Path"
    branches = _branches(node)
    if len(branches) != 1:
        return None
    b = branches[0]
    if "enum" in b or "const" in b:
        values = b.get("enum", [b.get("const")])
        if all(isinstance(v, str) for v in values):
            return "Enum(&[" + ", ".join(json.dumps(v.replace("_", "-")) for v in values) + "])"
        return None
    t = b.get("type")
    if t == "integer":
        return "Dur" if name.endswith("_ns") else "Int"
    if t == "number":
        return "Num"
    if t == "string":
        return "Str"
    if t == "boolean":
        return "NoFlag" if node.get("default") is True else "Flag"
    return None


def phrase(description: str) -> str:
    """A description as CLI help: its first sentence, lower case first letter, no full stop."""
    text = " ".join(description.split())
    first = text.split(". ")[0].rstrip(".")
    if len(first) > 1 and first[0].isupper() and not first[1].isupper():
        first = first[0].lower() + first[1:]
    return first


def flags(path: tuple[str, ...], cmd: Command) -> list[dict[str, Any]]:
    """The flags of one command (module docstring); raises SystemExit naming a field that needs a hand entry."""
    schema = json.loads((SCHEMAS / f"{cmd.schema}.schema.json").read_text(encoding="utf-8"))
    props: dict[str, Any] = schema["properties"]
    defs: dict[str, Any] = schema.get("$defs", {})
    required = set(schema.get("required", []))
    where = " ".join(path)
    for name in (*cmd.order, *cmd.overrides, *cmd.not_flags):
        if name not in props:
            raise SystemExit(f"gen_flags: {where}: {name!r} is not a field of {cmd.schema}")
    both = set(cmd.order) & set(cmd.not_flags)
    if both:
        raise SystemExit(f"gen_flags: {where}: {sorted(both)} are both flags and not flags")
    names = [*cmd.order, *(n for n in props if n not in cmd.order and n not in cmd.not_flags)]
    out = []
    for name in names:
        node = props[name]
        o = cmd.overrides.get(name, Override())
        kind = o.kind or kind_of(name, node, defs)
        if kind is None:
            raise SystemExit(
                f"gen_flags: {where}: field {name!r} of {cmd.schema} has no flag rule; give it an override "
                "(kind) or a not-a-flag reason in crates/cli/gen_flags.py"
            )
        long = o.long or name.replace("_", "-")
        if kind == "NoFlag" and o.long is None:
            long = "no-" + long
        description = node.get("description")
        if o.help is None and not description:
            raise SystemExit(
                f"gen_flags: {where}: field {name!r} of {cmd.schema} has no description; add one to the Pydantic "
                "model (it is the flag's help)"
            )
        out.append(
            {
                "long": long,
                "kind": kind,
                "to": name,
                "required": o.required if o.required is not None else name in required,
                "help": o.help or phrase(description),
            }
        )
    return out


def generate() -> str:
    parts = [
        "//! Generated by crates/cli/gen_flags.py from the spec schemas (`schemas/json`) and the hand-kept overrides in\n"
        "//! that script. Do not edit by hand; rerun it (`make generate`).\n\n"
        "use crate::grammar::{Flag, Kind};\n"
    ]
    for path, cmd in COMMANDS.items():
        rows = []
        for f in flags(path, cmd):
            rows.append(
                "    Flag {\n"
                f"        long: {json.dumps(f['long'])},\n"
                f"        kind: Kind::{f['kind']},\n"
                f"        to: {json.dumps(f['to'])},\n"
                f"        required: {'true' if f['required'] else 'false'},\n"
                f"        help: {json.dumps(f['help'])},\n"
                "        short: None,\n"
                "        value_name: None,\n"
                "    },\n"
            )
        parts.append(
            f"\n/// `{' '.join(path)}`, from `{cmd.schema}`.\n"
            f"pub(crate) const {cmd.const}: &[Flag] = &[\n{''.join(rows)}];\n"
        )
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the file is out of date (writes nothing)")
    a = ap.parse_args(argv)
    text = generate()
    current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if a.check:
        if text != current:
            print(f"{OUT.relative_to(REPO)} is stale; run python crates/cli/gen_flags.py", file=sys.stderr)
            return 1
        return 0
    if text != current:
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
