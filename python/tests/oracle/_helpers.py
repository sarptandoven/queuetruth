"""Shared helpers for the oracle tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qt_oracle.compare import build_report, read_json_expectations
from qt_oracle.engine import OracleConfig, PartitionRunner

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures"


def run_scenario(sc, cfg: OracleConfig | None = None) -> dict[str, dict[str, Any]]:
    cfg = cfg or OracleConfig(exclude_order_ids=sc.exclude)
    runner = PartitionRunner(sc.meta(), sc.shadows, sc.amends, cfg)
    return {r["shadow_id"]: r for r in runner.run(sc.records())}


def check_oracle_facts(sc, res: dict[str, dict[str, Any]]) -> None:
    for sid, want in sc.oracle.items():
        r = res[sid]
        for k, v in want.items():
            if k == "fills":
                got = [
                    {
                        kk: f[kk]
                        for kk in ("ts_event", "fill_qty", "reason", *(x for x in ("fill_px", "liquidity") if x in w))
                    }
                    for f, w in zip(r["fills"], v, strict=False)
                ]
                assert len(r["fills"]) == len(v), (sc.name, sid, r["fills"])
                assert got == v, (sc.name, sid, r["fills"])
            elif k == "traj":
                got = [
                    (t["ts_event"], t["queue_ahead_after"], t["rank_after"])
                    for t in r["trajectory"]
                    if t["kind"] in ("live", "event")
                ]
                assert got == v, (sc.name, sid, got)
            else:
                assert r[k] == v, (sc.name, sid, k, r[k], v)


def expectations_file(tmp: Path, sc) -> Path:
    """Writes the scenario's SPEC-as-written facts as a JSON expectations file (the `--expectations` format)."""
    p = tmp / f"{sc.name}.json"
    p.write_text(json.dumps({"shadows": sc.as_written}))
    return p


def report_vs_expectations(tmp: Path, sc, res: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ref = read_json_expectations(expectations_file(tmp, sc))
    return build_report(list(res.values()), ref, reference_kind="expectations")
