"""Test helpers: hftbacktest order-state rows (the snapshot format of `queuetruth.live.import_sim("hftbacktest")`,
which the replay script writes) built from queuetruth's exact fills, so `score` can be checked on an hftbacktest
output whose answer is known without running hftbacktest."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import queuetruth as qt

from queuetruth_hftbacktest.replay import Prepared


def rows(path: Path) -> list[dict[str, Any]]:
    t = qt.read_table(str(path))
    cols = {}
    for name in t.column_names:
        c = t.column(name)
        cols[name] = (c.cast(pa.int64()) if pa.types.is_timestamp(c.type) else c).to_pylist()
    return [{k: v[i] for k, v in cols.items()} for i in range(t.num_rows)]


def units(data: Path) -> dict[int, dict[str, Any]]:
    out = {}
    for r in rows(data / "instruments.parquet"):
        out[int(r["instrument_id"])] = r
    return out


def exact_as_hbt(
    p: Prepared, path: Path, *, drop: set[str] = frozenset(), delay_ns: int = 0, stop_at: int | None = None
) -> Path:
    """hftbacktest order states that reproduce the exact replay of `p`: one `new` row at entry, one row per exact
    fill (`delay_ns` later; none for the shadow ids in `drop`), and a summary with `fatal.at_ns = stop_at`."""
    u = units(p.data)
    res = {str(r["shadow_id"]): r for r in rows(p.exact / "shadow_results.parquet")}
    fills: dict[str, list[dict[str, Any]]] = {}
    fp = p.exact / "shadow_fills.parquet"
    for f in rows(fp) if fp.exists() else []:
        fills.setdefault(str(f["shadow_id"]), []).append(f)
    out = []
    for oid, (sid, r) in enumerate(sorted(res.items(), key=lambda kv: (kv[1]["ts_live"], kv[0])), start=1):
        x = u[int(r["instrument_id"])]
        tick = Decimal(int(x["tick_size"])).scaleb(int(x["price_exp"]))
        scale = Decimal(1).scaleb(int(x["size_exp"]))
        qty = Decimal(int(r["qty"])) * scale
        base = {
            "order_id": oid,
            "order_key": f"qthbt:qthbt:{sid}",
            "instrument": x["symbol"],
            "asset_no": int(r["instrument_id"]) - 1,
            "side": 1 if int(r["side"]) == 66 else -1,
            "price_tick": int(r["price"]) // int(x["tick_size"]),
            "tick_size": str(tick),
            "qty": float(qty),
            "order_type": 0,
            "time_in_force": 1,
            "maker": True,
        }
        ts0 = int(r["ts_live"])
        out.append({**base, "status": 1, "leaves_qty": float(qty), "exec_qty": 0.0, "exec_price_tick": 0,
                    "exch_timestamp": ts0, "local_timestamp": ts0})  # fmt: skip
        leaves = qty
        for f in [] if sid in drop else sorted(fills.get(sid, []), key=lambda f: int(f["fill_seq"])):
            ts = int(f["ts_event"]) + delay_ns
            if stop_at is not None and ts >= stop_at:
                break
            q = Decimal(int(f["fill_qty"])) * scale
            leaves -= q
            out.append({**base, "status": 3 if leaves <= 0 else 5, "leaves_qty": float(leaves), "exec_qty": float(q),
                        "exec_price_tick": int(f["fill_px"]) // int(x["tick_size"]), "exch_timestamp": ts,
                        "local_timestamp": ts})  # fmt: skip
    out.sort(key=lambda o: (o["exch_timestamp"], o["order_id"]))
    path.write_text("".join(json.dumps(o) + "\n" for o in out), encoding="utf-8")
    summ: dict[str, Any] = {"errors": {}, "end_of_data": False}
    if stop_at is not None:
        summ["fatal"] = {"rc": 14, "at_ns": stop_at}
    Path(str(path) + ".summary.json").write_text(json.dumps(summ), encoding="utf-8")
    return path
