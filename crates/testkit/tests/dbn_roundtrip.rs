//! `encode_dbn` -> `dbn` crate decode round trip (raw and Zstandard), and checked field conversions.

use std::path::PathBuf;

use dbn::decode::{DbnDecoder, DbnMetadata, DecodeRecord};
use dbn::enums::Schema;
use dbn::record::MboMsg;
use qt_testkit::{SynthConfig, encode_dbn, generate};
use qt_types::{MboEvent, Venue};

fn tmp(name: &str) -> PathBuf {
    let d = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("qt-testkit-dbn-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&d).expect("tmp dir");
    d
}

fn back(m: &MboMsg) -> MboEvent {
    MboEvent {
        ts_event: m.hd.ts_event as i64,
        ts_recv: m.ts_recv as i64,
        instrument_id: m.hd.instrument_id,
        action: m.action as u8,
        side: m.side as u8,
        price: m.price,
        size: u64::from(m.size),
        order_id: m.order_id,
        flags: m.flags.raw(),
        sequence: u64::from(m.sequence),
        channel_id: u16::from(m.channel_id),
        publisher_id: m.hd.publisher_id,
    }
}

#[test]
fn dbn_encode_decode_round_trip() {
    let dir = tmp("rt");
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        let mut c = SynthConfig::for_venue(venue, 4);
        c.events_per_instrument = 1_500;
        let out = generate(&c);
        let raw = dir.join(format!("{venue}.mbo.dbn"));
        encode_dbn(&out.events, &raw).expect("encode");
        let dec = DbnDecoder::from_file(&raw).expect("decoder");
        let md = dec.metadata().clone();
        assert_eq!(md.schema, Some(Schema::Mbo));
        assert_eq!(
            md.dataset,
            if venue == Venue::Nasdaq {
                "XNAS.ITCH"
            } else {
                "GLBX.MDP3"
            }
        );
        let recs: Vec<MboMsg> = dec.decode_records().expect("decode");
        let got: Vec<MboEvent> = recs.iter().map(back).collect();
        assert_eq!(got, out.events, "{venue}: raw round trip");
        let zst = dir.join(format!("{venue}.mbo.dbn.zst"));
        encode_dbn(&out.events, &zst).expect("encode zst");
        let recs: Vec<MboMsg> = DbnDecoder::from_zstd_file(&zst)
            .expect("zstd")
            .decode_records()
            .expect("decode");
        assert_eq!(
            recs.iter().map(back).collect::<Vec<_>>(),
            out.events,
            "{venue}: zstd round trip"
        );
        // Deterministic bytes.
        let again = dir.join("again.dbn");
        encode_dbn(&out.events, &again).expect("encode");
        assert_eq!(std::fs::read(&raw).expect("read"), std::fs::read(&again).expect("read"));
    }
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn dbn_rejects_fields_that_do_not_fit() {
    let dir = tmp("bad");
    let ok = MboEvent {
        ts_event: 1,
        ts_recv: 1,
        instrument_id: 1,
        action: b'A',
        side: b'B',
        price: 100,
        size: 1,
        order_id: 1,
        flags: 128,
        sequence: 1,
        ..MboEvent::default()
    };
    for bad in [
        MboEvent {
            size: u64::from(u32::MAX) + 1,
            ..ok
        },
        MboEvent {
            sequence: u64::from(u32::MAX) + 1,
            ..ok
        },
        MboEvent { channel_id: 256, ..ok },
        MboEvent { ts_event: -1, ..ok },
    ] {
        let err = encode_dbn(&[bad], &dir.join("x.dbn")).expect_err("must not fit");
        assert_eq!(err.code(), "QT-INPUT-002");
    }
    encode_dbn(&[ok], &dir.join("ok.dbn")).expect("fits");
    std::fs::remove_dir_all(dir).ok();
}
