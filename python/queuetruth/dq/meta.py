"""Metadata checks: symbol mapping across days (`instruments.parquet`) and data vintage / file integrity
(`manifest.json`: partition file hashes, `dataset_id`, recorded inputs, DBN `source_vintage`)."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

from queuetruth.dq.data import date_str
from queuetruth.dq.framework import Check
from queuetruth.dq.model import DqCheck

#: Formats whose instrument ids are per-day feed ids (ITCH stock locate codes): changes across days are expected.
PER_DAY_ID_FORMATS = frozenset({"itch", "etcsv"})
UNIT_FIELDS = ("price_exp", "tick_size", "lot_size", "size_exp", "currency", "contract_multiplier")


class SymbologyCheck(Check):
    id = "symbology"
    title = "Symbol mapping across days"
    mbo_only = False

    def finish(self) -> DqCheck:
        ds = self.ds
        ev = self.evidence("mappings", ["issue", "symbol", "date", "instrument_id", "detail"])
        counts: dict[str, int] = defaultdict(int)
        fail_n = warn_n = 0
        for d, iid in ds.duplicate_instrument_rows:
            counts["duplicate_instrument_rows"] += 1
            ev.add(("duplicate_instrument_row", None, date_str(d), iid, "two instrument rows for one (date, id)"))
            fail_n += 1
        for ref in ds.parts:
            if ref.instrument is None:
                counts["partitions_without_instrument_row"] += 1
                ev.add(("partition_without_instrument_row", None, ref.date, ref.instrument_id, "no instrument row"))
                fail_n += 1
        part_keys = {ref.key for ref in ds.parts}
        counts["instrument_rows_without_partition"] = sum(1 for k in ds.inst_by_key if k not in part_keys)
        by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in sorted(ds.inst_by_key.values(), key=lambda r: (r["session_date"], r["instrument_id"])):
            by_sym[str(r.get("symbol"))].append(r)
            by_id[int(r["instrument_id"])].append(r)
        per_day_ids = ds.source_format in PER_DAY_ID_FORMATS
        for sym, rs in sorted(by_sym.items()):
            ids = sorted({int(r["instrument_id"]) for r in rs})
            dates = {r["session_date"] for r in rs}
            if any(sum(1 for r in rs if r["session_date"] == d) > 1 for d in dates):
                counts["symbol_on_two_ids_same_day"] += 1
                ev.add(("symbol_on_two_ids_same_day", sym, None, None, f"ids {ids}"))
                fail_n += 1
            if len(ids) > 1:
                counts["symbol_id_changes"] += 1
                path = " -> ".join(f"{date_str(r['session_date'])}:{r['instrument_id']}" for r in rs)
                ev.add(("instrument_id_changed", sym, None, None, path))
                if not per_day_ids:
                    warn_n += 1
            for f in UNIT_FIELDS:
                vals = [r.get(f) for r in rs]
                if len({str(v) for v in vals}) > 1:
                    counts["unit_changes"] += 1
                    path = " -> ".join(f"{date_str(r['session_date'])}:{r.get(f)}" for r in rs)
                    ev.add((f"{f}_changed", sym, None, None, path))
                    warn_n += 1
        for iid, rs in sorted(by_id.items()):
            syms = sorted({str(r.get("symbol")) for r in rs})
            if len(syms) > 1:
                counts["id_symbol_changes"] += 1
                path = " -> ".join(f"{date_str(r['session_date'])}:{r.get('symbol')}" for r in rs)
                ev.add(("symbol_changed_for_id", None, None, iid, path))
                if not per_day_ids:
                    warn_n += 1
            exps = {r.get("price_exp") for r in rs}
            if len(exps) > 1:
                counts["price_exp_conflicts"] += 1
                ev.add(("price_exp_differs_for_id", None, None, iid, f"price_exp {sorted(exps)}"))
                fail_n += 1
        remapped = ds.ingest_counter("dbn_instrument_id_remapped")
        notes = []
        if per_day_ids:
            notes.append(
                f"source format {ds.source_format!r}: instrument ids are per-day feed ids (ITCH stock locate codes), "
                "so id changes across days are informational; join days by symbol"
            )
        if remapped:
            notes.append(f"ingest remapped {remapped} DBN instrument id(s) from the definitions (informational)")
        if not ds.instruments:
            notes.append("no instruments.parquet in the dataset")
            warn_n += 1
        status = "fail" if fail_n else ("warn" if warn_n else "pass")
        n_sym = len(by_sym)
        found = [f"{v} {k.replace('_', ' ')}" for k, v in sorted(counts.items()) if v]
        if per_day_ids and (counts["symbol_id_changes"] or counts["id_symbol_changes"]):
            found.append("id changes are per-day feed ids (informational)")
        summary = (", ".join(found) if found else "one instrument id and one set of units per symbol") + (
            f" ({n_sym} symbol(s), {len(ds.session_dates)} date(s))"
        )
        metrics: dict[str, Any] = {
            "symbols": n_sym,
            "dates": len(ds.session_dates),
            "dbn_instrument_id_remapped": remapped,
        }
        for k in (
            "duplicate_instrument_rows",
            "partitions_without_instrument_row",
            "instrument_rows_without_partition",
            "symbol_on_two_ids_same_day",
            "symbol_id_changes",
            "id_symbol_changes",
            "unit_changes",
            "price_exp_conflicts",
        ):
            metrics[k] = counts[k]
        return self.result(status, summary, metrics, [ev], notes)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def dataset_id(shas: list[str], source_format: str) -> str:
    """SPEC 3.10: sha256 of the sorted partition file hashes, newline-joined, then a newline and the format."""
    return hashlib.sha256(("\n".join(sorted(shas)) + "\n" + source_format).encode()).hexdigest()


class VintageCheck(Check):
    id = "vintage"
    title = "Data vintage and file integrity"
    mbo_only = False

    def finish(self) -> DqCheck:
        ds = self.ds
        m = ds.manifest
        ev_files = self.evidence("partition_files", ["issue", "path", "manifest_sha256", "actual_sha256"])
        ev_in = self.evidence("inputs", ["path", "bytes", "sha256", "on_disk"])
        ev_v = self.evidence("source_vintage", ["sha256", "dbn_version", "dataset", "dbn_schema", "start_ns", "end_ns"])
        fail_n = warn_n = 0
        shas: list[str] = []
        listed: set[Path] = set()
        n_files = 0
        for p in m.get("partitions") or []:
            for f in p.get("files") or []:
                n_files += 1
                want = str(f.get("sha256") or "")
                shas.append(want)
                path = ds.root / f["path"]
                listed.add(path.resolve())
                if not path.is_file():
                    ev_files.add(("missing", f["path"], want, None))
                    fail_n += 1
                    continue
                got = sha256_file(path)
                if got != want:
                    ev_files.add(("hash_mismatch", f["path"], want, got))
                    fail_n += 1
        for sub in ("mbo", "l2"):
            d = ds.root / sub
            if d.is_dir():
                for f in sorted(d.rglob("*.parquet")):
                    if f.resolve() not in listed:
                        ev_files.add(("not_in_manifest", str(f.relative_to(ds.root)), None, None))
                        warn_n += 1
        recomputed = dataset_id(shas, str(m.get("source_format") or ""))
        id_ok = recomputed == m.get("dataset_id")
        if not id_ok:
            fail_n += 1
        inputs = m.get("inputs") or []
        verify = self.ctx.spec.verify_sources
        on_disk_counts: dict[str, int] = defaultdict(int)
        for i in inputs:
            path = Path(str(i.get("path") or ""))
            if not path.is_absolute():
                # relative to where ingest ran: try the working directory, the dataset and its parent
                path = next((c for c in (path, ds.root / path, ds.root.parent / path) if c.is_file()), path)
            state = "not_found"
            if path.is_file():
                size = path.stat().st_size
                if i.get("bytes") is not None and size != int(i["bytes"]):
                    state = "size_mismatch"
                    warn_n += 1
                elif verify and i.get("sha256"):
                    state = "sha256_ok" if sha256_file(path) == i["sha256"] else "sha256_mismatch"
                    if state == "sha256_mismatch":
                        warn_n += 1
                else:
                    state = "size_ok"
            on_disk_counts[state] += 1
            ev_in.add((i.get("path"), i.get("bytes"), i.get("sha256"), state))
        notes = []
        vint = m.get("source_vintage") or []
        for v in vint:
            ev_v.add(
                (
                    v.get("sha256"),
                    v.get("dbn_version"),
                    v.get("dataset"),
                    v.get("dbn_schema"),
                    v.get("start_ns"),
                    v.get("end_ns"),
                )
            )
        versions = sorted({str(v.get("dbn_version")) for v in vint if v.get("dbn_version") is not None})
        dsets = sorted({str(v.get("dataset")) for v in vint if v.get("dataset")})
        if len(versions) > 1 or len(dsets) > 1:
            warn_n += 1
            notes.append(f"mixed vintages in one dataset: DBN versions {versions}, datasets {dsets}")
        fmt = str(m.get("source_format") or "")
        status_v = str(m.get("validation_status") or "")
        if not inputs and fmt not in ("synthetic",):
            warn_n += 1
            notes.append("the manifest records no input files: the source of this dataset cannot be traced")
        if fmt == "dbn" and not vint:
            warn_n += 1
            notes.append("DBN dataset without source_vintage (DBN version, dataset, schema): re-ingest to record it")
        if status_v in ("synthetic_only", "fixture_only", "assumed"):
            notes.append(
                f"validation_status {status_v!r}: the reader for this source has not been validated on real data"
            )
        if not verify and inputs:
            notes.append("input files compared by size only; --verify-sources re-hashes them")
        status = "fail" if fail_n else ("warn" if warn_n else "pass")
        bad_files = ev_files.total
        files_txt = "match" if not bad_files else f"checked, {bad_files} problem(s)"
        summary = (
            f"{n_files} partition file(s) {files_txt}; "
            f"dataset_id {'recomputes' if id_ok else 'does NOT recompute'}; {len(inputs)} input(s) recorded"
        )
        if on_disk_counts:
            summary += " (" + ", ".join(f"{k}: {v}" for k, v in sorted(on_disk_counts.items())) + ")"
        metrics: dict[str, Any] = {
            "dataset_id": m.get("dataset_id"),
            "dataset_id_recomputed": recomputed,
            "dataset_id_ok": id_ok,
            "partition_files": n_files,
            "partition_file_problems": bad_files,
            "inputs": len(inputs),
            "created_by": m.get("created_by"),
            "validation_status": status_v or None,
            "source_format": fmt,
            "dbn_versions": ",".join(versions) or None,
            "vendor_datasets": ",".join(dsets) or None,
        }
        for k, v in sorted(on_disk_counts.items()):
            metrics[f"inputs_{k}"] = v
        return self.result(status, summary, metrics, [ev_files, ev_in, ev_v], notes)


__all__ = ["SymbologyCheck", "VintageCheck", "dataset_id", "sha256_file"]
