#!/usr/bin/env python3
"""Score l3_mbo.py output against the exact arm, order by order (queuetruth venv; pyarrow + pandas).

Writes <work>/l3_report{suffix}.md and .json, where suffix is what the outcomes file name adds to `l3_outcomes`.

Usage: l3_score.py <work> [outcomes file name under <work>/nt, default l3_outcomes.parquet]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

H = {"0.1s": 100_000_000, "1s": 1_000_000_000, "10s": 10_000_000_000, "60s": 60_000_000_000}
TRUTH_MODEL = "nt_prob_touch[mode=faithful;p=1]"  # any model row carries the exact arm's truth_filled
L2_QUEUE_MODEL = "nt_queue_l2[mode=faithful]"


def ci(x: np.ndarray, b: int = 1000) -> tuple[float, float]:
    rng = np.random.default_rng(0)
    m = x[rng.integers(0, len(x), (b, len(x)))].mean(1)
    return 100 * float(np.quantile(m, 0.025)), 100 * float(np.quantile(m, 0.975))


def main() -> int:
    work = Path(sys.argv[1])
    fname = sys.argv[2] if len(sys.argv) > 2 else "l3_outcomes.parquet"
    suffix = fname.removeprefix("l3_outcomes").removesuffix(".parquet")
    sh = pq.read_table(work / "shadows.parquet").to_pandas().set_index("shadow_id")
    l3 = pq.read_table(work / "nt" / fname).to_pandas()
    ver = pq.read_schema(work / "nt" / fname).metadata[b"nautilus_trader"].decode()
    zp = pq.read_table(work / "zoo" / "zoo_predictions.parquet").to_pandas()
    ids = sorted(sh.index)
    truth = zp[zp.model_id == TRUTH_MODEL].pivot(index="shadow_id", columns="horizon_ns", values="truth_filled")
    truth = truth.astype(float)
    qmodel = zp[zp.model_id == L2_QUEUE_MODEL].pivot(index="shadow_id", columns="horizon_ns", values="p_fill")

    sub = l3[l3.config == l3.config.iloc[0]].set_index("shadow_id")
    mism = int((sub.q_l3.astype(int) != sh.loc[sub.index, "q_view"].astype(int)).sum())
    out: dict = {
        "nautilus_trader": ver,
        "n_shadows": len(ids),
        "n_submitted": len(sub),
        "l3_vs_l2_queue_at_live_mismatches": mism,
        "configs": {},
    }
    lines = [
        f"nautilus_trader {ver}; L3 (MBO) feed; {len(ids)} shadows, {len(sub)} submitted; "
        f"queue at live, L3 book vs L2 view, mismatches: {mism}",
        "",
        "| NT config | h | NT rate | exact | NT bias pp [95% CI] | agree with exact | NT1/exact0 | NT0/exact1 "
        "| `nt_queue_l2` (L2) bias pp |",
        "|" + "---|" * 9,
    ]
    for c in sorted(set(l3.config)):
        g = l3[l3.config == c].set_index("shadow_id")
        out["configs"][c] = {}
        ff = g.first_fill_ts.dropna().astype("int64")
        for hn, h in H.items():
            ntf = pd.Series(0.0, index=ids)
            ok = ff[ff <= g.loc[ff.index, "ts_live"].astype("int64") + h]
            ntf.loc[ok.index] = 1.0
            tr = truth[h].reindex(ids).fillna(0.0)
            zq = qmodel[h].reindex(ids).fillna(0.0)
            d = (ntf - tr).to_numpy()
            lo, hi = ci(d)
            hz = {
                "nt_rate": float(ntf.mean()),
                "exact_rate": float(tr.mean()),
                "nt_bias_pp": 100 * float(d.mean()),
                "nt_bias_ci_pp": [lo, hi],
                "agree_exact": float(((ntf > 0.5) == (tr > 0.5)).mean()),
                "nt1_exact0": int(((ntf > 0.5) & (tr < 0.5)).sum()),
                "nt0_exact1": int(((ntf < 0.5) & (tr > 0.5)).sum()),
                "nt_queue_l2_bias_pp": 100 * float((zq - tr).mean()),
            }
            out["configs"][c][hn] = hz
            lines.append(
                f"| {c} | {hn} | {100 * hz['nt_rate']:.2f}% | {100 * hz['exact_rate']:.2f}% | "
                f"{hz['nt_bias_pp']:+.2f} [{lo:+.2f}, {hi:+.2f}] | {100 * hz['agree_exact']:.2f}% | "
                f"{hz['nt1_exact0']} | {hz['nt0_exact1']} | {hz['nt_queue_l2_bias_pp']:+.2f} |"
            )
    # Harness check: each order is cancelled at ts_live + 60 s + 1 ns, so none may first fill after that.
    late = {}
    for c in sorted(set(l3.config)):
        g = l3[l3.config == c]
        ff = g.first_fill_ts.dropna().astype("int64")
        late[c] = int((ff > g.loc[ff.index, "ts_live"].astype("int64") + H["60s"] + 1).sum())
    out["filled_after_cancel"] = late
    lines += [
        "",
        "Orders first filled after their end-of-window cancel (must be 0): "
        + ", ".join(f"{c} {n}" for c, n in late.items()),
    ]
    (work / f"l3_report{suffix}.json").write_text(json.dumps(out, indent=1))
    (work / f"l3_report{suffix}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
