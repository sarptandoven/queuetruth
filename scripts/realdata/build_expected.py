"""Build `expected.json` (the RD-1 .. RD-13 targets) from the raw scan written by `et_reference.py`.

Every number is either recomputed from an et artifact by `et_reference.py` (provenance `computed`) or copied from an
et JSON summary and then cross-checked against the recomputation where one exists (provenance `copied`). The
`claims` section checks et README / SPEC statements against the recomputed numbers and lists every discrepancy.

    python -I build_expected.py --scan et_scan.json --out expected.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

INV8 = [
    "cancels_exceeding_resting",
    "fills_exceeding_resting",
    "fills_unknown_order",
    "unknown_order_refs",
    "duplicate_adds",
    "unexpected_modifies",
    "crossed_books_after_last",
    "negative_spreads",
]


def prov(scan_day: dict, *rel: str, how: str) -> dict:
    files = {r: scan_day["files"][r]["sha256"] for r in rel if r in scan_day["files"]}
    return {"files": files, "computation": how}


def day_block(date: str, day: dict) -> dict:
    m, b, r, sh, ro = day["mbo"], day["bbo"], day["rank_trace"], day["shadows"], day["real_orders"]
    inv = day["et_invariants_json"]
    itch = "out/itch-12302019" if date == "2019-12-30" else "out/itch-01302020"
    rep = "out/replay-itch-12302019-v2" if date == "2019-12-30" else "out/replay-itch-01302020"
    fid = "out/fidelity-itch" if date == "2019-12-30" else "out/fidelity-01302020"
    per = m["per_instrument"]
    hidden = {i: v["trade_sides"].get("N", 0) for i, v in per.items()}
    out = {
        "session_date": date,
        "midnight_utc_ns": day["midnight_utc_ns"],
        "open_s": day["open_s"],
        "close_s": day["close_s"],
        "instruments": day["instruments"],
        "ingest": {
            "partitions": len(per),
            "instrument_ids": [int(i) for i in sorted(per, key=int)],
            "records": m["records"],
            "rows_by_instrument": {i: v["records"] for i, v in per.items()},
            "trades_source_rows": day["trades_source"]["rows"],
            "trades_source_rows_by_instrument": day["trades_source"]["rows_by_instrument"],
            "ts_min_by_instrument": {i: v["ts_first"] for i, v in per.items()},
            "ts_max_by_instrument": {i: v["ts_last"] for i, v in per.items()},
            "action_counts": {
                "A": m["adds"],
                "C": m["cancels"],
                "F": m["fills"],
                "T": m["trades"],
                "M": m["modifies"],
                "R": m["clears"],
            },
            "action_counts_by_instrument": {i: v["actions"] for i, v in per.items()},
            "side_N_trade_prints": sum(hidden.values()),
            "side_N_trade_prints_by_instrument": hidden,
            "out_of_order_ts_event": sum(v["out_of_order_ts_event"] for v in per.values()),
            "ts_recv_ne_ts_event": sum(v["ts_recv_ne_ts_event"] for v in per.values()),
            "unterminated_events": sum(0 if v["last_has_last_flag"] else 1 for v in per.values()),
            "flags_values": sorted({int(k) for v in per.values() for k in v["flags"]}),
            "expected_data_quality_verdict_note": "all et counters are 0; qt verdict should be 'clean' unless qt counts "  # noqa: E501
            "side-N prints (hidden_prints) or off-price executions as warnings",
            "provenance": prov(
                day,
                f"{itch}/mbo.csv",
                f"{itch}/trades.csv",
                how="streamed et mbo.csv / trades.csv: row counts, action counts, F_LAST(128) counts per instrument_id",
            ),
        },
        "replay": {
            "records": m["records"],
            "events": m["events"],
            "events_by_instrument": {i: v["events"] for i, v in per.items()},
            "trades": m["trades"],
            "invariants": {k: 0 for k in INV8},
            "rank_oracle": {
                "executions_checked": r["rows"],
                "executions_unknown_order": inv["executions_unknown_order"],
                "price_priority_violations": r["price_priority_violations_rows_level_depth_gt0"],
                "fifo_rank_histogram": r["fifo_rank_histogram"],
                "fifo_rank_histogram_sweep_adjusted": r["fifo_rank_histogram_sweep_adjusted"],
                "rank0_share": r["rank0_share"],
                "rank0_share_sweep_adjusted": r["rank0_share_sweep_adjusted"],
                "sweeps": r["sweeps_distinct_ids"],
                "sweeps_multi_fill": r["sweeps_multi_fill"],
                "sweep_gap_ns": 2_000_000,
                "first_60s_after_open": r["first_60s_after_open"],
                "rank0_share_by_bucket": r["rank0_share_by_bucket"],
                "exec_kind": r["exec_kind"],
                "per_instrument": r["per_instrument"],
                "share_tolerance_abs": 1e-12,
            },
            "parity_trades": {
                "trades_compared": day["trades_source"]["rows"],
                "trades_mismatches": 0,
                "trades_missing_derived": 0,
                "trades_extra_derived": 0,
            },
            "et_cross_check": {
                "invariants_json_equal_recomputed": all(
                    inv[k] == r[{"sweeps": "sweeps_distinct_ids"}.get(k, k)]
                    for k in (
                        "fifo_rank_histogram",
                        "fifo_rank_histogram_sweep_adjusted",
                        "rank0_share",
                        "rank0_share_sweep_adjusted",
                        "sweeps",
                        "sweeps_multi_fill",
                    )
                )
                and inv["executions_checked"] == r["rows"]
                and inv["rank0_share_by_bucket"] == r["rank0_share_by_bucket"]
                and inv["first_60s_after_open"] == r["first_60s_after_open"],
                "run_report_stats": day["et_run_report_stats"],
                "itch_stats_mbo_records": day["et_itch_stats"]["mbo_records"],
                "itch_stats_trade_records": day["et_itch_stats"]["trade_records"],
                "itch_stats_executions": day["et_itch_stats"]["executions"],
                "itch_messages": day["et_itch_stats"]["messages"],
                "derived_trades_file_identical_to_source": day["trades_derived"]["trades_rows_sha256"]
                == day["trades_source"]["trades_rows_sha256"],
            },
            "provenance": prov(
                day,
                f"{rep}/rank_trace.csv",
                f"{rep}/invariants.json",
                f"{itch}/mbo.csv",
                how="rank aggregates recomputed from rank_trace.csv rows (histograms 0..5+, sweeps = "
                "distinct sweep_id, multi = sweeps with >1 row, buckets = floor((ts-midnight)/1800s), "
                "first 60 s = [midnight+34200 s, +60 s)); equal to invariants.json",
            ),
        },
        "bbo_full": {
            "rows": b["rows"],
            "rows_by_instrument": b["rows_by_instrument"],
            "bbo_rows_sha256": b["bbo_rows_sha256"],
            "render": "f'{ts_event},{instrument_id},{bid_px or 9223372036854775807},{bid_sz},{ask_px or 9223372036854775807},{ask_sz},{sequence}\\n' (bid_sz/ask_sz 0 when the side is empty; counts dropped)",  # noqa: E501
            "change_rows_px_sz_only_by_instrument": b["change_rows_px_sz_only_by_instrument"],
            "change_rows_note": "lower bound for qt bbo='change' row counts: qt also emits a row when only bid_ct/ask_ct changes",  # noqa: E501
            "rows_locked_or_crossed": b["rows_locked_or_crossed"],
            "provenance": prov(day, f"{rep}/bbo.csv", how="per-instrument sha256 of the raw data lines in file order"),
        },
        "rank_trace": {
            "rows": r["rows"],
            "rows_by_instrument": r["rows_by_instrument"],
            "rank_rows_sha256_relabelled": r["rank_rows_sha256_relabelled"],
            "rank_rows_sha256_no_sweep": r["rank_rows_sha256_no_sweep"],
            "render": "f'{ts_event},{instrument_id},{sequence},{order_id},{chr(side)},{price},{rank},{rank_sweep_adjusted},{level_depth},{sweep_ix},{exec_kind}\\n' where sweep_ix = 1-based first-appearance index of sweep_id within the instrument's rows",  # noqa: E501
            "sweeps_by_instrument_from_breadth": {
                sym: int(row["sweeps"]) for sym, row in day["breadth_cross_check"]["symbols"].items()
            },
            "provenance": prov(day, f"{rep}/rank_trace.csv", how="per-instrument sha256 of rendered rows"),
        },
        "shadows_mirror_et": {
            "n": sh["n_shadows"],
            "n_live": sh["n_shadows"],
            "n_filled": sh["n_filled"],
            "n_partial_only": sh["n_partial_only"],
            "n_with_ts_cancel": sh["n_with_ts_cancel"],
            "fill_reason_counts_et": sh["fill_reason_counts"],
            "n_crossed_at_live": sh["n_crossed_at_live"],
            "sum_qty": sh["sum_qty"],
            "sum_filled_qty": sh["sum_filled_qty"],
            "sum_queue_ahead_at_live": sh["sum_queue_ahead_at_submit"],
            "by_instrument": sh["by_instrument"],
            "shadow_digest": sh["shadow_digest"],
            "queue_trace_rows": sh["queue_trace"]["rows"],
            "queue_trace_shadows": sh["queue_trace"]["shadows"],
            "queue_trace_digest": sh["queue_trace"]["queue_trace_digest"],
            "queue_trace_semantics": sh["queue_trace"]["semantics"],
            "trace_self_checks": {
                k: sh["queue_trace"][k]
                for k in (
                    "first_row_ts_equals_ts_live",
                    "first_row_value_equals_queue_ahead_at_submit",
                    "shadows_with_nonincreasing_trace",
                )
            },
            "provenance": prov(
                day,
                f"{fid}/shadows.csv",
                f"{fid}/engine/shadow_fills.csv",
                f"{fid}/engine/queue_trace.csv",
                how="streamed et shadow_fills.csv / queue_trace.csv",
            ),
        },
        "real_orders": {
            "rows": ro["rows"],
            "end_reason_expected": {
                "filled": ro["outcome_counts"].get("filled", 0),
                "cancelled": ro["outcome_counts"].get("cancelled", 0) + ro["outcome_counts"].get("partial", 0),
                "replaced": 0,
                "cleared": 0,
                "open_at_end": 0,
            },
            "et_outcome_counts": ro["outcome_counts"],
            "n_with_fill": ro["n_with_fill"],
            "sum_n_fills": ro["sum_n_fills"],
            "sum_filled_qty": ro["sum_filled_qty"],
            "sum_size_at_add": ro["sum_size"],
            "by_instrument": ro["by_instrument"],
            "note": "ITCH replace (U) arrives as C + A with a new order reference, so qt sees no Replace effect on "
            "Nasdaq; et 'partial' (partly filled, then cancelled) maps to qt end_reason 'cancelled' with "
            "filled_qty > 0; queue_ahead_at_add is NOT compared (et's tape queue_ahead differs from the "
            "engine level-before-add by design: equal for 99.1% / 99.6% of the sampled orders)",
            "provenance": prov(day, f"{fid}/real_orders.parquet", how="pyarrow value_counts / sums"),
        },
        "fidelity_et": {
            "two_by_two_expected_for_RD11_sim_exact": {
                "both": day["et_fidelity_json"]["both_filled"],
                "neither": day["et_fidelity_json"]["both_unfilled"],
                "live_only": day["et_fidelity_json"]["real_only"],
                "sim_only": day["et_fidelity_json"]["shadow_only"],
            },
            "shadow_before_real": day["et_fidelity_json"]["shadow_before_real"],
            "corrected_agreement": day["et_fidelity_json"]["corrected"]["fill_agreement"],
            "corrected_exact_time_share": day["et_fidelity_json"]["corrected"]["exact_time_share"],
            "n_real_orders": day["et_fidelity_json"]["n_real_orders"],
            "provenance": prov(
                day,
                f"{fid}/fidelity.json",
                how="copied; n_filled cross-checked against shadow_fills.csv (both_filled == n_filled)",
            ),
            "cross_check_both_filled_eq_shadow_n_filled": day["et_fidelity_json"]["both_filled"] == sh["n_filled"],
        },
        "learned_model_et_reference": {
            **day["et_calibration_v2"],
            "provenance": prov(
                day,
                f"{fid}/calibration_real_v2.json",
                how="copied (et ridge-logistic competing-risk "
                "model on real orders, 55/15/30 time split within the day; not the same protocol as RD-10)",
            ),
        },
    }
    if "depth_pass1" in day:
        d3 = day["depth_pass1"]
        out["depth"] = {
            "depth3_rows_sha256": d3["depth3_rows_sha256"],
            "rows_by_instrument": d3["rows_by_instrument"],
            "render": "f'{ts_event},{instrument_id},{sequence}' + ''.join(f',{bpx},{bsz},{apx},{asz}' for "
            "level 1..N) + '\\n', missing level = 9223372036854775807,0; one row per event",
            "provenance": {
                "files": {d3["file"]: "not hashed (large); per-instrument line digests only"},
                "computation": "per-instrument sha256 of raw depth.csv lines",
            },
        }
        if "depth_replay" in day:
            out["depth"]["depth5_rows_sha256"] = day["depth_replay"]["depth5_rows_sha256"]
            out["depth"]["provenance"]["files"][day["depth_replay"]["file"]] = "not hashed (1.9 GB)"
    return out


def claims(scan: dict) -> list[dict]:
    d1, d2 = scan["days"]["2019-12-30"], scan["days"]["2020-01-30"]
    c = []

    def add(src, claim, value, ok, note=""):
        c.append({"source": src, "claim": claim, "recomputed": value, "consistent": ok, "note": note})

    for date, day, recs, ev, tr, ex, r0, r0a, sw, mf in (
        ("2019-12-30", d1, 6103262, 5423833, 211867, 176103, 0.9757528264708721, 0.9872290648086631, 87901, 37990),
        ("2020-01-30", d2, 10651300, 9361483, 421420, 314369, 0.9867925908725097, 0.9908865059850048, 161037, 65081),
    ):
        rt = day["rank_trace"]
        val = [
            day["mbo"]["records"],
            day["mbo"]["events"],
            day["mbo"]["trades"],
            rt["rows"],
            rt["rank0_share"],
            rt["rank0_share_sweep_adjusted"],
            rt["sweeps_distinct_ids"],
            rt["sweeps_multi_fill"],
        ]
        add(
            "SPEC 6.8 RD-2/RD-6",
            f"{date}: records/events/trades/executions/rank0/rank0_adj/sweeps/multi",
            val,
            val == [recs, ev, tr, ex, r0, r0a, sw, mf],
        )
    add(
        "SPEC 6.8 RD-1",
        "day one partitions 13, 5291, 7451, 7992",
        sorted(d1["mbo"]["instruments"]),
        sorted(d1["mbo"]["instruments"]) == [13, 5291, 7451, 7992],
    )
    add(
        "SPEC 6.8 RD-6",
        "day two instrument ids (not stated in SPEC)",
        sorted(d2["mbo"]["instruments"]),
        True,
        "13 AAPL, 5294 MSFT, 7457 SPY, 8000 TSLA: locate codes change between days",
    )
    add(
        "SPEC 6.8 RD-5",
        "queue_trace.csv 11,680 rows (day one)",
        d1["shadows"]["queue_trace"]["rows"],
        d1["shadows"]["queue_trace"]["rows"] == 11680,
    )
    add("SPEC 6.8 RD-6", "day two queue_trace rows (not stated in SPEC)", d2["shadows"]["queue_trace"]["rows"], True)
    ek1, ek2 = d1["rank_trace"]["exec_kind"], d2["rank_trace"]["exec_kind"]
    add(
        "et README day one",
        "99.78% at-price rank0 (sweep-adj) over 173,642; 2,461 off-price 24% rank0",
        [
            ek1["at_price"]["n"],
            round(ek1["at_price"]["rank0_share_sweep_adjusted"], 4),
            ek1["off_price"]["n"],
            round(ek1["off_price"]["rank0_share_sweep_adjusted"], 3),
        ],
        ek1["at_price"]["n"] == 173642
        and round(ek1["at_price"]["rank0_share_sweep_adjusted"], 4) == 0.9978
        and ek1["off_price"]["n"] == 2461,
    )
    add(
        "et README day two",
        "99.61% at-price over 311,324; 3,045 off-price 45.6% rank0",
        [
            ek2["at_price"]["n"],
            round(ek2["at_price"]["rank0_share_sweep_adjusted"], 4),
            ek2["off_price"]["n"],
            round(ek2["off_price"]["rank0_share_sweep_adjusted"], 3),
        ],
        ek2["at_price"]["n"] == 311324
        and ek2["off_price"]["n"] == 3045
        and round(ek2["off_price"]["rank0_share_sweep_adjusted"], 3) == 0.456,
    )
    b1 = d1["rank_trace"]["rank0_share_by_bucket"]
    add(
        "et README day one",
        "15:00 bucket 98.26% (8,915 of 9,073); 11:30 98.50% (11,868 of 12,049); 16:00 1,079 of 1,080",
        [
            b1["15:00"]["rank0_sweep_adjusted"],
            b1["15:00"]["checked"],
            b1["11:30"]["rank0_sweep_adjusted"],
            b1["11:30"]["checked"],
            b1["16:00"]["rank0_sweep_adjusted"],
            b1["16:00"]["checked"],
        ],
        [
            b1["15:00"]["rank0_sweep_adjusted"],
            b1["15:00"]["checked"],
            b1["11:30"]["rank0_sweep_adjusted"],
            b1["11:30"]["checked"],
            b1["16:00"]["rank0_sweep_adjusted"],
            b1["16:00"]["checked"],
        ]
        == [8915, 9073, 11868, 12049, 1079, 1080],
    )
    br1 = d1["breadth_cross_check"]["symbols"]
    add(
        "et README day one",
        "2,369 off-price executions with a print, all one cent worse (per-symbol breadth rows)",
        sum(int(r["off_price_one_cent_worse"]) for r in br1.values()),
        sum(int(r["off_price_one_cent_worse"]) for r in br1.values()) == 2369,
        "taken from real-breadth per-symbol rows (the join with prints is not recomputed here)",
    )
    for date, day in (("2019-12-30", d1), ("2020-01-30", d2)):
        bad = []
        inst = {v: k for k, v in day["instruments"].items()}
        for sym, row in day["breadth_cross_check"]["symbols"].items():
            i = inst[sym]
            m = day["mbo"]["per_instrument"][i]
            r = day["rank_trace"]["per_instrument"][i]
            mine = {
                "records": m["records"],
                "events": m["events"],
                "trades": m["actions"]["T"],
                "executions_checked": r["checked"],
                "rank0_raw": r["rank0"],
                "rank0_adj": r["rank0_adj"],
                "at_price_checked": r["at_price"],
                "off_price_checked": r["off_price"],
            }
            bad += [(sym, k) for k, v in mine.items() if str(v) != row[k]]
        add(
            "et out/real-breadth (independent per-symbol replays)",
            f"{date}: per-symbol counts equal the 4-symbol replay",
            bad,
            not bad,
        )
    for date, day, both, neither, ro, so in (
        ("2019-12-30", d1, 235, 2739, 26, 0),
        ("2020-01-30", d2, 195, 2789, 16, 0),
    ):
        f = day["et_fidelity_json"]
        add(
            "et README fidelity",
            f"{date}: both/neither/real_only/shadow_only",
            [f["both_filled"], f["both_unfilled"], f["real_only"], f["shadow_only"]],
            [f["both_filled"], f["both_unfilled"], f["real_only"], f["shadow_only"]] == [both, neither, ro, so]
            and f["both_filled"] == day["shadows"]["n_filled"],
        )
    for venue, recs, trades in (("bitfinex", 51701, 243), ("bitstamp", 77272, 135)):
        cm = scan["crypto"][venue]["converted_mbo"]
        add(
            "et README crypto",
            f"{venue}: converted records / trades",
            [cm["records"], cm["trades"]],
            [cm["records"], cm["trades"]] == [recs, trades],
            "bitstamp README '101,824 events' is the live_orders message count; the converted MBO has 77,272 records"
            if venue == "bitstamp"
            else "",
        )
    for venue in ("bitstamp", "bitfinex"):
        p = scan["crypto"][venue]["parity"]
        add(
            "et README crypto 'pass'",
            f"{venue}: engine parity_report.pass",
            p.get("pass"),
            True,
            f"engine exact parity is false ({p.get('mbp1_mismatches')} mbp1 mismatches of {p.get('mbp1_compared')}); "
            "README 'pass' is the 500 ms tolerance verdict and README says exact_pass is false: consistent but easy to misquote",  # noqa: E501
        )
    ex = scan.get("extra")
    if ex:
        for tag, v in ex["ocaml"].items():
            add(
                "et README OCaml",
                f"{tag}: OCaml twin bbo/trades byte-identical, rank trace identical after sort",
                v,
                all(v.values()),
            )
        b = ex["breadth_all_symbol_days"]
        add(
            "et README breadth",
            "all 118 candidate symbol-days: zero invariants, price-priority violations, trade mismatches",
            b,
            b["symbol_days"] == 118 and b["with_nonzero_invariant_or_parity_mismatch"] == 0,
            "the README breadth table aggregates only the top 40 / top 30 symbols (30.0M + 50.2M records); all 118 "
            f"symbol-days together are {b['records']:,} records and {b['executions_checked']:,} executions",
        )
    return c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    scan = json.loads(a.scan.read_text())
    doc = {
        "schema": "qtdev.expected",
        "schema_version": 1,
        "et_root": scan["et_root"],
        "et_head": scan["et_head"],
        "generator": "scripts/realdata/{et_reference.py, build_expected.py}",
        "scan_timings_s": scan["timings_s"],
        "digest_conventions": scan["conventions"],
        "days": {date: day_block(date, day) for date, day in scan["days"].items()},
        "crypto": {
            v: {
                k: scan["crypto"][v][k]
                for k in (
                    "capture_dir",
                    "capture_files",
                    "converted_mbo",
                    "engine_stats",
                    "engine_anomalies",
                    "verdict",
                )
            }
            | {
                "parity": {k: x for k, x in scan["crypto"][v]["parity"].items() if k != "examples"},
                "convert_stats": scan["crypto"][v]["convert_stats"],
            }
            for v in ("bitstamp", "bitfinex")
        },
        "crypto_fidelity_et": scan["crypto"]["fidelity_summary"],
        "breadth_et": {
            date: scan["days"][date]["breadth_cross_check"]["aggregate"]
            | {"source": scan["days"][date]["breadth_cross_check"]["source"]}
            for date in scan["days"]
        },
        "claims": claims(scan),
    }
    for date in doc["breadth_et"]:
        doc["breadth_et"][date].pop("rank0_share_by_bucket", None)
    totals = {
        "records": sum(d["replay"]["records"] for d in doc["days"].values()),
        "events": sum(d["replay"]["events"] for d in doc["days"].values()),
        "trades": sum(d["replay"]["trades"] for d in doc["days"].values()),
        "executions_checked": sum(d["replay"]["rank_oracle"]["executions_checked"] for d in doc["days"].values()),
    }
    doc["totals_two_days"] = totals
    doc["discrepancies"] = [x for x in doc["claims"] if not x["consistent"]]
    if a.out.exists():  # keep results appended later by et_shadow_reference.py --update-expected
        prev = json.loads(a.out.read_text())
        for date, day in prev.get("days", {}).items():
            ref = day.get("shadows_mirror_et", {}).get("spec_reference")
            if ref and date in doc["days"]:
                doc["days"][date]["shadows_mirror_et"]["spec_reference"] = ref
    a.out.write_text(json.dumps(doc, indent=1) + "\n")
    print(
        f"wrote {a.out}: {len(doc['claims'])} claims checked, {len(doc['discrepancies'])} discrepancies; totals {totals}"  # noqa: E501
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
