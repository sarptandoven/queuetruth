//! O-PAR-3 (part 1): per-message-type ITCH 5.0 byte fixtures assembled by hand from the specification tables parse
//! to the expected `Message`, and `encode` round-trips every known type at its spec length (ported from et
//! `crates/itch/src/message.rs` tests).

use qt_ingest::itch::{
    Body, DirectListing, Header, KNOWN_TYPES, Message, Noii, ParseError, StockDirectory, encode, message_len, parse,
    stock, stock_str,
};

fn hdr(kind: u8, locate: u16, ts_ns: u64) -> Header {
    Header {
        kind,
        locate,
        tracking: 7,
        ts_ns,
    }
}

/// Hand-assembled bytes straight from the spec tables (not via `encode`).
#[test]
fn o_par_3_add_order_bytes_from_spec_table() {
    // 1.3.1: type(0) locate(1,2) tracking(3,2) ts(5,6) ref(11,8) B/S(19) shares(20,4) stock(24,8) price(32,4)
    let mut b = vec![b'A', 0x00, 0x2A, 0x00, 0x07];
    b.extend_from_slice(&34_200_000_000_000u64.to_be_bytes()[2..]); // 09:30 ET as 48-bit ns
    b.extend_from_slice(&1234u64.to_be_bytes());
    b.push(b'B');
    b.extend_from_slice(&100u32.to_be_bytes());
    b.extend_from_slice(b"AAPL    ");
    b.extend_from_slice(&1_852_500u32.to_be_bytes()); // $185.2500
    assert_eq!(b.len(), 36);
    let m = parse(&b).unwrap();
    assert_eq!(m.header, hdr(b'A', 42, 34_200_000_000_000));
    assert_eq!(
        m.body,
        Body::AddOrder {
            order_ref: 1234,
            buy_sell: b'B',
            shares: 100,
            stock: stock("AAPL"),
            price: 1_852_500,
            mpid: None,
        }
    );
    // F adds the 4-byte attribution at offset 36.
    b[0] = b'F';
    b.extend_from_slice(b"NSDQ");
    let m = parse(&b).unwrap();
    assert!(matches!(
        m.body,
        Body::AddOrder {
            mpid: Some(ref a),
            ..
        } if a == b"NSDQ"
    ));
}

#[test]
fn o_par_3_execution_cancel_delete_replace_bytes_from_spec_tables() {
    let head = |kind: u8| {
        let mut b = vec![kind, 0, 1, 0, 0];
        b.extend_from_slice(&[0, 0, 0, 0, 0x03, 0xE8]); // ts 1000
        b
    };
    // E (1.4.1): ref(11,8) shares(19,4) match(23,8) = 31
    let mut e = head(b'E');
    e.extend_from_slice(&9u64.to_be_bytes());
    e.extend_from_slice(&50u32.to_be_bytes());
    e.extend_from_slice(&777u64.to_be_bytes());
    assert_eq!(e.len(), 31);
    assert_eq!(
        parse(&e).unwrap().body,
        Body::Executed {
            order_ref: 9,
            shares: 50,
            match_no: 777
        }
    );
    // C (1.4.2): E + printable(31) price(32,4) = 36
    let mut c = e.clone();
    c[0] = b'C';
    c.push(b'N');
    c.extend_from_slice(&1_000_100u32.to_be_bytes());
    assert_eq!(
        parse(&c).unwrap().body,
        Body::ExecutedWithPrice {
            order_ref: 9,
            shares: 50,
            match_no: 777,
            printable: b'N',
            price: 1_000_100
        }
    );
    // X (1.4.3): ref(11,8) shares(19,4) = 23
    let mut x = head(b'X');
    x.extend_from_slice(&9u64.to_be_bytes());
    x.extend_from_slice(&5u32.to_be_bytes());
    assert_eq!(
        parse(&x).unwrap().body,
        Body::Cancel {
            order_ref: 9,
            shares: 5
        }
    );
    // D (1.4.4): ref(11,8) = 19
    let mut d = head(b'D');
    d.extend_from_slice(&9u64.to_be_bytes());
    assert_eq!(parse(&d).unwrap().body, Body::Delete { order_ref: 9 });
    // U (1.4.5): old(11,8) new(19,8) shares(27,4) price(31,4) = 35
    let mut u = head(b'U');
    u.extend_from_slice(&9u64.to_be_bytes());
    u.extend_from_slice(&10u64.to_be_bytes());
    u.extend_from_slice(&40u32.to_be_bytes());
    u.extend_from_slice(&1_000_200u32.to_be_bytes());
    assert_eq!(u.len(), 35);
    assert_eq!(
        parse(&u).unwrap().body,
        Body::Replace {
            old_ref: 9,
            new_ref: 10,
            shares: 40,
            price: 1_000_200
        }
    );
    // P (1.5.1): ref(11,8) B/S(19) shares(20,4) stock(24,8) price(32,4) match(36,8) = 44
    let mut p = head(b'P');
    p.extend_from_slice(&0u64.to_be_bytes());
    p.push(b'B');
    p.extend_from_slice(&30u32.to_be_bytes());
    p.extend_from_slice(b"MSFT    ");
    p.extend_from_slice(&4_000_000u32.to_be_bytes());
    p.extend_from_slice(&778u64.to_be_bytes());
    assert_eq!(p.len(), 44);
    assert_eq!(
        parse(&p).unwrap().body,
        Body::Trade {
            order_ref: 0,
            buy_sell: b'B',
            shares: 30,
            stock: stock("MSFT"),
            price: 4_000_000,
            match_no: 778
        }
    );
    // Q (1.5.2): shares(11,8) stock(19,8) price(27,4) match(31,8) type(39) = 40
    let mut q = head(b'Q');
    q.extend_from_slice(&123_456u64.to_be_bytes());
    q.extend_from_slice(b"MSFT    ");
    q.extend_from_slice(&4_000_000u32.to_be_bytes());
    q.extend_from_slice(&779u64.to_be_bytes());
    q.push(b'O');
    assert_eq!(q.len(), 40);
    assert_eq!(
        parse(&q).unwrap().body,
        Body::CrossTrade {
            shares: 123_456,
            stock: stock("MSFT"),
            price: 4_000_000,
            match_no: 779,
            cross_type: b'O'
        }
    );
    // B (1.5.3): match(11,8) = 19
    let mut br = head(b'B');
    br.extend_from_slice(&777u64.to_be_bytes());
    assert_eq!(parse(&br).unwrap().body, Body::BrokenTrade { match_no: 777 });
}

#[test]
fn o_par_3_stock_directory_and_system_event_bytes_from_spec_tables() {
    // R (1.2.1) 39 bytes
    let mut r = vec![b'R', 0, 1, 0, 0, 0, 0, 0, 0, 0, 0];
    r.extend_from_slice(b"AAPL    ");
    r.push(b'Q'); // market category 19
    r.push(b'N'); // financial status 20
    r.extend_from_slice(&100u32.to_be_bytes()); // round lot 21
    r.push(b'N'); // round lots only 25
    r.push(b'C'); // issue classification 26
    r.extend_from_slice(b"Z "); // sub type 27
    r.push(b'P'); // authenticity 29
    r.push(b'N'); // short sale threshold 30
    r.push(b'N'); // ipo 31
    r.push(b'1'); // luld tier 32
    r.push(b'N'); // etp 33
    r.extend_from_slice(&0u32.to_be_bytes()); // leverage 34
    r.push(b'N'); // inverse 38
    assert_eq!(r.len(), 39);
    let Body::StockDirectory(d) = parse(&r).unwrap().body else {
        panic!()
    };
    assert_eq!(stock_str(&d.stock), "AAPL");
    assert_eq!(
        (
            d.market_category,
            d.financial_status,
            d.round_lot_size,
            d.issue_classification,
            d.luld_tier,
            d.inverse_indicator
        ),
        (b'Q', b'N', 100, b'C', b'1', b'N')
    );
    // S (1.1) 12 bytes, locate always 0
    let s = [b'S', 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, b'O'];
    let m = parse(&s).unwrap();
    assert_eq!(m.header.locate, 0);
    assert_eq!(m.body, Body::SystemEvent { event: b'O' });
}

#[test]
fn o_par_3_every_known_type_round_trips_through_encode_at_its_spec_length() {
    let st = stock("XYZ");
    let bodies: Vec<(u8, Body)> = vec![
        (b'S', Body::SystemEvent { event: b'Q' }),
        (
            b'R',
            Body::StockDirectory(StockDirectory {
                stock: st,
                market_category: b'N',
                financial_status: b' ',
                round_lot_size: 100,
                round_lots_only: b'N',
                issue_classification: b'C',
                issue_sub_type: *b"Z ",
                authenticity: b'P',
                short_sale_threshold: b'N',
                ipo_flag: b' ',
                luld_tier: b'2',
                etp_flag: b'Y',
                etp_leverage_factor: 3,
                inverse_indicator: b'Y',
            }),
        ),
        (
            b'H',
            Body::TradingAction {
                stock: st,
                state: b'H',
                reason: *b"LUDP",
            },
        ),
        (
            b'Y',
            Body::RegSho {
                stock: st,
                action: b'1',
            },
        ),
        (
            b'L',
            Body::MarketParticipant {
                mpid: *b"NSDQ",
                stock: st,
                primary: b'Y',
                mode: b'N',
                state: b'A',
            },
        ),
        (
            b'V',
            Body::MwcbLevels {
                level1: 1,
                level2: 2,
                level3: 3,
            },
        ),
        (b'W', Body::MwcbStatus { level: b'1' }),
        (
            b'K',
            Body::IpoQuoting {
                stock: st,
                release_time_s: 34_200,
                qualifier: b'A',
                price: 120_000,
            },
        ),
        (
            b'J',
            Body::LuldCollar {
                stock: st,
                reference: 1,
                upper: 2,
                lower: 3,
                extension: 4,
            },
        ),
        (
            b'h',
            Body::OperationalHalt {
                stock: st,
                market: b'Q',
                action: b'H',
            },
        ),
        (
            b'A',
            Body::AddOrder {
                order_ref: 1,
                buy_sell: b'S',
                shares: 2,
                stock: st,
                price: 3,
                mpid: None,
            },
        ),
        (
            b'F',
            Body::AddOrder {
                order_ref: 1,
                buy_sell: b'B',
                shares: 2,
                stock: st,
                price: 3,
                mpid: Some(*b"MPID"),
            },
        ),
        (
            b'E',
            Body::Executed {
                order_ref: 1,
                shares: 2,
                match_no: 3,
            },
        ),
        (
            b'C',
            Body::ExecutedWithPrice {
                order_ref: 1,
                shares: 2,
                match_no: 3,
                printable: b'Y',
                price: 4,
            },
        ),
        (
            b'X',
            Body::Cancel {
                order_ref: 1,
                shares: 2,
            },
        ),
        (b'D', Body::Delete { order_ref: 1 }),
        (
            b'U',
            Body::Replace {
                old_ref: 1,
                new_ref: 2,
                shares: 3,
                price: 4,
            },
        ),
        (
            b'P',
            Body::Trade {
                order_ref: 0,
                buy_sell: b'B',
                shares: 1,
                stock: st,
                price: 2,
                match_no: 3,
            },
        ),
        (
            b'Q',
            Body::CrossTrade {
                shares: 0,
                stock: st,
                price: 2,
                match_no: 3,
                cross_type: b'C',
            },
        ),
        (b'B', Body::BrokenTrade { match_no: 1 }),
        (
            b'I',
            Body::Noii(Noii {
                paired_shares: 1,
                imbalance_shares: 2,
                imbalance_direction: b'B',
                stock: st,
                far_price: 3,
                near_price: 4,
                reference_price: 5,
                cross_type: b'O',
                price_variation: b'L',
            }),
        ),
        (
            b'N',
            Body::Rpii {
                stock: st,
                interest: b'A',
            },
        ),
        (
            b'O',
            Body::DirectListing(DirectListing {
                stock: st,
                open_eligibility: b'Y',
                min_price: 1,
                max_price: 2,
                near_execution_price: 3,
                near_execution_time: 4,
                lower_collar: 5,
                upper_collar: 6,
            }),
        ),
    ];
    assert_eq!(bodies.len(), KNOWN_TYPES.len());
    for (kind, body) in bodies {
        let m = Message {
            header: hdr(kind, 5, 0xFFFF_FFFF_FFFF), // 48-bit max survives
            body,
        };
        let mut buf = Vec::new();
        encode(&m, &mut buf);
        let len = u16::from_be_bytes([buf[0], buf[1]]) as usize;
        assert_eq!(len, message_len(kind).unwrap(), "{}", kind as char);
        assert_eq!(buf.len(), len + 2);
        assert_eq!(parse(&buf[2..]).unwrap(), m, "{}", kind as char);
        // One byte short is an error naming the type.
        let e = parse(&buf[2..buf.len() - 1]).unwrap_err();
        assert_eq!(
            e,
            ParseError::Short {
                kind,
                got: len - 1,
                want: len
            }
        );
    }
    // Unknown type: header decoded, body Unknown, any length accepted.
    let z = [b'Z', 0, 3, 0, 0, 0, 0, 0, 0, 0, 9, 1, 2, 3];
    let m = parse(&z).unwrap();
    assert_eq!((m.header.locate, m.header.ts_ns, m.body), (3, 9, Body::Unknown));
    assert_eq!(parse(b"Z").unwrap().body, Body::Unknown);
    assert_eq!(parse(&[]), Err(ParseError::Empty));
    assert_eq!(stock_str(&stock("A")), "A");
}

mod fuzz {
    use super::*;
    use proptest::prelude::*;

    proptest! {
        #![proptest_config(ProptestConfig { cases: 4000, ..ProptestConfig::default() })]

        /// Arbitrary bytes never panic; known types parse iff they are at least their spec length.
        #[test]
        fn parse_never_panics_on_arbitrary_bytes(kind in prop::sample::select(b"SRHYLVWKJhAFECXDUPQBINOZz\x00\xff".to_vec()),
                                                 rest in prop::collection::vec(any::<u8>(), 0..70)) {
            let mut b = vec![kind];
            b.extend_from_slice(&rest);
            match parse(&b) {
                Ok(m) => {
                    prop_assert_eq!(m.header.kind, kind);
                    if let Some(want) = message_len(kind) {
                        prop_assert!(b.len() >= want);
                        // Re-encoding a parsed known message reproduces its spec-length prefix of the input
                        // (except H, whose reserved byte 20 is not kept).
                        let mut enc = Vec::new();
                        encode(&m, &mut enc);
                        if kind != b'H' {
                            prop_assert_eq!(&enc[2..], &b[..want]);
                        }
                    } else {
                        prop_assert_eq!(m.body, Body::Unknown);
                    }
                }
                Err(ParseError::Short { got, want, .. }) => prop_assert!(got < want),
                Err(ParseError::Empty) => prop_assert!(false, "non-empty input"),
            }
        }
    }
}
