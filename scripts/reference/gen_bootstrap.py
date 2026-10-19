"""Day-block bootstrap goldens (SPEC 5.7.5 / 7.2, oracle O-STAT-4 "bootstrap CI deterministic for a seed")
-> tests/fixtures/reference/bootstrap.json.

SPEC fixes the generator (`numpy.random.default_rng(stable_seed(seed, [b"bootstrap", test_id]))`) but not the
draw procedure, so these values pin ONE reading (same pattern as et's calibration
bootstrap):

    labels  = numpy.unique(blocks)                       # sorted block labels (dates or 30-min buckets)
    members = [numpy.flatnonzero(blocks == l) for l in labels]   # row indices, original order
    rng     = numpy.random.default_rng(derived_seed)     # derived_seed = stable_seed(seed, [b"bootstrap", test_id])
    draws   = rng.integers(0, len(labels), size=(b, len(labels)))   # ONE call, int64, row r = replicate r
    reps[r] = stat(numpy.concatenate([members[j] for j in draws[r]]))   # stat receives ROW INDICES
    estimate = stat(numpy.arange(n_rows))
    lo, hi  = numpy.quantile(reps, [alpha / 2, 1 - alpha / 2])          # method "linear"

Also recorded per case: `boot_sd_ddof1` / `boot_sd_ddof0` (SPEC "se = bootstrap SD" does not fix ddof),
the fill-rate-bias bootstrap test `p = min(1, 2 min((1 + #{reps <= 0}) / (1 + B), (1 + #{reps >= 0}) / (1 + B)))`
(SPEC 5.7.5 primary family, capped at 1 and in the (1 + #)/(1 + B) form so it is
never exactly 0), and the 90% CI used by TOST.

`xoshiro_block_resample` pins the stream of the RUST calibration bootstrap (SPEC 5.4, in qt_calib, not in
queuetruth.stats): stream `Xoshiro256StarStar::new(stable_seed(seed, &[b"boot"]))`, block index for slot j of
replicate r = `range_inclusive(0, n_blocks - 1)`, drawn replicate-major. Only the index stream is recorded.
"""

from __future__ import annotations

import numpy as np
from _common import Xoshiro256StarStar, header, rng_for, stable_seed, write_fixture


def make_rows(name: str, n_blocks: int, sizes: tuple[int, int], p_live: float, p_sim: float):
    r = rng_for(name)
    blocks, live, sim = [], [], []
    for k in range(n_blocks):
        m = r.range_inclusive(*sizes)
        for _ in range(m):
            blocks.append(20260101 + k if n_blocks < 40 else k)
            u = r.next_f64()
            lv = u < p_live
            # sim agrees with live most of the time, with a bias toward extra fills
            sv = lv if r.next_f64() < 0.8 else (r.next_f64() < p_sim)
            live.append(int(lv))
            sim.append(int(sv))
    return np.array(blocks, dtype=np.int64), np.array(live), np.array(sim)


def bootstrap(blocks, stat, b, derived_seed, alpha):
    labels = np.unique(blocks)
    members = [np.flatnonzero(blocks == lab) for lab in labels]
    rng = np.random.default_rng(derived_seed)
    draws = rng.integers(0, len(labels), size=(b, len(labels)))
    reps = np.array([stat(np.concatenate([members[j] for j in row])) for row in draws])
    est = stat(np.arange(len(blocks)))
    lo, hi = np.quantile(reps, [alpha / 2, 1 - alpha / 2])
    return est, float(lo), float(hi), reps, draws


def case(name, n_blocks, sizes, p_live, p_sim, seed, test_id, b):
    blocks, live, sim = make_rows(name, n_blocks, sizes, p_live, p_sim)
    derived = stable_seed(seed, [b"bootstrap", test_id.encode()])

    def bias(idx):
        return float(np.mean(sim[idx] - live[idx]))

    est, lo, hi, reps, draws = bootstrap(blocks, bias, b, derived, 0.05)
    _, lo90, hi90, reps90, _ = bootstrap(blocks, bias, b, derived, 0.10)
    assert np.array_equal(reps, reps90)
    lo_hits, hi_hits = int(np.sum(reps <= 0)), int(np.sum(reps >= 0))
    p_test = min(1.0, 2 * min((1 + lo_hits) / (1 + b), (1 + hi_hits) / (1 + b)))
    return {
        "name": name,
        "seed": seed,
        "test_id": test_id,
        "derived_seed": derived,
        "b": b,
        "alpha": 0.05,
        "input": {"blocks": blocks, "live_filled": live, "sim_filled": sim},
        "stat": "fill_rate_bias = mean(sim_filled[idx] - live_filled[idx])",
        "n_blocks": len(np.unique(blocks)),
        "first_draws": draws[:5],
        "estimate": est,
        "ci95": [lo, hi],
        "ci90": [lo90, hi90],
        "boot_mean": float(np.mean(reps)),
        "boot_sd_ddof1": float(np.std(reps, ddof=1)),
        "boot_sd_ddof0": float(np.std(reps, ddof=0)),
        "p_bootstrap_test": p_test,
        "replicates_first_10": reps[:10],
    }


def main() -> None:
    cases = [
        case("days7_b1000", 7, (20, 60), 0.4, 0.9, 0, "fill_rate_bias", 1000),
        case("days5_b200_seed42", 5, (5, 30), 0.3, 0.5, 42, "fill_rate_bias", 200),
        case("days12_b500_unbiased", 12, (10, 25), 0.5, 0.5, 7, "fill_rate_bias", 500),
        case("buckets48_30min_b300", 48, (2, 9), 0.35, 0.7, 3, "fill_rate_bias", 300),
    ]
    xo = []
    for seed, nb in ((0, 7), (7, 48), (2**64 - 1, 5)):
        r = Xoshiro256StarStar(stable_seed(seed, [b"boot"]))
        draws = [[r.range_inclusive(0, nb - 1) for _ in range(nb)] for _ in range(4)]
        xo.append({"seed": seed, "n_blocks": nb, "stream_seed": stable_seed(seed, [b"boot"]), "draws": draws})
    doc = header(["O-STAT-4"], "gen_bootstrap.py", __doc__.strip(), ["numpy"])
    doc["tolerance"] = {"draws": "exact", "estimate_ci_sd": "abs 1e-15 (same numpy); rel 1e-12 otherwise"}
    doc["day_block_bootstrap"] = cases
    doc["xoshiro_block_resample"] = xo
    print(write_fixture("bootstrap.json", doc))


if __name__ == "__main__":
    main()
