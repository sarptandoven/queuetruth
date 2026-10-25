"""Synthetic worlds with a known forecasting edge, for validation and the worked example.

`binary_world`: the market prices question i at `q_i ~ Beta(a, b)` (clipped to [0.03, 0.97]); the true
probability is `p_i = expit(logit(q_i) + edge_sd Z_i)`, `Z_i ~ N(0, 1)`, so `edge_sd` is the spread of the
information the market misses (0 = the market is right); the outcome is `y_i ~ Bernoulli(p_i)`. A forecaster who
reports `p` has expected Brier edge `mean((q - p)^2)` over the market and expected full-Kelly log growth at the
mid `mean(KL(p || q))` (`queuetruth.forecast.power`).

`write_example` writes the input files of `docs/how-to/score-forecasts.md` for that world: `forecasts.csv`
(forecaster `sharp` reports `p`, forecaster `crowd` reports the market price plus noise), `late.csv` (a third
forecaster who sends the outcomes after resolution), `resolutions.csv` and `quotes.csv` (one quote per question an
hour before `start`, `bid = q - half_spread`, `ask = q + half_spread`, `depth` contracts a side). Questions resolve
`resolve_after_ns` after `start` with the information cutoff an hour earlier. Every value, nonces included, is a
function of `seed` and `start`.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import special

from queuetruth.forecast.io import format_ts
from queuetruth.stats import stable_seed

HOUR_NS = 3_600_000_000_000
DAY_NS = 24 * HOUR_NS


@dataclass(frozen=True)
class BinaryWorld:
    """Market price, true probability and outcome per question."""

    q: np.ndarray
    p: np.ndarray
    y: np.ndarray


def binary_world(n: int, edge_sd: float, seed: int, a: float = 2.0, b: float = 2.0) -> BinaryWorld:
    """`n` questions of the known-edge world (deterministic in `seed`)."""
    rng = np.random.default_rng(stable_seed(seed, [b"forecast-world"]))
    q = np.clip(rng.beta(a, b, size=n), 0.03, 0.97)
    p = special.expit(special.logit(q) + edge_sd * rng.standard_normal(n))
    y = (rng.random(n) < p).astype(float)
    return BinaryWorld(q=q, p=p, y=y)


def write_example(
    out: str | Path,
    *,
    n_questions: int = 400,
    edge_sd: float = 0.6,
    seed: int = 0,
    start_ns: int,
    half_spread: float = 0.01,
    depth: float = 500.0,
    crowd_noise_sd: float = 0.3,
    resolve_after_ns: int = DAY_NS,
) -> dict[str, Path]:
    """Writes the example inputs under `out`; returns the paths by name."""
    if n_questions < 1:
        raise ValueError("n_questions must be positive")
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    w = binary_world(n_questions, edge_sd, seed)
    rng = np.random.default_rng(stable_seed(seed, [b"forecast-example"]))
    crowd = special.expit(special.logit(w.q) + crowd_noise_sd * rng.standard_normal(n_questions))

    def nonce() -> str:
        return rng.bytes(32).hex()

    def r4(x: float) -> str:
        return f"{x:.4f}"

    qids = [f"q{i:04d}" for i in range(n_questions)]
    paths = {k: d / f"{k}.csv" for k in ("forecasts", "late", "resolutions", "quotes")}
    with open(paths["forecasts"], "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["question_id", "forecaster", "p", "nonce"])
        for i, qid in enumerate(qids):
            wr.writerow([qid, "sharp", r4(w.p[i]), nonce()])
        for i, qid in enumerate(qids):
            wr.writerow([qid, "crowd", r4(crowd[i]), nonce()])
    with open(paths["late"], "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["question_id", "forecaster", "p", "nonce"])
        for i, qid in enumerate(qids):
            wr.writerow([qid, "late", r4(0.99 if w.y[i] else 0.01), nonce()])
    resolved = start_ns + resolve_after_ns
    with open(paths["resolutions"], "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["question_id", "outcome", "resolved_at", "info_cutoff"])
        for i, qid in enumerate(qids):
            wr.writerow([qid, int(w.y[i]), format_ts(resolved), format_ts(resolved - HOUR_NS)])
    with open(paths["quotes"], "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh, lineterminator="\n")
        wr.writerow(["question_id", "ts", "best_bid", "best_ask", "bid_depth", "ask_depth"])
        for i, qid in enumerate(qids):
            bid = max(0.0, w.q[i] - half_spread)
            ask = min(1.0, w.q[i] + half_spread)
            wr.writerow([qid, format_ts(start_ns - HOUR_NS), r4(bid), r4(ask), int(depth), int(depth)])
    return paths


def expected_kelly_growth(p: float, price: float, side_p: float, fraction: float) -> float:
    """Expected log growth of one fractional-Kelly bet on a contract costing `price` (no fee) that pays 1 with
    true probability `side_p`, sized on the belief `p` (the bettor's probability of the same side)."""
    if not p > price:
        return 0.0
    f = fraction * (p - price) / (1.0 - price)
    return side_p * math.log(1.0 - f + f / price) + (1.0 - side_p) * math.log(1.0 - f)


__all__ = ["DAY_NS", "HOUR_NS", "BinaryWorld", "binary_world", "expected_kelly_growth", "write_example"]
