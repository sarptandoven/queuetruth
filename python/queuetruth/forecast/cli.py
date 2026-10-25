"""`queuetruth forecast commit|resolve|verify|score|power|simulate` (Python-only command group).

`queuetruth.cli.main` hands every command line whose first command word is `forecast` to `forecast_main`; the
global flags (`--format` / `--output-format`, `--quiet`, `--verbose`, `--log-level`, `--seed`, `--threads`) are
accepted before or after the subcommand. Exit codes as the rest of the CLI: 0 success, 1 a gate failed
(`verify` found integrity problems; `QT-GATE-001` on stderr), 2 any error (a `qt.error` document with
`--format json`). With `--format json` exactly one JSON document goes to stdout: a `qt.*` document for `commit`,
`resolve`, `verify` and `score`; `power` and `simulate` print small schema-less objects (their fields are listed
in `docs/how-to/score-forecasts.md`), since they describe a calculation or the files written, not a result to keep.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from queuetruth.errors import EXIT_ERROR, EXIT_GATE, EXIT_OK, QtError

#: Global flags of the main grammar and whether they take a value.
GLOBAL_FLAGS = {
    "--format": True,
    "--output-format": True,
    "--log-level": True,
    "--seed": True,
    "--threads": True,
    "--quiet": False,
    "--verbose": False,
}
_UNITS = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000, "m": 60_000_000_000}
_UNITS |= {"h": 3_600_000_000_000, "d": 86_400_000_000_000}
MAX_LISTED = 50


def command_word(argv: list[str]) -> str | None:
    """The first word of `argv` that is not a global flag or a global flag's value."""
    i = 0
    while i < len(argv):
        a = argv[i]
        name = a.split("=", 1)[0]
        if name in GLOBAL_FLAGS:
            i += 1 if ("=" in a or not GLOBAL_FLAGS[name]) else 2
            continue
        return a
    return None


def _split_globals(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    g: dict[str, Any] = {"format": "human", "quiet": False, "verbose": False, "seed": None}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition("=")
        if name in GLOBAL_FLAGS:
            if GLOBAL_FLAGS[name]:
                if not eq:
                    if i + 1 >= len(argv):
                        raise QtError("QT-USAGE-001", f"{name} needs a value")
                    val = argv[i + 1]
                    i += 1
                key = "format" if name in ("--format", "--output-format") else name[2:].replace("-", "_")
                if key == "format" and val not in ("human", "json"):
                    raise QtError("QT-USAGE-001", f"{name}: expected human or json, got {val!r}")
                if key == "seed":
                    try:
                        g["seed"] = int(val)
                    except ValueError:
                        raise QtError("QT-USAGE-001", f"--seed: expected an integer, got {val!r}") from None
                else:
                    g[key] = val
            else:
                g[name[2:]] = True
            i += 1
            continue
        rest.append(a)
        i += 1
    return g, rest


def parse_duration(text: str, flag: str) -> int:
    """`5m`, `1h`, `250ms`, `0` -> ns; a unit-less non-zero number is refused (`QT-USAGE-001`)."""
    t = text.strip()
    if t == "0":
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ns|us|ms|s|m|h|d)", t)
    if m is None:
        if re.fullmatch(r"\d+(?:\.\d+)?", t):
            raise QtError("QT-USAGE-001", f'{flag}: "{t}" has no unit; write {t}s')
        raise QtError("QT-USAGE-001", f"{flag}: expected a duration such as 30s, 5m or 1h, got {text!r}")
    ns = float(m.group(1)) * _UNITS[m.group(2)]
    if ns != int(ns):
        raise QtError("QT-USAGE-001", f"{flag}: {text!r} is not a whole number of ns")
    return int(ns)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise QtError("QT-USAGE-001", f"forecast: {message}", "run `queuetruth forecast --help`")


def build_parser() -> argparse.ArgumentParser:
    """The argparse grammar of the command group."""
    p = _Parser(
        prog="queuetruth forecast",
        description="Forecast referee: commit forecasts to a hash-chained ledger before their questions resolve, "
        "record resolutions, and score proper scores, calibration, skill and executable PnL.",
    )
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    c = sub.add_parser("commit", help="append forecasts to the ledger (hashed, chained, timestamped)")
    c.add_argument("--forecasts", required=True, help="forecasts file (.csv, .jsonl, .json, .parquet)")
    c.add_argument("--ledger", required=True, help="ledger file (created when missing)")
    c.add_argument("--forecaster", help="forecaster name for rows without a forecaster column")
    c.add_argument("--at", help="record this time instead of the system clock (clock: declared, not trusted)")
    c.add_argument("--commitment-out", help="also write the head commitment (qt.forecast_commitment) here")
    c.add_argument(
        "--allow-repeat",
        action="store_true",
        help="record a row without a nonce even when the same forecast is already in the ledger",
    )

    r = sub.add_parser("resolve", help="append question resolutions to the ledger")
    r.add_argument("--resolutions", required=True, help="resolutions file (.csv, .jsonl, .json, .parquet)")
    r.add_argument("--ledger", required=True, help="ledger file")
    r.add_argument("--at", help="record this time instead of the system clock (clock: declared)")
    r.add_argument("--correct", action="store_true", help="allow a corrected second resolution of a question")
    r.add_argument("--commitment-out", help="also write the head commitment here")

    v = sub.add_parser("verify", help="check the ledger chain (and optionally a commitment)")
    v.add_argument("--ledger", required=True, help="ledger file")
    v.add_argument("--commitment", help="a qt.forecast_commitment the ledger must still contain")

    s = sub.add_parser("score", help="score the ledger: proper scores, calibration, skill, power, PnL")
    s.add_argument("--ledger", required=True, help="ledger file")
    s.add_argument("--out", required=True, help="output directory")
    s.add_argument("--market", help="quote file: question_id, ts, best_bid, best_ask, bid_depth, ask_depth")
    s.add_argument("--market-max-age", help="oldest quote the market baseline uses, e.g. 1h (--max-quote-age)")
    s.add_argument(
        "--commitment",
        action="append",
        default=[],
        help="an anchored qt.forecast_commitment (repeatable; pair each with --anchored-at)",
    )
    s.add_argument(
        "--anchored-at",
        action="append",
        default=[],
        help="when a third party attested the matching --commitment (timestamp)",
    )
    s.add_argument(
        "--require-anchor",
        action="store_true",
        help="score only forecasts covered by an anchored commitment attested before their cutoff",
    )
    s.add_argument("--unresolved", choices=["warn", "fail"], help="forecasts on unresolved questions (warn)")
    s.add_argument("--pairwise", help="forecaster-pair comparisons: all, none or a top-k count (all)")
    s.add_argument("--policy", choices=["last", "first", "mean"], help="forecasts scored per question (last)")
    s.add_argument("--baselines", help="comma-separated: market, climatology, uniform (all three)")
    s.add_argument("--cluster", choices=["question", "group"], help="bootstrap cluster (question)")
    s.add_argument("--bins", type=int, help="reliability bins (10)")
    s.add_argument("--bootstrap-b", type=int, help="bootstrap resamples (1000)")
    s.add_argument("--alpha", type=float, help="test level (0.05)")
    s.add_argument("--power", type=float, help="target power of the power analysis (0.8)")
    s.add_argument("--min-edge", type=float, help="edge to size the power analysis for (loss units)")
    s.add_argument("--dm-lag", type=int, help="Diebold-Mariano HAC lags (Newey-West rule)")
    s.add_argument("--min-lead", help="minimum time from commit to cutoff, e.g. 1h (0)")
    s.add_argument("--allow-declared-clock", action="store_true", help="score entries with a declared time")
    s.add_argument("--log-eps", type=float, help="log score probability floor (1e-6)")
    s.add_argument("--climatology-min-prior", type=int, help="resolved questions before climatology counts (5)")
    s.add_argument("--no-pnl", action="store_true", help="skip executable PnL")
    s.add_argument("--kelly-fraction", type=float, help="fraction of the Kelly stake (0.25)")
    s.add_argument("--bankroll", type=float, help="starting bankroll (1000)")
    s.add_argument("--latency", help="decision-to-arrival latency, e.g. 250ms (0)")
    s.add_argument("--max-participation", type=float, help="share of the displayed depth an order may take (1)")
    s.add_argument("--max-quote-age", help="oldest usable quote at the decision, e.g. 5m (no limit)")
    s.add_argument(
        "--price-at",
        choices=["decision", "arrival"],
        help="price orders from the quote at the decision (a limit filled at arrival) or at arrival (decision)",
    )
    s.add_argument("--pnl-min-edge", type=float, help="minimum edge per contract after costs (0)")
    s.add_argument("--lot", type=float, help="contract granularity, 0 = continuous (1)")
    s.add_argument("--fee-per-contract", type=float, help="fee per contract")
    s.add_argument("--fee-notional-rate", type=float, help="fee per contract per unit of price")
    s.add_argument("--fee-quadratic", type=float, help="fee per contract times P(1 - P), e.g. 0.07")
    s.add_argument("--fee-round-up-to", type=float, help="round each order's fee up to this, e.g. 0.01")

    w = sub.add_parser("power", help="resolved questions needed to detect an edge")
    w.add_argument("--edge", type=float, required=True, help="expected loss difference per question")
    w.add_argument("--sd", type=float, required=True, help="standard deviation of the per-question difference")
    w.add_argument("--alpha", type=float, default=0.05, help="two-sided test level (0.05)")
    w.add_argument("--power", type=float, default=0.8, help="target power (0.8)")
    w.add_argument("--design-effect", type=float, default=1.0, help="variance inflation from clustering (1)")
    w.add_argument("--n", type=int, help="also report the power at this many questions")

    m = sub.add_parser("simulate", help="write example inputs from a world with a known edge")
    m.add_argument("--out", required=True, help="output directory")
    m.add_argument("--questions", type=int, default=400, help="number of binary questions (400)")
    m.add_argument("--edge-sd", type=float, default=0.6, help="log-odds information the market misses (0.6)")
    m.add_argument("--half-spread", type=float, default=0.01, help="quoted half spread (0.01)")
    m.add_argument("--depth", type=float, default=500.0, help="contracts displayed per side (500)")
    m.add_argument("--start", help="time the questions open (default: now); they resolve a day later")
    return p


def _score_spec(a: argparse.Namespace, seed: int | None) -> dict[str, Any]:
    spec: dict[str, Any] = {"ledger": {"path": a.ledger}, "out": a.out}
    if a.market:
        spec["market"] = {"path": a.market}
    simple = {
        "policy": a.policy,
        "cluster": a.cluster,
        "n_bins": a.bins,
        "bootstrap_b": a.bootstrap_b,
        "alpha": a.alpha,
        "power": a.power,
        "min_edge": a.min_edge,
        "dm_lag": a.dm_lag,
        "log_eps": a.log_eps,
        "climatology_min_prior": a.climatology_min_prior,
    }
    spec |= {k: v for k, v in simple.items() if v is not None}
    if seed is not None:
        spec["seed"] = seed
    if a.baselines:
        spec["baselines"] = [x.strip() for x in a.baselines.split(",") if x.strip()]
    if a.min_lead is not None:
        spec["min_lead_ns"] = parse_duration(a.min_lead, "--min-lead")
    if a.allow_declared_clock:
        spec["allow_declared_clock"] = True
    if a.market_max_age is not None:
        spec["market_max_age_ns"] = parse_duration(a.market_max_age, "--market-max-age")
    if len(a.commitment) != len(a.anchored_at):
        raise QtError("QT-USAGE-001", "each --commitment needs one --anchored-at (in the same order)")
    if a.commitment:
        from queuetruth.forecast.io import parse_ts

        spec["anchors"] = [
            {"commitment": {"path": c}, "anchored_at_ns": parse_ts(t, "--anchored-at")}
            for c, t in zip(a.commitment, a.anchored_at, strict=True)
        ]
    if a.require_anchor:
        spec["require_anchor"] = True
    if a.unresolved is not None:
        spec["unresolved"] = a.unresolved
    if a.pairwise is not None:
        pw = a.pairwise.strip()
        if pw in ("all", "none"):
            spec["pairwise"] = pw
        elif pw.isdigit():
            spec["pairwise"] = int(pw)
        else:
            raise QtError("QT-USAGE-001", f"--pairwise: expected all, none or a count, got {a.pairwise!r}")
    if a.no_pnl:
        spec["pnl"] = None
        return spec
    pnl: dict[str, Any] = {}
    for key, val in (
        ("kelly_fraction", a.kelly_fraction),
        ("bankroll", a.bankroll),
        ("max_participation", a.max_participation),
        ("min_edge", a.pnl_min_edge),
        ("lot", a.lot),
    ):
        if val is not None:
            pnl[key] = val
    if a.latency is not None:
        pnl["latency_ns"] = parse_duration(a.latency, "--latency")
    if a.max_quote_age is not None:
        pnl["max_quote_age_ns"] = parse_duration(a.max_quote_age, "--max-quote-age")
    if a.price_at is not None:
        pnl["price_at"] = a.price_at
    fees = {
        k: v
        for k, v in (
            ("per_contract", a.fee_per_contract),
            ("notional_rate", a.fee_notional_rate),
            ("quadratic", a.fee_quadratic),
            ("round_up_to", a.fee_round_up_to),
        )
        if v is not None
    }
    if fees:
        pnl["fees"] = fees
    if pnl:
        spec["pnl"] = pnl
    return spec


def _ledger_summary(action: str, ledger: str, new: list[Any], problems: list[Any] | None = None) -> Any:
    from queuetruth.forecast.ledger import commitment, verify_ledger
    from queuetruth.forecast.schemas import ForecastLedgerSummary, LedgerEntryRef

    v = verify_ledger(ledger)
    probs = list(v.problems) + list(problems or [])
    refs = [
        LedgerEntryRef(
            seq=e.seq,
            type=e.payload.type,
            question_id=e.payload.question_id,
            forecaster=getattr(e.payload, "forecaster", None),
            entry_sha256=e.entry_sha256,
        )
        for e in new[:MAX_LISTED]
    ]
    return ForecastLedgerSummary(
        action=action,  # type: ignore[arg-type]
        ledger=ledger,
        engine_version=_version(),
        appended=len(new),
        entries=refs,
        entries_truncated=len(new) > MAX_LISTED,
        commitment=commitment(v.entries) if v.entries and v.ok else None,
        verified=not probs,
        problems=probs,
        warnings=[],
    )


def _version() -> str:
    from queuetruth._version import __version__

    return __version__


def _write_commitment(summary: Any, path: str | None) -> None:
    if path and summary.commitment is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(summary.commitment.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _declared_column(rows: list[dict[str, Any]], where: str, at: int | None) -> list[int | None] | None:
    """Per-row declared times from a `declared_at` column (every row, non-decreasing), or None without one."""
    from queuetruth.forecast.io import parse_ts
    from queuetruth.forecast.ledger import _present

    if not any(_present(r, "declared_at") for r in rows):
        return None
    if at is not None:
        raise QtError("QT-USAGE-001", f"{where}: give --at or a declared_at column, not both")
    out: list[int | None] = []
    for i, r in enumerate(rows, 1):
        if not _present(r, "declared_at"):
            raise QtError("QT-INPUT-002", f"{where}: row {i}: declared_at is missing (the column is all or none)")
        t = parse_ts(r["declared_at"], f"{where}: row {i}: declared_at")
        if out and t < out[-1]:  # type: ignore[operator]
            raise QtError(
                "QT-INPUT-002",
                f"{where}: row {i}: declared_at goes backwards; sort the file by declared_at (ledger order is time "
                "order)",
            )
        out.append(t)
    return out


def run(a: argparse.Namespace, g: dict[str, Any]) -> tuple[Any, bool]:
    """Runs a parsed subcommand: (summary document or object, gate failed)."""
    from queuetruth.forecast import ledger as lg
    from queuetruth.forecast.io import parse_ts, read_records

    if a.cmd in ("commit", "resolve"):
        at = parse_ts(a.at, "--at") if a.at else None
        if a.cmd == "commit":
            rows = read_records(a.forecasts)
            pls = [lg.normalize_forecast(r, f"{a.forecasts}: row {i}", a.forecaster) for i, r in enumerate(rows, 1)]
            declared = _declared_column(rows, a.forecasts, at)
            generated = None if a.allow_repeat else [not lg._present(r, "nonce") for r in rows]
            new = lg.append(
                a.ledger, pls, declared_at_ns=declared if declared is not None else at, generated_nonce=generated
            )
            if declared is not None:
                at = declared[0]
        else:
            rows = read_records(a.resolutions)
            pls = [lg.normalize_resolution(r, f"{a.resolutions}: row {i}") for i, r in enumerate(rows, 1)]
            new = lg.append(a.ledger, pls, declared_at_ns=at, allow_correction=a.correct)
        summary = _ledger_summary(a.cmd, a.ledger, new)
        _write_commitment(summary, a.commitment_out)
        if at is not None:
            summary.warnings.append("entries carry clock: declared; `forecast score` ignores them by default")
        return summary, False
    if a.cmd == "verify":
        from queuetruth.forecast.schemas import ForecastCommitment, LedgerProblem

        extra = []
        if a.commitment:
            try:
                c = ForecastCommitment.model_validate_json(Path(a.commitment).read_text(encoding="utf-8"))
            except OSError as e:
                raise QtError("QT-INPUT-001", f"{a.commitment}: {e}") from None
            except ValueError as e:
                raise QtError("QT-INPUT-002", f"{a.commitment}: not a qt.forecast_commitment: {e}") from None
            v = lg.verify_ledger(a.ledger)
            extra = [LedgerProblem(seq=c.head_seq, line=0, problem=x) for x in lg.check_commitment(v.entries, c)]
        summary = _ledger_summary("verify", a.ledger, [], extra)
        return summary, not summary.verified
    if a.cmd == "score":
        from queuetruth.forecast.engine import score

        return score(_score_spec(a, g.get("seed"))), False
    if a.cmd == "power":
        from queuetruth.forecast.power import power_at, required_n

        if not (0 < a.alpha < 1 and 0 < a.power < 1) or a.design_effect < 1 or a.sd <= 0:
            raise QtError("QT-CONFIG-001", "power: need 0 < alpha, power < 1, sd > 0 and design_effect >= 1")
        n = required_n(a.edge, a.sd, a.alpha, a.power, a.design_effect)
        doc: dict[str, Any] = {
            "edge": a.edge,
            "sd": a.sd,
            "alpha": a.alpha,
            "power": a.power,
            "design_effect": a.design_effect,
            "n_required": n,
        }
        if a.n is not None:
            pw = power_at(a.n, a.edge, a.sd, a.alpha, a.design_effect)
            doc |= {"n": a.n, "power_at_n": pw if math.isfinite(pw) else None}
        return doc, False
    if a.cmd == "simulate":
        import time

        from queuetruth.forecast.simulate import write_example

        start = parse_ts(a.start, "--start") if a.start else (time.time_ns() // 1_000_000_000) * 1_000_000_000
        seed = g.get("seed") or 0
        if a.questions < 1 or a.edge_sd < 0 or not 0 <= a.half_spread < 0.5 or a.depth < 0:
            raise QtError("QT-CONFIG-001", "simulate: need questions >= 1, edge-sd >= 0, 0 <= half-spread < 0.5")
        paths = write_example(
            a.out,
            n_questions=a.questions,
            edge_sd=a.edge_sd,
            seed=seed,
            start_ns=start,
            half_spread=a.half_spread,
            depth=a.depth,
        )
        return {
            "out": a.out,
            "seed": seed,
            "questions": a.questions,
            "files": {k: str(v) for k, v in paths.items()},
        }, False
    raise QtError("QT-USAGE-001", f"unknown forecast command {a.cmd!r}")


# ---- human views ----------------------------------------------------------------------------------------------------


def _num(x: Any, fmt: str = ".4f") -> str:
    return "n/a" if x is None else format(x, fmt)


def _ci(v: Any, fmt: str = ".4f") -> str:
    if v is None or v.value is None:
        return "n/a"
    if v.ci_lo is None or v.ci_hi is None:
        return format(v.value, fmt)
    return f"{format(v.value, fmt)} [{format(v.ci_lo, fmt)}, {format(v.ci_hi, fmt)}]"


def render_scorecard(card: Any) -> str:
    """Terminal view of a scorecard: the answer per forecaster first."""
    from queuetruth.forecast.schemas import REJECT_STATUSES

    c = card.counts
    rej = sum(v for k, v in c.by_status.items() if k in REJECT_STATUSES)
    lines = [
        f"forecast scorecard {card.run_id}: {c.by_status.get('scored', 0)} of {c.forecasts} forecasts scored, "
        f"{rej} rejected by the leakage and validity guard, {c.questions_resolved} of {c.questions} questions resolved",
        "",
    ]
    for a in card.ledger.anchors:
        lines.insert(1, f"anchor: head seq {a.head_seq} attested {_ts(a.anchored_at_ns)} ({a.path})")
    for f in card.forecasters:
        lines.append(f"{f.forecaster}: {f.n_scored} scored, {f.n_rejected} rejected")
        sel = f.selection
        extra = [
            (sel.unresolved, "on unresolved questions"),
            (sel.void, "on voided questions"),
            (sel.voided_after_outcome, "questions voided after an outcome"),
            (sel.outcome_corrected, "questions with a corrected outcome"),
            (sel.cutoff_tightened, "questions with a tightened cutoff"),
        ]
        if any(n for n, _ in extra):
            lines.append("  resolutions: " + ", ".join(f"{n} {what}" for n, what in extra if n))
        for s in f.scores:
            if s.metric in ("brier", "crps"):
                lines.append(f"  {s.kind} {s.metric}: {_ci(s.mean)} (n {s.n})")
        for k in f.skill:
            if k.metric in ("brier", "crps"):
                lines.append(
                    f"  vs {k.reference}: skill {_ci(k.skill, '.3f')}, DM p {_num(k.dm.p_value, '.3g')}, "
                    f"Holm p {_num(k.p_holm, '.3g')} ({_units(k)})"
                )
        for cal in f.calibration:
            lines.append(
                f"  calibration ({cal.target}): ECE {_ci(cal.ece)}, expected if calibrated "
                f"{_num(cal.ece_if_calibrated)}, p {_num(cal.ece_p_value, '.3g')}"
            )
        for pw in f.power:
            if pw.reference == "baseline:market" or len(f.power) == 1:
                plan = f"min edge {pw.min_edge}: {pw.n_required_min_edge or 'n/a'} questions; " if pw.min_edge else ""
                lines.append(
                    f"  power vs {pw.reference}: {plan}at the observed edge {_num(pw.observed_edge, '.5f')} "
                    f"(noisy): {pw.n_required_observed or 'n/a'} questions"
                )
        if f.pnl is not None:
            p = f.pnl
            lines.append(
                f"  PnL per question (isolated): {_ci(p.isolated_growth, '.5f')}, frictionless "
                f"{_num(p.frictionless_growth, '.5f')}; {p.n_bets} bets of {p.n_candidates}, fees {p.fees_paid:.2f}, "
                f"spread {p.spread_paid:.2f}"
            )
            path = (
                f"  bankroll path{' (CASH-LIMITED: an arbitrary subset of bets)' if p.path_cash_limited else ''}: "
                f"return {p.total_return:+.2%}, growth/bet {_num(p.growth_per_bet, '.5f')}, max drawdown "
                f"{p.max_drawdown:.2%}"
            )
            lines.append(path)
            if p.n_skipped:
                skipped = ", ".join(f"{k} {v}" for k, v in sorted(p.n_skipped.items(), key=lambda kv: (-kv[1], kv[0])))
                lines.append(f"  PnL no bet: {skipped}")
        lines.append("")
    for k in card.comparisons:
        if k.metric in ("brier", "crps"):
            lead = f", lead diff {_dur(k.lead_diff_ns)}" if k.lead_diff_ns and abs(k.lead_diff_ns) >= 1e9 else ""
            lines.append(
                f"{k.subject} vs {k.reference} ({k.kind} {k.metric}): mean diff {_ci(k.mean_diff, '.4f')}, "
                f"DM p {_num(k.dm.p_value, '.3g')}, Holm p {_num(k.p_holm, '.3g')} ({_units(k)}{lead})"
            )
    for fe in card.files:
        lines.append(f"file: {fe.name} ({fe.rows} rows)")
    return "\n".join(lines).rstrip()


def _units(k: Any) -> str:
    if k.dm.units and k.dm.units != k.n:
        return f"n {k.n} in {k.dm.units} clusters"
    return f"n {k.n}"


def _dur(ns: int) -> str:
    """Signed duration in the largest unit of h, min and s that keeps it at least 1."""
    for unit, size in (("h", 3_600_000_000_000), ("min", 60_000_000_000), ("s", 1_000_000_000)):
        if abs(ns) >= size or unit == "s":
            return f"{ns / size:+.1f} {unit}"
    return f"{ns} ns"  # pragma: no cover


def _ts(ns: int) -> str:
    from queuetruth.forecast.io import format_ts

    return format_ts(ns)


def render(cmd: str, doc: Any) -> str:
    """Terminal view of any forecast command's result."""
    if cmd == "score":
        return render_scorecard(doc)
    if cmd in ("commit", "resolve", "verify"):
        c = doc.commitment
        head = f"head seq {c.head_seq} {c.head_sha256}" if c is not None else "no commitment"
        lines = [
            f"forecast {cmd}: {'verified' if doc.verified else 'INTEGRITY PROBLEMS'}, appended {doc.appended}, {head}"
        ]
        for p in doc.problems[:20]:
            lines.append(f"  line {p.line} seq {p.seq}: {p.problem}")
        return "\n".join(lines)
    lines = []
    for k, v in doc.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            lines.extend(f"  {kk}: {vv}" for kk, vv in v.items())
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def _plain(doc: Any) -> Any:
    from pydantic import BaseModel

    if isinstance(doc, BaseModel):
        return doc.model_dump(mode="json")
    return doc


def forecast_main(argv: list[str]) -> int | None:
    """Runs a `forecast` command line (without the program name); None when `argv` is not one."""
    if command_word(argv) != "forecast":
        return None
    json_out = any(
        a in ("--format=json", "--output-format=json") or (a in ("--format", "--output-format") and b == "json")
        for a, b in zip(argv, [*argv[1:], ""], strict=True)
    )
    try:
        g, rest = _split_globals(argv)
        json_out = g["format"] == "json"
        rest = rest[1:]  # drop "forecast"
        parser = build_parser()
        if not rest or rest[0] in ("-h", "--help"):
            print(parser.format_help().rstrip())
            return EXIT_OK if rest else EXIT_ERROR
        try:
            a = parser.parse_args(rest)
        except SystemExit as e:  # --help of a subcommand
            return int(e.code or 0)
        doc, gate = run(a, g)
    except QtError as e:
        if json_out:
            print(json.dumps(e.to_document(), ensure_ascii=False))
        else:
            print(str(e), file=sys.stderr)
            if e.hint:
                print(f"hint: {e.hint}", file=sys.stderr)
        return e.exit_code
    except Exception as e:  # unexpected failures are QT-ENGINE-001, exit 2
        err = QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}")
        if json_out:
            print(json.dumps(err.to_document(), ensure_ascii=False))
        else:
            print(str(err), file=sys.stderr)
        return EXIT_ERROR
    if json_out:
        print(json.dumps(_plain(doc), ensure_ascii=False, allow_nan=False))
    else:
        print(render(a.cmd, doc))
    if not g.get("quiet"):
        sys.stdout.flush()
        for w in getattr(doc, "warnings", None) or []:
            print(f"warning: {w}", file=sys.stderr)
        if gate:
            print("QT-GATE-001: a requested gate failed", file=sys.stderr)
    return EXIT_GATE if gate else EXIT_OK


__all__ = ["build_parser", "command_word", "forecast_main", "parse_duration", "render", "run"]
