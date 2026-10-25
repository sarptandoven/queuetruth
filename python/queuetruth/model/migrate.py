"""`python -m queuetruth.model.migrate`: converts a v0.1 model directory (`model.pkl`) to the safe `model.npz`.

    python -m queuetruth.model.migrate DIR --allow-pickle --sha256 <digest of model.pkl> [--out OUT] [--format json]
    python -m queuetruth.model.migrate DIR --allow-pickle --trusted [--out OUT] [--format json]

Unpickling runs code from the file, so nothing is unpickled without `--allow-pickle`, and, as `HazardModel.load`
requires, the pickle's bytes must match the trusted `--sha256` (the digest `fit_hazard` returned when it wrote it)
or `--trusted` must state that only you can write the directory. The loaded model is written as `model.npz` and
`model.json` (the same card; its file entry for `model.pkl` becomes the `model.npz` entry) into `OUT` (default: `DIR`
itself; `model.pkl` is left in place but no longer listed). With `--out`, the other files the card lists
(`predictions.parquet`) are copied; every listed name and digest is checked before anything is created or copied.
A directory that already lists `model.npz` is converted to nothing (exit 0) unless `--out` asks for a copy.

Output follows the `queuetruth` CLI convention (SPEC 4.4): with `--format text` (default) stdout is the sha256 of the
new `model.npz` (the trusted digest for `HazardModel.load(..., sha256=...)`) and stderr a summary, the
`LegacyPickleWarning` and, on error, the message and hint; with `--format json` stdout is exactly one JSON document,
`{"model": <path>, "sha256": <digest>, "legacy_pickle_left": <bool>}` or a `qt.error` document. Exit 0, or 2 on
error (`QT-CONFIG-001` without `--allow-pickle` or a trusted digest, `QT-INPUT-002` for a digest mismatch,
`QT-INPUT-001` for a missing path or file).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from queuetruth.errors import QtError
from queuetruth.model._common import config_error, input_error
from queuetruth.model.hazard import CARD_NAME, MODEL_NAME, PICKLE_NAME, HazardModel, model_sha256
from queuetruth.schemas.io import sha256_file
from queuetruth.schemas.models import HazardModelCard


def _card(directory: Path) -> HazardModelCard:
    path = directory / CARD_NAME
    if not path.is_file():
        raise QtError("QT-INPUT-001", f"migrate: {path} not found")
    try:
        return HazardModelCard.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, RecursionError) as err:  # RecursionError: JSON nested too deep
        raise input_error(f"migrate: invalid {path}: {type(err).__name__}: {err}") from None


def _model_dir(src: str | Path) -> Path:
    """The model directory named by `src` (the directory itself, or an existing file in it)."""
    p = Path(src)
    if p.is_dir():
        return p
    if p.is_file():
        return p.parent
    raise QtError("QT-INPUT-001", f"migrate: {p} does not exist", "pass the fit-hazard output directory")


def _check_copies(card: HazardModelCard, src_dir: Path) -> list[str]:
    """The non-model files the card lists, each a plain name whose bytes match the card's sha256 (checked before
    anything is created or copied)."""
    names = []
    for f in card.files:
        if f.name in (MODEL_NAME, PICKLE_NAME):
            continue
        if Path(f.name).name != f.name or f.name in ("", ".", ".."):
            raise input_error(f"migrate: {CARD_NAME} lists {f.name!r}, not a file name in the directory")
        try:
            got = sha256_file(src_dir / f.name)
        except OSError as err:
            raise QtError("QT-INPUT-001", f"migrate: cannot read {src_dir / f.name}: {err}") from None
        if got != f.sha256:
            raise input_error(f"migrate: sha256 of {src_dir / f.name} is {got}, card records {f.sha256}; refusing")
        names.append(f.name)
    return names


def migrate(
    src: str | Path,
    out: str | Path | None = None,
    *,
    allow_pickle: bool = False,
    sha256: str | None = None,
    trusted: bool = False,
) -> HazardModelCard:
    """Converts the model directory `src` (or a file in it) to `model.npz` in `out` (default `src`) and returns the
    new card (module docstring)."""
    src_dir = _model_dir(src)
    dst = src_dir if out is None else Path(out)
    card = _card(src_dir)
    names = [f.name for f in card.files]
    same = dst.resolve() == src_dir.resolve() if dst.exists() else False
    if MODEL_NAME not in names and PICKLE_NAME in names:
        if not allow_pickle:
            raise config_error(
                f"migrate: refusing to unpickle {src_dir / PICKLE_NAME} without --allow-pickle: unpickling runs code "
                "from the file; pass --allow-pickle with --sha256 <digest recorded when it was written> (or "
                "--trusted for a directory only you can write)",
                field="allow_pickle",
            )
        if sha256 is None and not trusted:
            raise config_error(
                f"migrate: refusing to unpickle {src_dir / PICKLE_NAME} without a trusted sha256: pass --sha256 "
                f"<digest recorded when it was written> (or --trusted for a directory only you can write); the "
                f"{CARD_NAME} beside the pickle is not an authenticity check",
                field="sha256",
            )
    elif MODEL_NAME in names and same:
        return card  # already the safe format; nothing to convert
    copies = [] if same else _check_copies(card, src_dir)
    model = HazardModel.load(str(src_dir), sha256=sha256, trusted=trusted, allow_pickle=allow_pickle)
    dst.mkdir(parents=True, exist_ok=True)
    for name in copies:
        shutil.copyfile(src_dir / name, dst / name)
    model.save(str(dst))
    assert model.card is not None
    return model.card


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point (module docstring)."""
    ap = argparse.ArgumentParser(
        prog="python -m queuetruth.model.migrate",
        description="Convert a v0.1 hazard model directory (model.pkl) to model.npz (no pickle).",
    )
    ap.add_argument("dir", help="model directory written by fit-hazard (or a file in it)")
    ap.add_argument("--out", default=None, help="write model.npz and model.json here (default: DIR itself)")
    ap.add_argument("--allow-pickle", action="store_true", help="allow unpickling the legacy model.pkl (runs code)")
    ap.add_argument("--sha256", default=None, help="trusted sha256 of model.pkl (recorded when it was written)")
    ap.add_argument("--trusted", action="store_true", help="the directory is writable only by you (no digest)")
    ap.add_argument("--format", choices=("text", "json"), default="text", help="stdout format (default text)")
    args = ap.parse_args(argv)
    json_out = args.format == "json"
    try:
        card = migrate(args.dir, args.out, allow_pickle=args.allow_pickle, sha256=args.sha256, trusted=args.trusted)
    except QtError as err:
        if json_out:
            print(json.dumps(err.to_document(), ensure_ascii=False))
        else:
            print(str(err), file=sys.stderr)
            if err.hint:
                print(f"hint: {err.hint}", file=sys.stderr)
        return err.exit_code
    digest = model_sha256(card)
    dst = Path(args.out) if args.out else _model_dir(args.dir)
    left = (dst / PICKLE_NAME).exists()
    if json_out:
        doc = {"model": str(dst / MODEL_NAME), "sha256": digest, "legacy_pickle_left": left}
        print(json.dumps(doc, ensure_ascii=False))
    else:
        print(digest)
    note = f"; {PICKLE_NAME} left in place, no longer listed in {CARD_NAME}" if left else ""
    print(f"{dst / MODEL_NAME}: sha256 {digest}{note}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess in the tests
    sys.exit(main())
