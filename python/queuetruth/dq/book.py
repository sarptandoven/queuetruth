"""Book-level checks: the `qt.data_quality` counters and verdict, crossed and locked books at event end, a stale
book while trades print (quiet intervals and prints through the standing book) and trade parity with the source
prints. The book comes from the exact replay (`api.replay` with `bbo = change` and, when the dataset has source
prints, `parity_trades`); the quiet-interval part reads the records directly."""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.dq.data import F_SNAPSHOT, SIDE_A, SIDE_B, A, C, F, M, Part, R, T
from queuetruth.dq.feed import _fmt_ns
from queuetruth.dq.framework import Check, Evidence, level, percentiles, ratio
from queuetruth.dq.model import DqCheck

DQ_SCALARS = (
    "records",
    "maybe_bad_book_records",
    "bad_ts_recv_records",
    "snapshot_records",
    "pre_snapshot_rows_skipped",
    "unknown_order_refs",
    "duplicate_adds",
    "unexpected_modifies",
    "sequence_gaps",
    "sequence_regressions",
    "out_of_order_ts_event",
    "crossed_books_after_last",
    "hidden_prints",
    "off_price_executions",
)


class DqCountersCheck(Check):
    id = "dq_counters"
    title = "qt.data_quality counters and verdict"
    mbo_only = False

    def finish(self) -> DqCheck:
        rp = self.ctx.replay.summary.get("data_quality") if self.ctx.replay else None
        doc, origin = (rp, "replay") if rp else (self.ds.ingest_dq, "ingest")
        if not doc:
            return self.result(
                "warn",
                "no data_quality.json in the dataset and no replay counters",
                notes=["datasets written by `queuetruth ingest` carry data_quality.json; re-ingest to get it"],
            )
        verdict = str(doc.get("verdict", "unknown"))
        status = {"clean": "pass", "warnings": "warn", "unusable": "fail"}.get(verdict, "warn")
        metrics: dict[str, Any] = {"origin": origin, "verdict": verdict}
        for k in DQ_SCALARS:
            metrics[k] = doc.get(k)
        for k, v in sorted((doc.get("source_specific") or {}).items()):
            metrics[f"source_specific.{k}"] = v
        metrics["feed_gaps"] = len(doc.get("feed_gaps") or [])
        ev_r = self.evidence("reasons", ["reason"])
        ev_r.extend((r,) for r in doc.get("reasons") or [])
        ev_g = self.evidence("feed_gaps", ["instrument_id", "ts_start", "ts_end", "reason"])
        ev_g.extend(
            (g.get("instrument_id"), g.get("ts_start"), g.get("ts_end"), g.get("reason"))
            for g in doc.get("feed_gaps") or []
        )
        reasons = doc.get("reasons") or []
        summary = f"{origin} verdict {verdict}" + (f": {'; '.join(reasons[:3])}" if reasons else "")
        notes = [
            "SPEC 5.1.5: unusable when maybe_bad_book_records > 0 or crossed books or unknown references exceed 1e-3; "
            "warnings for any other non-zero anomaly counter; hidden prints, off-price executions, zero-share "
            "crosses, cross prints and DBN instrument id remaps are informational"
        ]
        if origin == "replay" and self.ds.ingest_dq:
            iv = self.ds.ingest_dq.get("verdict")
            notes.append(f"ingest-time verdict: {iv}; the replay verdict adds the book counters")
        return self.result(status, summary, metrics, [ev_r, ev_g], notes)


def _replay_needed(check: Check) -> DqCheck | None:
    if check.skip_reason:
        return check.skipped()
    if check.ctx.replay is None:
        why = check.ctx.replay_error or "replay disabled (--no-replay)"
        return check.skipped(why)
    return None


def _bbo(check: Check) -> dict[str, np.ndarray] | None:
    p = check.ctx.replay.out / "bbo.parquet" if check.ctx.replay else None
    if p is None or not p.is_file():
        return None
    t = pq.read_table(p)
    out = {}
    for c in t.column_names:
        col = t.column(c)
        if pa.types.is_timestamp(col.type):
            col = col.cast(pa.int64())
        if col.null_count:
            out[c] = np.asarray(col.fill_null(0).to_numpy())
            out[c + "_valid"] = np.asarray(col.is_valid().to_numpy(zero_copy_only=False))
        else:
            out[c] = col.to_numpy()
            out[c + "_valid"] = np.ones(len(col), dtype=bool)
    return out


class CrossedBookCheck(Check):
    id = "crossed_book"
    title = "Crossed and locked books after LAST"

    def finish(self) -> DqCheck:
        early = _replay_needed(self)
        if early:
            return early
        s = self.ctx.replay.summary
        inv = s.get("invariants") or {}
        events = int((s.get("stats") or {}).get("events") or 0)
        crossed_or_locked = int(inv.get("crossed_books_after_last") or 0)
        crossed = int(inv.get("negative_spreads") or 0)
        locked = max(crossed_or_locked - crossed, 0)
        ev = self.evidence(
            "book_states", ["state", "ts_event", "instrument_id", "sequence", "bid_px", "bid_sz", "ask_px", "ask_sz"]
        )
        b = _bbo(self)
        if b is not None and len(b.get("ts_event", [])):
            both = b["bid_px_valid"] & b["ask_px_valid"]
            bad = np.nonzero(both & (b["bid_px"] >= b["ask_px"]))[0]
            ev.extend(
                (
                    (
                        "crossed" if b["bid_px"][i] > b["ask_px"][i] else "locked",
                        b["ts_event"][i],
                        b["instrument_id"][i],
                        b["sequence"][i],
                        b["bid_px"][i],
                        b["bid_sz"][i],
                        b["ask_px"][i],
                        b["ask_sz"][i],
                    )
                    for i in bad[: ev.room]
                ),
                total=int(bad.size),
            )
        status = level(crossed_or_locked, events)
        summary = f"{crossed} crossed and {locked} locked event end(s) of {events} event(s)"
        if crossed_or_locked:
            summary += f" (share {ratio(crossed_or_locked, events):.2e})"
        notes = [
            "checked at the end of every event (the record carrying F_LAST), where the book is consistent; evidence "
            "rows are BBO changes into a crossed or locked state",
            "fails above 1e-3 of events (SPEC 5.1.5 unusable threshold)",
        ]
        metrics = {
            "events": events,
            "crossed_or_locked": crossed_or_locked,
            "crossed": crossed,
            "locked": locked,
            "share": ratio(crossed_or_locked, events),
        }
        return self.result(status, summary, metrics, [ev], notes)


BOOK_ACTIONS = (A, C, M, F, R)


class StaleBookCheck(Check):
    id = "stale_book"
    title = "Stale book while trades print"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.stale_ns = ctx.spec.stale_ns
        self.prints = 0
        self.stale_prints = 0
        self.max_quiet: tuple[int, str, int] = (0, "", 0)
        self.ev = self.evidence(
            "quiet_intervals", ["date", "instrument_id", "symbol", "quiet_from", "quiet_to", "quiet_ns", "prints"]
        )

    def partition(self, p: Part) -> None:
        if len(p) == 0:
            return
        act, fl, ts = p["action"], p["flags"], p["ts_event"]
        ns = (fl & F_SNAPSHOT) == 0
        win = self.ds.session_window(p.ref)
        lo, hi = (win[1], win[2]) if win else (int(ts.min()), int(ts.max()) + 1)
        upd_ts = np.maximum.accumulate(ts[ns & np.isin(act, BOOK_ACTIONS)]) if ns.any() else ts[:0]
        prints = np.sort(ts[ns & (act == T) & (ts >= lo) & (ts < hi)])
        self.prints += int(prints.size)
        if prints.size == 0:
            return
        k = np.searchsorted(upd_ts, prints, side="right") - 1
        base = np.where(k >= 0, upd_ts[np.maximum(k, 0)], max(lo, int(ts[0])))
        base = np.maximum(base, lo)
        quiet = prints - base
        stale = quiet > self.stale_ns
        if stale.any():
            self.stale_prints += int(stale.sum())
            for grp in np.unique(k[stale]):
                sel = stale & (k == grp)
                q_from = int(base[sel][0])
                nxt = int(upd_ts[grp + 1]) if grp + 1 < upd_ts.size else int(prints[sel][-1])
                q_to = min(nxt, hi)
                self.ev.add(
                    (p.ref.date, p.ref.instrument_id, p.ref.symbol, q_from, q_to, q_to - q_from, int(sel.sum()))
                )
                if q_to - q_from > self.max_quiet[0]:
                    self.max_quiet = (q_to - q_from, p.ref.date, p.ref.instrument_id)

    def _through(self) -> tuple[int, int, list[int], Evidence]:
        """Prints beyond the standing book: a buy above the best ask (a sell below the best bid) both at the end of
        the print's own event and after every event of the print's timestamp. Requiring both keeps sweeps, feeds
        that send the removal of a filled order as a later message of the same match (crypto L3 captures) and
        same-timestamp refills from counting.
        Returns (prints checked, prints through, catch-up times in ns of the through prints that the book caught up
        with, evidence); the catch-up time is how long the book took to stop showing a better opposite price."""
        ev = self.evidence(
            "prints_through_book",
            ["ts_event", "instrument_id", "sequence", "aggressor", "price", "size", "bid_px", "ask_px", "catch_up_ns"],
        )
        out = self.ctx.replay.out if self.ctx.replay else None
        tp = out / "trades.parquet" if out else None
        b = _bbo(self)
        if tp is None or not tp.is_file() or b is None:
            return 0, 0, [], ev
        t = pq.read_table(tp)
        tr = {
            "ts": t.column("ts_event").cast("int64").to_numpy(),
            "iid": t.column("instrument_id").to_numpy(),
            "seq": t.column("sequence").to_numpy().astype(np.uint64),
            "px": t.column("price").to_numpy(),
            "sz": t.column("size").to_numpy(),
            "agg": t.column("aggressor_side").to_numpy(),
            "kind": np.asarray(t.column("kind").cast("string").to_pylist(), dtype=object),
        }
        n_checked = through = 0
        catch_up: list[int] = []
        for iid in np.unique(tr["iid"]):
            ti = np.nonzero((tr["iid"] == iid) & np.isin(tr["agg"], (SIDE_B, SIDE_A)) & (tr["kind"] != "cross"))[0]
            bi = np.nonzero(b["instrument_id"] == iid)[0]
            if ti.size == 0 or bi.size == 0:
                continue
            bseq = b["sequence"][bi].astype(np.uint64)
            bo = bi[np.lexsort((bseq, b["ts_event"][bi]))]
            bts, bsq = b["ts_event"][bo], b["sequence"][bo].astype(np.uint64)
            ask_ok, bid_ok = b["ask_px_valid"][bo], b["bid_px_valid"][bo]
            asks, bids = b["ask_px"][bo], b["bid_px"][bo]
            tts, tsq = tr["ts"][ti], tr["seq"][ti]
            # state at the end of the print's own event: last row with (ts, sequence) <= the print's
            lo = np.searchsorted(bts, tts, side="left")
            hi = np.searchsorted(bts, tts, side="right")
            pos = lo.copy()
            for j in np.nonzero(hi > lo)[0]:
                pos[j] = lo[j] + int(np.searchsorted(bsq[lo[j] : hi[j]], tsq[j], side="right"))
            pos -= 1
            # state after every event of the print's timestamp: last row with ts <= the print's
            pos_ts = hi - 1
            ok = pos >= 0
            n_checked += int(ok.sum())
            px = tr["px"][ti]
            buy = tr["agg"][ti] == SIDE_B

            bad = ok & (pos_ts >= 0)
            for q in (np.maximum(pos, 0), np.maximum(pos_ts, 0)):
                bad &= (buy & ask_ok[q] & (px > asks[q])) | (~buy & bid_ok[q] & (px < bids[q]))
            row = bo[np.maximum(pos, 0)]
            idx = np.nonzero(bad)[0]
            through += int(idx.size)
            cu: dict[int, int | None] = {}
            for j in idx:
                k0 = int(pos[j]) + 1
                if buy[j]:
                    done = ~ask_ok[k0:] | (asks[k0:] >= px[j])
                else:
                    done = ~bid_ok[k0:] | (bids[k0:] <= px[j])
                hit = int(np.argmax(done)) if done.size and done.any() else -1
                cu[int(j)] = int(bts[k0 + hit] - tr["ts"][ti[j]]) if hit >= 0 else None
                if cu[int(j)] is not None:
                    catch_up.append(cu[int(j)])  # type: ignore[arg-type]
            ev.extend(
                (
                    (
                        tr["ts"][ti[j]],
                        int(iid),
                        tr["seq"][ti[j]],
                        chr(int(tr["agg"][ti[j]])),
                        px[j],
                        tr["sz"][ti[j]],
                        b["bid_px"][row[j]] if b["bid_px_valid"][row[j]] else None,
                        b["ask_px"][row[j]] if b["ask_px_valid"][row[j]] else None,
                        cu.get(int(j)),
                    )
                    for j in idx[: ev.room]
                ),
                total=int(idx.size),
            )
        return n_checked, through, catch_up, ev

    def finish(self) -> DqCheck:
        if self.skip_reason:
            return self.skipped()
        notes = [
            f"a quiet interval is a stretch of regular hours with no add, cancel, modify, fill or clear while trades "
            f"print; reported when it exceeds {_fmt_ns(self.stale_ns)} (--stale-ns). Hidden-order executions print "
            "without changing the book, so short quiet intervals with prints are normal on Nasdaq"
        ]
        checked = through = 0
        catch_up: list[int] = []
        evs = [self.ev]
        if self.ctx.replay is not None:
            checked, through, catch_up, ev_t = self._through()
            evs.append(ev_t)
            notes.append(
                "a print through the book is a buy above the best ask (a sell below the best bid) standing both at the "
                "end of the print's event and after every event of its timestamp: the reconstructed book missed an "
                "update, lags the prints, or the print is wrong; catch_up_ns is how long the book took to agree"
            )
        else:
            notes.append("replay not run: prints through the book not checked")
        st_through = level(through, checked)
        st_quiet = "warn" if self.ev.total else "pass"
        status = st_through if st_through != "pass" else st_quiet
        summary = (
            f"{self.ev.total} quiet interval(s) over {_fmt_ns(self.stale_ns)} with {self.stale_prints} print(s) "
            f"of {self.prints}"
        )
        if self.ctx.replay is not None:
            summary += f"; {through} of {checked} print(s) through the standing book"
            if catch_up:
                summary += f" (book caught up after a median {_fmt_ns(percentiles(np.asarray(catch_up), (50,))[0])})"
        metrics = {
            "prints_regular_hours": self.prints,
            "stale_ns": self.stale_ns,
            "quiet_intervals": self.ev.total,
            "stale_prints": self.stale_prints,
            "longest_quiet_ns": self.max_quiet[0] or None,
            "longest_quiet_partition": f"{self.max_quiet[1]} {self.max_quiet[2]}" if self.max_quiet[0] else None,
            "prints_checked_against_book": checked,
            "prints_through_book": through,
            "through_catch_up_p50_ns": percentiles(np.asarray(catch_up), (50,))[0] if catch_up else None,
            "through_catch_up_max_ns": max(catch_up) if catch_up else None,
            "through_never_caught_up": through - len(catch_up),
        }
        return self.result(status, summary, metrics, evs, notes)


class TradeParityCheck(Check):
    id = "trade_parity"
    title = "Trade parity with source prints"

    def finish(self) -> DqCheck:
        early = _replay_needed(self)
        if early:
            return early
        if not self.ds.has_trades_source:
            return self.skipped(
                f"the dataset has no trades_source (source format {self.ds.source_format!r} carries no separate "
                "print stream)"
            )
        par = self.ctx.replay.summary.get("parity")
        if not par:
            return self.skipped("the replay produced no parity block")
        compared = int(par.get("trades_compared") or 0)
        mism = int(par.get("trades_mismatches") or 0)
        missing = int(par.get("trades_missing_derived") or 0)
        extra = int(par.get("trades_extra_derived") or 0)
        bad = mism + missing + extra
        status = level(bad, compared)
        first = par.get("first_mismatches") or []
        cols: list[str] = []
        for m in first:
            for k in m:
                if k not in cols:
                    cols.append(k)
        ev = self.evidence("first_mismatches", cols or ["mismatch"])
        ev.extend(
            ([m.get(c) if not isinstance(m.get(c), (dict, list)) else str(m.get(c)) for c in cols] for m in first),
            total=bad,
        )
        summary = (
            f"{compared} derived trade(s) compared with the source prints: {mism} mismatch(es), {missing} missing "
            f"from the replay, {extra} extra"
        )
        notes = [
            "SPEC 5.1.7: replay-derived trades (T records) against trades_source, matched in order per partition on "
            "(ts_event, sequence, price, size, aggressor_side)"
        ]
        metrics = {
            "trades_compared": compared,
            "trades_mismatches": mism,
            "trades_missing_derived": missing,
            "trades_extra_derived": extra,
            "pass": bool(par.get("pass")),
        }
        return self.result(status, summary, metrics, [ev], notes)


__all__ = ["CrossedBookCheck", "DqCountersCheck", "StaleBookCheck", "TradeParityCheck"]
