"""`python -m livefill <command>`: capture, check, export, import-jsonl, harness, match, pipeline, stand-in, synth.

Every command prints one JSON document on stdout; errors print a `qt.error` document on stderr and exit 2.
`--book-seed` (check, export, match, pipeline) picks the book snapshot a segment is replayed from; `--seed` (harness,
stand-in, synth) is a random seed."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from livefill import check, export, harness, matcher, pipeline, standin, synth
from livefill.capture import CaptureConfig, run_capture
from livefill.legacy import import_jsonl
from queuetruth.errors import QtError


def duration_s(text: str) -> float:
    """`90`, `90s`, `2m`, `1h`, `500ms` -> seconds."""
    t = text.strip().lower()
    num, mult = t, 1.0
    for suffix, factor in (("ms", 1e-3), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if t.endswith(suffix):
            num, mult = t[: -len(suffix)], factor
            break
    try:
        v = float(num) * mult
    except ValueError as e:
        raise QtError("QT-USAGE-001", f"duration {text!r}: expected e.g. 90s, 2m, 1h") from e
    if v <= 0:
        raise QtError("QT-USAGE-001", f"duration {text!r} must be positive")
    return v


def _dec(text: str, what: str) -> Decimal:
    try:
        d = Decimal(text)
    except InvalidOperation as e:
        raise QtError("QT-USAGE-001", f"{what}: not a decimal: {text!r}") from e
    if not d.is_finite() or d <= 0:
        raise QtError("QT-USAGE-001", f"{what}: must be a positive decimal")
    return d


def _ints(text: str, what: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in text.split(",") if x.strip())
    except ValueError as e:
        raise QtError("QT-USAGE-001", f"{what}: comma-separated integers, got {text!r}") from e


def _print(doc: Any) -> None:
    print(json.dumps(doc, indent=1, default=str))


def cmd_capture(a: argparse.Namespace) -> int:
    cfg = CaptureConfig(
        out=Path(a.out),
        markets=a.market,
        channels=a.channels.split(","),
        duration_s=duration_s(a.duration) if a.duration else None,
        rest_snapshot=not a.no_rest_snapshot,
        stall_s=a.stall_s,
        heartbeat_s=a.heartbeat_s,
        url=a.url,
    )
    meta = asyncio.run(run_capture(cfg))
    keys = ("rows", "counts", "connections", "reconnects", "rest_snapshots", "anomalies", "errors", "transport_errors")
    _print({**{k: meta[k] for k in keys}, "gaps": len(meta["gaps"]), "segments": len(meta["segments"]), "out": a.out})
    return 0 if not meta["errors"] else 1


def cmd_check(a: argparse.Namespace) -> int:
    doc = check.check_capture(a.capture, book_seed=a.book_seed, window=a.window, id_ties=not a.arrival_ties)
    _print(doc)
    return 1 if a.strict and doc["verdict"] not in ("consistent",) else 0


def cmd_export(a: argparse.Namespace) -> int:
    doc = export.export_segments(a.capture, a.out, book_seed=a.book_seed)
    if a.ingest:
        for s in doc["segments"]:
            if s["dir"]:
                ds = Path(a.ingest) / Path(s["dir"]).name
                ing = export.ingest_segment(s["dir"], ds)
                s["dataset"] = str(ds)
                s["records"] = ing["data_quality"]["records"]
    _print(doc)
    return 0


def cmd_import(a: argparse.Namespace) -> int:
    meta = import_jsonl(a.src, a.out, markets=a.market or None)
    _print({k: meta[k] for k in ("rows", "counts", "gaps", "segments", "anomalies")})
    return 0


def _harness_cfg(a: argparse.Namespace) -> harness.HarnessConfig:
    return harness.HarnessConfig(
        market=a.market,
        n_orders=a.n_orders,
        notional_min=_dec(a.notional_min, "--notional-min"),
        notional_max=_dec(a.notional_max, "--notional-max"),
        ticks_behind=_ints(a.ticks_behind, "--ticks-behind"),
        hold_mean_s=duration_s(a.hold_mean),
        hold_max_s=duration_s(a.hold_max),
        gap_mean_s=duration_s(a.gap_mean),
        gap_max_s=duration_s(a.gap_max),
        side=a.side,
        maker_or_cancel=not a.no_moc,
        max_order_notional=_dec(a.max_order_notional, "--max-order-notional") if a.max_order_notional else None,
        max_total_notional=_dec(a.max_total_notional, "--max-total-notional") if a.max_total_notional else None,
        max_net_position=_dec(a.max_net_position, "--max-net-position") if a.max_net_position else None,
        seed=a.seed,
    )


def cmd_harness(a: argparse.Namespace) -> int:
    cfg = _harness_cfg(a)
    log = Path(a.log)
    if a.ref_bid or a.ref_ask:
        if not (a.ref_bid and a.ref_ask):
            raise QtError("QT-USAGE-001", "--ref-bid and --ref-ask go together")
        if a.live:
            raise QtError("QT-USAGE-001", "a live run reads the real touch; --ref-bid / --ref-ask are dry-run only")
        info = harness.MarketInfo(cfg.market, a.base_decimals, a.counter_decimals, _dec(a.min_value, "--min-value"))
        public: harness.PublicData = harness.StaticPublic(
            info, _dec(a.ref_bid, "--ref-bid"), _dec(a.ref_ask, "--ref-ask")
        )
    else:
        public = harness.PublicRest()
    if a.live or a.confirm:
        auth = harness.authorize_live(live=a.live, confirm=a.confirm, cfg=cfg)
        if a.fast:
            raise QtError("QT-USAGE-001", "--fast is dry-run only")
        h = harness.live_harness(cfg, auth, public, log, stop_file=log.parent / "STOP")
        summary = h.run()
    else:
        clock = harness.FakeClock() if a.fast else harness.SystemClock()
        h, venue = harness.dry_run_harness(cfg, public, log, clock)
        summary = h.run()
        summary = {**summary, "dry_run_requests": len(venue.requests)}
    _print({"mode": h.mode, "log": str(log), "summary": summary})
    return 0


def cmd_match(a: argparse.Namespace) -> int:
    doc = matcher.match(a.capture, a.log, out=a.out, book_seed=a.book_seed)
    _print({k: v for k, v in doc.items() if k not in ("orders", "fills")})
    return 0


def cmd_pipeline(a: argparse.Namespace) -> int:
    models = pipeline.split_models(a.models) if a.models else []
    doc = pipeline.run_pipeline(
        list(a.capture),
        a.log,
        a.out,
        account=a.account,
        models=models,
        book_seed=a.book_seed,
        exact=not a.no_exact,
        require_consistent=a.require_consistent,
    )
    _print({k: v for k, v in doc.items() if k != "commands"})
    for line in pipeline.summary_lines(doc):
        print(line, file=sys.stderr)
    if a.strict and (doc["exact_agrees"] is False or not doc["consistent_captures"]):
        print(
            "livefill pipeline: --strict: the exact arm disagrees with live or a capture is inconsistent",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_synth(a: argparse.Namespace) -> int:
    r = synth.build(
        a.out,
        seed=a.seed,
        steps=a.steps,
        n_own=a.n_own,
        drop=_ints(a.drop, "--drop") if a.drop else (),
        reconnect_at=_ints(a.reconnect_at, "--reconnect-at") if a.reconnect_at else (),
    )
    _print({"capture": str(r.capture), "log": str(r.log), "messages": r.messages, "dropped": r.dropped})
    return 0


def cmd_stand_in(a: argparse.Namespace) -> int:
    res = standin.write_stand_in_log(a.capture, a.out, n=a.n, seed=a.seed, market=a.market, sample=a.sample)
    _print({"log": a.out, **res})
    return 0


BOOK_SEED_HELP = "book snapshot each segment is replayed from: auto (REST book if any), rest or ws"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="livefill", description="Own-account live-fill kit for queuetruth (Bitstamp).")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="record public live_orders / live_trades / detail_order_book to Parquet")
    c.add_argument("--market", action="append", required=True, help="market symbol, e.g. usdtusd (repeatable)")
    c.add_argument("--out", required=True, help="new capture directory")
    c.add_argument("--duration", help="stop after this long (e.g. 2m); default: until SIGINT / SIGTERM")
    c.add_argument("--channels", default=",".join(("live_orders", "live_trades", "detail_order_book")))
    c.add_argument("--no-rest-snapshot", action="store_true", help="do not fetch REST order_book?group=2 seeds")
    c.add_argument("--stall-s", type=float, default=60.0, help="reconnect after this many silent seconds")
    c.add_argument("--heartbeat-s", type=float, default=15.0)
    c.add_argument("--url", default="wss://ws.bitstamp.net")
    c.set_defaults(fn=cmd_capture)

    c = sub.add_parser("check", help="gaps and replay-vs-venue-snapshot consistency of a capture")
    c.add_argument("capture")
    c.add_argument("--book-seed", choices=check.SEED_CHOICES, default="auto", help=BOOK_SEED_HELP)
    c.add_argument("--window", type=int, default=check.DEFAULT_WINDOW)
    c.add_argument(
        "--arrival-ties",
        action="store_true",
        help="rank same-millisecond orders by arrival instead of order id (the ingest's rule; to measure it)",
    )
    c.add_argument("--strict", action="store_true", help="exit 1 unless the verdict is consistent")
    c.set_defaults(fn=cmd_check)

    c = sub.add_parser("export", help="per-segment `queuetruth ingest --format bitstamp` inputs")
    c.add_argument("capture")
    c.add_argument("--out", required=True, help="new or empty directory")
    c.add_argument("--book-seed", choices=check.SEED_CHOICES, default="auto", help=BOOK_SEED_HELP)
    c.add_argument("--ingest", metavar="DIR", help="also ingest each segment into DIR/seg-NNNN")
    c.set_defaults(fn=cmd_export)

    c = sub.add_parser("import-jsonl", help="JSONL capture directory (ingest layout) -> Parquet capture")
    c.add_argument("src")
    c.add_argument("--out", required=True)
    c.add_argument("--market", action="append")
    c.set_defaults(fn=cmd_import)

    c = sub.add_parser("harness", help="randomized passive orders; DRY RUN unless --live and --confirm are given")
    c.add_argument("--market", required=True)
    c.add_argument("--log", required=True, help="new harness log (JSON lines)")
    c.add_argument("--n-orders", type=int, default=20)
    c.add_argument("--notional-min", default="12")
    c.add_argument("--notional-max", default="25")
    c.add_argument("--ticks-behind", default="0,0,0,1,2")
    c.add_argument("--hold-mean", default="30s")
    c.add_argument("--hold-max", default="3m")
    c.add_argument("--gap-mean", default="5s")
    c.add_argument("--gap-max", default="30s")
    c.add_argument("--side", choices=("random", "buy", "sell"), default="random")
    c.add_argument("--no-moc", action="store_true", help="plain limit orders instead of maker-or-cancel (dry run only)")
    c.add_argument("--max-order-notional")
    c.add_argument("--max-total-notional")
    c.add_argument("--max-net-position", help="bound on |net filled base amount| (buys minus sells), base units")
    c.add_argument("--seed", type=int, default=0, help="random seed of the plan")
    c.add_argument("--fast", action="store_true", help="dry run on a simulated clock (no waiting)")
    c.add_argument("--ref-bid", help="offline dry run: fixed best bid instead of the public REST book")
    c.add_argument("--ref-ask")
    c.add_argument("--base-decimals", type=int, default=5)
    c.add_argument("--counter-decimals", type=int, default=5)
    c.add_argument("--min-value", default="10")
    c.add_argument("--live", action="store_true", help="PLACE REAL ORDERS (needs --confirm and API keys in env)")
    c.add_argument("--confirm", help="exactly place-real-orders-on-<market>")
    c.set_defaults(fn=cmd_harness)

    c = sub.add_parser("match", help="find harness orders in the public feed; exact queue positions and fills")
    c.add_argument("capture")
    c.add_argument("--log", action="append", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--book-seed", choices=check.SEED_CHOICES, default="auto", help=BOOK_SEED_HELP)
    c.set_defaults(fn=cmd_match)

    c = sub.add_parser("pipeline", help="check, export, match, canonical live log, ingest, score and pool")
    c.add_argument("capture", nargs="+", help="one or more capture directories")
    c.add_argument("--log", action="append", required=True)
    c.add_argument("--out", required=True, help="new or empty directory")
    c.add_argument("--account", default="main")
    c.add_argument(
        "--models",
        default=",".join(pipeline.DEFAULT_MODELS),
        help='zoo model ids, comma-separated as `queuetruth zoo --models` takes them (e.g. "hbt_power3[n=2],'
        'nt_prob_touch[p=0.3]"), or all',
    )
    c.add_argument("--no-exact", action="store_true")
    c.add_argument("--book-seed", choices=check.SEED_CHOICES, default="auto", help=BOOK_SEED_HELP)
    c.add_argument("--require-consistent", action="store_true", help="fail when a capture check is inconsistent")
    c.add_argument(
        "--strict", action="store_true", help="exit 1 when the exact arm disagrees with live or a check is inconsistent"
    )
    c.set_defaults(fn=cmd_pipeline)

    c = sub.add_parser("stand-in", help="harness log that treats other traders' orders of a capture as own (rehearsal)")
    c.add_argument("capture")
    c.add_argument("--out", required=True, help="new harness log")
    c.add_argument("--n", type=int, default=50)
    c.add_argument("--seed", type=int, default=0, help="random seed of the sample")
    c.add_argument("--market")
    c.add_argument("--sample", choices=standin.SAMPLES, default="filled_first")
    c.set_defaults(fn=cmd_stand_in)

    c = sub.add_parser("synth", help="synthetic capture with own orders and its harness log")
    c.add_argument("--out", required=True)
    c.add_argument("--seed", type=int, default=7)
    c.add_argument("--steps", type=int, default=1500)
    c.add_argument("--n-own", type=int, default=24)
    c.add_argument("--drop", help="live_orders message numbers to drop (comma-separated)")
    c.add_argument("--reconnect-at", help="steps before which the connection is reopened")
    c.set_defaults(fn=cmd_synth)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        return int(a.fn(a))
    except QtError as e:
        print(json.dumps(e.to_document()), file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
