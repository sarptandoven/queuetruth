"""Capture(s) + harness log(s) -> checked captures, MBO datasets, canonical live logs, scorecards and pooled results.

One pipeline call takes one or more captures (`cap-K/` below, K = 0, 1, ... in the order given) and the harness
logs of the run. Each capture gets the orders whose submit was sent while it was recording (between its
`start_ns` and its end) on one of its markets; an order that falls in no capture is counted in
`orders_outside_captures`, and one that falls in two captures of the same market is refused (`QT-USAGE-001`).

Steps per capture (each writes under `out/cap-K/`):

1. `check.json`: `check.check_capture` (gaps, and the replay compared with the venue's own snapshots);
2. `segments/seg-NNNN/`: `export.export_segments` (one `queuetruth ingest --format bitstamp` input per segment);
3. `match/`: `matcher.match` (own orders found in the feed, exact queue positions, fills, fees, consistency checks);
4. `live/seg-NNNN/`: `canonical.write_logs` (the canonical live log, the directory `queuetruth live import`
   writes; validated with `queuetruth.live.check`);
5. `datasets/seg-NNNN/`: `queuetruth ingest` of each segment that holds a scorable own order;
6. `score/seg-NNNN/<arm>/`: `queuetruth score` with the exact arm (`exact`) and each zoo model (`models`).

Pooled result (`pooled`, the headline). Per arm, the scored orders of every segment of every capture: the summed
2x2 table, the exact McNemar test on the pooled discordant counts, and the fill-rate bias (sim minus live fill rate)
with a 95% bootstrap interval that resamples whole segments (clusters `K:segment`) and its cluster-robust p value
(`queuetruth.stats.clustered_mcnemar`, B = 2000, seed 0). Segments are the natural clusters: a gap ends one and
orders inside one share its market conditions.

`pipeline.json` (`livefill.pipeline`) gathers the step results and a `run_id`: the first 16 hex digits of the
sha256 of the canonical JSON (RFC 8785, `queuetruth.schemas.runid.canonical_json`) of
`{engine_version, spec}`, as in section 7.3 of the design, where `engine_version` names the queuetruth and livefill
versions and `spec` holds the sha256 of every capture part, of each capture's market list (it fixes the instrument
ids) and of every harness log, plus the options. The equivalent `queuetruth` commands are listed in `commands`.

The exact arm removes each own order from the tape and re-enters it as its own shadow (self-exclusion, matched by
venue order id), so with a consistent capture it reproduces every live fill: `exact_agrees` is true when its 2x2
table has no discordant order. The zoo arms are the models being refereed.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from livefill import __version__, canonical, check, export, matcher
from livefill.store import capture_parts, read_meta
from queuetruth.errors import QtError

DEFAULT_MODELS = ("front_of_queue", "hbt_risk_adverse", "trade_through_only")
BOOTSTRAP_B = 2000
BOOTSTRAP_SEED = 0


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run_id(captures: list[Path], logs: list[Path], options: dict[str, Any]) -> str:
    """First 16 hex digits of sha256(canonical_json({engine_version, spec})) (design section 7.3)."""
    import queuetruth
    from queuetruth.schemas.runid import canonical_json

    spec = {
        "captures": [
            {
                "parts": [[p.name, _sha(p)] for p in capture_parts(c)],
                "markets_sha256": hashlib.sha256(canonical_json(read_meta(c)["markets"]).encode()).hexdigest(),
            }
            for c in captures
        ],
        "logs": [_sha(p) for p in logs],
        "options": options,
    }
    engine = f"queuetruth {queuetruth.__version__}; livefill {__version__}"
    return hashlib.sha256(canonical_json({"engine_version": engine, "spec": spec}).encode()).hexdigest()[:16]


def split_models(text: str) -> list[str]:
    """Zoo model ids separated by commas outside brackets (`;` also separates), as `queuetruth zoo --models` takes
    them; `all` expands to the zoo's default set."""
    out: list[str] = []
    depth, cur = 0, ""
    for ch in text:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch in ",;" and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    models: list[str] = []
    for m in (x.strip() for x in out):
        if not m:
            continue
        for x in zoo_all_models() if m == "all" else [m]:
            if x not in models:
                models.append(x)
    return models


def zoo_all_models() -> list[str]:
    """The model ids of `queuetruth zoo --models all` (from `queuetruth zoo --list-models --format json`)."""
    from queuetruth import cli as qcli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = qcli.main(["zoo", "--list-models", "--format", "json"])
    try:
        doc = json.loads(buf.getvalue())
    except json.JSONDecodeError as e:
        raise QtError("QT-ENGINE-001", f"queuetruth zoo --list-models gave no JSON (exit {rc})") from e
    return [str(m["id"]) for m in doc["all"]]


def _score(live_dir: Path, ds: Path, out: Path, arm: str) -> dict[str, Any]:
    from queuetruth import api

    sim: dict[str, Any] = {"kind": "exact"} if arm == "exact" else {"kind": "zoo", "model": arm}
    spec = {"live": {"path": str(live_dir)}, "sim": sim, "dataset": {"path": str(ds)}, "out": str(out)}
    s = api.score(spec)
    d = s.model_dump(mode="json", by_alias=True)
    return {
        "arm": arm,
        "out": str(out),
        "run_id": d["run_id"],
        "n_orders": d["n_orders"],
        "n_matched": d["n_matched"],
        "two_by_two": d["two_by_two"],
        "fill_rate_bias": d["bias"]["fill_rate_bias"],
        "bias_ci": [d["bias"]["ci_lo"], d["bias"]["ci_hi"]],
        "direction": d["bias"]["direction"],
        "twin_match_rate": d.get("twin_match_rate"),
        "headline": d["verdict"].get("headline"),
        "warnings": d["warnings"],
    }


def _capture_range(meta: dict[str, Any]) -> tuple[int, int]:
    """(start_ns, end_ns) of a capture: its start to its close (or its last received frame when it never closed)."""
    last = max((s.get("last_recv_ns") or 0 for s in meta.get("segments", [])), default=0)
    end = max(meta.get("end_ns") or 0, last)
    return int(meta["start_ns"]), int(end)


def _assign(captures: list[Path], logs: list[Path]) -> tuple[list[set[tuple[int, int]]], int]:
    """Per capture, the keys of the orders it gets; and the number of orders outside every capture."""
    metas = [read_meta(c) for c in captures]
    ranges = [_capture_range(m) for m in metas]
    keys: list[set[tuple[int, int]]] = [set() for _ in captures]
    outside = 0
    for o in matcher.harness_orders(logs):
        hits = [
            k
            for k, (m, (lo, hi)) in enumerate(zip(metas, ranges, strict=True))
            if o.market in m["markets"] and lo <= o.submit_sent_ns <= hi
        ]
        if len(hits) > 1:
            raise QtError(
                "QT-USAGE-001",
                f"order {o.client_order_id} was sent while captures {hits} were all recording {o.market}; "
                "give each market's orders one capture",
            )
        if not hits:
            outside += 1
            continue
        keys[hits[0]].add(o.key)
    return keys, outside


def _pool(arms: list[str], scored: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Pooled 2x2, exact McNemar and segment-clustered bias per arm, from each scored segment's score_orders."""
    from queuetruth.stats import clustered_mcnemar

    out: dict[str, Any] = {}
    for arm in arms:
        live: list[bool] = []
        sim: list[bool] = []
        clusters: list[str] = []
        for cluster, seg_doc in scored:
            sc = next((s for s in seg_doc["scores"] if s["arm"] == arm), None)
            if sc is None:
                continue
            t = pq.read_table(Path(sc["out"]) / "score_orders.parquet", columns=["live_filled", "sim_filled"])
            live += t.column("live_filled").to_pylist()
            sim += t.column("sim_filled").to_pylist()
            clusters += [cluster] * t.num_rows
        lv, sv = np.array(live, dtype=bool), np.array(sim, dtype=bool)
        two = {
            "both": int(np.sum(lv & sv)),
            "neither": int(np.sum(~lv & ~sv)),
            "live_only": int(np.sum(lv & ~sv)),
            "sim_only": int(np.sum(~lv & sv)),
        }
        doc: dict[str, Any] = {"n_orders": len(live), "n_segments": len(set(clusters)), "two_by_two": two}
        if live:
            r = clustered_mcnemar(lv, sv, np.array(clusters), b=BOOTSTRAP_B, seed=BOOTSTRAP_SEED)
            doc.update(
                live_fill_rate=float(lv.mean()),
                sim_fill_rate=float(sv.mean()),
                fill_rate_bias=float(r.estimate),
                bias_ci=[_num(r.ci_lo), _num(r.ci_hi)],
                mcnemar_p_exact=_num(r.mcnemar.p_exact),
                p_clustered=_num(r.p_primary),
                p_clustered_method=r.primary_method,
                n_clusters=r.n_clusters,
            )
        if arm == "exact":
            doc["exact_agrees"] = two["live_only"] == 0 and two["sim_only"] == 0
        out[arm] = doc
    return out


def _num(x: float | None) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


def _one_capture(
    k: int,
    capture: Path,
    logs: list[Path],
    keys: set[tuple[int, int]],
    out: Path,
    account: str,
    arms: list[str],
    book_seed: str,
    require_consistent: bool,
    commands: list[str],
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    from queuetruth import live as qlive

    doc: dict[str, Any] = {"capture": str(capture), "out": str(out)}
    chk = check.check_capture(capture, book_seed=book_seed)
    (out / "check.json").write_text(json.dumps(chk, indent=1) + "\n")
    doc["check"] = {k2: chk[k2] for k2 in ("verdict", "compared", "exact", "explained", "unexplained")}
    doc["check"]["unknown_deletes"] = chk["unknown_deletes"]
    doc["check"]["gaps"] = len(chk["gaps"])
    if require_consistent and chk["verdict"] == "inconsistent":
        raise QtError(
            "QT-DATA-001",
            f"capture check is inconsistent ({chk['unexplained']} unexplained snapshot(s), {chk['unknown_deletes']} "
            f"unknown delete(s)); see {out / 'check.json'}",
        )
    exp = export.export_segments(capture, out / "segments", book_seed=book_seed)
    doc["export"] = {
        "segments": [{k2: s[k2] for k2 in ("segment", "dir", "unseeded", "skipped")} for s in exp["segments"]]
    }
    m = matcher.match(capture, logs, out / "match", book_seed=book_seed, select=lambda o: o.key in keys)
    doc["match"] = {k2: v for k2, v in m.items() if k2 not in ("orders", "fills")}
    logs_doc = canonical.write_logs(m, out / "live", account)
    doc["live"] = logs_doc
    doc["segments"] = {}
    scored: list[tuple[str, dict[str, Any]]] = []
    for seg, info in logs_doc["segments"].items():
        live_dir = Path(info["path"])
        val = qlive.check(str(live_dir))
        val_d = val if isinstance(val, dict) else val.model_dump(mode="json", by_alias=True)
        seg_doc: dict[str, Any] = {
            "live": str(live_dir),
            "validation": {"errors": val_d.get("errors", []), "warnings": val_d.get("warnings", [])},
            "scores": [],
        }
        doc["segments"][seg] = seg_doc
        seg_dir = export.segment_dir(out / "segments", int(seg))
        accepted = [r for r in m["orders"] if r["segment"] == int(seg) and r["entry_ahead_orders"] is not None]
        if not accepted or not seg_dir.exists():
            seg_doc["skipped"] = "no scorable own order in this segment"
            continue
        ds = out / "datasets" / f"seg-{int(seg):04d}"
        ing = export.ingest_segment(seg_dir, ds)
        seg_doc["dataset"] = str(ds)
        seg_doc["data_quality"] = {
            k2: ing["data_quality"][k2] for k2 in ("records", "verdict", "crossed_books_after_last", "feed_gaps")
        }
        commands.append(
            f"queuetruth ingest --format bitstamp --venue generic --source-venue bitstamp --in {seg_dir} --out {ds}"
        )
        for arm in arms:
            so = out / "score" / f"seg-{int(seg):04d}" / arm.replace("[", "_").replace("]", "").replace("=", "")
            seg_doc["scores"].append(_score(live_dir, ds, so, arm))
            flag = "--sim exact" if arm == "exact" else f"--sim-model '{arm}'"
            commands.append(f"queuetruth score --live {live_dir} --data {ds} {flag} --out {so}")
        seg_doc["ingest_suppressed"] = sum(1 for r in accepted if r.get("ingest_suppressed"))
        ex = next((s for s in seg_doc["scores"] if s["arm"] == "exact"), None)
        if ex is not None:
            t = ex["two_by_two"]
            seg_doc["exact_agrees"] = t["live_only"] == 0 and t["sim_only"] == 0
        scored.append((f"{k}:{seg}", seg_doc))
    return doc, scored


def run_pipeline(
    captures: str | Path | list[str | Path],
    logs: list[str | Path],
    out: str | Path,
    account: str = "main",
    models: tuple[str, ...] | list[str] = DEFAULT_MODELS,
    book_seed: str = "auto",
    exact: bool = True,
    require_consistent: bool = False,
) -> dict[str, Any]:
    """Runs every step for every capture and pools the results; returns (and writes) the `livefill.pipeline`
    document. `out` must not exist or be empty (`QT-CONFIG-001`)."""
    caps = [Path(c) for c in (captures if isinstance(captures, list) else [captures])]
    logs_p = [Path(x) for x in logs]
    if not caps:
        raise QtError("QT-USAGE-001", "at least one capture is required")
    if not logs_p:
        raise QtError("QT-USAGE-001", "at least one harness log is required")
    if len({c.resolve() for c in caps}) != len(caps):
        raise QtError("QT-USAGE-001", "a capture is given twice")
    if book_seed not in check.SEED_CHOICES:
        raise QtError("QT-USAGE-001", f"book seed: expected one of {list(check.SEED_CHOICES)}, got {book_seed!r}")
    out = export.require_empty_dir(out)
    arms = (["exact"] if exact else []) + list(models)
    options = {"account": account, "models": list(models), "book_seed": book_seed, "exact": exact}
    keys, outside = _assign(caps, logs_p)
    doc: dict[str, Any] = {
        "schema": "livefill.pipeline",
        "schema_version": 1,
        "run_id": run_id(caps, logs_p, options),
        "captures": [str(c) for c in caps],
        "logs": [str(p) for p in logs_p],
        "options": options,
        "orders_outside_captures": outside,
        "commands": [],
        "per_capture": [],
    }
    scored: list[tuple[str, dict[str, Any]]] = []
    for k, cap in enumerate(caps):
        cdir = out / f"cap-{k}"
        cdir.mkdir()
        cdoc, sc = _one_capture(
            k, cap, logs_p, keys[k], cdir, account, arms, book_seed, require_consistent, doc["commands"]
        )
        doc["per_capture"].append(cdoc)
        scored += sc
    doc["pooled"] = _pool(arms, scored)
    ex = doc["pooled"].get("exact")
    doc["exact_agrees"] = None if ex is None or not ex["n_orders"] else ex["exact_agrees"]
    doc["consistent_captures"] = all(c["check"]["verdict"] != "inconsistent" for c in doc["per_capture"])
    (out / "pipeline.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    return doc


def summary_lines(doc: dict[str, Any]) -> list[str]:
    """A short human summary of a `livefill.pipeline` document, one line per arm (the CLI prints it to stderr)."""
    caps = doc["per_capture"]
    lines = [
        f"livefill pipeline {doc['run_id']}: {len(caps)} capture(s), checks "
        + ", ".join(c["check"]["verdict"] for c in caps)
        + f"; {sum(c['match']['n_in_feed'] for c in caps)} own order(s) found in the feed"
    ]
    for arm, p in doc["pooled"].items():
        t = p["two_by_two"]
        line = (
            f"  {arm}: {p['n_orders']} orders in {p['n_segments']} segment(s); both {t['both']}, neither "
            f"{t['neither']}, live only {t['live_only']}, sim only {t['sim_only']}"
        )
        if p.get("fill_rate_bias") is not None:
            lo, hi = p["bias_ci"]
            ci = "" if lo is None or hi is None else f" (95% CI {100 * lo:+.1f} to {100 * hi:+.1f})"
            line += (
                f"; fill-rate bias {100 * p['fill_rate_bias']:+.1f} pp{ci}, McNemar exact p {p['mcnemar_p_exact']:.3g}"
            )
        lines.append(line)
    return lines
