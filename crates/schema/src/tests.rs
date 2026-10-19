use std::path::{Path, PathBuf};

use qt_types::{BookSide, Instrument, MboEvent, Side, Venue};
use serde_json::Value;

use crate::codec::{self, TableRow};
use crate::io::{TableWriter, read_table, read_table_as, sha256_file};
use crate::rows::*;
use crate::{json, specs};

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> Self {
        let p = std::env::temp_dir().join(format!("qt-schema-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
        Self(p)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn read_json(path: &Path) -> Value {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

// ---- schemas ----------------------------------------------------------------------------------------------------

/// O-SCHEMA-1 (Rust side): every Rust schema dump equals the committed dump generated from the pyarrow mirror.
#[test]
fn o_schema_1_arrow_dumps_match_committed() {
    let dir = repo().join("schemas/arrow");
    let mut committed: Vec<String> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned()))
        .filter(|n| n.ends_with(".json"))
        .collect();
    committed.sort();
    let mut names: Vec<String> = crate::schema_names().iter().map(|n| format!("{n}.json")).collect();
    names.sort();
    assert_eq!(
        committed, names,
        "schemas/arrow file set differs from qt_schema::schema_names()"
    );
    for name in crate::schema_names() {
        let ours = crate::schema_json(name).unwrap_or_default();
        let theirs = std::fs::read_to_string(dir.join(format!("{name}.json"))).unwrap_or_default();
        assert_eq!(ours, theirs, "schema dump differs for {name}");
    }
    assert_eq!(crate::schema_names().len(), 29);
    assert!(crate::schema("qt.nope").is_none());
}

#[test]
fn schema_metadata_and_types() {
    let s = crate::schema("qt.mbo").unwrap_or_else(|| panic!("qt.mbo"));
    assert_eq!(s.metadata().get("qt.schema").map(String::as_str), Some("qt.mbo"));
    assert_eq!(s.metadata().get("qt.schema_version").map(String::as_str), Some("1.1"));
    assert_eq!(
        s.metadata().get("qt.engine_version").map(String::as_str),
        Some(crate::ENGINE_VERSION)
    );
    assert_eq!(s.field(0).data_type(), &crate::ts_type());
    let names: Vec<&str> = s.fields().iter().map(|f| f.name().as_str()).collect();
    assert_eq!(
        names,
        [
            "ts_event",
            "ts_recv",
            "instrument_id",
            "action",
            "side",
            "price",
            "size",
            "order_id",
            "flags",
            "sequence",
            "channel_id",
            "publisher_id"
        ]
    );
    let f = crate::features_schema(&["imb_l1", "dist_ticks"]).unwrap_or_else(|e| panic!("{e}"));
    let names: Vec<&str> = f.fields().iter().map(|f| f.name().as_str()).collect();
    assert_eq!(&names[6..10], ["side_sign", "imb_l1", "dist_ticks", "event_ns"]);
    assert!(crate::features_schema(&["cause"]).is_err());
    assert!(crate::features_schema(&["a", "a"]).is_err());
    // 1.1: the full layout appends `midnight_utc_ns` (nullable int64); the name is reserved in both layouts.
    assert_eq!(names.len(), 11);
    let full = crate::features_full_schema(&["imb_l1", "dist_ticks"]).unwrap_or_else(|e| panic!("{e}"));
    let full_names: Vec<&str> = full.fields().iter().map(|f| f.name().as_str()).collect();
    assert_eq!(full_names[..11], names[..]);
    assert_eq!(full_names[11..], ["midnight_utc_ns"]);
    assert_eq!(full.field(11).data_type(), &arrow_schema::DataType::Int64);
    assert!(full.field(11).is_nullable());
    assert!(crate::features_schema(&["midnight_utc_ns"]).is_err());
    assert!(crate::features_full_schema(&["midnight_utc_ns"]).is_err());
    assert_eq!(
        crate::features_columns(&f),
        Some((vec!["imb_l1".to_owned(), "dist_ticks".to_owned()], false))
    );
    assert_eq!(
        crate::features_columns(&full),
        Some((vec!["imb_l1".to_owned(), "dist_ticks".to_owned()], true))
    );
    let fixed_only = crate::features_schema(&[]).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(crate::features_columns(&fixed_only), Some((vec![], false)));
    let short = arrow_schema::Schema::new(fixed_only.fields()[..5].to_vec());
    assert_eq!(crate::features_columns(&short), None);
}

#[test]
fn dict_enums_match_table_values() {
    fn check<E: DictEnum>(table: &str, col: &str) {
        assert_eq!(crate::dict_values(table, col), Some(E::VALUES), "{table}.{col}");
        for (i, v) in E::VALUES.iter().enumerate() {
            let e = E::from_index(i).unwrap_or_else(|| panic!("{v}"));
            assert_eq!(e.index(), i);
            assert_eq!(e.name(), *v);
            assert_eq!(E::from_name(v).map(|x| x.index()), Some(i));
        }
        assert!(E::from_index(E::VALUES.len()).is_none());
    }
    check::<Venue>("qt.instruments", "venue");
    check::<TradeKind>("qt.trades", "kind");
    check::<ExecKind>("qt.rank_trace", "exec_kind");
    check::<RealOrderEnd>("qt.real_orders", "end_reason");
    check::<PriceMode>("qt.shadow_spec", "price_mode");
    check::<ShadowSource>("qt.shadow_spec", "source");
    check::<FirstFillReason>("qt.shadow_results", "first_fill_reason");
    check::<EndState>("qt.shadow_results", "end_state");
    check::<FillReason>("qt.shadow_fills", "reason");
    check::<TrajectoryKind>("qt.queue_trajectory", "kind");
    check::<L2Kind>("qt.l2", "kind");
    check::<ZooFillReason>("qt.zoo_fills", "reason");
    check::<Quirk>("qt.zoo_fills", "quirk");
    check::<Population>("qt.features", "population");
    check::<Cause>("qt.features", "cause");
    check::<Liquidity>("qt.shadow_fills", "liquidity");
    check::<Liquidity>("qt.zoo_fills", "liquidity");
    check::<Liquidity>("qt.zoo_predictions", "liquidity");
    check::<PlannedEndKind>("qt.shadow_results", "planned_end_kind");
    assert_eq!(
        serde_json::to_string(&EndState::CensoredSessionEnd).ok().as_deref(),
        Some("\"censored_session_end\"")
    );
    assert_eq!("hbt_312".parse::<Quirk>().ok(), Some(Quirk::Hbt312));
    assert_eq!("zzz".parse::<Quirk>().err().map(|e| e.code()), Some("QT-CONFIG-001"));
}

// ---- codecs -----------------------------------------------------------------------------------------------------

fn roundtrip<T: TableRow + PartialEq + std::fmt::Debug>(rows: &[T], dir: &Path) {
    let batch = T::to_batch(rows);
    assert_eq!(batch.num_rows(), rows.len());
    assert_eq!(
        batch.schema().fields(),
        crate::schema(T::TABLE)
            .unwrap_or_else(|| panic!("{}", T::TABLE))
            .fields()
    );
    let back = T::from_batch(&batch).unwrap_or_else(|e| panic!("{}: {e}", T::TABLE));
    assert_eq!(back, rows, "{} in-memory round trip", T::TABLE);
    // Parquet round trip, twice: identical bytes (determinism).
    let mut hashes = Vec::new();
    for k in 0..2 {
        let path = dir.join(format!("{}-{k}.parquet", T::TABLE));
        let mut w = TableWriter::create(&path, T::TABLE).unwrap_or_else(|e| panic!("{e}"));
        w.write(&batch).unwrap_or_else(|e| panic!("{e}"));
        w.write(&T::to_batch(&[])).unwrap_or_else(|e| panic!("{e}"));
        let entry = w.finish().unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(entry.rows, Some(rows.len() as u64));
        assert_eq!(entry.sha256, sha256_file(&path).unwrap_or_default());
        hashes.push(entry.sha256);
        let reader = read_table_as(&path, T::TABLE).unwrap_or_else(|e| panic!("{e}"));
        let mut got = Vec::new();
        for b in reader {
            got.extend(T::from_batch(&b.unwrap_or_else(|e| panic!("{e}"))).unwrap_or_else(|e| panic!("{e}")));
        }
        assert_eq!(got, rows, "{} parquet round trip", T::TABLE);
    }
    assert_eq!(hashes[0], hashes[1], "{} parquet bytes not deterministic", T::TABLE);
}

fn mbo(i: u64) -> MboEvent {
    MboEvent {
        ts_event: 1_577_696_400_000_000_000 + i as i64,
        ts_recv: 1_577_696_400_000_000_100 + i as i64,
        instrument_id: 7 + (i % 3) as u32,
        action: b"ACMRTFN"[(i % 7) as usize],
        side: b"BAN"[(i % 3) as usize],
        price: if i % 5 == 0 {
            qt_types::UNDEF_PRICE
        } else {
            100_000_000_000 + i as i64
        },
        size: i * 10,
        order_id: i + 1000,
        flags: if i % 4 == 0 { qt_types::flags::LAST } else { 0 },
        sequence: i,
        channel_id: (i % 2) as u16,
        publisher_id: 2,
    }
}

#[test]
fn codec_round_trips_every_row_type() {
    let tmp = TempDir::new("codec");
    let d = tmp.0.as_path();
    roundtrip(&(0..50).map(mbo).collect::<Vec<_>>(), d);
    roundtrip(
        &[
            Instrument {
                instrument_id: 13,
                session_date: 18_260,
                symbol: "AAPL".into(),
                venue: Venue::Nasdaq,
                source_venue: "nasdaq".into(),
                price_exp: -9,
                tick_size: 10_000_000,
                lot_size: 1,
                size_exp: 0,
                contract_multiplier: 1.0,
                currency: "USD".into(),
                session_tz: "America/New_York".into(),
                midnight_utc_ns: 1_577_682_000_000_000_000,
                open_s: 34_200,
                close_s: 57_600,
                source: "etcsv".into(),
            },
            Instrument {
                instrument_id: 2,
                session_date: 18_261,
                symbol: "ESH0".into(),
                venue: Venue::Cme,
                source_venue: "cme".into(),
                price_exp: -2,
                tick_size: 25,
                lot_size: 1,
                size_exp: 0,
                contract_multiplier: 50.0,
                currency: "USD".into(),
                session_tz: "UTC".into(),
                midnight_utc_ns: 0,
                open_s: 0,
                close_s: 86_400,
                source: "dbn".into(),
            },
        ],
        d,
    );
    roundtrip(
        &[
            BboRow {
                ts_event: 1,
                instrument_id: 1,
                sequence: 2,
                bid_px: Some(99),
                bid_sz: 5,
                bid_ct: 1,
                ask_px: None,
                ask_sz: 0,
                ask_ct: 0,
            },
            BboRow {
                ts_event: 2,
                instrument_id: 1,
                sequence: 3,
                bid_px: None,
                bid_sz: 0,
                bid_ct: 0,
                ask_px: Some(101),
                ask_sz: 7,
                ask_ct: 2,
            },
        ],
        d,
    );
    roundtrip(
        &[
            DepthRow {
                ts_event: 1,
                instrument_id: 1,
                sequence: 1,
                side: BookSide::Bid,
                level: 0,
                price: Some(99),
                size: 5,
                count: 1,
            },
            DepthRow {
                ts_event: 1,
                instrument_id: 1,
                sequence: 1,
                side: BookSide::Ask,
                level: 3,
                price: None,
                size: 0,
                count: 0,
            },
        ],
        d,
    );
    roundtrip(
        &[
            TradeRow {
                ts_event: 5,
                instrument_id: 1,
                sequence: 9,
                price: 100,
                size: 3,
                aggressor_side: Side::Ask,
                kind: TradeKind::Displayed,
                maker_order_id: Some(77),
            },
            TradeRow {
                ts_event: 6,
                instrument_id: 1,
                sequence: 10,
                price: 100,
                size: 500,
                aggressor_side: Side::None,
                kind: TradeKind::Hidden,
                maker_order_id: None,
            },
            TradeRow {
                ts_event: 7,
                instrument_id: 2,
                sequence: 11,
                price: 101,
                size: 1,
                aggressor_side: Side::Bid,
                kind: TradeKind::Cross,
                maker_order_id: None,
            },
        ],
        d,
    );
    roundtrip(
        &[RankTraceRow {
            ts_event: 5,
            instrument_id: 1,
            sequence: 9,
            order_id: 4,
            side: BookSide::Ask,
            price: 100,
            rank: 1,
            rank_sweep_adjusted: 0,
            level_depth: 0,
            sweep_id: 1,
            exec_kind: ExecKind::OffPrice,
        }],
        d,
    );
    roundtrip(
        &[RealOrderRow {
            instrument_id: 1,
            session_date: 18_260,
            order_id: 5,
            side: BookSide::Bid,
            price: 100,
            size_at_add: 10,
            ts_add: 3,
            sequence_add: 4,
            queue_ahead_at_add: 30,
            rank_at_add: 2,
            level_qty_at_add: 30,
            ts_first_fill: Some(9),
            ts_last_fill: None,
            filled_qty: 4,
            n_fills: 1,
            n_reduces: 0,
            ts_end: None,
            end_reason: RealOrderEnd::OpenAtEnd,
            in_snapshot: true,
        }],
        d,
    );
    roundtrip(
        &[
            ShadowSpec {
                shadow_id: "s1".into(),
                instrument_id: 1,
                side: BookSide::Bid,
                price: PriceSpec::Abs(100),
                qty: 10,
                ts_submit: 15,
                latency_ns: 0,
                ts_cancel: Some(35),
                group: Some("g".into()),
                source: ShadowSource::User,
                order_key: None,
            },
            ShadowSpec {
                shadow_id: "binance:acct:123#2".replace('#', "_"),
                instrument_id: 2,
                side: BookSide::Ask,
                price: PriceSpec::Touch { offset_ticks: -1 },
                qty: 1,
                ts_submit: 20,
                latency_ns: 5,
                ts_cancel: None,
                group: None,
                source: ShadowSource::LiveLog,
                order_key: Some("binance:acct:123".into()),
            },
        ],
        d,
    );
    roundtrip(
        &[ShadowAmend {
            shadow_id: "s1".into(),
            ts_send: 25,
            new_qty: 10,
        }],
        d,
    );
    roundtrip(
        &[ShadowResultRow {
            shadow_id: "s1".into(),
            instrument_id: 1,
            side: BookSide::Bid,
            price: Some(100),
            qty: 10,
            ts_submit: 15,
            ts_live: 15,
            ts_activated_event: Some(20),
            ts_cancel_effective: None,
            queue_ahead_at_live: Some(100),
            rank_at_live: Some(2),
            level_qty_at_live: Some(100),
            ts_first_fill: Some(40),
            ts_last_fill: Some(40),
            ts_full_fill: Some(40),
            filled_qty: 10,
            n_fills: 1,
            queue_ahead_at_first_fill: Some(40),
            first_fill_reason: FirstFillReason::QueueDepleted,
            end_state: EndState::Filled,
            latency_applied_ns: 0,
            ambiguous_race: false,
            out_of_session: true,
            dq_flags: 1,
        }],
        d,
    );
    roundtrip(
        &[ShadowFillRow {
            shadow_id: "s1".into(),
            fill_seq: 1,
            ts_event: 40,
            sequence: 9,
            fill_qty: 10,
            cum_qty: 10,
            leaves_qty: 0,
            fill_px: 100,
            reason: FillReason::QueueDepleted,
            queue_ahead_before: 40,
            traded_at_px_event: 50,
            trade_through_event: 0,
            behind_volume_executed: 10,
        }],
        d,
    );
    roundtrip(
        &[TrajectoryRow {
            shadow_id: "s1".into(),
            ts_event: 15,
            sequence: 3,
            kind: TrajectoryKind::Live,
            queue_ahead_before: 100,
            queue_ahead_after: 100,
            rank_before: 2,
            rank_after: 2,
            level_qty_before: 100,
            level_qty_after: 130,
            cancel_ahead_qty: 0,
            cancel_behind_qty: 0,
            add_behind_qty: 30,
            traded_at_px_qty: 0,
            trade_through_qty: 0,
            filled_qty: 0,
        }],
        d,
    );
    roundtrip(
        &[
            L2Row {
                ts_event: 1,
                ts_recv: 2,
                instrument_id: 1,
                event_id: 1,
                sequence: 4,
                kind: L2Kind::Clear,
                side: Side::None,
                price: None,
                prev_qty: 0,
                new_qty: 0,
                prev_ct: None,
                new_ct: None,
                trade_qty: 0,
                aggressor: Side::None,
                level: 255,
                flags: l2flags::SNAPSHOT,
            },
            L2Row {
                ts_event: 1,
                ts_recv: 2,
                instrument_id: 1,
                event_id: 1,
                sequence: 4,
                kind: L2Kind::Depth,
                side: Side::Bid,
                price: Some(99),
                prev_qty: 5,
                new_qty: 0,
                prev_ct: Some(1),
                new_ct: Some(0),
                trade_qty: 0,
                aggressor: Side::None,
                level: 255,
                flags: l2flags::OUT_OF_VIEW | l2flags::LAST_IN_BATCH,
            },
        ],
        d,
    );
    roundtrip(
        &[ZooPredictionRow {
            model_id: "hbt_power3[exch=partial;mode=faithful;n=2]".into(),
            shadow_id: "s1".into(),
            qty: 10,
            horizon_ns: 1_000_000_000,
            p_fill: 0.25,
            e_fill_qty: 2.5,
            model_ts_first_fill: None,
            truth_filled: Some(false),
            truth_ts_first_fill: None,
            truth_filled_qty: Some(0),
            censored: false,
            queue_ahead_at_live: None,
            level_qty_at_live: Some(3),
            visible_level_qty_at_live: 3,
            out_of_view_at_live: false,
            instrument_id: 1,
            session_date: 18_260,
            ts_live: 15,
            trade_vol_60s: 1000,
        }],
        d,
    );
    roundtrip(
        &[ZooFillRow {
            model_id: "front_of_queue[mode=faithful]".into(),
            shadow_id: "s1".into(),
            fill_seq: 1,
            ts_event: 30,
            fill_qty: 1.5,
            cum_qty: 1.5,
            queue_ahead_est_before: -0.5,
            reason: ZooFillReason::TouchDraw,
            quirk: Quirk::HbtNanReset,
        }],
        d,
    );
    roundtrip(
        &[ZooTrajectoryRow {
            model_id: "m".into(),
            shadow_id: "s1".into(),
            ts_event: 30,
            queue_ahead_est: 26.896551724137932,
            queue_ahead_exact: 27,
        }],
        d,
    );
    roundtrip(
        &[AllocEventRow {
            shadow_id: "s1".into(),
            instrument_id: 1,
            session_date: 18_260,
            ts_event: 30,
            front: 30.0,
            back: 70.0,
            ahead_cancel: 5.0,
            behind_cancel: 15.0,
            l2_prev: Some(100.0),
            l2_new: None,
            l2_trades: Some(0.0),
        }],
        d,
    );
    roundtrip(
        &[TouchRecordRow {
            shadow_id: "s1".into(),
            ts_event: 30,
            touch_index: 3,
            nt_front_before: 10.0,
            nt_excess: 0.0,
            truth_filled_now: false,
            truth_first_fill_here: false,
        }],
        d,
    );
}

#[test]
fn features_codec_round_trip() {
    let tmp = TempDir::new("features");
    let names = ["dist_ticks", "imb_l1"];
    let rows = vec![
        FeatureRow {
            order_ref: "s1".into(),
            population: Population::Shadows,
            instrument_id: 1,
            session_date: 18_260,
            ts_landmark: 15,
            landmark_ns: 0,
            side_sign: 1,
            values: vec![Some(0.0), None],
            event_ns: 25,
            cause: Cause::Fill,
            midnight_utc_ns: Some(1_577_854_800_000_000_000),
        },
        FeatureRow {
            order_ref: "1:77".into(),
            population: Population::RealOrders,
            instrument_id: 1,
            session_date: 18_260,
            ts_landmark: 16,
            landmark_ns: 1_000_000_000,
            side_sign: -1,
            values: vec![Some(-2.0), Some(0.25)],
            event_ns: 60_000_000_000,
            cause: Cause::Censor,
            midnight_utc_ns: None,
        },
    ];
    let batch = codec::features_to_batch(&names, &rows).unwrap_or_else(|e| panic!("{e}"));
    let (n, back) = codec::batch_to_features(&batch).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(n, names);
    assert_eq!(back, rows);
    let path = tmp.0.join("features.parquet");
    let mut w = TableWriter::create_with_schema(&path, batch.schema()).unwrap_or_else(|e| panic!("{e}"));
    w.write(&batch).unwrap_or_else(|e| panic!("{e}"));
    w.finish().unwrap_or_else(|e| panic!("{e}"));
    let r = read_table(&path).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(r.name(), "qt.features");
    let mut got = Vec::new();
    for b in r {
        got.extend(
            codec::batch_to_features(&b.unwrap_or_else(|e| panic!("{e}")))
                .unwrap_or_else(|e| panic!("{e}"))
                .1,
        );
    }
    assert_eq!(got, rows);
    // A v0.1 file (no `midnight_utc_ns`) still reads; the column is then None.
    let core = crate::features_schema(&names).unwrap_or_else(|e| panic!("{e}"));
    let v0_1 = arrow_array::RecordBatch::try_new(core.clone(), batch.columns()[..core.fields().len()].to_vec())
        .unwrap_or_else(|e| panic!("{e}"));
    let old_rows: Vec<FeatureRow> = rows
        .iter()
        .map(|r| FeatureRow {
            midnight_utc_ns: None,
            ..r.clone()
        })
        .collect();
    assert_eq!(
        codec::batch_to_features(&v0_1).ok(),
        Some((names.map(String::from).to_vec(), old_rows.clone()))
    );
    let path0 = tmp.0.join("features_v0_1.parquet");
    let mut w = TableWriter::create_with_schema(&path0, core).unwrap_or_else(|e| panic!("{e}"));
    w.write(&v0_1).unwrap_or_else(|e| panic!("{e}"));
    w.finish().unwrap_or_else(|e| panic!("{e}"));
    let mut got0 = Vec::new();
    for b in read_table(&path0).unwrap_or_else(|e| panic!("{e}")) {
        got0.extend(
            codec::batch_to_features(&b.unwrap_or_else(|e| panic!("{e}")))
                .unwrap_or_else(|e| panic!("{e}"))
                .1,
        );
    }
    assert_eq!(got0, old_rows);
    let bad = vec![FeatureRow {
        values: vec![None],
        ..rows[0].clone()
    }];
    assert!(codec::features_to_batch(&names, &bad).is_err());
}

#[test]
fn batch_schema_mismatch_is_input_error() {
    let batch = codec::bbo_to_batch(&[]);
    assert_eq!(
        codec::batch_to_trades(&batch).err().map(|e| e.code()),
        Some("QT-INPUT-002")
    );
    let tmp = TempDir::new("mismatch");
    let p = tmp.0.join("bbo.parquet");
    let mut w = TableWriter::create(&p, "qt.bbo").unwrap_or_else(|e| panic!("{e}"));
    assert!(w.write(&codec::trades_to_batch(&[])).is_err());
    w.write(&batch).unwrap_or_else(|e| panic!("{e}"));
    w.finish().unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        read_table_as(&p, "qt.trades").err().map(|e| e.code()),
        Some("QT-INPUT-002")
    );
    assert_eq!(
        read_table(&tmp.0.join("missing.parquet")).err().map(|e| e.code()),
        Some("QT-INPUT-001")
    );
}

#[test]
fn parquet_writer_settings() {
    use parquet::file::reader::{FileReader, SerializedFileReader};
    let tmp = TempDir::new("props");
    let p = tmp.0.join("mbo.parquet");
    let mut w = TableWriter::create(&p, "qt.mbo").unwrap_or_else(|e| panic!("{e}"));
    w.write(&codec::mbo_to_batch(&(0..10).map(mbo).collect::<Vec<_>>()))
        .unwrap_or_else(|e| panic!("{e}"));
    w.finish().unwrap_or_else(|e| panic!("{e}"));
    let f = std::fs::File::open(&p).unwrap_or_else(|e| panic!("{e}"));
    let r = SerializedFileReader::new(f).unwrap_or_else(|e| panic!("{e}"));
    let md = r.metadata().file_metadata();
    assert_eq!(
        md.created_by(),
        Some(format!("queuetruth {}", crate::ENGINE_VERSION).as_str())
    );
    let kv = md.key_value_metadata().cloned().unwrap_or_default();
    assert!(
        kv.iter()
            .any(|k| k.key == "qt.schema" && k.value.as_deref() == Some("qt.mbo"))
    );
    let col = r.metadata().row_group(0).column(0);
    assert!(matches!(col.compression(), parquet::basic::Compression::ZSTD(_)));
}

#[test]
fn shadow_inputs_csv_and_parquet() {
    let tmp = TempDir::new("shadows");
    let csv = tmp.0.join("shadows.csv");
    std::fs::write(
        &csv,
        "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel\n\
         a,1,B,100,2,10,5,\n\
         b,1,sell,101,3,20,5,30\n",
    )
    .unwrap_or_else(|e| panic!("{e}"));
    let s = codec::read_shadow_specs(&csv).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(s.len(), 2);
    assert_eq!(
        (s[0].side, s[0].ts_cancel, s[0].price),
        (BookSide::Bid, None, PriceSpec::Abs(100))
    );
    assert_eq!(
        (s[1].side, s[1].ts_cancel, s[1].source),
        (BookSide::Ask, Some(30), ShadowSource::User)
    );
    // Columns in any order, ts_cancel absent, extra columns tolerated (et behaviour).
    std::fs::write(
        &csv,
        "instrument_id,shadow_id,qty,price,side,latency_ns,ts_submit,strategy\n2,c,1,99,ask,0,7,mm\n",
    )
    .unwrap_or_else(|e| panic!("{e}"));
    let s = codec::read_shadow_specs(&csv).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        (s[0].shadow_id.as_str(), s[0].instrument_id, s[0].ts_submit),
        ("c", 2, 7)
    );
    // S15: duplicate ids.
    std::fs::write(
        &csv,
        "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns\nx,1,B,1,1,1,0\nx,1,B,1,1,1,0\n",
    )
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        codec::read_shadow_specs(&csv).err().map(|e| e.code()),
        Some("QT-INPUT-003")
    );
    std::fs::write(
        &csv,
        "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns\nbad id,1,B,1,1,1,0\n",
    )
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        codec::read_shadow_specs(&csv).err().map(|e| e.code()),
        Some("QT-INPUT-002")
    );
    std::fs::write(
        &csv,
        "shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns\nq,1,X,1,1,1,0\n",
    )
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        codec::read_shadow_specs(&csv).err().map(|e| e.code()),
        Some("QT-INPUT-002")
    );
    // Parquet.
    let pq = tmp.0.join("shadows.parquet");
    let specs = vec![ShadowSpec {
        shadow_id: "p1".into(),
        instrument_id: 1,
        side: BookSide::Ask,
        price: PriceSpec::Touch { offset_ticks: 1 },
        qty: 4,
        ts_submit: 1,
        latency_ns: 0,
        ts_cancel: None,
        group: None,
        source: ShadowSource::Sampled,
        order_key: None,
    }];
    let mut w = TableWriter::create(&pq, "qt.shadow_spec").unwrap_or_else(|e| panic!("{e}"));
    w.write(&codec::shadow_specs_to_batch(&specs))
        .unwrap_or_else(|e| panic!("{e}"));
    w.finish().unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(codec::read_shadow_specs(&pq).ok(), Some(specs));
    let am = tmp.0.join("amends.parquet");
    let amends = vec![ShadowAmend {
        shadow_id: "p1".into(),
        ts_send: 2,
        new_qty: 3,
    }];
    let mut w = TableWriter::create(&am, "qt.shadow_amend").unwrap_or_else(|e| panic!("{e}"));
    w.write(&codec::shadow_amends_to_batch(&amends))
        .unwrap_or_else(|e| panic!("{e}"));
    w.finish().unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(codec::read_shadow_amends(&am).ok(), Some(amends));
    assert_eq!(
        codec::read_shadow_amends(&pq).err().map(|e| e.code()),
        Some("QT-INPUT-002")
    );
}

#[test]
fn id_validation() {
    for ok in ["a", "A-b_c.d:e", &"x".repeat(128)] {
        assert!(codec::validate_id("shadow_id", ok, "t").is_ok(), "{ok}");
    }
    for bad in ["", "a b", "é", "a/b", &"x".repeat(129)] {
        assert!(codec::validate_id("shadow_id", bad, "t").is_err(), "{bad}");
    }
}

// ---- JSON mirrors -----------------------------------------------------------------------------------------------

fn rt<T: serde::Serialize + serde::de::DeserializeOwned>(name: &str, v: &Value) -> Value {
    let t: T = serde_json::from_value(v.clone()).unwrap_or_else(|e| panic!("{name}: deserialize: {e}\n{v}"));
    serde_json::to_value(&t).unwrap_or_else(|e| panic!("{name}: serialize: {e}"))
}

fn doc_roundtrip(name: &str, v: &Value) -> Option<Value> {
    Some(match name {
        "qt.error" => rt::<json::ErrorDoc>(name, v),
        "qt.dataset_manifest" => rt::<json::DatasetManifest>(name, v),
        "qt.data_quality" => rt::<json::DataQuality>(name, v),
        "qt.ingest_summary" => rt::<json::IngestSummary>(name, v),
        "qt.replay_summary" => rt::<json::ReplaySummary>(name, v),
        "qt.shadow_sample_summary" => rt::<json::ShadowSampleSummary>(name, v),
        "qt.zoo_summary" => rt::<json::ZooSummary>(name, v),
        "qt.calibration_result" => rt::<json::CalibrationResult>(name, v),
        "qt.feature_summary" => rt::<json::FeatureSummary>(name, v),
        "qt.hazard_model_card" => rt::<json::HazardModelCard>(name, v),
        "qt.live_import_summary" => rt::<json::LiveImportSummary>(name, v),
        "qt.live_manifest" => rt::<json::LiveManifest>(name, v),
        "qt.live_validation" => rt::<json::LiveValidation>(name, v),
        "qt.scorecard_summary" => rt::<json::ScorecardSummary>(name, v),
        "qt.referee_report" => rt::<json::RefereeReport>(name, v),
        "qt.why_trace" => rt::<json::WhyTrace>(name, v),
        "qt.report_summary" => rt::<json::ReportSummary>(name, v),
        "qt.export_summary" => rt::<json::ExportSummary>(name, v),
        "qt.fixtures_summary" => rt::<json::FixturesSummary>(name, v),
        "qt.doctor" => rt::<json::Doctor>(name, v),
        "qt.bench" => rt::<json::Bench>(name, v),
        "qt.job" => rt::<json::Job>(name, v),
        "qt.file_manifest" => rt::<json::FileManifest>(name, v),
        "qt.estimate" => rt::<json::Estimate>(name, v),
        "qt.problem" => rt::<json::Problem>(name, v),
        "qt.ingest_spec" => rt::<specs::IngestSpec>(name, v),
        "qt.replay_spec" => rt::<specs::ReplaySpec>(name, v),
        "qt.shadow_sample_spec" => rt::<specs::ShadowSampleSpec>(name, v),
        "qt.mirror_spec" => rt::<specs::MirrorSpec>(name, v),
        "qt.zoo_spec" => rt::<specs::ZooSpec>(name, v),
        "qt.calibration_spec" => rt::<specs::CalibrationSpec>(name, v),
        "qt.feature_spec" => rt::<specs::FeatureSpec>(name, v),
        "qt.hazard_fit_spec" => rt::<specs::HazardFitSpec>(name, v),
        "qt.live_import_spec" => rt::<specs::LiveImportSpec>(name, v),
        "qt.scorecard_spec" => rt::<specs::ScorecardSpec>(name, v),
        "qt.report_spec" => rt::<specs::ReportSpec>(name, v),
        "qt.referee_spec" => rt::<specs::RefereeSpec>(name, v),
        "qt.why_spec" => rt::<specs::WhySpec>(name, v),
        "qt.export_spec" => rt::<specs::ExportSpec>(name, v),
        "qt.live_calibration_spec" => rt::<specs::LiveCalibrationSpec>(name, v),
        "qt.live_calibration_test" => rt::<json::LiveCalibrationTest>(name, v),
        _ => return None,
    })
}

/// Service-only documents of SPEC 4.5 without a Rust mirror.
const PYTHON_ONLY: [&str; 9] = [
    "qt.api_key",
    "qt.api_key_created",
    "qt.dataset",
    "qt.health",
    "qt.meta",
    "qt.page",
    "qt.tenant",
    "qt.upload",
    "qt.usage_report",
];

/// Every committed sample (generated from the Pydantic models) round-trips through the Rust mirror unchanged
/// (names, order, types, nesting); spec samples also check that Rust defaults equal Pydantic defaults.
#[test]
fn json_mirrors_round_trip_committed_samples() {
    let dir = repo().join("schemas/samples");
    let mut entries: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .filter_map(|e| e.ok().map(|e| e.path()))
        .collect();
    entries.sort();
    let mut checked = 0;
    for p in entries {
        let name = p
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        let doc = read_json(&p);
        let sample = &doc["sample"];
        match doc_roundtrip(&name, sample) {
            Some(back) => assert_eq!(&back, sample, "{name}: sample round trip"),
            None => {
                assert!(PYTHON_ONLY.contains(&name.as_str()), "{name}: no Rust mirror");
                continue;
            }
        }
        if let Some(minimal) = doc.get("minimal") {
            let full = doc_roundtrip(&name, minimal).unwrap_or(Value::Null);
            assert_eq!(
                full, doc["defaults"],
                "{name}: Rust defaults differ from Pydantic defaults"
            );
        }
        checked += 1;
    }
    assert_eq!(checked, 26 + 15);
}

#[test]
fn json_tags_are_checked() {
    let mut v = serde_json::to_value(json::DataQuality::default()).unwrap_or(Value::Null);
    assert_eq!(v["schema"], "qt.data_quality");
    assert_eq!(v["schema_version"], 1);
    let keys: Vec<&String> = v.as_object().map(|o| o.keys().collect()).unwrap_or_default();
    assert_eq!(keys[..3], ["schema", "schema_version", "records"]);
    v["schema"] = "qt.other".into();
    assert!(serde_json::from_value::<json::DataQuality>(v.clone()).is_err());
    v["schema"] = "qt.data_quality".into();
    v["schema_version"] = 2.into();
    assert!(serde_json::from_value::<json::DataQuality>(v.clone()).is_err());
    v["schema_version"] = 1.into();
    v["extra"] = 1.into();
    assert!(serde_json::from_value::<json::DataQuality>(v).is_err());
    let e = json::ErrorDoc::from_error(&qt_types::QtError::Duplicate {
        what: "shadow_id".into(),
        id: "x".into(),
    });
    let ev = serde_json::to_value(&e).unwrap_or(Value::Null);
    assert_eq!(
        ev,
        serde_json::json!({"schema": "qt.error", "schema_version": 1, "code": "QT-INPUT-003",
                           "message": "duplicate shadow_id: x", "hint": null, "details": {}})
    );
}

#[test]
fn spec_unknown_fields_rejected_and_refs() {
    let v = serde_json::json!({"dataset": {"path": "d"}, "bogus": 1});
    assert!(serde_json::from_value::<specs::ReplaySpec>(v).is_err());
    let v = serde_json::json!({"dataset": {"path": "d", "x": 1}});
    assert!(serde_json::from_value::<specs::ReplaySpec>(v).is_err());
    let v = serde_json::json!({"dataset": {"dataset_id": "ds_1"}});
    let s: specs::ReplaySpec = serde_json::from_value(v).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        s.dataset,
        specs::DatasetRef::DatasetId(specs::DatasetIdRef {
            dataset_id: "ds_1".into()
        })
    );
    assert_eq!(s.dataset.as_path(), None);
    let v = serde_json::json!({"live": {"path": "l"}, "sim": {"kind": "exact"}});
    let s: specs::ScorecardSpec = serde_json::from_value(v).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(s.sim, specs::SimSpec::Exact {});
    let v = serde_json::json!({"live": {"path": "l"}, "sim": {"kind": "exact", "model": "m"}});
    assert!(serde_json::from_value::<specs::ScorecardSpec>(v).is_err());
    let v = serde_json::json!({"live": {"path": "l"}, "sim": {"kind": "zoo", "model": "front_of_queue"}});
    let s: specs::ScorecardSpec = serde_json::from_value(v).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        s.sim,
        specs::SimSpec::Zoo {
            model: "front_of_queue".into(),
            l2: specs::L2Config::default()
        }
    );
    let lat: qt_types::LatencyModel = serde_json::from_value(serde_json::json!({"feed_ns": 5})).unwrap_or_default();
    assert_eq!(
        lat,
        qt_types::LatencyModel {
            feed_ns: 5,
            ..Default::default()
        }
    );
}

#[test]
fn dataset_id_rule() {
    let a = "a".repeat(64);
    let b = "b".repeat(64);
    let id = json::dataset_id(&[b.clone(), a.clone()], "etcsv");
    assert_eq!(id, crate::io::sha256_bytes(format!("{a}\n{b}\netcsv").as_bytes()));
    assert_eq!(id.len(), 64);
}

// ---- run identity -----------------------------------------------------------------------------------------------

/// Shared known answers with `queuetruth.schemas.run_id` (generated by `make schemas`).
#[test]
fn run_id_known_answers() {
    let kat = read_json(&repo().join("schemas/run_id_kat.json"));
    let cases = kat["cases"].as_array().cloned().unwrap_or_default();
    assert!(cases.len() >= 4);
    for case in cases {
        let refs = case["refs"].as_object().cloned().unwrap_or_default();
        let resolve = |r: &Value| -> crate::QtResult<String> {
            let key = crate::canonical_json(r)?;
            refs.get(&key)
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or_else(|| crate::QtError::Config(format!("unresolved ref {key}")))
        };
        let spec = &case["spec"];
        let ev = case["engine_version"].as_str().unwrap_or_default();
        let doc = crate::identity_document(spec, ev, &resolve).unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(
            doc,
            case["identity_document"].as_str().unwrap_or_default(),
            "{}",
            case["name"]
        );
        let id = crate::run_id(spec, ev, &resolve).unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(id, case["run_id"].as_str().unwrap_or_default(), "{}", case["name"]);
        assert_eq!(&crate::spec_echo(spec), &case["echo"], "{}", case["name"]);
    }
    for case in kat["numbers"].as_array().cloned().unwrap_or_default() {
        let v: Value = serde_json::from_str(case[0].as_str().unwrap_or("null")).unwrap_or(Value::Null);
        assert_eq!(
            crate::canonical_json(&v).ok().as_deref(),
            case[1].as_str(),
            "{}",
            case[0]
        );
    }
}

/// Writes one sample Parquet file per Rust-written table into `$QT_DUMP_DIR` (manual cross-check with pyarrow:
/// `QT_DUMP_DIR=... cargo test -p queuetruth-schema -- --ignored dump_sample_tables`).
#[test]
#[ignore = "manual: writes files to $QT_DUMP_DIR"]
fn dump_sample_tables() {
    let Some(dir) = std::env::var_os("QT_DUMP_DIR") else {
        return;
    };
    let dir = PathBuf::from(dir);
    let write = |name: &str, batch: arrow_array::RecordBatch| {
        let mut w = TableWriter::create(&dir.join(format!("{name}.parquet")), name).unwrap_or_else(|e| panic!("{e}"));
        w.write(&batch).unwrap_or_else(|e| panic!("{e}"));
        w.finish().unwrap_or_else(|e| panic!("{e}"));
    };
    write("qt.mbo", codec::mbo_to_batch(&(0..20).map(mbo).collect::<Vec<_>>()));
    write(
        "qt.trades",
        codec::trades_to_batch(&[TradeRow {
            ts_event: 5,
            instrument_id: 1,
            sequence: 9,
            price: 100,
            size: 3,
            aggressor_side: Side::Ask,
            kind: TradeKind::Hidden,
            maker_order_id: None,
        }]),
    );
}

/// Reads a `qt.shadow_spec` Parquet file written by another tool (`$QT_READ_SHADOWS`, manual pyarrow cross-check).
#[test]
#[ignore = "manual: reads $QT_READ_SHADOWS"]
fn read_external_shadow_specs() {
    let Some(p) = std::env::var_os("QT_READ_SHADOWS") else {
        return;
    };
    let specs = codec::read_shadow_specs(Path::new(&p)).unwrap_or_else(|e| panic!("{e}"));
    println!("{specs:?}");
    assert!(!specs.is_empty());
}

// ---- schema 1.1 -------------------------------------------------------------------------------------------------

fn fill(reason: FillReason) -> ShadowFillRow {
    ShadowFillRow {
        shadow_id: "s1".into(),
        fill_seq: 1,
        ts_event: 40,
        sequence: 9,
        fill_qty: 10,
        cum_qty: 10,
        leaves_qty: 0,
        fill_px: 100,
        reason,
        queue_ahead_before: 40,
        traded_at_px_event: 50,
        trade_through_event: 0,
        behind_volume_executed: 10,
    }
}

/// 1.1 columns follow the v0.1 columns; `schema` is the v0.1 layout, `full_schema` adds `added_columns`.
#[test]
fn v1_1_layouts() {
    let expected: [(&str, &[&str]); 10] = [
        ("qt.shadow_results", &["ts_planned_end", "planned_end_kind"]),
        ("qt.shadow_fills", &["liquidity"]),
        ("qt.zoo_predictions", &["liquidity", "status"]),
        ("qt.zoo_fills", &["liquidity"]),
        ("qt.live_orders", &["replaces_order_key"]),
        (
            "qt.score_fills",
            &["side", "instrument_id", "fee", "fee_ccy", "price_exp", "size_exp"],
        ),
        ("qt.score_orders", &["price_exp", "size_exp"]),
        ("qt.score_markouts", &["mo_bps", "mo_net_ccy"]),
        ("qt.zoo_shadows", &[]),
        ("qt.features", &["midnight_utc_ns"]),
    ];
    for name in crate::schema_names() {
        let core = crate::schema(name).unwrap_or_else(|| panic!("{name}"));
        let full = crate::full_schema(name).unwrap_or_else(|| panic!("{name}"));
        let added = crate::added_columns(name).unwrap_or_default();
        let want: &[&str] = expected.iter().find(|(n, _)| n == name).map(|(_, a)| *a).unwrap_or(&[]);
        assert_eq!(added, want, "{name}");
        assert_eq!(full.fields().len(), core.fields().len() + added.len(), "{name}");
        assert_eq!(
            &full.fields()[..core.fields().len()],
            &core.fields()[..],
            "{name}: prefix"
        );
        assert_eq!(full.metadata(), core.metadata(), "{name}");
        assert_eq!(
            full.metadata().get("qt.schema_version").map(String::as_str),
            Some("1.1")
        );
    }
    assert!(crate::added_columns("qt.nope").is_none());
    assert_eq!(
        crate::dict_values("qt.live_orders", "tif").map(|v| v.last().copied()),
        Some(Some("gtd"))
    );
    assert_eq!(
        crate::dict_values("qt.live_orders", "validation_status"),
        Some(&["real", "fixture_only", "simulated"][..])
    );
    assert_eq!(
        crate::dict_values("qt.live_events", "source").map(|v| v.last().copied()),
        Some(Some("sim"))
    );
    assert_eq!(FillReason::Crossed.liquidity(), Liquidity::Maker);
    assert_eq!(FillReason::CrossedAtLive.liquidity(), Liquidity::Taker);
    assert_eq!(FillReason::QueueDepleted.liquidity(), Liquidity::Maker);
    assert_eq!(FillReason::TradeThrough.liquidity(), Liquidity::Maker);
    assert_eq!(ZooFillReason::TouchDraw.liquidity(), Liquidity::Maker);
    assert_eq!(ZooFillReason::CrossedAtLive.liquidity(), Liquidity::Taker);
}

/// Old (v0.1) and new (1.1) files are both readable; full writers upgrade v0.1 batches.
#[test]
fn v1_1_tables_read_both_layouts() {
    let tmp = TempDir::new("v11");
    let d = tmp.0.as_path();
    let rows = vec![fill(FillReason::QueueDepleted), fill(FillReason::CrossedAtLive)];
    let core = codec::shadow_fills_to_batch(&rows);
    assert!(!codec::has_added_columns(&core, "qt.shadow_fills"));
    assert_eq!(codec::batch_liquidity(&core, "qt.shadow_fills").ok(), Some(None));
    let full = codec::upgrade_batch("qt.shadow_fills", &core).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(full.num_columns(), core.num_columns() + 1);
    assert_eq!(
        codec::batch_liquidity(&full, "qt.shadow_fills").ok().flatten(),
        Some(vec![Some(Liquidity::Maker), Some(Liquidity::Taker)])
    );
    assert_eq!(codec::batch_to_shadow_fills(&full).ok(), Some(rows.clone()));
    for (k, layout) in [("core", 0), ("full", 1), ("full_from_core", 2)] {
        let path = d.join(format!("{k}.parquet"));
        let mut w = if layout == 0 {
            TableWriter::create(&path, "qt.shadow_fills")
        } else {
            TableWriter::create_full(&path, "qt.shadow_fills")
        }
        .unwrap_or_else(|e| panic!("{e}"));
        w.write(if layout == 1 { &full } else { &core })
            .unwrap_or_else(|e| panic!("{k}: {e}"));
        w.finish().unwrap_or_else(|e| panic!("{e}"));
        let r = read_table_as(&path, "qt.shadow_fills").unwrap_or_else(|e| panic!("{k}: {e}"));
        let ncols = r.schema().fields().len();
        let mut got = Vec::new();
        for b in r {
            let b = b.unwrap_or_else(|e| panic!("{e}"));
            if layout > 0 {
                assert_eq!(
                    codec::batch_liquidity(&b, "qt.shadow_fills").ok().flatten(),
                    Some(vec![Some(Liquidity::Maker), Some(Liquidity::Taker)])
                );
            }
            got.extend(codec::batch_to_shadow_fills(&b).unwrap_or_else(|e| panic!("{e}")));
        }
        assert_eq!(ncols, if layout == 0 { 13 } else { 14 }, "{k}");
        assert_eq!(got, rows, "{k}");
    }
    // zoo fills derive liquidity too; a non-derivable non-null column must be supplied.
    let zf = codec::zoo_fills_to_batch(&[ZooFillRow {
        model_id: "m".into(),
        shadow_id: "s".into(),
        fill_seq: 1,
        ts_event: 1,
        fill_qty: 1.0,
        cum_qty: 1.0,
        queue_ahead_est_before: 0.0,
        reason: ZooFillReason::CrossedAtLive,
        quirk: Quirk::None,
    }]);
    let zf = codec::upgrade_batch("qt.zoo_fills", &zf).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        codec::batch_liquidity(&zf, "qt.zoo_fills").ok().flatten(),
        Some(vec![Some(Liquidity::Taker)])
    );
    let empty_results = codec::shadow_results_to_batch(&[]);
    assert!(codec::upgrade_batch("qt.shadow_results", &empty_results).is_err());
    let with = codec::with_added_columns("qt.shadow_results", &empty_results, codec::planned_end_columns(&[]))
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(codec::batch_planned_ends(&with).ok(), Some(Some(vec![])));
    assert_eq!(codec::batch_planned_ends(&empty_results).ok(), Some(None));
    let wrong = codec::with_added_columns("qt.shadow_fills", &core, vec![]);
    assert!(wrong.is_err());
}

/// Specs without the 1.1 fields keep their v0.1 run_id; non-default values change it.
#[test]
fn v1_1_run_id_compat() {
    let same = |_: &Value| -> crate::QtResult<String> { Ok("0".repeat(64)) };
    let r: specs::ReplaySpec =
        serde_json::from_value(serde_json::json!({"dataset": {"path": "d"}})).unwrap_or_else(|e| panic!("{e}"));
    let full = serde_json::to_value(&r).unwrap_or(Value::Null);
    let mut v0_1 = full.clone();
    for k in ["fill_rule", "hidden_print_policy", "exclude_order_ids"] {
        assert!(v0_1.as_object_mut().and_then(|o| o.remove(k)).is_some(), "{k}");
    }
    let id = |v: &Value| crate::run_id(v, "0.1.0", &same).unwrap_or_default();
    assert_eq!(id(&full), id(&v0_1));
    let mut et = full.clone();
    et["fill_rule"] = "et".into();
    assert_ne!(id(&et), id(&full));
    let mut ex = full.clone();
    ex["exclude_order_ids"] = serde_json::json!([{"instrument_id": 1, "order_id": 2}]);
    assert_ne!(id(&ex), id(&full));
    // Nested default (LiveImportSpec.fix.venue_name) and the discriminator rule.
    let li: specs::LiveImportSpec =
        serde_json::from_value(serde_json::json!({"venue": "fix44", "inputs": [{"path": "x"}]}))
            .unwrap_or_else(|e| panic!("{e}"));
    let li = serde_json::to_value(&li).unwrap_or(Value::Null);
    let dropped = crate::drop_v1_1_defaults(&li).unwrap_or(Value::Null);
    assert!(dropped.get("tz").is_none() && dropped["fix"].get("venue_name").is_none());
    assert_eq!(dropped["fix"]["delimiter"], "auto");
    let other = serde_json::json!({"tz": null, "amends": null, "window_ns": 60_000_000_000_i64});
    assert_eq!(crate::drop_v1_1_defaults(&other).ok(), Some(other.clone()));
    // ReplaySpec.amends predates 1.1 and stays in the identity document.
    let doc = crate::identity_document(&full, "0.1.0", &same).unwrap_or_default();
    assert!(doc.contains("\"amends\":null") && !doc.contains("fill_rule"));
    // Float-typed defaults canonicalise like integers.
    let sc = serde_json::json!({"markout_horizons_ns": [], "window_ns": 6e10, "match_window_ns": 1e6});
    assert_eq!(
        crate::drop_v1_1_defaults(&sc).ok(),
        Some(serde_json::json!({"markout_horizons_ns": []}))
    );
    // v0.1 run ids of the committed known answers.
    let kat = read_json(&repo().join("schemas/run_id_kat.json"));
    let ids: Vec<(String, String)> = kat["cases"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|c| {
            (
                c["name"].as_str().unwrap_or_default().to_owned(),
                c["run_id"].as_str().unwrap_or_default().to_owned(),
            )
        })
        .collect();
    for (name, v0_1_id) in [
        ("replay_defaults", "8fe5ca7372885231"),
        ("shadow_sample_floats", "b766b42bc44fcf6a"),
        ("calibration_nested", "0cd47ab5167fff23"),
        ("scorecard_refs", "ac555f359b65ad3e"),
        ("raw_edge_cases", "cf7ec15b8d295933"),
        // Defaults of the specs whose 1.1 fields arrived last (ids from before those fields existed).
        ("report_v1_1_defaults", "d66c2a2ce04f8ea3"),
        ("hazard_fit_v1_1_defaults", "7e5fe3f069f57418"),
        ("live_calibration_v1_1_defaults", "7d64f9816022cbe8"),
    ] {
        assert!(ids.contains(&(name.to_owned(), v0_1_id.to_owned())), "{name}");
    }
    // Late 1.1 fields: defaults leave the identity document, set values enter it; `family` (shared with
    // CalibrationSpec) only drops fields CalibrationSpec does not have.
    let h: specs::HazardFitSpec =
        serde_json::from_value(serde_json::json!({"features": {"path": "f"}})).unwrap_or_else(|e| panic!("{e}"));
    let rp: specs::ReportSpec =
        serde_json::from_value(serde_json::json!({"inputs": ["o"]})).unwrap_or_else(|e| panic!("{e}"));
    let lc: specs::LiveCalibrationSpec = serde_json::from_value(
        serde_json::json!({"dataset": {"path": "d"}, "live": {"path": "l"}, "family": "power3"}),
    )
    .unwrap_or_else(|e| panic!("{e}"));
    for (v, fields) in [
        (serde_json::to_value(&h).unwrap_or(Value::Null), &["zoo_shadows"][..]),
        (
            serde_json::to_value(&rp).unwrap_or(Value::Null),
            &["leaderboard_sort_horizon_ns"][..],
        ),
        (
            serde_json::to_value(&lc).unwrap_or(Value::Null),
            &["self_exclude", "match_window_ns"][..],
        ),
    ] {
        let mut v0 = v.clone();
        for k in fields {
            assert!(v0.as_object_mut().and_then(|o| o.remove(*k)).is_some(), "{k}");
        }
        assert_eq!(id(&v), id(&v0), "{fields:?}");
        assert_eq!(crate::drop_v1_1_defaults(&v).ok(), Some(v0));
    }
    let hv = serde_json::to_value(&h).unwrap_or(Value::Null);
    let mut hz = hv.clone();
    hz["zoo_shadows"] = serde_json::json!({"path": "z"});
    assert_ne!(id(&hz), id(&hv));
    let lv = serde_json::to_value(&lc).unwrap_or(Value::Null);
    for (k, x) in [
        ("self_exclude", Value::Bool(false)),
        ("match_window_ns", Value::from(0)),
    ] {
        let mut l2 = lv.clone();
        l2[k] = x;
        assert_ne!(id(&l2), id(&lv), "{k}");
    }
    let cal = serde_json::json!({"kind": "alloc", "family": "power", "self_exclude": true});
    assert_eq!(
        crate::drop_v1_1_defaults(&cal)
            .ok()
            .map(|c| c.get("self_exclude").is_some()),
        Some(false)
    );
    let cal = serde_json::json!({"kind": "alloc", "family": "power"});
    assert_eq!(crate::drop_v1_1_defaults(&cal).ok(), Some(cal.clone()));
}

/// 1.1 spec fields: defaults, enums and rejection rules.
#[test]
fn v1_1_spec_fields() {
    let r: specs::ReplaySpec =
        serde_json::from_value(serde_json::json!({"dataset": {"path": "d"}})).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        (r.fill_rule, r.hidden_print_policy, r.exclude_order_ids.len()),
        (specs::FillRuleName::Exact, None, 0)
    );
    let r: specs::ReplaySpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": "d"}, "fill_rule": "et", "hidden_print_policy": "behind_and_through",
        "exclude_order_ids": [{"instrument_id": 3, "order_id": 9}]
    }))
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(r.fill_rule, specs::FillRuleName::Et);
    assert_eq!(
        r.hidden_print_policy,
        Some(specs::HiddenPrintPolicyName::BehindAndThrough)
    );
    assert_eq!(
        r.exclude_order_ids,
        [specs::ExcludeOrderId {
            instrument_id: 3,
            order_id: 9,
            session_date: None,
        }]
    );
    let bad = serde_json::json!({"dataset": {"path": "d"}, "exclude_order_ids": [{"instrument_id": 3}]});
    assert!(serde_json::from_value::<specs::ReplaySpec>(bad).is_err());
    // A5 as refined (E48): only crossed_at_live is taker.
    assert_eq!(FillReason::Crossed.liquidity(), Liquidity::Maker);
    assert_eq!(ZooFillReason::Crossed.liquidity(), Liquidity::Maker);
    // session_date: a dated exclusion enters run_id, an undated one (null) does not change it.
    let same = |_: &Value| -> crate::QtResult<String> { Ok("0".repeat(64)) };
    let id = |v: &Value| crate::run_id(v, "0.1.0", &same).unwrap_or_default();
    let base = serde_json::json!({"dataset": {"path": "d"}, "sweep_gap_ns": 1,
        "exclude_order_ids": [{"instrument_id": 3, "order_id": 9}]});
    let mut undated = base.clone();
    undated["exclude_order_ids"][0]["session_date"] = Value::Null;
    let mut dated = base.clone();
    dated["exclude_order_ids"][0]["session_date"] = "2019-12-30".into();
    assert_eq!(id(&undated), id(&base));
    assert_ne!(id(&dated), id(&base));
    let r: specs::ReplaySpec = serde_json::from_value(dated).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(r.exclude_order_ids[0].session_date.as_deref(), Some("2019-12-30"));
    let d = specs::ExcludeOrderId {
        instrument_id: 3,
        ..Default::default()
    };
    assert_eq!((d.order_id, d.session_date), (0, None));
    let sc: specs::ScorecardSpec =
        serde_json::from_value(serde_json::json!({"live": {"path": "l"}, "sim": {"kind": "exact"}}))
            .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        (sc.window_ns, sc.match_window_ns, sc.self_exclude, sc.markout_past_end),
        (60_000_000_000, 1_000_000, true, None)
    );
    let li: specs::LiveImportSpec =
        serde_json::from_value(serde_json::json!({"venue": "sim_nautilus", "inputs": [{"path": "x"}]}))
            .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        (li.venue, li.tz, li.fix.venue_name),
        (specs::LiveVenue::SimNautilus, None, None)
    );
    let z: specs::ZooSpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": "d"}, "shadows": {"path": "s"},
        "exclude_order_ids": [{"instrument_id": 1, "order_id": 2}]
    }))
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!((z.amends.clone(), z.exclude_order_ids.len()), (None, 1));
    let zv = serde_json::to_value(&z).unwrap_or(Value::Null);
    let mut zd = zv.clone();
    zd["exclude_order_ids"] = serde_json::json!([]);
    let same = |_: &Value| -> crate::QtResult<String> { Ok("0".repeat(64)) };
    let zid = |v: &Value| crate::run_id(v, "0.1.0", &same).unwrap_or_default();
    assert_ne!(zid(&zv), zid(&zd));
    zd.as_object_mut().map(|o| o.remove("exclude_order_ids"));
    zd.as_object_mut().map(|o| o.remove("amends"));
    let mut zd2 = zd.clone();
    zd2["exclude_order_ids"] = serde_json::json!([]);
    zd2["amends"] = Value::Null;
    assert_eq!(zid(&zd), zid(&zd2));
    let cal = serde_json::json!({"kind": "live", "family": "power"});
    assert!(serde_json::from_value::<specs::CalibrationSpec>(cal).is_err());
    let cal = serde_json::json!({"kind": "alloc", "family": "power"});
    assert!(serde_json::from_value::<specs::CalibrationSpec>(cal).is_ok());
    let res = serde_json::to_value(json::CalibrationKind::Live).unwrap_or(Value::Null);
    assert_eq!(res, "live");
    // Late 1.1 fields: defaults and values.
    let rp: specs::ReportSpec =
        serde_json::from_value(serde_json::json!({"inputs": ["o"], "leaderboard_sort_horizon_ns": 10_000_000_000_i64}))
            .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(rp.leaderboard_sort_horizon_ns, Some(10_000_000_000));
    let h: specs::HazardFitSpec = serde_json::from_value(serde_json::json!({
        "features": {"path": "f"}, "zoo_predictions": {"path": "z/p"}, "zoo_shadows": {"path": "z/s"}
    }))
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(h.zoo_shadows.as_ref().and_then(|f| f.as_path()), Some("z/s"));
    let lc: specs::LiveCalibrationSpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": "d"}, "live": {"path": "l"}, "family": "power", "self_exclude": false,
        "match_window_ns": 5
    }))
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!((lc.self_exclude, lc.match_window_ns), (false, 5));
    // FeatureSpec.cancel_away_ticks: default 2; null and 0 (never cancel) are both kept as given.
    let fs = |x: Value| -> specs::FeatureSpec {
        let mut v = serde_json::json!({"dataset": {"path": "d"}});
        if !x.is_string() {
            v["cancel_away_ticks"] = x;
        }
        serde_json::from_value(v).unwrap_or_else(|e| panic!("{e}"))
    };
    assert_eq!(fs(Value::from("absent")).cancel_away_ticks, Some(2));
    assert_eq!(fs(Value::Null).cancel_away_ticks, None);
    assert_eq!(fs(Value::from(0)).cancel_away_ticks, Some(0));
    let v = serde_json::to_value(fs(Value::Null)).unwrap_or(Value::Null);
    assert!(v["cancel_away_ticks"].is_null());
}

/// v0.1 documents (without the 1.1 fields) still deserialize; the 1.1 fields serialize as null.
#[test]
fn v1_1_documents_accept_v0_1() {
    let m: json::LiveManifest = serde_json::from_value(serde_json::json!({
        "schema": "qt.live_manifest", "schema_version": 1, "venues": [], "accounts": [], "n_orders": 0,
        "n_events": 0, "instrument_map_sha256": null, "created_by": "x"
    }))
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!((m.coverage.clone(), m.entry_fallback_share), (None, None));
    let v = serde_json::to_value(&m).unwrap_or(Value::Null);
    let keys: Vec<&String> = v.as_object().map(|o| o.keys().collect()).unwrap_or_default();
    assert_eq!(keys[keys.len() - 2..], ["coverage", "entry_fallback_share"]);
    assert!(v["coverage"].is_null());
    let mut rs = serde_json::to_value(json::ReplaySummary::default()).unwrap_or(Value::Null);
    assert!(rs["shadow_diagnostics"].is_null());
    rs.as_object_mut().map(|o| o.remove("shadow_diagnostics"));
    assert!(serde_json::from_value::<json::ReplaySummary>(rs).is_ok());
    let mut sc = serde_json::to_value(json::ScorecardSummary::default()).unwrap_or(Value::Null);
    for k in [
        "n_twin_unmatched",
        "twin_match_rate",
        "discordance_clustered",
        "n_taker_fills",
        "fills_only",
    ] {
        assert!(sc.as_object_mut().and_then(|o| o.remove(k)).is_some(), "{k}");
    }
    assert!(serde_json::from_value::<json::ScorecardSummary>(sc).is_ok());
    let mut wt = serde_json::to_value(json::WhyTrace::default()).unwrap_or(Value::Null);
    for k in [
        "model_id",
        "model_queue_ahead",
        "first_divergence",
        "conclusion",
        "warnings",
    ] {
        assert!(wt.as_object_mut().and_then(|o| o.remove(k)).is_some(), "{k}");
    }
    let wt: json::WhyTrace = serde_json::from_value(wt).unwrap_or_else(|e| panic!("{e}"));
    assert!(wt.warnings.is_empty() && wt.model_queue_ahead.is_none());
    let mut dm = serde_json::to_value(json::DatasetManifest::default()).unwrap_or(Value::Null);
    assert!(dm["rule_set"].is_null());
    dm.as_object_mut().map(|o| o.remove("rule_set"));
    let dm: json::DatasetManifest = serde_json::from_value(dm).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(dm.effective_rule_set(), qt_types::RuleSet::Generic);
    let dm = json::DatasetManifest {
        venue: Venue::Generic,
        rule_set: Some(qt_types::RuleSet::CryptoL3),
        ..json::DatasetManifest::default()
    };
    let v = serde_json::to_value(&dm).unwrap_or(Value::Null);
    assert_eq!(v["rule_set"], "crypto_l3");
    assert_eq!(dm.effective_rule_set(), qt_types::RuleSet::CryptoL3);
    let fx = specs::FixOptions::default();
    assert_eq!(
        (
            fx.checksum,
            fx.data_dictionary.clone(),
            fx.session_venues.len(),
            fx.instrument_tag
        ),
        (specs::FixChecksum::Exclude, None, 0, None)
    );
    let fx: specs::FixOptions =
        serde_json::from_value(serde_json::json!({"data_dictionary": "a.xml", "instrument_tag": 107}))
            .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(fx.data_dictionary.map(|d| d.to_vec()), Some(vec!["a.xml".to_owned()]));
    assert!(serde_json::from_value::<specs::FixOptions>(serde_json::json!({"checksum": "strict"})).is_err());
    let ex: specs::ExportSpec =
        serde_json::from_value(serde_json::json!({"target": "hftbacktest"})).unwrap_or_else(|e| panic!("{e}"));
    assert!(!ex.force);
    let tr: json::CalibrationTrust = serde_json::from_value(serde_json::json!({
        "verdict": "refused", "reasons": ["few_events"], "flags": [],
        "beats_default": {"1000000000": "n/a", "100000000": "better"}
    }))
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(tr.verdict, json::TrustVerdict::Refused);
    assert_eq!(
        tr.beats_default.get("1000000000"),
        Some(&json::BeatsDefault::NotAvailable)
    );
    let mut li = serde_json::to_value(json::LiveImportSummary::default()).unwrap_or(Value::Null);
    assert_eq!(li["unmapped_instruments"], serde_json::json!([]));
    assert!(li["instrument_scales"].is_null());
    li.as_object_mut().map(|o| o.remove("unmapped_instruments"));
    li.as_object_mut().map(|o| o.remove("instrument_scales"));
    assert!(serde_json::from_value::<json::LiveImportSummary>(li).is_ok());
    let ws: specs::WhySpec =
        serde_json::from_value(serde_json::json!({"dataset": {"path": "d"}})).unwrap_or_else(|e| panic!("{e}"));
    assert!(ws.exclude_order_ids.is_empty());
}

/// Twin-linking counters of `ShadowDiagnostics` (additive, default 0): a document without them still reads, and
/// they serialize under their schema names.
#[test]
fn shadow_diagnostics_twin_counters_are_additive() {
    let old = serde_json::json!({
        "hidden_print_fills": 1, "hidden_at_px_with_display_ahead": 0, "taker_fills": 0, "crossed_unfilled": 0,
        "behind_exec_with_ahead": 0, "excluded_requested": 2, "excluded_matched": 2, "excluded_records": 0
    });
    let d: json::ShadowDiagnostics = serde_json::from_value(old).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        (d.twins_linked, d.twin_fills_with_queue_ahead, d.twin_fills_off_display),
        (0, 0, 0)
    );
    let d = json::ShadowDiagnostics {
        twins_linked: 2,
        twin_fills_with_queue_ahead: 1,
        twin_fills_off_display: 3,
        ..d
    };
    let v = serde_json::to_value(&d).unwrap_or(Value::Null);
    assert_eq!(
        (
            &v["twins_linked"],
            &v["twin_fills_with_queue_ahead"],
            &v["twin_fills_off_display"]
        ),
        (&serde_json::json!(2), &serde_json::json!(1), &serde_json::json!(3))
    );
}

/// `qt.zoo_predictions.status` (faithful hbt #312 aborts): the column builds as `hbt_312_abort` / null, and a batch
/// written before the column existed (core + `liquidity`) still reads.
#[test]
fn zoo_prediction_status_and_prefix_layout() {
    use arrow_array::Array as _;
    let col = codec::zoo_prediction_status_column([false, true]);
    assert!(col.is_null(0) && !col.is_null(1));
    assert_eq!(
        crate::dict_values("qt.zoo_predictions", "status"),
        Some(&["hbt_312_abort"][..])
    );
    let core = codec::zoo_predictions_to_batch(&[]);
    let older = codec::with_added_columns("qt.zoo_predictions", &core, vec![codec::liquidity_column([])]);
    assert!(older.is_err(), "with_added_columns needs every 1.1 column");
    let full = codec::with_added_columns(
        "qt.zoo_predictions",
        &core,
        vec![codec::liquidity_column([]), codec::zoo_prediction_status_column([])],
    )
    .unwrap_or_else(|e| panic!("{e}"));
    let prefix = arrow_array::RecordBatch::try_new(
        std::sync::Arc::new(arrow_schema::Schema::new_with_metadata(
            full.schema().fields()[..full.num_columns() - 1].to_vec(),
            full.schema().metadata().clone(),
        )),
        full.columns()[..full.num_columns() - 1].to_vec(),
    )
    .unwrap_or_else(|e| panic!("{e}"));
    assert!(codec::check_layout(&prefix, "qt.zoo_predictions").is_ok());
    assert!(codec::batch_to_zoo_predictions(&prefix).is_ok());
}
