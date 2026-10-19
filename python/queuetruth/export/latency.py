"""hftbacktest `IntpOrderLatency` `.npz` from a canonical live log (SPEC 5.10).

One row per submitted order that has `submit_sent.ts_local_send` and a first `ack` / `reject` /
`post_only_reject` with `ts_local_recv` AND an exchange timestamp of kind `match_time` or `gateway_time` (the same
eligibility as `queuetruth.live.latency_stats`): `req_ts` = send time, `exch_ts` = the response's `ts_exchange`,
`resp_ts` = its `ts_local_recv`, `_padding = 0`; sorted by (`req_ts`, `order_key`).

Rows hbt cannot use are dropped and counted (Python review E1, E2):
- a reject without an exchange timestamp: hbt reads `exch_ts <= 0` as "rejected before the matching engine" and
  interpolates a NEGATIVE entry latency through it, rejecting the neighbouring simulated orders and stretching their
  response latencies (a reject that carries `ts_exchange` reached the engine and is kept with that time);
- an ack whose exchange time is missing or not `match_time` / `gateway_time` (e.g. `record_time`);
- a negative latency (`exch_ts < req_ts` or `resp_ts < exch_ts`, clock skew): hbt rejects orders around a negative
  entry latency and asserts `lat >= 0` on responses.

The file is the `np.savez_compressed(out, data=arr)` layout (zip member `data.npy`, deflate) written with a fixed
zip timestamp so the bytes are deterministic; like `np.savez_compressed`, `.npz` is appended to a path without it
(hftbacktest picks the loader from the extension).
"""

from __future__ import annotations

import io
import warnings
import zipfile
from pathlib import Path

import numpy as np

from queuetruth.errors import QtError
from queuetruth.schemas.models import FileEntry
from queuetruth.score._io import file_entry, read_live_log

LATENCY_DTYPE = np.dtype([("req_ts", "<i8"), ("exch_ts", "<i8"), ("resp_ts", "<i8"), ("_padding", "<i8")])
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def write_npz(arr: np.ndarray, out: str | Path, member: str = "data") -> Path:
    """Deterministic `np.savez_compressed(out, data=arr)` equivalent."""
    buf = io.BytesIO()
    np.lib.format.write_array(buf, np.ascontiguousarray(arr), allow_pickle=False)
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    info = zipfile.ZipInfo(f"{member}.npy", date_time=ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(info, buf.getvalue())
    return p


#: Reasons a submitted order has no latency row.
SKIP_REASONS = ("no_response", "no_exchange_time", "reject_without_exchange_time", "negative_latency")
_EXCH_KINDS = ("match_time", "gateway_time")


def latency_table(live_dir: str | Path) -> tuple[np.ndarray, dict[str, int]]:
    """(structured array, orders skipped per reason in `SKIP_REASONS`)."""
    _, events = read_live_log(live_dir)
    by_order: dict[str, list[dict]] = {}
    for ev in events:
        by_order.setdefault(str(ev["order_key"]), []).append(ev)
    out: list[tuple[int, str, int, int]] = []
    skipped = dict.fromkeys(SKIP_REASONS, 0)
    for key in sorted(by_order):
        evs = by_order[key]
        send = next((e for e in evs if e["event_type"] == "submit_sent" and e.get("ts_local_send") is not None), None)
        if send is None:
            continue
        resp = next((e for e in evs if e["event_type"] in ("ack", "reject", "post_only_reject")), None)
        if resp is None or resp.get("ts_local_recv") is None:
            skipped["no_response"] += 1
            continue
        exch = resp.get("ts_exchange")
        if exch is None or resp.get("ts_exchange_kind") not in _EXCH_KINDS:
            skipped["no_exchange_time" if resp["event_type"] == "ack" else "reject_without_exchange_time"] += 1
            continue
        req, exch, rcv = int(send["ts_local_send"]), int(exch), int(resp["ts_local_recv"])
        if exch < req or rcv < exch:
            skipped["negative_latency"] += 1
            continue
        out.append((req, key, exch, rcv))
    out.sort(key=lambda r: (r[0], r[1]))
    arr = np.zeros(len(out), dtype=LATENCY_DTYPE)
    for i, (req, _, exch, resp) in enumerate(out):
        arr[i] = (req, exch, resp, 0)
    return arr, skipped


def latency_rows(live_dir: str | Path) -> tuple[np.ndarray, int]:
    """(structured array, number of submitted orders skipped; reasons in `latency_table`)."""
    arr, skipped = latency_table(live_dir)
    return arr, sum(skipped.values())


def npz_path(out: str | Path) -> Path:
    """`out` with `.npz` appended when it has another suffix (as `np.savez_compressed`)."""
    p = Path(out)
    return p if p.suffix == ".npz" else p.with_name(p.name + ".npz")


def write_latency_npz(live_dir: str, out: str) -> FileEntry:
    """IntpOrderLatency `.npz` from a live log (written to `npz_path(out)`); raises `QT-INPUT-002` when no order has
    the needed timestamps. Skipped orders are reported as a `LatencyRowsSkipped` warning."""
    arr, skipped = latency_table(live_dir)
    if arr.size == 0:
        raise QtError(
            "QT-INPUT-002",
            f"{live_dir}: no order with submit_sent.ts_local_send and an ack/reject with ts_local_recv and an "
            f"exchange match/gateway timestamp (skipped: {_skips(skipped)})",
            "latency npz needs your own gateway capture (local send and receive times) and exchange timestamps",
        )
    if any(skipped.values()):
        warnings.warn(LatencyRowsSkipped(f"latency npz: orders skipped ({_skips(skipped)})"), stacklevel=2)
    p = write_npz(arr, npz_path(out))
    return FileEntry.model_validate(file_entry(p, int(arr.size)))


def _skips(skipped: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in skipped.items() if v) or "none"


class LatencyRowsSkipped(UserWarning):
    """Orders without a usable latency row (see the module doc)."""
