"""The terminal answer of `score` and `calibrate-live` carries the hftbacktest #332 checks in plain lines: the
Kaplan-Meier time-to-fill line, the markout line at 100ms, 1s and 10s, and the latency-first line."""

from __future__ import annotations

from queuetruth import cli

S = 1_000_000_000


def _summary() -> dict:
    at = lambda v: [  # noqa: E731
        {"horizon_ns": h, "survival": v, "ci_lo": v - 0.05, "ci_hi": v + 0.05} for h in (S // 10, S, 10 * S)
    ]
    return {
        "ttf_survival": {
            "n": 100,
            "horizons_ns": [S // 10, S, 10 * S],
            "arms": {"live": {"at": at(0.9)}, "sim": {"at": at(0.7)}},
            "diff": [{"horizon_ns": h, "diff": -0.2, "ci_lo": -0.3, "ci_hi": -0.1} for h in (S // 10, S, 10 * S)],
            "logrank": {"statistic": 8.0, "p_value": 0.0047, "method": "m", "n_clusters": 12, "n_strata": 1},
        },
        "markout_diff": [
            {"horizon_ns": h, "ref": "mid", "n": 40, "mean_diff_ticks": d, "ci_lo": d - 0.1, "ci_hi": d + 0.1}
            for h, d in ((50_000_000, 9.0), (S // 10, 0.5), (S, 0.25), (10 * S, -0.13), (60 * S, 9.0))
        ],
    }


def test_time_to_fill_and_markout_lines():
    ttf, mk = cli._practitioner_lines(_summary())
    assert ttf == (
        "time to fill: still unfilled after 1s (Kaplan-Meier, cancels censored): live 90% [85%, 95%] vs "
        "sim 70% [65%, 75%]; sim minus live -20.0 pp (95% CI -30.0 to -10.0); log-rank p = 0.0047 (by instrument, "
        "12 clusters)"
    )
    assert mk == (
        "markouts: sim minus live, ticks of mid, orders filled in both (95% CI): 100ms +0.50 [+0.40, +0.60], "
        "1s +0.25 [+0.15, +0.35], 10s -0.13 [-0.23, -0.03]"
    )


def test_lines_absent_without_the_parts():
    assert cli._practitioner_lines({"ttf_survival": None, "markout_diff": []}) == []


def test_latency_first_line_in_calibrate_live():
    q = {"p10": 1.5e6, "p50": 2e6, "p90": 3e6, "p99": 4e6, "n": 24}
    doc = {
        "schema": "qt.calibration_result",
        "kind": "live",
        "family": "power3",
        "param_name": "n",
        "estimate": 1.6487,
        "latency": {"entry_ns": q},
        "train": {"dates": ["2026-01-01"]},
        "n_events": 16,
        "n_shadows": 24,
    }
    text = cli._render_calibration(doc, {}, ["calibrate-live"]) or ""
    assert (
        "latency (fitted first, on the live log): median entry latency 2ms (p10 1.5ms, p90 3ms, 24 orders); "
        "the queue below was fitted with it"
    ) in text
    assert text.index("latency (fitted first") < text.index("fitted: n =")
