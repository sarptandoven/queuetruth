use super::*;

const ALL_ACTIONS: [Action; 7] = [
    Action::Add,
    Action::Cancel,
    Action::Modify,
    Action::Clear,
    Action::Trade,
    Action::Fill,
    Action::None,
];

#[test]
fn action_bytes_round_trip() {
    let bytes = b"ACMRTFN";
    for (a, &b) in ALL_ACTIONS.iter().zip(bytes.iter()) {
        assert_eq!(a.as_byte(), b);
        assert_eq!(Action::from_byte(b), Some(*a));
    }
    for b in 0..=u8::MAX {
        if !bytes.contains(&b) {
            assert_eq!(Action::from_byte(b), None, "byte {b}");
        }
    }
}

#[test]
fn side_bytes_round_trip() {
    for s in [Side::Bid, Side::Ask, Side::None] {
        assert_eq!(Side::from_byte(s.as_byte()), s);
        assert_eq!(s.opposite().opposite(), s);
    }
    assert_eq!(Side::Bid.as_byte(), b'B');
    assert_eq!(Side::Ask.as_byte(), b'A');
    assert_eq!(Side::None.as_byte(), b'N');
    assert_eq!(Side::from_byte(b'x'), Side::None);
    assert_eq!(Side::Bid.opposite(), Side::Ask);
    assert_eq!(Side::None.opposite(), Side::None);
    for bs in [BookSide::Bid, BookSide::Ask] {
        assert_eq!(BookSide::from_byte(bs.as_byte()), Some(bs));
        assert_eq!(BookSide::from_side(bs.to_side()), Some(bs));
        assert_eq!(bs.opposite().opposite(), bs);
    }
    assert_eq!(BookSide::from_side(Side::None), None);
    assert_eq!(BookSide::from_byte(b'N'), None);
    assert_eq!(BookSide::Bid.opposite(), BookSide::Ask);
}

#[test]
fn mbo_event_default_and_accessors() {
    let e = MboEvent::default();
    assert_eq!(e.price, UNDEF_PRICE);
    assert_eq!((e.action, e.side), (b'N', b'N'));
    assert_eq!(e.action(), Some(Action::None));
    assert_eq!(e.side(), Side::None);
    assert!(!e.is_last() && !e.is_snapshot());
    let e = MboEvent {
        flags: flags::LAST | flags::SNAPSHOT,
        action: b'A',
        side: b'B',
        ..e
    };
    assert!(e.is_last() && e.is_snapshot());
    assert_eq!(e.action(), Some(Action::Add));
    assert_eq!(e.side(), Side::Bid);
    assert_eq!(MboEvent { action: b'z', ..e }.action(), None);
}

#[test]
fn flag_bits_match_dbn() {
    assert_eq!(flags::LAST, 128);
    assert_eq!(flags::TOB, 64);
    assert_eq!(flags::SNAPSHOT, 32);
    assert_eq!(flags::MBP, 16);
    assert_eq!(flags::BAD_TS_RECV, 8);
    assert_eq!(flags::MAYBE_BAD_BOOK, 4);
    assert_eq!(flags::PUBLISHER_SPECIFIC, 2);
}

#[test]
fn venue_parse_aliases() {
    for (s, v) in [
        ("nasdaq", Venue::Nasdaq),
        ("itch", Venue::Nasdaq),
        ("xnas", Venue::Nasdaq),
        ("XNAS.ITCH", Venue::Nasdaq),
        ("cme", Venue::Cme),
        ("glbx", Venue::Cme),
        ("GLBX.MDP3", Venue::Cme),
        ("coinbase", Venue::Coinbase),
        ("generic", Venue::Generic),
        ("bitstamp", Venue::Generic),
        ("bitfinex", Venue::Generic),
    ] {
        assert_eq!(s.parse::<Venue>().ok(), Some(v), "{s}");
    }
    let err = "binance".parse::<Venue>().err();
    assert_eq!(err.map(|e| e.code()), Some("QT-CONFIG-001"));
    assert_eq!(Venue::default(), Venue::Generic);
    assert_eq!(serde_json::to_string(&Venue::Cme).ok().as_deref(), Some("\"cme\""));
}

#[test]
fn priority_rule_truth_table() {
    let k = PriorityRule::KeepOnSizeDecrease;
    assert!(k.keeps_priority(100, 10, 100, 10));
    assert!(k.keeps_priority(100, 10, 100, 5));
    assert!(!k.keeps_priority(100, 10, 100, 11));
    assert!(!k.keeps_priority(100, 10, 101, 5));
    let a = PriorityRule::AlwaysLose;
    assert!(!a.keeps_priority(100, 10, 100, 5));
    assert!(!a.keeps_priority(100, 10, 100, 10));
}

/// SPEC 3.2 table, transcribed column by column.
#[test]
fn rules_table_equals_spec_3_2() {
    use PriorityRule::*;
    use ValidationStatus::*;
    // venue, modify_records_present, modify_priority, snapshot_begins_with_clear, snapshot_records_flagged,
    // trades_have_aggressor, supports_hidden_orders, fill_removal_shares_sequence, utc_midnight_snapshot,
    // sequence_meaningful, validation
    type Row = (
        Venue,
        bool,
        PriorityRule,
        bool,
        bool,
        bool,
        bool,
        bool,
        bool,
        bool,
        ValidationStatus,
    );
    let table: [Row; 4] = [
        (
            Venue::Nasdaq,
            false,
            AlwaysLose,
            false,
            false,
            true,
            true,
            true,
            false,
            true,
            RealData,
        ),
        (
            Venue::Cme,
            true,
            KeepOnSizeDecrease,
            true,
            true,
            true,
            false,
            false,
            true,
            true,
            SyntheticOnly,
        ),
        (
            Venue::Coinbase,
            true,
            KeepOnSizeDecrease,
            false,
            false,
            true,
            false,
            true,
            false,
            true,
            Assumed,
        ),
        (
            Venue::Generic,
            true,
            KeepOnSizeDecrease,
            false,
            true,
            false,
            true,
            true,
            false,
            false,
            Assumed,
        ),
    ];
    for (v, mrp, mp, sbc, srf, tha, sho, frss, ums, sm, val) in table {
        let expected = VenueRules {
            venue: v,
            modify_records_present: mrp,
            modify_priority: mp,
            snapshot_begins_with_clear: sbc,
            snapshot_records_flagged: srf,
            trades_have_aggressor: tha,
            supports_hidden_orders: sho,
            fill_removal_shares_sequence: frss,
            utc_midnight_snapshot: ums,
            sequence_meaningful: sm,
            validation: val,
            rule_set: RuleSet::from(v),
            iceberg_refresh_loses_priority: v == Venue::Cme,
            hidden_lower_priority: v == Venue::Nasdaq,
        };
        assert_eq!(rules(v), expected, "{v}");
        assert_eq!(rules_for(RuleSet::from(v)), expected, "{v}");
    }
}

/// Schema 1.1 flags (3.11 #4) and the `crypto_l3` rule set (E38, M25).
#[test]
fn rule_sets_1_1() {
    // iceberg_refresh_loses_priority: CME only; hidden_lower_priority: Nasdaq only.
    let flags: Vec<(Venue, bool, bool)> = [Venue::Nasdaq, Venue::Cme, Venue::Coinbase, Venue::Generic]
        .into_iter()
        .map(|v| {
            (
                v,
                rules(v).iceberg_refresh_loses_priority,
                rules(v).hidden_lower_priority,
            )
        })
        .collect();
    assert_eq!(
        flags,
        [
            (Venue::Nasdaq, false, true),
            (Venue::Cme, true, false),
            (Venue::Coinbase, false, false),
            (Venue::Generic, false, false)
        ]
    );
    let c = rules_for(RuleSet::CryptoL3);
    let g = rules(Venue::Generic);
    assert_eq!(c.venue, Venue::Generic);
    assert_eq!(c.rule_set, RuleSet::CryptoL3);
    assert!(!c.supports_hidden_orders);
    assert!(g.supports_hidden_orders, "generic is unchanged");
    assert_eq!(
        VenueRules {
            supports_hidden_orders: true,
            rule_set: RuleSet::Generic,
            ..c
        },
        g,
        "crypto_l3 differs from generic only in supports_hidden_orders"
    );
    for (s, rs) in [
        ("crypto_l3", RuleSet::CryptoL3),
        ("CRYPTO-L3", RuleSet::CryptoL3),
        ("nasdaq", RuleSet::Nasdaq),
        ("GLBX.MDP3", RuleSet::Cme),
        ("coinbase", RuleSet::Coinbase),
        ("bitstamp", RuleSet::Generic),
        ("generic", RuleSet::Generic),
    ] {
        assert_eq!(s.parse::<RuleSet>().ok(), Some(rs), "{s}");
    }
    assert_eq!(
        "binance".parse::<RuleSet>().err().map(|e| e.code()),
        Some("QT-CONFIG-001")
    );
    assert!("crypto_l3".parse::<Venue>().is_err(), "Venue keeps its v0.1 names");
    assert_eq!(RuleSet::CryptoL3.venue(), Venue::Generic);
    assert_eq!(RuleSet::CryptoL3.to_string(), "crypto_l3");
    assert_eq!(RuleSet::default(), RuleSet::Generic);
    assert_eq!(
        serde_json::to_string(&RuleSet::CryptoL3).ok().as_deref(),
        Some("\"crypto_l3\"")
    );
    for v in [Venue::Nasdaq, Venue::Cme, Venue::Coinbase, Venue::Generic] {
        assert_eq!(RuleSet::from(v).venue(), v);
        assert_eq!(RuleSet::from(v).name(), v.name());
    }
}

#[test]
fn error_codes_and_display() {
    let cases: Vec<(QtError, &str)> = vec![
        (QtError::Usage("u".into()), "QT-USAGE-001"),
        (QtError::Config("c".into()), "QT-CONFIG-001"),
        (
            QtError::InputMissing {
                path: "p".into(),
                msg: "m".into(),
            },
            "QT-INPUT-001",
        ),
        (
            QtError::InputFormat {
                location: "l".into(),
                msg: "m".into(),
            },
            "QT-INPUT-002",
        ),
        (
            QtError::Duplicate {
                what: "shadow_id".into(),
                id: "x".into(),
            },
            "QT-INPUT-003",
        ),
        (QtError::Units("u".into()), "QT-INPUT-004"),
        (QtError::DataUnusable("d".into()), "QT-DATA-001"),
        (QtError::Strict("s".into()), "QT-DATA-002"),
        (QtError::Engine("e".into()), "QT-ENGINE-001"),
        (QtError::Io(std::io::Error::other("io")), "QT-INPUT-001"),
    ];
    for (e, code) in cases {
        assert_eq!(e.code(), code);
        assert!(e.to_string().starts_with(&format!("{code}: ")), "{e}");
    }
    let d = QtError::Duplicate {
        what: "shadow_id".into(),
        id: "x".into(),
    };
    assert_eq!(d.to_string(), "QT-INPUT-003: duplicate shadow_id: x");
    assert_eq!(d.message(), "duplicate shadow_id: x");
    let f = QtError::InputFormat {
        location: "a.parquet row 3".into(),
        msg: "bad side".into(),
    };
    assert_eq!(f.to_string(), "QT-INPUT-002: a.parquet row 3: bad side");
}

/// Reference splitmix64 outputs for state 0 (Vigna's splitmix64.c, seed 0).
#[test]
fn splitmix64_known_answers() {
    let expected = [
        0xe220_a839_7b1d_cdaf_u64,
        0x6e78_9e6a_a1b9_65f4,
        0x06c4_5d18_8009_454f,
        0xf88b_b8a8_724c_81ec,
    ];
    let mut x = 0u64;
    for e in expected {
        assert_eq!(splitmix64(x), e);
        x = x.wrapping_add(0x9e37_79b9_7f4a_7c15);
    }
}

/// Reference xoshiro256** outputs for the raw state [1, 2, 3, 4] (Blackman-Vigna reference implementation).
#[test]
fn xoshiro_known_answers_raw_state() {
    let mut r = Xoshiro256StarStar::from_state([1, 2, 3, 4]);
    let expected = [11520_u64, 0, 1509978240, 1215971899390074240];
    for e in expected {
        assert_eq!(r.next_u64(), e);
    }
}

#[test]
fn xoshiro_seeding_uses_splitmix_sequence() {
    let seed = 12345u64;
    let g = 0x9e37_79b9_7f4a_7c15_u64;
    let state = [
        splitmix64(seed),
        splitmix64(seed.wrapping_add(g)),
        splitmix64(seed.wrapping_add(g.wrapping_mul(2))),
        splitmix64(seed.wrapping_add(g.wrapping_mul(3))),
    ];
    assert_eq!(Xoshiro256StarStar::new(seed), Xoshiro256StarStar::from_state(state));
}

#[test]
fn next_f64_and_range_inclusive() {
    let mut a = Xoshiro256StarStar::new(9);
    let mut b = Xoshiro256StarStar::new(9);
    for _ in 0..1000 {
        let f = a.next_f64();
        assert!((0.0..1.0).contains(&f));
        assert_eq!(f, (b.next_u64() >> 11) as f64 / 9007199254740992.0);
    }
    for _ in 0..1000 {
        let x = a.range_inclusive(3, 7);
        assert_eq!(x, 3 + b.next_u64() % 5);
    }
    assert_eq!(a.range_inclusive(0, u64::MAX), b.next_u64());
    assert_eq!(a.range_inclusive(5, 5), 5);
    b.next_u64();
    assert_eq!(a.range_inclusive(9, 2), 9);
    assert_eq!(a, b);
}

#[test]
fn stable_seed_definition() {
    // Independent re-derivation of the byte stream for (7, ["a", "bc"]).
    let mut bytes = 7u64.to_le_bytes().to_vec();
    bytes.extend_from_slice(&1u32.to_le_bytes());
    bytes.extend_from_slice(b"a");
    bytes.extend_from_slice(&2u32.to_le_bytes());
    bytes.extend_from_slice(b"bc");
    let mut h = 0xcbf2_9ce4_8422_2325_u64;
    for b in bytes {
        h = (h ^ u64::from(b)).wrapping_mul(0x100_0000_01b3);
    }
    assert_eq!(stable_seed(7, &[b"a", b"bc"]), splitmix64(h));
    // Part boundaries matter.
    assert_ne!(stable_seed(7, &[b"ab", b"c"]), stable_seed(7, &[b"a", b"bc"]));
    assert_ne!(stable_seed(7, &[]), stable_seed(7, &[b""]));
}

fn kat_path() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../schemas/rng_kat.json")
}

/// Cases shared with the Python mirrors: stable_seed values and xoshiro streams.
fn kat_document() -> serde_json::Value {
    let ss = stable_seed(7, &[b"a", b"bc"]);
    let seed_cases: Vec<(u64, Vec<&str>)> = vec![
        (0, vec![]),
        (0, vec![""]),
        (7, vec!["a", "bc"]),
        (42, vec!["latency", "s1"]),
        (u64::MAX, vec!["boot"]),
        (1, vec!["bootstrap", "primary:mcnemar"]),
    ];
    let stable: Vec<serde_json::Value> = seed_cases
        .iter()
        .map(|(seed, parts)| {
            let raw: Vec<&[u8]> = parts.iter().map(|p| p.as_bytes()).collect();
            serde_json::json!({"run_seed": seed, "parts": parts, "value": stable_seed(*seed, &raw)})
        })
        .collect();
    let streams: Vec<serde_json::Value> = [0u64, 1, ss]
        .iter()
        .map(|&seed| {
            let mut r = Xoshiro256StarStar::new(seed);
            let v: Vec<u64> = (0..1000).map(|_| r.next_u64()).collect();
            serde_json::json!({"seed": seed, "next_u64": v})
        })
        .collect();
    let mut r = Xoshiro256StarStar::new(0);
    let f: Vec<f64> = (0..20).map(|_| r.next_f64()).collect();
    let mut r = Xoshiro256StarStar::new(1);
    let ri: Vec<u64> = (0..20).map(|_| r.range_inclusive(0, 999)).collect();
    serde_json::json!({
        "description": "Known answers for qt_types::{stable_seed, Xoshiro256StarStar} (SPEC 3.1); regenerate with QT_REGEN_KAT=1 cargo test -p queuetruth-types",
        "stable_seed": stable,
        "stable_seed_7_a_bc": ss,
        "streams": streams,
        "next_f64_seed_0": f,
        "range_inclusive_seed_1_0_999": ri,
    })
}

#[test]
fn rng_kat_file_matches() {
    let doc = kat_document();
    let text = serde_json::to_string_pretty(&doc).unwrap_or_default() + "\n";
    let path = kat_path();
    if std::env::var_os("QT_REGEN_KAT").is_some() {
        std::fs::write(&path, &text).unwrap_or_else(|e| panic!("writing {}: {e}", path.display()));
    }
    let committed = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
    let committed: serde_json::Value = serde_json::from_str(&committed).unwrap_or_default();
    assert_eq!(committed, doc, "schemas/rng_kat.json differs from qt_types output");
    assert_eq!(committed["streams"][0]["next_u64"].as_array().map(Vec::len), Some(1000));
}
