"""Pre-flight estimates (`qt.estimate`) from dataset manifests: records from the manifest's partition row counts,
shadows from the shadow-spec upload size (about 64 bytes per Parquet row, an upper-bound heuristic), CPU time from
the SPEC 7.1 throughput targets, and `cost_units` = estimated CPU seconds."""

from __future__ import annotations

from typing import Any

import psycopg

from queuetruth_service.documents import fetch_one

REPLAY_RPS = 4_000_000.0
ZOO_PAIR_S = 0.004
BYTES_PER_SHADOW = 64
QUEUE_OVERHEAD_S = 5.0


def _records(conn: psycopg.Connection, tenant_id: str, ref: Any) -> int:
    if not isinstance(ref, dict) or "dataset_id" not in ref:
        return 0
    row = fetch_one(
        conn, "select manifest from datasets where id = %s and tenant_id = %s", (str(ref["dataset_id"]), tenant_id)
    )
    manifest = (row or {}).get("manifest") or {}
    return int(sum(int(p.get("rows", 0) or 0) for p in manifest.get("partitions") or []))


def _shadows(conn: psycopg.Connection, tenant_id: str, ref: Any) -> int:
    if not isinstance(ref, dict) or "upload_id" not in ref:
        return 0
    row = fetch_one(
        conn, "select size_bytes from uploads where id = %s and tenant_id = %s", (str(ref["upload_id"]), tenant_id)
    )
    return int((row or {}).get("size_bytes", 0) or 0) // BYTES_PER_SHADOW


def estimate(conn: psycopg.Connection, tenant_id: str, kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """`qt.estimate` for a replay, calibration or scorecard spec of one tenant."""
    records = _records(conn, tenant_id, spec.get("dataset"))
    shadows = _shadows(conn, tenant_id, spec.get("shadows"))
    cpu = records / REPLAY_RPS
    if kind == "calibration":
        cpu = cpu * 3 + shadows * ZOO_PAIR_S * max(1, int((spec.get("bootstrap") or {}).get("b", 200)) // 50)
    elif kind == "scorecard":
        cpu = cpu * 2 + 1.0
    cpu = round(cpu + shadows * ZOO_PAIR_S, 3)
    bytes_out = records * 24 + shadows * 512
    return {
        "schema": "qt.estimate",
        "schema_version": 1,
        "kind": kind,
        "records_est": records,
        "shadows_est": shadows,
        "cpu_seconds_est": cpu,
        "bytes_out_est": bytes_out,
        "cost_units": cpu,
        "eta_seconds": round(cpu + QUEUE_OVERHEAD_S, 3),
    }
