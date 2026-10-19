//! Hidden `bench` subcommands (SPEC 4.4 hidden rows, 7.1 harness; E25).
//!
//! Output: one `qt.bench` document: `data` = a case label, `records` = units of work per repetition (MBO records,
//! or ITCH messages for `bench itch`), `wall_s` = one entry per measured repetition (warmups excluded),
//! `records_per_s_median`. Setup (synthetic generation, dataset writing, shadow construction) is untimed; Parquet
//! decode is inside the timed region for `--source parquet` (default), as SPEC 7.1 states its targets from Parquet.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Instant;

use qt_ingest::{Dataset, itch};
use qt_l2::{L2Builder, L2Config, TradePosition};
use qt_replay::{BboMode, Engine, EngineConfig, Sink, TrajectoryMode};
use qt_schema::json::Bench;
use qt_schema::rows::{L2Row, PriceSpec, ShadowSource, ShadowSpec};
use qt_schema::{codec, specs};
use qt_types::{BookSide, MboEvent, QtError, QtResult, Venue};
use serde_json::Value;

/// Parsed flags of the `bench` subcommands (unset flags keep the defaults).
#[derive(Clone, Debug)]
struct BenchArgs {
    data: Option<PathBuf>,
    synth_records: Option<u64>,
    synth_messages: Option<u64>,
    write: Option<PathBuf>,
    seed: u64,
    mode: String,
    source: String,
    shadows: Option<PathBuf>,
    synth_shadows: Option<u32>,
    trajectory: String,
    l2_depth: u16,
    l2_cadence_ns: i64,
    warmup: u32,
    repeat: u32,
    out: Option<PathBuf>,
    scratch: Option<PathBuf>,
}

fn u64_of(a: &Value, k: &str) -> QtResult<Option<u64>> {
    match a.get(k) {
        None | Some(Value::Null) => Ok(None),
        Some(v) => v
            .as_u64()
            .map(Some)
            .ok_or_else(|| QtError::Usage(format!("--{}: expected a non-negative integer", k.replace('_', "-")))),
    }
}

fn str_of(a: &Value, k: &str) -> Option<String> {
    a.get(k).and_then(Value::as_str).map(str::to_owned)
}

fn u32_of(a: &Value, k: &str, default: u32) -> QtResult<u32> {
    match u64_of(a, k)? {
        Some(v) => u32::try_from(v).map_err(|_| QtError::Usage(format!("--{}: out of range", k.replace('_', "-")))),
        None => Ok(default),
    }
}

impl BenchArgs {
    fn from_args(a: &Value) -> QtResult<Self> {
        Ok(Self {
            data: str_of(a, "data").map(PathBuf::from),
            synth_records: u64_of(a, "synth_records")?,
            synth_messages: u64_of(a, "synth_messages")?,
            write: str_of(a, "write").map(PathBuf::from),
            seed: u64_of(a, "seed")?.unwrap_or(7),
            mode: str_of(a, "mode").unwrap_or_else(|| "book".into()),
            source: str_of(a, "source").unwrap_or_else(|| "parquet".into()),
            shadows: str_of(a, "shadows").map(PathBuf::from),
            synth_shadows: u64_of(a, "synth_shadows")?
                .map(|n| u32::try_from(n).map_err(|_| QtError::Usage("--synth-shadows: out of range".into())))
                .transpose()?,
            trajectory: str_of(a, "trajectory").unwrap_or_else(|| "none".into()),
            l2_depth: u16::try_from(u32_of(a, "l2_depth", 10)?)
                .map_err(|_| QtError::Usage("--l2-depth: out of range".into()))?,
            l2_cadence_ns: a.get("l2_cadence_ns").and_then(Value::as_i64).unwrap_or(0),
            warmup: u32_of(a, "warmup", 1)?,
            repeat: u32_of(a, "repeat", 3)?.max(1),
            out: str_of(a, "out").map(PathBuf::from),
            scratch: str_of(a, "scratch").map(PathBuf::from),
        })
    }
}

/// Discards every row (the "no sinks" configuration).
struct NoSink;
impl Sink for NoSink {}

/// Runs `bench <kind>` with the parsed command arguments.
pub(crate) fn run(kind: &str, args: &Value) -> QtResult<Bench> {
    let a = BenchArgs::from_args(args)?;
    match kind {
        "replay" => bench_replay(&a),
        "l2" => bench_l2(&a),
        "itch" => bench_itch(&a),
        other => Err(QtError::Usage(format!("unknown bench kind {other:?}"))),
    }
}

/// Times `f` `warmup + repeat` times (with an untimed `before` hook); returns the walls and the last work count.
fn timed(
    a: &BenchArgs,
    mut before: impl FnMut() -> QtResult<()>,
    mut f: impl FnMut() -> QtResult<u64>,
) -> QtResult<(Vec<f64>, u64)> {
    let mut walls = Vec::with_capacity(a.repeat as usize);
    let mut work = 0;
    for i in 0..(a.warmup + a.repeat) {
        before()?;
        let t = Instant::now();
        work = f()?;
        let s = t.elapsed().as_secs_f64();
        if i >= a.warmup {
            walls.push(s);
        }
    }
    Ok((walls, work))
}

fn doc(label: String, records: u64, wall_s: Vec<f64>) -> Bench {
    let mut r: Vec<f64> = wall_s
        .iter()
        .filter(|w| **w > 0.0)
        .map(|w| records as f64 / w)
        .collect();
    r.sort_by(f64::total_cmp);
    let median = if r.is_empty() {
        None
    } else if r.len() % 2 == 1 {
        Some(r[r.len() / 2])
    } else {
        Some((r[r.len() / 2 - 1] + r[r.len() / 2]) / 2.0)
    };
    Bench {
        data: label,
        records,
        wall_s,
        records_per_s_median: median,
        ..Bench::default()
    }
}

/// `--data` or a synthetic dataset (4 Nasdaq instruments) written once to scratch (untimed, reused).
fn dataset(a: &BenchArgs) -> QtResult<(PathBuf, String)> {
    if let Some(d) = &a.data {
        return Ok((d.clone(), d.display().to_string()));
    }
    let n = a
        .synth_records
        .ok_or_else(|| QtError::Usage("bench: give --data DIR or --synth-records N".into()))?;
    let root = a
        .scratch
        .clone()
        .unwrap_or_else(|| PathBuf::from(format!(".scratch/bench/synth-{n}-{}", a.seed)));
    if !root.join("manifest.json").exists() {
        let mut cfg = qt_testkit::SynthConfig::for_venue(Venue::Nasdaq, a.seed);
        cfg.instruments = 4;
        // events count F_LAST batches (about 1.1-1.3 records each); the reported work is the true record count.
        cfg.events_per_instrument = (n / 4 * 10 / 12).max(1);
        let out = qt_testkit::generate(&cfg);
        qt_testkit::write_dataset(&out, &root)?;
    }
    Ok((root, format!("synth:{n}:seed{}", a.seed)))
}

fn trajectory(a: &BenchArgs) -> QtResult<TrajectoryMode> {
    Ok(match a.trajectory.as_str() {
        "none" => TrajectoryMode::None,
        "ahead" => TrajectoryMode::Ahead,
        "full" => TrajectoryMode::Full,
        t => return Err(QtError::Usage(format!("--trajectory: {t:?}"))),
    })
}

/// `n` touch-pegged shadows round-robin over partitions, sides alternating, submit times evenly spread over each
/// partition's `[ts_min, ts_max]`, qty 1, no cancel.
fn synth_shadows(ds: &Dataset, n: u32) -> Vec<ShadowSpec> {
    let parts = ds.partitions();
    if parts.is_empty() {
        return Vec::new();
    }
    let per = (i64::from(n) / parts.len() as i64).max(1);
    (0..n)
        .map(|i| {
            let p = &parts[i as usize % parts.len()];
            let k = (i as usize / parts.len()) as i64;
            let span = (p.ts_max - p.ts_min).max(1);
            ShadowSpec {
                shadow_id: format!("b{i}"),
                instrument_id: p.instrument_id,
                side: if i % 2 == 0 { BookSide::Bid } else { BookSide::Ask },
                price: PriceSpec::Touch { offset_ticks: 0 },
                qty: 1,
                ts_submit: p.ts_min + span / per * k,
                latency_ns: 0,
                ts_cancel: None,
                group: None,
                source: ShadowSource::Sampled,
                order_key: None,
            }
        })
        .collect()
}

fn preload(ds: &Dataset, a: &BenchArgs) -> QtResult<Option<Vec<Vec<MboEvent>>>> {
    match a.source.as_str() {
        "memory" => Ok(Some(
            ds.partitions()
                .iter()
                .map(|p| p.mbo()?.collect::<QtResult<Vec<_>>>())
                .collect::<QtResult<Vec<_>>>()?,
        )),
        "parquet" => Ok(None),
        s => Err(QtError::Usage(format!("--source: {s:?}"))),
    }
}

type Events<'a> = Box<dyn Iterator<Item = QtResult<MboEvent>> + 'a>;

fn events<'a>(ds: &'a Dataset, pre: &'a Option<Vec<Vec<MboEvent>>>, i: usize) -> QtResult<Events<'a>> {
    Ok(match pre {
        Some(v) => Box::new(v[i].iter().copied().map(Ok)),
        None => ds.partitions()[i].mbo()?,
    })
}

fn bench_replay(a: &BenchArgs) -> QtResult<Bench> {
    let (root, data) = dataset(a)?;
    let ds = Dataset::open(&root)?;
    let shadow_label = a
        .shadows
        .as_ref()
        .map(|p| p.display().to_string())
        .or(a.synth_shadows.map(|n| n.to_string()))
        .unwrap_or_else(|| "0".into());
    let label = format!(
        "replay:{}:{}:{data}:shadows={shadow_label}:traj={}",
        a.mode, a.source, a.trajectory
    );
    match a.mode.as_str() {
        "book" | "decode" => {
            let mut cfg = EngineConfig::new(ds.rules());
            cfg.bbo = BboMode::None;
            cfg.trajectory = trajectory(a)?;
            let list = match (&a.shadows, a.synth_shadows) {
                (Some(p), _) => codec::read_shadow_specs(p)?,
                (None, Some(n)) => synth_shadows(&ds, n),
                (None, None) => Vec::new(),
            };
            let shadows: BTreeMap<(i32, u32), Vec<ShadowSpec>> = qt_replay::split_shadows(&ds, list)?;
            let pre = preload(&ds, a)?;
            let decode_only = a.mode == "decode";
            let (walls, records) = timed(
                a,
                || Ok(()),
                || {
                    let mut records = 0u64;
                    for (i, p) in ds.partitions().iter().enumerate() {
                        let it = events(&ds, &pre, i)?;
                        if decode_only {
                            for e in it {
                                std::hint::black_box(e?);
                                records += 1;
                            }
                            continue;
                        }
                        let inst: Vec<_> = ds
                            .instrument(p.instrument_id, p.session_date)
                            .into_iter()
                            .cloned()
                            .collect();
                        let sh = shadows
                            .get(&(p.session_date, p.instrument_id))
                            .cloned()
                            .unwrap_or_default();
                        let mut eng = Engine::new(cfg.clone(), &inst, sh, Vec::new())?;
                        eng.set_sequence_gap_detection(false); // as run_dataset (ingest-time gaps)
                        let mut sink = NoSink;
                        for e in it {
                            eng.apply(&e?, &mut sink)?;
                        }
                        records += eng.finish(&mut sink)?.stats.records;
                    }
                    Ok(records)
                },
            )?;
            Ok(doc(label, records, walls))
        }
        "sinks" | "rank-oracle" => {
            let out = a
                .out
                .clone()
                .ok_or_else(|| QtError::Usage("--mode sinks|rank-oracle needs --out DIR".into()))?;
            let rank = a.mode == "rank-oracle";
            let spec: specs::ReplaySpec = crate::ops::spec(
                &serde_json::json!({
                    "dataset": {"path": root.display().to_string()},
                    "out": out.display().to_string(),
                    "rank_oracle": rank,
                    "bbo": if rank { "none" } else { "change" },
                    "trajectory": "none",
                }),
                "bench replay",
            )?;
            let wipe = || {
                if out.exists() {
                    std::fs::remove_dir_all(&out)?;
                }
                Ok(())
            };
            let (walls, records) = timed(a, wipe, || Ok(qt_replay::run_dataset(&spec)?.stats.records))?;
            if out.exists() {
                std::fs::remove_dir_all(&out)?;
            }
            Ok(doc(label, records, walls))
        }
        m => Err(QtError::Usage(format!("--mode: {m:?}"))),
    }
}

fn bench_l2(a: &BenchArgs) -> QtResult<Bench> {
    let (root, data) = dataset(a)?;
    let ds = Dataset::open(&root)?;
    let rules = ds.rules();
    let cfg = L2Config {
        depth_levels: a.l2_depth,
        cadence_ns: a.l2_cadence_ns,
        trade_position: TradePosition::Before,
        include_counts: true,
        emit_hidden_trades: true,
    };
    let pre = preload(&ds, a)?;
    let (walls, records) = timed(
        a,
        || Ok(()),
        || {
            let mut records = 0u64;
            let mut rows: Vec<L2Row> = Vec::with_capacity(8192);
            let mut emitted = 0u64;
            for (i, p) in ds.partitions().iter().enumerate() {
                let mut b = L2Builder::new(cfg, rules, p.instrument_id);
                let mut last_ts = None;
                for e in events(&ds, &pre, i)? {
                    let e = e?;
                    last_ts = Some(e.ts_event);
                    b.push(&e, &mut rows);
                    records += 1;
                    if rows.len() >= 4096 {
                        emitted += rows.len() as u64;
                        std::hint::black_box(&rows);
                        rows.clear();
                    }
                }
                if let Some(ts) = last_ts {
                    b.flush(ts, &mut rows);
                }
                emitted += rows.len() as u64;
                rows.clear();
            }
            std::hint::black_box(emitted);
            Ok(records)
        },
    )?;
    Ok(doc(
        format!("l2:d{}:c{}:{}:{data}", a.l2_depth, a.l2_cadence_ns, a.source),
        records,
        walls,
    ))
}

/// et's throughput mix: one directory entry, then A, A, E, X, U, P repeating on one locate, 1 us apart (no RNG).
fn itch_stream(n: u64) -> Vec<u8> {
    use itch::{Body, Header, Message, StockDirectory, encode, stock};
    let mut bytes = Vec::with_capacity(n as usize * 40);
    let hdr = |kind: u8, ts: u64| Header {
        kind,
        locate: 1,
        tracking: 0,
        ts_ns: ts,
    };
    let dir = StockDirectory {
        stock: stock("AAPL"),
        market_category: b'Q',
        financial_status: b'N',
        round_lot_size: 100,
        round_lots_only: b'N',
        issue_classification: b'C',
        issue_sub_type: *b"Z ",
        authenticity: b'P',
        short_sale_threshold: b'N',
        ipo_flag: b'N',
        luld_tier: b'1',
        etp_flag: b'N',
        etp_leverage_factor: 0,
        inverse_indicator: b'N',
    };
    encode(
        &Message {
            header: hdr(b'R', 0),
            body: Body::StockDirectory(dir),
        },
        &mut bytes,
    );
    let mut id = 1u64;
    for i in 1..n {
        let t = 34_200_000_000_000 + i * 1000;
        let (kind, body) = match i % 6 {
            0 | 1 => {
                id += 1;
                (
                    b'A',
                    Body::AddOrder {
                        order_ref: id,
                        buy_sell: if i % 2 == 0 { b'B' } else { b'S' },
                        shares: 100,
                        stock: stock("AAPL"),
                        price: 1_000_000 + (i % 50) as u32 * 100,
                        mpid: None,
                    },
                )
            }
            2 => (
                b'E',
                Body::Executed {
                    order_ref: id,
                    shares: 30,
                    match_no: i,
                },
            ),
            3 => (
                b'X',
                Body::Cancel {
                    order_ref: id,
                    shares: 20,
                },
            ),
            4 => (
                b'U',
                Body::Replace {
                    old_ref: id,
                    new_ref: id + 1_000_000_000,
                    shares: 60,
                    price: 1_000_050,
                },
            ),
            _ => (
                b'P',
                Body::Trade {
                    order_ref: 0,
                    buy_sell: b'B',
                    shares: 10,
                    stock: stock("AAPL"),
                    price: 1_000_000,
                    match_no: i,
                },
            ),
        };
        encode(
            &Message {
                header: hdr(kind, t),
                body,
            },
            &mut bytes,
        );
    }
    bytes
}

fn bench_itch(a: &BenchArgs) -> QtResult<Bench> {
    let n = a
        .synth_messages
        .ok_or_else(|| QtError::Usage("bench itch needs --synth-messages N".into()))?;
    let bytes = itch_stream(n);
    if let Some(path) = &a.write {
        write_raw(&bytes, path)?;
        return Ok(doc(format!("itch-write:{}", path.display()), n, Vec::new()));
    }
    let midnight = 1_577_682_000_000_000_000; // 2019-12-30 US/Eastern (RD-1)
    let (walls, msgs) = timed(
        a,
        || Ok(()),
        || {
            let mut r = itch::Reader::new(std::io::Cursor::new(&bytes));
            let mut m = itch::Mapper::new(midnight, None, -9)?;
            let mut out = itch::MapOutput::default();
            let mut count = 0u64;
            while let Some(b) = r.next_message()? {
                let msg = itch::parse(b).map_err(|e| QtError::InputFormat {
                    location: format!("synthetic ITCH message {count}"),
                    msg: format!("{e:?}"),
                })?;
                m.apply(&msg, &mut out)?;
                out.mbo.clear();
                out.trades.clear();
                count += 1;
            }
            std::hint::black_box(m.finish());
            Ok(count)
        },
    )?;
    Ok(doc(format!("itch:parse+map:synth{n}"), msgs, walls))
}

/// Writes the raw (uncompressed) ITCH stream; `ingest --format itch` reads raw files as well as `.gz`.
fn write_raw(bytes: &[u8], path: &Path) -> QtResult<()> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, bytes).map_err(|e| QtError::InputMissing {
        path: path.display().to_string(),
        msg: e.to_string(),
    })
}
