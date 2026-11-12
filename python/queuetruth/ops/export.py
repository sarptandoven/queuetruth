"""`export` and `export replay-schedule`."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from queuetruth.errors import QtError
from queuetruth.ops._call import _native, _validate
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


def export(spec: s.ExportSpec | dict[str, Any], *, force: bool = False) -> m.ExportSummary:
    """`export`: `l2_npz` in Rust, the snippets and the latency npz in `queuetruth.export.run_export`. `force` (or
    the spec's `force`) exports a calibration that `run_export` refuses (fitted on nothing, degenerate, or worse than
    the default on held-out data); it is passed straight through."""
    sp = _validate(s.ExportSpec, spec)
    if sp.target == "l2_npz":
        return _native("export_l2_npz", sp, m.ExportSummary)
    from queuetruth.export import run_export

    return run_export(sp, force=force)


#: `queuetruth.export` functions tried, in order, for `export replay-schedule` (the first that exists is called).
REPLAY_SCHEDULE_FUNCTIONS = ("replay_schedule", "export_replay_schedule")


def export_replay_schedule(
    target: str,
    *,
    out: str,
    schedule: str | None = None,
    amends: str | None = None,
    live: str | None = None,
    dataset: str | None = None,
    entry: str | None = None,
) -> m.ExportSummary:
    """`export replay-schedule`: a NautilusTrader strategy or an hftbacktest script that replays a live order schedule
    (`schedule` + optional `amends` tables and/or the live-log directory `live`; `dataset` supplies tick and lot
    sizes), so the simulator's fills can be imported (`live import --venue sim-*`) and scored with `score --sim-log`.
    Delegates to `queuetruth.export.replay_schedule(target, out, *, schedule, amends, live, dataset, entry)` (only
    the arguments given are passed); a build without it raises QT-USAGE-001. The adapter's notes are emitted as
    warnings."""
    import dataclasses
    import inspect

    import queuetruth.export as qexp

    target = target.replace("-", "_")
    if target not in ("hftbacktest", "nautilus"):
        raise QtError("QT-CONFIG-001", f"target: expected hftbacktest or nautilus, got {target!r}")
    if schedule is None and live is None:
        raise QtError(
            "QT-CONFIG-001", "export replay-schedule: a schedule or a live log is required", "--schedule FILE"
        )
    fn = next((getattr(qexp, n) for n in REPLAY_SCHEDULE_FUNCTIONS if callable(getattr(qexp, n, None))), None)
    if fn is None:
        raise QtError(
            "QT-USAGE-001",
            "export replay-schedule: this queuetruth build has no schedule-replay adapters "
            "(queuetruth.export.replay_schedule)",
            "upgrade queuetruth",
        )
    given = {"schedule": schedule, "amends": amends, "live": live, "dataset": dataset}
    if entry is not None:
        given["entry"] = entry.replace("-", "_")
    params = inspect.signature(fn).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        unknown = sorted(k for k, v in given.items() if v is not None and k not in params)
        if unknown:
            raise QtError("QT-CONFIG-001", f"export replay-schedule: this build does not take {', '.join(unknown)}")
    res = fn(target=target, out=out, **{k: v for k, v in given.items() if v is not None})
    if isinstance(res, m.ExportSummary):
        return res
    info: dict[str, Any] = {}
    if dataclasses.is_dataclass(res) and not isinstance(res, type):
        info = dataclasses.asdict(res)
    elif isinstance(res, BaseModel):
        info = res.model_dump(mode="json")
    elif isinstance(res, dict):
        info = dict(res)
    for note in info.get("notes") or []:
        warnings.warn(str(note), UserWarning, stacklevel=2)
    from queuetruth.schemas.io import sha256_file

    p = Path(str(info.get("path") or out))
    files = [m.FileEntry(name=p.name, bytes=p.stat().st_size, sha256=sha256_file(p))] if p.is_file() else []
    text = res if isinstance(res, str) and not Path(res).exists() else None
    return m.ExportSummary(target=target, files=files, text=text)  # type: ignore[arg-type]


OPERATIONS = (
    OperationDef(
        "export",
        s.ExportSpec,
        export,
        m.ExportSummary,
        "Exports for a backtester: the L2 npz, the calibration snippets, the latency npz.",
    ),
)
