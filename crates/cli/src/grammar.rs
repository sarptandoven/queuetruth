//! The command table of SPEC 4.4 and its translation into clap and into spec-shaped JSON arguments.
//!
//! Every command is one row of `COMMANDS`: its path, where it runs, its flags and how each flag maps onto the
//! spec object of SPEC 4.2 (dotted field paths; `inputs[].path`-style lists and the irregular flags of SPEC 4.4 have
//! their own kinds). Flags are parsed by clap as strings and converted here, so the same table yields the help
//! text, the validation and the JSON. Options that are not spec fields (the scorer's evaluation window, job
//! polling, ...) go under the reserved key `_cli` of the arguments.

use clap::builder::PossibleValuesParser;
use clap::parser::ValueSource;
use clap::{Arg, ArgAction, ArgMatches, Command};
use serde_json::{Map, Value, json};

use crate::Implementation;

/// How a flag's string value(s) become JSON.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Kind {
    /// String as given.
    Str,
    /// Signed integer.
    Int,
    /// Floating-point number.
    Num,
    /// A fill-rate fraction: `0.02`, or percentage points with a `pp` suffix (`2pp` = 0.02).
    Frac,
    /// Present -> `true`.
    Flag,
    /// Present -> `false` (`--no-fills`).
    NoFlag,
    /// Duration string -> integer ns.
    Dur,
    /// Comma-separated durations -> [ns].
    DurList,
    /// Comma-separated strings.
    StrList,
    /// Path -> `{"path": ...}`.
    Path,
    /// Repeatable path -> `[{"path": ...}]`.
    PathAppend,
    /// Repeatable string -> `[...]`.
    StrAppend,
    /// Enumerated value (CLI spelling with hyphens, stored with underscores).
    Enum(&'static [&'static str]),
    /// `F,D,G` -> `{feed_ns, decision_ns, gateway_ns}` (merged into the target object).
    Latency,
    /// `o:w,...` -> `[{offset_ticks, weight}]`.
    Placements,
    /// `lots:N:w,median:w` -> `[{kind, lots?, weight}]`.
    Qty,
    /// `YYYY-MM-DD,...` or `blocks:0-2/5` -> WindowSpec.
    Window,
    /// `NAME[@VERSION]` -> `{name, version}`.
    Simulator,
    /// `America/New_York` | `UTC` (kept verbatim).
    Tz,
    /// Repeatable `KEY=VALUE` merged into an object (values parsed as JSON when possible).
    KeyValue,
    /// Present -> an empty object (spec defaults), e.g. `--write-l2`.
    EmptyObject,
}

/// One flag of one command.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Flag {
    /// Long name without dashes.
    pub long: &'static str,
    /// Value conversion.
    pub kind: Kind,
    /// Dotted target path in the arguments object (`-name` = handled by the command's post-processing).
    pub to: &'static str,
    /// Required.
    pub required: bool,
    /// Help text.
    pub help: &'static str,
}

const fn f(long: &'static str, kind: Kind, to: &'static str, help: &'static str) -> Flag {
    Flag {
        long,
        kind,
        to,
        required: false,
        help,
    }
}

const fn r(long: &'static str, kind: Kind, to: &'static str, help: &'static str) -> Flag {
    Flag {
        long,
        kind,
        to,
        required: true,
        help,
    }
}

/// Which global flags a command's spec accepts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Globals {
    /// Neither.
    None,
    /// `seed`.
    Seed,
    /// `seed` and `threads`.
    SeedThreads,
    /// `--seed` -> `bootstrap.seed` (scorecard).
    BootstrapSeed,
}

/// One command of SPEC 4.4.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Cmd {
    /// Command path.
    pub path: &'static [&'static str],
    /// Where it runs.
    pub imp: Implementation,
    /// One-line description.
    pub about: &'static str,
    /// Flags.
    pub flags: &'static [Flag],
    /// Positional arguments: (name, target, required).
    pub positional: &'static [(&'static str, &'static str, bool)],
    /// Global flags accepted by the spec.
    pub globals: Globals,
    /// Hidden from help.
    pub hidden: bool,
}

const DATA_FORMATS: &[&str] = &[
    "itch",
    "dbn",
    "etcsv",
    "parquet",
    "bitstamp",
    "bitfinex",
    "coinbase",
    "coinbase-level3",
    "kraken-l3",
    "hyperliquid-node",
    "tardis-l2",
    "iex-deep-plus",
];
const BBO: &[&str] = &["none", "change", "full"];
const DQ_FAIL_ON: &[&str] = &["warn", "fail", "never"];
const TRAJ: &[&str] = &["none", "ahead", "full"];
const TRADES: &[&str] = &["before", "after"];
const MODE: &[&str] = &["faithful", "corrected"];
const EXCH: &[&str] = &["partial", "no-partial"];
const ALLOCATION: &[&str] = &["equal", "proportional", "per_partition"];
const ENTRY: &[&str] = &["exchange-ack", "send-plus-latency"];
const POPULATION: &[&str] = &["shadows", "real-orders"];
const SPLIT: &[&str] = &["auto", "days", "blocks"];
const RECAL: &[&str] = &["isotonic", "none"];
const LIVE_VENUES: &[&str] = &[
    "binance",
    "bybit",
    "okx",
    "coinbase",
    "kraken",
    "deribit",
    "hyperliquid",
    "ibkr-flex",
    "fix44",
    "sim-hftbacktest",
    "sim-nautilus",
];
const REPLAY_TARGETS: &[&str] = &["hftbacktest", "nautilus"];
const FIX_CHECKSUM: &[&str] = &["exclude", "keep"];
const FDR: &[&str] = &["bh", "by"];
const BLOCK: &[&str] = &["auto", "day", "30min"];
const SIM_EXACT: &[&str] = &["exact"];
const ALLOC_FAMILIES: &[&str] = &["power", "power2", "power3", "databento"];
const SURVIVAL_FAMILIES: &[&str] = &["databento"];
const LIVE_FAMILIES: &[&str] = &[
    "power",
    "power2",
    "power3",
    "databento",
    "nt-prob-touch",
    "nt-queue-l2-prob-touch",
];
const SIDES: &[&str] = &["B", "A"];
const SYNTH_VENUES: &[&str] = &["cme", "nasdaq"];
const BENCH_MODES: &[&str] = &["book", "decode", "sinks", "rank-oracle"];
const BENCH_SOURCES: &[&str] = &["parquet", "memory"];
const FILL_RULES: &[&str] = &["exact", "et"];
const HIDDEN_POLICIES: &[&str] = &["ignore", "behind", "behind-and-through"];
const MARKOUT_REFS: &[&str] = &["mid", "microprice"];

const LATENCY: Flag = f(
    "latency",
    Kind::Latency,
    "latency",
    "shadow latency FEED,DECISION,GATEWAY (durations)",
);
const JITTER: Flag = f(
    "jitter-ns",
    Kind::Dur,
    "latency.jitter_ns",
    "maximum uniform latency jitter",
);
const L2_DEPTH: Flag = f(
    "l2-depth",
    Kind::Int,
    "l2.depth_levels",
    "L2 view depth (0 = all levels)",
);
const L2_CADENCE: Flag = f(
    "l2-cadence-ns",
    Kind::Dur,
    "l2.cadence_ns",
    "L2 conflation cadence (0 = per event)",
);
const L2_TRADES: Flag = f(
    "l2-trades",
    Kind::Enum(TRADES),
    "l2.trade_position",
    "trades before or after depth",
);
const DATA: Flag = r("data", Kind::Path, "dataset", "dataset directory");
const MODE_F: Flag = f("mode", Kind::Enum(MODE), "mode", "zoo mode");
const EXCH_F: Flag = f("exchange", Kind::Enum(EXCH), "exchange", "hftbacktest exchange model");
const HORIZONS: Flag = f(
    "horizons",
    Kind::DurList,
    "horizons_ns",
    "evaluation horizons (durations)",
);
const H_MAX: Flag = f("h-max", Kind::Dur, "h_max_ns", "maximum horizon");
const PARTICLES: Flag = f("particles", Kind::Int, "particles", "particles of Monte Carlo models");
const TRAIN: Flag = f("train", Kind::Window, "train", "train window: dates or blocks:I-J/N");
const TEST: Flag = f("test", Kind::Window, "test", "test window: dates or blocks:I-J/N");
const BOOT_B: Flag = f("bootstrap-b", Kind::Int, "bootstrap.b", "bootstrap resamples");
const FORCE: Flag = f(
    "force",
    Kind::Flag,
    "force",
    "export even when the calibration is refused (no events, degenerate, loses to the default)",
);
const BOOT_BLOCK: Flag = f(
    "bootstrap-block",
    Kind::Enum(BLOCK),
    "bootstrap.block",
    "bootstrap block",
);

const CALIB_FLAGS_TAIL: [Flag; 13] = [
    DATA,
    r("shadows", Kind::Path, "shadows", "shadow spec file"),
    r("out", Kind::Str, "out", "output directory"),
    TRAIN,
    TEST,
    MODE_F,
    EXCH_F,
    L2_DEPTH,
    L2_CADENCE,
    L2_TRADES,
    BOOT_B,
    BOOT_BLOCK,
    PARTICLES,
];

const fn calib_flags(family: Flag) -> [Flag; 16] {
    let t = CALIB_FLAGS_TAIL;
    [
        family, t[0], t[1], t[2], t[3], t[4], t[5], t[6], t[7], t[8], t[9], t[10], t[11], t[12], HORIZONS, H_MAX,
    ]
}

const CAL_ALLOC: [Flag; 16] = calib_flags(r("family", Kind::Enum(ALLOC_FAMILIES), "family", "family to fit"));
const CAL_SURV: [Flag; 16] = calib_flags(r("family", Kind::Enum(SURVIVAL_FAMILIES), "family", "family to fit"));
const CAL_NT: [Flag; 16] = calib_flags(f(
    "queue-gated",
    Kind::Flag,
    "-queue_gated",
    "fit nt_queue_l2_prob_touch instead of nt_prob_touch",
));

/// Every command of SPEC 4.4, including `fixtures synth` and `calibrate-live`.
pub(crate) const COMMANDS: &[Cmd] = &[
    Cmd {
        path: &["ingest"],
        imp: Implementation::Native,
        about: "ingest source files into a normalized dataset",
        flags: &[
            r(
                "format",
                Kind::Enum(DATA_FORMATS),
                "format",
                "source format (also --input-format)",
            ),
            r("in", Kind::PathAppend, "inputs", "input file (repeatable)"),
            r(
                "venue",
                Kind::Str,
                "venue",
                "venue rule set (nasdaq, cme, coinbase, crypto_l3, iex, generic; generic resolves to crypto_l3 for the \
                 crypto L3 formats: bitstamp, bitfinex, coinbase, coinbase-level3, kraken-l3, hyperliquid-node)",
            ),
            r("out", Kind::Str, "out", "output dataset directory"),
            f("symbols", Kind::StrList, "symbols", "symbol filter"),
            f("date", Kind::Str, "session_date", "session date YYYY-MM-DD"),
            f(
                "midnight-ns",
                Kind::Int,
                "midnight_utc_ns",
                "local midnight as UTC ns (etcsv)",
            ),
            f("tz", Kind::Tz, "tz", "session time zone: America/New_York or UTC"),
            f("instruments", Kind::Path, "instruments", "etcsv instruments.csv"),
            f("price-exp", Kind::Int, "price_exp", "price exponent"),
            f("size-exp", Kind::Int, "size_exp", "size exponent"),
            f("tick-size", Kind::Str, "tick_size", "tick size (decimal)"),
            f("lot-size", Kind::Str, "lot_size", "lot size (decimal)"),
            f("source-venue", Kind::Str, "source_venue", "source venue label"),
            f(
                "option",
                Kind::KeyValue,
                "options",
                "format option KEY=VALUE, VALUE parsed as JSON when it parses (repeatable; e.g. depth, \
                 checksum_precision for kraken-l3; snapshot_block, max_block_gap_ms for hyperliquid-node)",
            ),
            f(
                "strict",
                Kind::Flag,
                "strict",
                "exit 1 unless the data-quality verdict is clean",
            ),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["replay"],
        imp: Implementation::Native,
        about: "exact replay with optional shadows, rank oracle, parity and L2 output",
        flags: &[
            DATA,
            r("out", Kind::Str, "out", "output directory"),
            f(
                "shadows",
                Kind::Path,
                "shadows",
                "shadow spec file (Parquet or et shadows.csv)",
            ),
            f("amends", Kind::Path, "amends", "shadow amend file"),
            LATENCY,
            JITTER,
            f("bbo", Kind::Enum(BBO), "bbo", "BBO output"),
            f("depth", Kind::Int, "depth_levels", "depth levels to write (0 = none)"),
            f("rank-oracle", Kind::Flag, "rank_oracle", "queue-rank oracle"),
            f("sweep-gap-ns", Kind::Dur, "sweep_gap_ns", "sweep grouping gap"),
            f("trajectory", Kind::Enum(TRAJ), "trajectory", "queue trajectory output"),
            f("race-window-ns", Kind::Dur, "race_window_ns", "race window"),
            f(
                "real-orders",
                Kind::Flag,
                "real_orders",
                "extract real-order lifecycles",
            ),
            L2_DEPTH,
            L2_CADENCE,
            L2_TRADES,
            f("write-l2", Kind::EmptyObject, "l2", "write the synthesized L2 view"),
            f(
                "parity-trades",
                Kind::Flag,
                "parity_trades",
                "compare derived trades with source prints",
            ),
            f("parity-bbo", Kind::Path, "parity_bbo", "et bbo.csv reference"),
            f(
                "venue",
                Kind::Str,
                "venue",
                "rule set override (nasdaq, cme, coinbase, crypto_l3, iex, generic; default the manifest's rule_set)",
            ),
            f(
                "strict",
                Kind::Flag,
                "strict",
                "exit 1 on any invariant, anomaly or parity mismatch",
            ),
            f(
                "fill-rule",
                Kind::Enum(FILL_RULES),
                "fill_rule",
                "shadow fill rule: exact (audit rulings) or et (parity with the earlier et prototype, RD-5)",
            ),
            f(
                "hidden-print-policy",
                Kind::Enum(HIDDEN_POLICIES),
                "hidden_print_policy",
                "hidden prints (default: behind-and-through on nasdaq, ignore elsewhere)",
            ),
            f(
                "exclude-order-ids",
                Kind::Path,
                "_cli.exclude_order_ids",
                "real orders removed from the tape (A1): .json list, or one id / instrument_id,order_id per line",
            ),
        ],
        positional: &[],
        globals: Globals::SeedThreads,
        hidden: false,
    },
    Cmd {
        path: &["shadows", "sample"],
        imp: Implementation::Native,
        about: "seeded random shadow orders",
        flags: &[
            DATA,
            r("n", Kind::Int, "n", "number of shadows"),
            r("out", Kind::Str, "out", "output shadow spec file"),
            f("placements", Kind::Placements, "placements", "OFFSET:WEIGHT,..."),
            f("qty", Kind::Qty, "qty", "lots:N:WEIGHT,median:WEIGHT"),
            f("lifetime", Kind::Dur, "lifetime_ns", "shadow lifetime"),
            f("latency-ns", Kind::Dur, "latency_ns", "shadow latency"),
            f(
                "window-open-s",
                Kind::Int,
                "window_open_s",
                "seconds after the open excluded",
            ),
            f(
                "window-close-s",
                Kind::Int,
                "window_close_s",
                "seconds before the close excluded",
            ),
            f(
                "allocation",
                Kind::Enum(ALLOCATION),
                "allocation",
                "allocation across instrument-days",
            ),
            f("sides", Kind::StrList, "sides", "sides to sample (B,A)"),
        ],
        positional: &[],
        globals: Globals::Seed,
        hidden: false,
    },
    Cmd {
        path: &["shadows", "mirror-real"],
        imp: Implementation::Native,
        about: "shadows joining directly behind sampled real orders",
        flags: &[
            DATA,
            r("out", Kind::Str, "out", "output shadow spec file"),
            f("n", Kind::Int, "n", "number of shadows"),
            f(
                "no-stratify",
                Kind::NoFlag,
                "stratify",
                "do not stratify by instrument and outcome",
            ),
        ],
        positional: &[],
        globals: Globals::Seed,
        hidden: false,
    },
    Cmd {
        path: &["shadows", "from-live"],
        imp: Implementation::Python,
        about: "shadow schedule from a canonical live log",
        flags: &[
            r("live", Kind::Str, "live", "canonical live-log directory"),
            r("out", Kind::Str, "out", "output shadow spec file"),
            f("entry", Kind::Enum(ENTRY), "entry", "entry time rule"),
            f("latency-ns", Kind::Dur, "latency_ns", "latency for send-plus-latency"),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["zoo"],
        imp: Implementation::Hybrid,
        about: "run queue models on the L2 view and compare with exact truth",
        flags: &[
            DATA,
            r("shadows", Kind::Path, "shadows", "shadow spec file"),
            r("out", Kind::Str, "out", "output directory"),
            f("models", Kind::StrList, "models", "all or model ids"),
            MODE_F,
            EXCH_F,
            HORIZONS,
            H_MAX,
            L2_DEPTH,
            L2_CADENCE,
            L2_TRADES,
            PARTICLES,
            f(
                "no-fills",
                Kind::NoFlag,
                "write_fills",
                "do not write zoo_fills.parquet",
            ),
            f(
                "write-trajectory",
                Kind::Flag,
                "write_trajectory",
                "write zoo_trajectory.parquet",
            ),
            LATENCY,
            JITTER,
            f("bootstrap-b", Kind::Int, "bootstrap_b", "bootstrap resamples"),
            f(
                "amends",
                Kind::Path,
                "amends",
                "qt.shadow_amend file of the shadows (priority-keeping size reductions)",
            ),
            f(
                "exclude-order-ids",
                Kind::Path,
                "_cli.exclude_order_ids",
                "real orders removed from the tape and the L2 view (A1): .json list, or one id / \
                 instrument_id,order_id per line",
            ),
            f(
                "sort-horizon",
                Kind::Dur,
                "_cli.sort_horizon_ns",
                "leaderboard sort horizon in the terminal output (default 60s)",
            ),
        ],
        positional: &[],
        globals: Globals::SeedThreads,
        hidden: false,
    },
    Cmd {
        path: &["calibrate", "alloc"],
        imp: Implementation::Hybrid,
        about: "fit n (power families) or k (databento) by allocation quasi-likelihood",
        flags: &CAL_ALLOC,
        positional: &[],
        globals: Globals::SeedThreads,
        hidden: false,
    },
    Cmd {
        path: &["calibrate", "survival"],
        imp: Implementation::Hybrid,
        about: "fit databento_mc k by survival likelihood",
        flags: &CAL_SURV,
        positional: &[],
        globals: Globals::SeedThreads,
        hidden: false,
    },
    Cmd {
        path: &["calibrate", "nt-touch"],
        imp: Implementation::Hybrid,
        about: "fit the NautilusTrader prob_fill_on_limit p",
        flags: &CAL_NT,
        positional: &[],
        globals: Globals::SeedThreads,
        hidden: false,
    },
    Cmd {
        path: &["calibrate", "latency"],
        imp: Implementation::Python,
        about: "latency distributions from a live log",
        flags: &[
            r("live", Kind::Path, "live", "canonical live-log directory"),
            r("out", Kind::Str, "out", "output directory"),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["calibrate-live"],
        imp: Implementation::Python,
        about: "fit a zoo parameter to your own live fills (L2 or MBO tape)",
        flags: &[
            DATA,
            r("live", Kind::Path, "live", "canonical live-log directory"),
            r("family", Kind::Enum(LIVE_FAMILIES), "family", "family to fit"),
            f("out", Kind::Str, "out", "output directory"),
            f("entry", Kind::Enum(ENTRY), "entry", "entry time rule"),
            LATENCY,
            JITTER,
            MODE_F,
            EXCH_F,
            L2_DEPTH,
            L2_CADENCE,
            L2_TRADES,
            HORIZONS,
            f("window", Kind::Dur, "window_ns", "evaluation window H"),
            TRAIN,
            TEST,
            PARTICLES,
            BOOT_B,
            BOOT_BLOCK,
            f(
                "match-window-ns",
                Kind::Dur,
                "match_window_ns",
                "own-order twin match window (default 1ms)",
            ),
            f(
                "no-self-exclude",
                Kind::NoFlag,
                "self_exclude",
                "keep the live orders' own records (MBO) or resting size (L2 depth) in the tape",
            ),
        ],
        positional: &[],
        globals: Globals::SeedThreads,
        hidden: false,
    },
    Cmd {
        path: &["features"],
        imp: Implementation::Native,
        about: "landmark features and exact outcomes for the learned model",
        flags: &[
            DATA,
            r("out", Kind::Str, "out", "output directory"),
            f("shadows", Kind::Path, "shadows", "shadow spec file"),
            f(
                "population",
                Kind::Enum(POPULATION),
                "population",
                "shadows or real orders",
            ),
            f("n-real", Kind::Int, "n_real_orders", "number of real orders"),
            f("landmarks", Kind::DurList, "landmarks_ns", "landmarks (durations)"),
            f(
                "cancel-away-ticks",
                Kind::Int,
                "cancel_away_ticks",
                "policy cancel distance in ticks (0 = never cancel, e.g. for the zoo comparison of fit-hazard)",
            ),
            H_MAX,
            f("no-privileged", Kind::NoFlag, "privileged", "omit exact-queue columns"),
            f("trackers", Kind::StrList, "trackers", "tracker model ids"),
            L2_DEPTH,
            L2_CADENCE,
            L2_TRADES,
        ],
        positional: &[],
        globals: Globals::SeedThreads,
        hidden: false,
    },
    Cmd {
        path: &["fit-hazard"],
        imp: Implementation::Python,
        about: "fit the discrete-time competing-risk hazard model",
        flags: &[
            r("features", Kind::Path, "features", "features directory or file"),
            r("out", Kind::Str, "out", "output directory"),
            f("split", Kind::Enum(SPLIT), "split.scheme", "split scheme"),
            f("n-blocks", Kind::Int, "split.n_blocks", "blocks per day"),
            f("embargo", Kind::Dur, "split.embargo_ns", "embargo"),
            f(
                "privileged",
                Kind::Flag,
                "privileged",
                "use exact-queue features (upper bound)",
            ),
            f("recalibration", Kind::Enum(RECAL), "recalibration", "recalibration"),
            f("grid", Kind::DurList, "grid_ns", "time grid (durations)"),
            f("bootstrap-b", Kind::Int, "bootstrap_b", "bootstrap resamples"),
            f(
                "zoo-predictions",
                Kind::Path,
                "zoo_predictions",
                "qt.zoo_predictions for zoo_comparison",
            ),
            f(
                "zoo-shadows",
                Kind::Path,
                "zoo_shadows",
                "qt.zoo_shadows of those predictions (default: zoo_shadows.parquet next to them)",
            ),
        ],
        positional: &[],
        globals: Globals::Seed,
        hidden: false,
    },
    Cmd {
        path: &["live", "import"],
        imp: Implementation::Python,
        about: "venue exports -> canonical live log",
        flags: &[
            r("venue", Kind::Enum(LIVE_VENUES), "venue", "venue"),
            r("in", Kind::PathAppend, "inputs", "input file (repeatable)"),
            r("out", Kind::Str, "out", "output live-log directory"),
            f("account", Kind::Str, "account", "account label"),
            f("instrument-map", Kind::Path, "instrument_map", "instrument_map.csv"),
            f("append", Kind::Flag, "append", "append to an existing log"),
            f(
                "tz",
                Kind::Str,
                "tz",
                "account time zone of venue-local times, e.g. IBKR Flex (IANA name, default UTC)",
            ),
            f(
                "fix-delimiter",
                Kind::Str,
                "fix.delimiter",
                "FIX field delimiter: auto (default), SOH or |",
            ),
            f(
                "fix-local-ts-regex",
                Kind::Str,
                "fix.local_ts_regex",
                "regex with one group capturing the local receive time in each FIX log line",
            ),
            f(
                "fix-venue-name",
                Kind::Str,
                "fix.venue_name",
                "venue name for a FIX drop copy (default fix44; used for instrument-map lookups)",
            ),
            f(
                "fix-checksum",
                Kind::Enum(FIX_CHECKSUM),
                "fix.checksum",
                "messages failing CheckSum(10)/BodyLength(9): exclude (default, with a warning) or keep",
            ),
            f(
                "fix-data-dictionary",
                Kind::StrAppend,
                "fix.data_dictionary",
                "QuickFIX data dictionary XML (custom tags and enums; repeatable)",
            ),
            f(
                "fix-session-venue",
                Kind::KeyValue,
                "fix.session_venues",
                "SESSION=VENUE: venue of a FIX session (file stem or counterparty CompID; repeatable)",
            ),
            f(
                "fix-instrument-tag",
                Kind::Int,
                "fix.instrument_tag",
                "tag holding the instrument instead of Symbol(55), e.g. 107",
            ),
            f(
                "option",
                Kind::KeyValue,
                "_cli.options",
                "simulator importer option KEY=VALUE (sim-* venues: venue, size_exp, key_from_client_id)",
            ),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["live", "validate"],
        imp: Implementation::Python,
        about: "validate a canonical live log",
        flags: &[r("live", Kind::Str, "live", "canonical live-log directory")],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["score"],
        imp: Implementation::Python,
        about: "score a simulator against the live log",
        flags: &[
            r("live", Kind::Path, "live", "canonical live-log directory"),
            r("out", Kind::Str, "out", "output directory"),
            f(
                "sim-log",
                Kind::Path,
                "-sim_log",
                "simulator output as a canonical live log",
            ),
            f("sim", Kind::Enum(SIM_EXACT), "-sim", "exact replay arm"),
            f("sim-model", Kind::Str, "-sim_model", "zoo model arm"),
            f("data", Kind::Path, "dataset", "dataset directory"),
            f("entry", Kind::Enum(ENTRY), "entry", "entry time rule"),
            LATENCY,
            JITTER,
            f(
                "markout-horizons",
                Kind::DurList,
                "markout_horizons_ns",
                "markout horizons",
            ),
            f(
                "markout-refs",
                Kind::StrList,
                "markout_refs",
                "markout references (mid,microprice)",
            ),
            f(
                "ttf-horizons",
                Kind::DurList,
                "ttf_horizons_ns",
                "time-to-fill horizons",
            ),
            BOOT_B,
            BOOT_BLOCK,
            f("fdr", Kind::Enum(FDR), "fdr.method", "FDR method"),
            f("q", Kind::Num, "fdr.q", "FDR level"),
            f(
                "fill-rate-margin",
                Kind::Num,
                "equivalence.fill_rate_margin",
                "TOST fill-rate margin",
            ),
            f(
                "markout-margin-ticks",
                Kind::Num,
                "equivalence.markout_margin_ticks",
                "TOST markout margin",
            ),
            f(
                "markout-horizon",
                Kind::Dur,
                "equivalence.markout_horizon_ns",
                "TOST markout horizon",
            ),
            f(
                "fail-abs-bias",
                Kind::Frac,
                "gates.max_abs_fill_rate_bias",
                "exit 1 if |fill-rate bias| exceeds X (a fraction, e.g. 0.02 = 2 pp; or percentage points: 2pp)",
            ),
            f(
                "fail-not-equivalent",
                Kind::Flag,
                "gates.require_equivalence",
                "exit 1 unless equivalent",
            ),
            f("perm-b", Kind::Int, "perm_b", "permutation resamples"),
            L2_DEPTH,
            L2_CADENCE,
            L2_TRADES,
            f(
                "window",
                Kind::Dur,
                "_cli.window_ns",
                "evaluation window H (default 60s)",
            ),
            f(
                "match-window-ns",
                Kind::Dur,
                "_cli.match_window_ns",
                "own-order twin match window",
            ),
            f(
                "no-self-exclude",
                Kind::NoFlag,
                "_cli.self_exclude",
                "keep the live orders' own records in the exact tape",
            ),
        ],
        positional: &[],
        globals: Globals::BootstrapSeed,
        hidden: false,
    },
    Cmd {
        path: &["report"],
        imp: Implementation::Python,
        about: "static HTML report from output directories",
        flags: &[
            r("in", Kind::StrAppend, "inputs", "output directory (repeatable)"),
            r("out", Kind::Str, "out", "output HTML file"),
            f("title", Kind::Str, "title", "report title"),
            f("max-why", Kind::Int, "max_why", "maximum why traces"),
            f(
                "sort-horizon",
                Kind::Dur,
                "leaderboard_sort_horizon_ns",
                "zoo leaderboard sort horizon (default 60s)",
            ),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["dq"],
        imp: Implementation::Python,
        about: "data-quality checks of a dataset: JSON and HTML report",
        flags: &[
            r("data", Kind::Path, "dataset", "dataset directory"),
            r(
                "out",
                Kind::Str,
                "out",
                "output directory (dq_report.json, dq_report.html)",
            ),
            f(
                "compare",
                Kind::Path,
                "compare",
                "second dataset of the same sessions (cross-source parity)",
            ),
            f("checks", Kind::StrList, "checks", "run only these check ids"),
            f(
                "fail-on",
                Kind::Enum(DQ_FAIL_ON),
                "fail_on",
                "exit 1 when a check reaches this status (default warn)",
            ),
            f(
                "max-evidence",
                Kind::Int,
                "max_evidence",
                "evidence rows kept per table (default 20)",
            ),
            f(
                "stale-ns",
                Kind::Dur,
                "stale_ns",
                "quiet book interval with prints that counts as stale (default 60s)",
            ),
            f(
                "latency-outlier-ns",
                Kind::Dur,
                "latency_outlier_ns",
                "ts_recv - ts_event outlier threshold (default 10 x p99, at least 1ms)",
            ),
            f(
                "session-tolerance-ns",
                Kind::Dur,
                "session_tolerance_ns",
                "slack at the session open and close (default 5m)",
            ),
            f(
                "no-replay",
                Kind::NoFlag,
                "replay",
                "skip the exact replay (no crossed-book, print-through or trade-parity checks)",
            ),
            f(
                "verify-sources",
                Kind::Flag,
                "verify_sources",
                "re-hash the raw input files recorded in the manifest",
            ),
            f("title", Kind::Str, "title", "HTML report title"),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["referee", "run"],
        imp: Implementation::Hybrid,
        about: "exact-FIFO regression oracle for another simulator",
        flags: &[
            DATA,
            r(
                "schedule",
                Kind::Path,
                "schedule",
                "shadow spec file of the simulator's orders",
            ),
            r(
                "sim-fills",
                Kind::Path,
                "sim_fills",
                "simulator fills (live-log dir or qt.shadow_fills)",
            ),
            r("out", Kind::Str, "out", "output directory"),
            f("simulator", Kind::Simulator, "simulator", "NAME[@VERSION]"),
            f(
                "max-discordant",
                Kind::Int,
                "tolerance.max_discordant",
                "tolerated discordant orders",
            ),
            f(
                "max-qty-mismatch",
                Kind::Int,
                "tolerance.max_qty_mismatch",
                "tolerated qty mismatches",
            ),
            f(
                "max-ttf-err-ns",
                Kind::Dur,
                "tolerance.max_abs_ttf_err_ns",
                "tolerated |ttf error|",
            ),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["referee", "fixtures"],
        imp: Implementation::Native,
        about: "write the synthetic referee bug-class fixtures",
        flags: &[r("out", Kind::Str, "out", "output directory")],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["fixtures", "synth"],
        imp: Implementation::Native,
        about: "write the synthetic quickstart dataset (with truth and own-order inputs)",
        flags: &[
            r("out", Kind::Str, "out", "output directory"),
            f("venue", Kind::Enum(SYNTH_VENUES), "venue", "cme (default) or nasdaq"),
            f("sessions", Kind::Int, "sessions", "session dates (default 2)"),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["why"],
        imp: Implementation::Native,
        about: "event-by-event trace of one shadow or live order",
        flags: &[
            DATA,
            f("shadows", Kind::Path, "shadows", "shadow spec file"),
            f("shadow-id", Kind::Str, "shadow_id", "shadow id"),
            f("live", Kind::Path, "live", "canonical live-log directory"),
            f("order-key", Kind::Str, "order_key", "live order key"),
            f("max-events", Kind::Int, "max_events", "maximum events"),
            f("out", Kind::Str, "out", "output JSON file"),
            f(
                "model",
                Kind::Str,
                "_cli.model",
                "zoo model whose queue estimate is shown next to the exact queue",
            ),
            f(
                "zoo",
                Kind::Str,
                "_cli.zoo",
                "zoo output directory to read the model trajectory from (with --write-trajectory)",
            ),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["export", "hftbacktest"],
        imp: Implementation::Python,
        about: "hftbacktest config snippet from a calibration",
        flags: &[
            r("calibration", Kind::Path, "calibration", "calibration.json"),
            f("out", Kind::Str, "out", "output file"),
            FORCE,
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["export", "nautilus"],
        imp: Implementation::Python,
        about: "NautilusTrader config snippet from a calibration",
        flags: &[
            r("calibration", Kind::Path, "calibration", "calibration.json"),
            f("out", Kind::Str, "out", "output file"),
            FORCE,
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["export", "replay-schedule"],
        imp: Implementation::Python,
        about: "strategy / loop that replays a live order schedule in NautilusTrader or hftbacktest",
        flags: &[
            r(
                "target",
                Kind::Enum(REPLAY_TARGETS),
                "target",
                "simulator to replay the schedule in",
            ),
            f(
                "schedule",
                Kind::Path,
                "schedule",
                "qt.shadow_spec schedule (score's schedule.parquet or shadows from-live)",
            ),
            f("amends", Kind::Path, "amends", "qt.shadow_amend file of the schedule"),
            f(
                "live",
                Kind::Path,
                "live",
                "canonical live-log directory (the schedule when --schedule is not given; units, post-only flags)",
            ),
            f(
                "data",
                Kind::Path,
                "dataset",
                "dataset directory (tick and lot sizes of the instruments)",
            ),
            f(
                "entry",
                Kind::Enum(ENTRY),
                "entry",
                "entry time rule when the schedule is built from --live",
            ),
            r("out", Kind::Str, "out", "output file"),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["export", "l2-npz"],
        imp: Implementation::Native,
        about: "hftbacktest .npz event file of one instrument-day",
        flags: &[
            DATA,
            r("instrument-id", Kind::Int, "instrument_id", "instrument id"),
            r("date", Kind::Str, "session_date", "session date YYYY-MM-DD"),
            r("out", Kind::Str, "out", "output .npz file"),
            L2_DEPTH,
            L2_CADENCE,
            L2_TRADES,
            f(
                "feed-latency-ns",
                Kind::Dur,
                "feed_latency_ns",
                "local_ts = ts_recv + latency",
            ),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["export", "latency-npz"],
        imp: Implementation::Python,
        about: "IntpOrderLatency .npz from a live log",
        flags: &[
            r("live", Kind::Path, "live", "canonical live-log directory"),
            r("out", Kind::Str, "out", "output .npz file"),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["jobs", "submit"],
        imp: Implementation::Python,
        about: "submit a job to the HTTP API (QT_API_URL, QT_API_KEY)",
        flags: &[
            r("spec", Kind::Str, "spec", "spec JSON file"),
            f("idempotency-key", Kind::Str, "idempotency_key", "idempotency key"),
            f("wait", Kind::Flag, "wait", "wait for a terminal state"),
            f("timeout", Kind::Num, "timeout", "wait timeout in seconds"),
        ],
        positional: &[("KIND", "kind", true)],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["jobs", "status"],
        imp: Implementation::Python,
        about: "show a job",
        flags: &[f("timeout", Kind::Num, "timeout", "request timeout in seconds")],
        positional: &[("ID", "id", true)],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["jobs", "wait"],
        imp: Implementation::Python,
        about: "wait for a job to finish",
        flags: &[f("timeout", Kind::Num, "timeout", "wait timeout in seconds")],
        positional: &[("ID", "id", true)],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["jobs", "files"],
        imp: Implementation::Python,
        about: "list a job's output files",
        flags: &[f("timeout", Kind::Num, "timeout", "request timeout in seconds")],
        positional: &[("ID", "id", true)],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["jobs", "download"],
        imp: Implementation::Python,
        about: "download a job's output files (sha256-verified)",
        flags: &[
            f("timeout", Kind::Num, "timeout", "request timeout in seconds"),
            f("out", Kind::Str, "out", "output directory (default .)"),
        ],
        positional: &[("ID", "id", true)],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["schema", "list"],
        imp: Implementation::Native,
        about: "list JSON document schemas and Arrow tables",
        flags: &[],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["schema", "show"],
        imp: Implementation::Native,
        about: "print a JSON Schema (qt.<doc>) or an Arrow table schema (qt.<table>)",
        flags: &[],
        positional: &[("NAME", "name", true)],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["doctor"],
        imp: Implementation::Hybrid,
        about: "check the installation",
        flags: &[],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["mcp", "serve"],
        imp: Implementation::Python,
        about: "MCP stdio server",
        flags: &[
            f(
                "allow-submit",
                Kind::Flag,
                "allow_submit",
                "list the job-submitting tools",
            ),
            f("local", Kind::Flag, "local", "only the local read tools"),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: false,
    },
    Cmd {
        path: &["bench", "replay"],
        imp: Implementation::Native,
        about: "replay throughput benchmark",
        flags: &[
            f("data", Kind::Str, "data", "dataset directory (or --synth-records)"),
            f("repeat", Kind::Int, "repeat", "timed repetitions (default 3)"),
            f("warmup", Kind::Int, "warmup", "untimed repetitions first (default 1)"),
            f(
                "mode",
                Kind::Enum(BENCH_MODES),
                "mode",
                "book (default), decode, sinks or rank-oracle",
            ),
            f(
                "source",
                Kind::Enum(BENCH_SOURCES),
                "source",
                "parquet (decode timed, default) or memory",
            ),
            f("shadows", Kind::Str, "shadows", "shadow spec file (mode book)"),
            f(
                "synth-shadows",
                Kind::Int,
                "synth_shadows",
                "synthetic touch shadows (mode book)",
            ),
            f(
                "trajectory",
                Kind::Enum(TRAJ),
                "trajectory",
                "trajectory mode (mode book)",
            ),
            f(
                "synth-records",
                Kind::Int,
                "synth_records",
                "generate a synthetic dataset of about N records",
            ),
            f(
                "out",
                Kind::Str,
                "out",
                "output directory (modes sinks, rank-oracle; removed after each run)",
            ),
            f(
                "scratch",
                Kind::Str,
                "scratch",
                "where --synth-records datasets are written (reused)",
            ),
        ],
        positional: &[],
        globals: Globals::Seed,
        hidden: true,
    },
    Cmd {
        path: &["bench", "l2"],
        imp: Implementation::Native,
        about: "L2 synthesis throughput benchmark",
        flags: &[
            f("data", Kind::Str, "data", "dataset directory (or --synth-records)"),
            f("repeat", Kind::Int, "repeat", "timed repetitions (default 3)"),
            f("warmup", Kind::Int, "warmup", "untimed repetitions first (default 1)"),
            f(
                "source",
                Kind::Enum(BENCH_SOURCES),
                "source",
                "parquet (default) or memory",
            ),
            f(
                "synth-records",
                Kind::Int,
                "synth_records",
                "generate a synthetic dataset of about N records",
            ),
            f(
                "scratch",
                Kind::Str,
                "scratch",
                "where --synth-records datasets are written (reused)",
            ),
            f("l2-depth", Kind::Int, "l2_depth", "L2 view depth (default 10)"),
            f("l2-cadence-ns", Kind::Dur, "l2_cadence_ns", "L2 cadence (default 0)"),
        ],
        positional: &[],
        globals: Globals::Seed,
        hidden: true,
    },
    Cmd {
        path: &["bench", "itch"],
        imp: Implementation::Native,
        about: "ITCH parse + map throughput benchmark",
        flags: &[
            f("synth-messages", Kind::Int, "synth_messages", "synthetic ITCH messages"),
            f(
                "write",
                Kind::Str,
                "write",
                "write the raw ITCH stream to FILE instead of timing",
            ),
            f("repeat", Kind::Int, "repeat", "timed repetitions (default 3)"),
            f("warmup", Kind::Int, "warmup", "untimed repetitions first (default 1)"),
        ],
        positional: &[],
        globals: Globals::None,
        hidden: true,
    },
];

/// Group descriptions for two-level commands.
const GROUPS: &[(&str, &str)] = &[
    ("shadows", "shadow schedules"),
    (
        "calibrate",
        "calibrate queue-model parameters against exact truth, or latency",
    ),
    ("live", "canonical live order logs"),
    ("referee", "exact-FIFO referee"),
    ("fixtures", "synthetic fixtures"),
    ("export", "config snippets and npz files"),
    ("jobs", "HTTP API jobs"),
    ("schema", "published schemas"),
    ("mcp", "MCP server"),
    ("bench", "benchmarks"),
];

fn flag_arg(fl: &Flag) -> Arg {
    let mut a = Arg::new(fl.long).long(fl.long).help(fl.help).required(fl.required);
    a = match fl.kind {
        Kind::Flag | Kind::NoFlag | Kind::EmptyObject => a.action(ArgAction::SetTrue),
        Kind::PathAppend | Kind::StrAppend | Kind::KeyValue => a.action(ArgAction::Append).allow_hyphen_values(true),
        _ => a.action(ArgAction::Set).allow_hyphen_values(true),
    };
    let name = match fl.kind {
        Kind::Path | Kind::PathAppend => Some("PATH"),
        Kind::Dur => Some("DURATION"),
        Kind::DurList => Some("D,..."),
        Kind::Int => Some("N"),
        Kind::Num | Kind::Frac => Some("X"),
        Kind::Str | Kind::StrAppend => Some("VALUE"),
        Kind::StrList => Some("A,..."),
        Kind::Window => Some("WINDOW"),
        Kind::Latency => Some("F,D,G"),
        Kind::Placements => Some("O:W,..."),
        Kind::Qty => Some("RULES"),
        Kind::Simulator => Some("NAME[@VER]"),
        Kind::Tz => Some("TZ"),
        Kind::KeyValue => Some("KEY=VALUE"),
        Kind::Enum(vals) => {
            a = a.value_parser(PossibleValuesParser::new(vals.iter().copied()));
            None
        }
        Kind::Flag | Kind::NoFlag | Kind::EmptyObject => None,
    };
    if let Some(n) = name {
        a = a.value_name(n);
    }
    if fl.long == "format" {
        a = a.alias("input-format");
    }
    a
}

fn leaf(cmd: &Cmd, name: &'static str) -> Command {
    let mut c = Command::new(name).about(cmd.about).hide(cmd.hidden);
    for fl in cmd.flags {
        c = c.arg(flag_arg(fl));
    }
    for (pname, _, req) in cmd.positional {
        c = c.arg(Arg::new(*pname).required(*req).action(ArgAction::Set));
    }
    c
}

/// The clap command tree (program name `bin`).
pub(crate) fn command(bin: &'static str) -> Command {
    let mut root = Command::new(bin)
        .version(qt_schema::ENGINE_VERSION)
        .about("queuetruth: fill-realism and queue-truth kit")
        .subcommand_required(true)
        .arg_required_else_help(true)
        .arg(
            Arg::new("output_format")
                .display_order(1000)
                .long("output-format")
                .global(true)
                .value_name("human|json")
                .value_parser(["human", "json"])
                .help("output format (also --format human|json)"),
        )
        .arg(
            Arg::new("log_level")
                .display_order(1000)
                .long("log-level")
                .global(true)
                .value_parser(["error", "warn", "info", "debug"])
                .help("stderr log level (default warn)"),
        )
        .arg(
            Arg::new("threads")
                .display_order(1000)
                .long("threads")
                .global(true)
                .value_parser(clap::value_parser!(u32).range(1..))
                .help("worker threads (outputs do not depend on it; default 1)"),
        )
        .arg(
            Arg::new("seed")
                .display_order(1000)
                .long("seed")
                .global(true)
                .value_parser(clap::value_parser!(u64))
                .help("random seed (default 0)"),
        )
        .arg(
            Arg::new("quiet")
                .display_order(1000)
                .long("quiet")
                .global(true)
                .action(ArgAction::SetTrue)
                .help("no progress or warnings on stderr"),
        )
        .arg(
            Arg::new("verbose")
                .display_order(1000)
                .long("verbose")
                .global(true)
                .action(ArgAction::SetTrue)
                .help("human output: every summary field after the answer"),
        );
    let mut seen: Vec<&str> = Vec::new();
    for cmd in COMMANDS {
        let top = cmd.path[0];
        if seen.contains(&top) {
            continue;
        }
        seen.push(top);
        if cmd.path.len() == 1 {
            root = root.subcommand(leaf(cmd, top));
        } else {
            let about = GROUPS.iter().find(|(g, _)| *g == top).map_or("", |(_, a)| *a);
            let mut g = Command::new(top)
                .about(about)
                .subcommand_required(true)
                .arg_required_else_help(true);
            let mut hidden = true;
            for c in COMMANDS.iter().filter(|c| c.path.len() == 2 && c.path[0] == top) {
                hidden &= c.hidden;
                g = g.subcommand(leaf(c, c.path[1]));
            }
            root = root.subcommand(g.hide(hidden));
        }
    }
    root
}

/// The table row of a command path.
pub(crate) fn find(path: &[String]) -> Option<&'static Cmd> {
    COMMANDS
        .iter()
        .find(|c| c.path.len() == path.len() && c.path.iter().zip(path).all(|(a, b)| a == b))
}

/// Walks the matches to the leaf: (path, leaf matches).
pub(crate) fn leaf_matches(m: &ArgMatches) -> (Vec<String>, &ArgMatches) {
    let mut path = Vec::new();
    let mut cur = m;
    while let Some((name, sub)) = cur.subcommand() {
        path.push(name.to_owned());
        cur = sub;
    }
    (path, cur)
}

fn usage(flag: &str, msg: impl std::fmt::Display) -> String {
    format!("--{flag}: {msg}")
}

fn parse_int(flag: &str, s: &str) -> Result<i64, String> {
    s.trim()
        .parse::<i64>()
        .map_err(|_| usage(flag, format!("expected an integer, got {s:?}")))
}

fn parse_num(flag: &str, s: &str) -> Result<Value, String> {
    let v: f64 = s
        .trim()
        .parse()
        .map_err(|_| usage(flag, format!("expected a number, got {s:?}")))?;
    if !v.is_finite() {
        return Err(usage(flag, "expected a finite number"));
    }
    Ok(json!(v))
}

/// A fraction (`0.02`) or percentage points with a `pp` suffix (`2pp`, `2 pp` = 0.02).
fn parse_frac(flag: &str, s: &str) -> Result<Value, String> {
    let t = s.trim();
    let (num, scale) = match t.strip_suffix("pp") {
        Some(n) => (n.trim(), 0.01),
        None => (t, 1.0),
    };
    let v: f64 = num.parse().map_err(|_| {
        usage(
            flag,
            format!("expected a fraction (0.02) or percentage points (2pp), got {s:?}"),
        )
    })?;
    if !v.is_finite() {
        return Err(usage(flag, "expected a finite number"));
    }
    Ok(json!(v * scale))
}

fn parse_dur(flag: &str, s: &str) -> Result<i64, String> {
    qt_schema::duration::parse(s).map_err(|e| usage(flag, e.message()))
}

fn split_list(s: &str) -> Vec<&str> {
    s.split(',').map(str::trim).filter(|x| !x.is_empty()).collect()
}

fn parse_enum(flag: &str, vals: &[&str], s: &str) -> Result<Value, String> {
    let cli = s.trim().replace('_', "-");
    if vals.contains(&cli.as_str()) {
        Ok(Value::String(cli.replace('-', "_")))
    } else {
        Err(usage(
            flag,
            format!("invalid value {s:?} (possible values: {})", vals.join(", ")),
        ))
    }
}

fn parse_latency(flag: &str, s: &str) -> Result<Value, String> {
    let parts: Vec<&str> = s.split(',').map(str::trim).collect();
    if parts.len() != 3 {
        return Err(usage(flag, format!("expected FEED,DECISION,GATEWAY, got {s:?}")));
    }
    Ok(json!({
        "feed_ns": parse_dur(flag, parts[0])?,
        "decision_ns": parse_dur(flag, parts[1])?,
        "gateway_ns": parse_dur(flag, parts[2])?,
    }))
}

fn parse_placements(flag: &str, s: &str) -> Result<Value, String> {
    let mut out = Vec::new();
    for item in split_list(s) {
        let (o, w) = item
            .split_once(':')
            .ok_or_else(|| usage(flag, format!("expected OFFSET:WEIGHT, got {item:?}")))?;
        out.push(json!({"offset_ticks": parse_int(flag, o)?, "weight": parse_num(flag, w)?}));
    }
    if out.is_empty() {
        return Err(usage(flag, "empty list"));
    }
    Ok(Value::Array(out))
}

fn parse_qty(flag: &str, s: &str) -> Result<Value, String> {
    let mut out = Vec::new();
    for item in split_list(s) {
        let p: Vec<&str> = item.split(':').collect();
        match p.as_slice() {
            ["lots", n, w] => {
                out.push(json!({"kind": "lots", "lots": parse_int(flag, n)?, "weight": parse_num(flag, w)?}))
            }
            ["median" | "median_order_size" | "median-order-size", w] => {
                out.push(json!({"kind": "median_order_size", "weight": parse_num(flag, w)?}));
            }
            _ => {
                return Err(usage(
                    flag,
                    format!("expected lots:N:WEIGHT or median:WEIGHT, got {item:?}"),
                ));
            }
        }
    }
    if out.is_empty() {
        return Err(usage(flag, "empty list"));
    }
    Ok(Value::Array(out))
}

fn parse_window(flag: &str, s: &str) -> Result<Value, String> {
    let t = s.trim();
    if let Some(rest) = t.strip_prefix("blocks:") {
        let (sel, n) = rest
            .split_once('/')
            .ok_or_else(|| usage(flag, format!("expected blocks:I-J/N, got {s:?}")))?;
        let n_blocks = parse_int(flag, n)?;
        let mut index = Vec::new();
        for part in split_list(sel) {
            match part.split_once('-') {
                Some((a, b)) => {
                    let (a, b) = (parse_int(flag, a)?, parse_int(flag, b)?);
                    if a > b {
                        return Err(usage(flag, format!("empty block range {part:?}")));
                    }
                    index.extend(a..=b);
                }
                None => index.push(parse_int(flag, part)?),
            }
        }
        if index.iter().any(|&i| i < 0 || i >= n_blocks) {
            return Err(usage(flag, format!("block indices must be in 0..{n_blocks}")));
        }
        return Ok(json!({"blocks": {"index": index, "n_blocks": n_blocks}}));
    }
    let dates = split_list(t);
    if dates.is_empty() {
        return Err(usage(flag, "expected dates or blocks:I-J/N"));
    }
    for d in &dates {
        let ok = d.len() == 10
            && d.bytes().enumerate().all(|(i, c)| {
                if i == 4 || i == 7 {
                    c == b'-'
                } else {
                    c.is_ascii_digit()
                }
            });
        if !ok {
            return Err(usage(flag, format!("expected YYYY-MM-DD, got {d:?}")));
        }
    }
    Ok(json!({ "dates": dates }))
}

fn parse_simulator(s: &str) -> Value {
    match s.split_once('@') {
        Some((n, v)) => json!({"name": n, "version": v}),
        None => json!({"name": s, "version": null}),
    }
}

fn parse_tz(flag: &str, s: &str) -> Result<Value, String> {
    match s.trim() {
        "America/New_York" | "US/Eastern" | "america/new_york" => Ok(json!("America/New_York")),
        "UTC" | "utc" => Ok(json!("UTC")),
        other => Err(usage(flag, format!("expected America/New_York or UTC, got {other:?}"))),
    }
}

fn parse_kv(flag: &str, s: &str) -> Result<(String, Value), String> {
    let (k, v) = s
        .split_once('=')
        .ok_or_else(|| usage(flag, format!("expected KEY=VALUE, got {s:?}")))?;
    let val = serde_json::from_str::<Value>(v).unwrap_or_else(|_| Value::String(v.to_owned()));
    Ok((k.trim().to_owned(), val))
}

/// Sets `value` at a dotted path, creating objects and merging objects into existing ones.
pub(crate) fn set_path(root: &mut Map<String, Value>, path: &str, value: Value) {
    let mut parts = path.split('.').peekable();
    let mut cur = root;
    while let Some(p) = parts.next() {
        if parts.peek().is_none() {
            match (cur.get_mut(p), value) {
                (Some(Value::Object(existing)), Value::Object(new)) => {
                    for (k, v) in new {
                        existing.insert(k, v);
                    }
                }
                (_, v) => {
                    cur.insert(p.to_owned(), v);
                }
            }
            return;
        }
        let entry = cur.entry(p.to_owned()).or_insert_with(|| Value::Object(Map::new()));
        if !entry.is_object() {
            *entry = Value::Object(Map::new());
        }
        let Value::Object(next) = entry else { return };
        cur = next;
    }
}

fn explicit(m: &ArgMatches, id: &str) -> bool {
    matches!(m.value_source(id), Some(ValueSource::CommandLine))
}

/// Converts one flag's matches into JSON (None when absent).
fn flag_value(m: &ArgMatches, fl: &Flag) -> Result<Option<Value>, String> {
    let id = fl.long;
    let flag = fl.long;
    match fl.kind {
        Kind::Flag | Kind::NoFlag | Kind::EmptyObject => {
            if !m.get_flag(id) {
                return Ok(None);
            }
            Ok(Some(match fl.kind {
                Kind::Flag => Value::Bool(true),
                Kind::NoFlag => Value::Bool(false),
                _ => Value::Object(Map::new()),
            }))
        }
        Kind::PathAppend | Kind::StrAppend | Kind::KeyValue => {
            let Some(vals) = m.get_many::<String>(id) else {
                return Ok(None);
            };
            let vals: Vec<&String> = vals.collect();
            Ok(Some(match fl.kind {
                Kind::PathAppend => Value::Array(vals.iter().map(|v| json!({ "path": v })).collect()),
                Kind::StrAppend => Value::Array(vals.iter().map(|v| json!(v)).collect()),
                _ => {
                    let mut o = Map::new();
                    for v in vals {
                        let (k, val) = parse_kv(flag, v)?;
                        // Venue names stay strings (`--fix-session-venue S=123` is the venue "123").
                        let val = match (fl.long, val) {
                            ("fix-session-venue", Value::String(x)) => Value::String(x),
                            ("fix-session-venue", other) => Value::String(other.to_string()),
                            (_, x) => x,
                        };
                        o.insert(k, val);
                    }
                    Value::Object(o)
                }
            }))
        }
        _ => {
            let Some(s) = m.get_one::<String>(id) else {
                return Ok(None);
            };
            let v = match fl.kind {
                Kind::Str => json!(s),
                Kind::Int => json!(parse_int(flag, s)?),
                Kind::Num => parse_num(flag, s)?,
                Kind::Frac => parse_frac(flag, s)?,
                Kind::Dur => json!(parse_dur(flag, s)?),
                Kind::DurList => {
                    let items = split_list(s);
                    if items.is_empty() {
                        return Err(usage(flag, "empty list"));
                    }
                    Value::Array(
                        items
                            .iter()
                            .map(|x| parse_dur(flag, x).map(Value::from))
                            .collect::<Result<_, _>>()?,
                    )
                }
                Kind::StrList => {
                    let items = split_list(s);
                    if items.is_empty() {
                        return Err(usage(flag, "empty list"));
                    }
                    if fl.long == "sides" {
                        for x in &items {
                            if !SIDES.contains(x) {
                                return Err(usage(flag, format!("expected B or A, got {x:?}")));
                            }
                        }
                    }
                    if fl.long == "markout-refs" {
                        for x in &items {
                            if !MARKOUT_REFS.contains(x) {
                                return Err(usage(flag, format!("expected mid or microprice, got {x:?}")));
                            }
                        }
                    }
                    json!(items)
                }
                Kind::Path => json!({ "path": s }),
                Kind::Enum(vals) => parse_enum(flag, vals, s)?,
                Kind::Latency => parse_latency(flag, s)?,
                Kind::Placements => parse_placements(flag, s)?,
                Kind::Qty => parse_qty(flag, s)?,
                Kind::Window => parse_window(flag, s)?,
                Kind::Simulator => parse_simulator(s),
                Kind::Tz => parse_tz(flag, s)?,
                Kind::Flag | Kind::NoFlag | Kind::EmptyObject | Kind::PathAppend | Kind::StrAppend | Kind::KeyValue => {
                    return Ok(None);
                }
            };
            Ok(Some(v))
        }
    }
}

/// Spec-shaped arguments of a leaf command, plus whether it runs hybrid (`why --live`).
pub(crate) fn build_args(
    cmd: &Cmd,
    m: &ArgMatches,
    root: &ArgMatches,
) -> Result<(Map<String, Value>, Implementation), String> {
    let mut args = Map::new();
    let mut special: Map<String, Value> = Map::new();
    // Fixed fields implied by the command path.
    match cmd.path {
        ["calibrate", k] => {
            let kind = k.replace('-', "_");
            args.insert("kind".into(), json!(kind));
            if kind == "latency" {
                args.insert("family".into(), json!("latency"));
            }
        }
        ["export", t] if *t != "replay-schedule" => {
            args.insert("target".into(), json!(t.replace('-', "_")));
        }
        _ => {}
    }
    for fl in cmd.flags {
        if let Some(v) = flag_value(m, fl)? {
            if let Some(name) = fl.to.strip_prefix('-') {
                special.insert(name.to_owned(), v);
            } else {
                set_path(&mut args, fl.to, v);
            }
        }
    }
    for (pname, to, _) in cmd.positional {
        if let Some(v) = m.get_one::<String>(pname) {
            args.insert((*to).to_owned(), json!(v));
        }
    }
    let mut imp = cmd.imp;
    match cmd.path {
        ["calibrate", "nt-touch"] => {
            let gated = special.get("queue_gated").and_then(Value::as_bool).unwrap_or(false);
            let fam = if gated {
                "nt_queue_l2_prob_touch"
            } else {
                "nt_prob_touch"
            };
            args.insert("family".into(), json!(fam));
        }
        ["score"] => {
            let mut sims = Vec::new();
            if let Some(v) = special.shift_remove("sim_log") {
                sims.push(json!({"kind": "external", "log": v}));
            }
            if special.shift_remove("sim").is_some() {
                sims.push(json!({"kind": "exact"}));
            }
            if let Some(v) = special.shift_remove("sim_model") {
                let mut o = Map::new();
                o.insert("kind".into(), json!("zoo"));
                o.insert("model".into(), v);
                if let Some(l2) = args.shift_remove("l2") {
                    o.insert("l2".into(), l2);
                }
                sims.push(Value::Object(o));
            }
            if sims.len() != 1 {
                return Err("exactly one of --sim-log DIR, --sim exact, --sim-model ID is required".into());
            }
            if args.contains_key("l2") {
                return Err("--l2-* flags apply to --sim-model only".into());
            }
            let sim = sims.remove(0);
            // Keep the spec field order of ScorecardSpec: live, sim, dataset, out, ...
            let mut ordered = Map::new();
            if let Some(l) = args.shift_remove("live") {
                ordered.insert("live".into(), l);
            }
            ordered.insert("sim".into(), sim);
            for (k, v) in args {
                ordered.insert(k, v);
            }
            args = ordered;
        }
        ["why"] => {
            let has_shadows = args.contains_key("shadows") || args.contains_key("shadow_id");
            let has_live = args.contains_key("live") || args.contains_key("order_key");
            match (has_shadows, has_live) {
                (true, false) => {
                    if !(args.contains_key("shadows") && args.contains_key("shadow_id")) {
                        return Err("--shadows FILE and --shadow-id ID go together".into());
                    }
                }
                (false, true) => {
                    if !(args.contains_key("live") && args.contains_key("order_key")) {
                        return Err("--live DIR and --order-key KEY go together".into());
                    }
                    imp = Implementation::Hybrid;
                }
                _ => {
                    return Err("give either --shadows FILE --shadow-id ID or --live DIR --order-key KEY".into());
                }
            }
            // The model column (`--model`, `--zoo`) is merged in Python.
            if args
                .get("_cli")
                .and_then(Value::as_object)
                .is_some_and(|c| !c.is_empty())
            {
                imp = Implementation::Hybrid;
            }
        }
        ["live", "import"] => {
            let venue = args.get("venue").and_then(Value::as_str).unwrap_or_default().to_owned();
            if args.get("fix").is_some() && venue != "fix44" {
                return Err("--fix-* flags apply to --venue fix44 only".into());
            }
            let has_options = args.get("_cli").and_then(|c| c.get("options")).is_some();
            if has_options && !venue.starts_with("sim_") {
                return Err("--option applies to --venue sim-hftbacktest|sim-nautilus only".into());
            }
        }
        ["export", "replay-schedule"] => {
            let (sched, live) = (args.contains_key("schedule"), args.contains_key("live"));
            if !sched && !live {
                return Err("give --schedule FILE and/or --live DIR".into());
            }
            if args.contains_key("amends") && !sched {
                return Err("--amends goes with --schedule (a live log brings its own amends)".into());
            }
            if args.contains_key("entry") && sched {
                return Err("--entry applies when the schedule is built from --live".into());
            }
        }
        _ => {}
    }
    // Global flags that map onto spec fields (only when given).
    if explicit(root, "seed") {
        let seed = root.get_one::<u64>("seed").copied().unwrap_or(0);
        match cmd.globals {
            Globals::Seed | Globals::SeedThreads => set_path(&mut args, "seed", json!(seed)),
            Globals::BootstrapSeed => set_path(&mut args, "bootstrap.seed", json!(seed)),
            Globals::None => {}
        }
    }
    if explicit(root, "threads") && cmd.globals == Globals::SeedThreads {
        let threads = root.get_one::<u32>("threads").copied().unwrap_or(1);
        set_path(&mut args, "threads", json!(threads));
    }
    // `BootstrapSpec.b` has no default of its own: fill the command's default when only other fields were given.
    if let Some(Value::Object(b)) = args.get_mut("bootstrap")
        && !b.contains_key("b")
    {
        let default_b = if cmd.path == ["score"] { 1000 } else { 200 };
        b.insert("b".into(), json!(default_b));
    }
    Ok((args, imp))
}

/// Rewrites `--format human|json` (the global output format) to `--output-format`; any other `--format` value is
/// the `ingest` source format and stays.
pub(crate) fn rewrite_format(argv: &[String]) -> Vec<String> {
    let mut out = Vec::with_capacity(argv.len());
    let mut i = 0;
    while i < argv.len() {
        let a = &argv[i];
        if a == "--" {
            out.extend(argv[i..].iter().cloned());
            break;
        }
        if a == "--format"
            && let Some(v) = argv.get(i + 1)
            && (v == "human" || v == "json")
        {
            out.push("--output-format".into());
            out.push(v.clone());
            i += 2;
            continue;
        }
        if let Some(v) = a.strip_prefix("--format=")
            && (v == "human" || v == "json")
        {
            out.push(format!("--output-format={v}"));
            i += 1;
            continue;
        }
        out.push(a.clone());
        i += 1;
    }
    out
}
