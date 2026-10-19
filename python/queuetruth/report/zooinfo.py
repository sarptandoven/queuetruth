"""Zoo facts beyond `qt.zoo_summary`: the `qt.zoo_errors` index (never-cancel metrics unprefixed, cancel-bearing
mirror metrics prefixed `cancel_bearing/`; Aalen-Johansen truth `fill_rate_bias_aj`; cluster-robust discordance
`discordance_p_primary`, `n_clusters`), the outcome-independent label eligibility of audit ruling A2 from
`zoo_shadows.parquet` (`qt.zoo_shadows`: planned cancel, administrative end, `never_cancel`), and taker fills
(schema 1.1 `qt.zoo_fills.liquidity = taker`; v0.1 tables: `reason` in `queuetruth.schemas.arrow.TAKER_REASONS`,
i.e. crossed_at_live: the binding A5 refinement makes a later `crossed` a maker fill)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.report import cols
from queuetruth.report.load import RunDir
from queuetruth.schemas.arrow import TAKER_REASONS as ARROW_TAKER_REASONS

CANCEL_PREFIX = "cancel_bearing/"
TAKER_REASONS = ARROW_TAKER_REASONS
ErrKey = tuple[str, str, int | None, str, str]
ErrVal = tuple[float, float | None, float | None, int]


def errors_index(run: RunDir) -> dict[ErrKey, ErrVal]:
    """(model, metric, horizon, stratum_kind, stratum) -> (value, ci_lo, ci_hi, n)."""
    t = run.table("qt.zoo_errors")
    if t is None:
        return {}
    out: dict[ErrKey, ErrVal] = {}
    for r in cols.pylist(t.read()):
        h = r["horizon_ns"]
        out[(r["model_id"], r["metric"], None if h is None else int(h), r["stratum_kind"], str(r["stratum"]))] = (
            r["value"],
            r["ci_lo"],
            r["ci_hi"],
            int(r["n"]),
        )
    return out


def has_cancel_bearing(idx: dict[ErrKey, ErrVal]) -> bool:
    """True when the evaluation produced a cancel-bearing (mirror) population."""
    return any(k[1].startswith(CANCEL_PREFIX) for k in idx)


def planned_life(run: RunDir) -> dict[str, tuple[int | None, int | None, bool | None]] | None:
    """shadow_id -> (ts_cancel_planned, ts_admin_end, never_cancel) from `qt.zoo_shadows`; None without the table."""
    t = run.table("qt.zoo_shadows")
    if t is None:
        return None
    tab = t.read()
    if "shadow_id" not in tab.column_names:
        return None
    rows = cols.pylist(tab)
    return {
        str(r["shadow_id"]): (r.get("ts_cancel_planned"), r.get("ts_admin_end"), r.get("never_cancel")) for r in rows
    }


def row_masks(run: RunDir, tab: pa.Table, h_max_ns: int | None) -> tuple[np.ndarray, np.ndarray, str]:
    """Per prediction row: (never-cancel population, label eligible at the row's horizon, rule used).

    With `qt.zoo_shadows` (A2): eligible iff the administrative end is null or >= ts_live + horizon, independent of
    the fill outcome; never-cancel iff the shadow's flag (else no planned cancel before ts_live + h_max). Without the
    table (or with a predicted shadow missing from it) the report refuses with `QT-INPUT-001` / `QT-INPUT-002`:
    eligibility is never inferred from the `censored` flag (E38 OC-7)."""
    n = tab.num_rows
    _, yok = cols.boolean(tab, "truth_filled")
    pl = planned_life(run)
    if pl is None:
        raise QtError(
            "QT-INPUT-001",
            f"{run.label}: zoo_predictions without zoo_shadows.parquet (qt.zoo_shadows); the planned-life table is "
            "required for label eligibility (audit ruling A2, E38 OC-7)",
            "re-run `queuetruth zoo` (it writes zoo_shadows.parquet) or add the table to the run directory",
        )
    sid = cols.strs(tab, "shadow_id").tolist()
    live, _ = cols.i64(tab, "ts_live")
    hz, _ = cols.i64(tab, "horizon_ns")
    hm = int(h_max_ns) if h_max_ns is not None else int(hz.max(initial=0))
    never = np.ones(n, dtype=bool)
    elig = np.zeros(n, dtype=bool)
    missing = sorted({s for s in sid if s not in pl})
    if missing:
        raise QtError(
            "QT-INPUT-002",
            f"{run.label}: zoo_shadows.parquet has no row for {len(missing)} predicted shadow(s) (first "
            f"{missing[:3]}); the planned-life table must cover every shadow (E38 OC-7)",
        )
    for i, s in enumerate(sid):
        cancel, admin, flag = pl[s]
        if flag is not None:
            never[i] = bool(flag)
        else:
            never[i] = cancel is None or int(cancel) - int(live[i]) >= hm
        elig[i] = admin is None or int(admin) >= int(live[i]) + int(hz[i])
    if "status" in tab.column_names:  # faithful hbt #312 aborts are not predictions
        elig &= np.array([v is None for v in tab.column("status").to_pylist()], dtype=bool)
    return never, elig & yok, "planned life of qt.zoo_shadows (audit ruling A2)"


def _taker_mask(tab: pa.Table) -> np.ndarray:
    """Taker rows: the 1.1 `liquidity` column where it is set, else the schema's taker reasons (crossed_at_live)."""
    by_reason = np.isin(cols.strs(tab, "reason"), TAKER_REASONS)
    if "liquidity" not in tab.column_names:
        return by_reason
    liq = cols.strs(tab, "liquidity")
    known = np.array([v is not None for v in liq.tolist()], dtype=bool)
    return np.where(known, liq == "taker", by_reason)


def taker_by_model(run: RunDir) -> list[dict[str, Any]]:
    """Per model: fills, taker fills, taker qty share, shadows whose first fill was taker."""
    t = run.table("qt.zoo_fills")
    if t is None:
        return []
    tab = t.read()
    if tab.num_rows == 0:
        return []
    mid = cols.strs(tab, "model_id")
    sid = cols.strs(tab, "shadow_id")
    seq, _ = cols.i64(tab, "fill_seq")
    qty = cols.f64(tab, "fill_qty")
    taker = _taker_mask(tab)
    out = []
    for m in sorted(set(mid.tolist())):
        mm = mid == m
        first = mm & (seq == 1)
        q_all = float(qty[mm].sum())
        out.append(
            {
                "model_id": m,
                "fills": int(mm.sum()),
                "taker_fills": int((mm & taker).sum()),
                "taker_qty_share": float(qty[mm & taker].sum()) / q_all if q_all > 0 else None,
                "shadows_filled": len(set(sid[mm].tolist())),
                "shadows_first_taker": int((first & taker).sum()),
            }
        )
    return out
