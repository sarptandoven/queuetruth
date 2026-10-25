//! Fuzz findings in `qt_crypto` (inputs under `fuzz/regressions/crypto_*`): each input used to panic (debug) or
//! wrap (release) and is now a typed error or a correct result.

mod common;

use common::tmpdir;
use qt_crypto::prepare;
use qt_schema::specs::IngestSpec;
use serde_json::json;

fn spec(format: &str, path: &std::path::Path, extra: serde_json::Value) -> IngestSpec {
    let mut v = json!({"format": format, "inputs": [{"path": path.to_str().unwrap()}], "venue": "generic"});
    for (k, x) in extra.as_object().unwrap() {
        v[k] = x.clone();
    }
    serde_json::from_value(v).unwrap()
}

/// `tardis_trade_in_last_i64_day`: a print at 9_223_372_036_854_775 us lies in the last UTC day `i64` ns can
/// hold; the end of that session day (needed for the instrument row) is not representable.
#[test]
fn timestamp_in_the_last_representable_day_is_an_input_error() {
    let d = tmpdir("fuzz_last_day");
    let f = d.join("trades.csv");
    let row = |us: i64| format!("binance-futures,BTCUSDT,{us},{us},1,sell,100.0,0.5\n");
    let header = "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n";
    std::fs::write(&f, format!("{header}{}", row(9_223_372_036_854_775))).unwrap();
    for tz in ["UTC", "America/New_York"] {
        let e = prepare(&spec("tardis_l2", &f, json!({"tz": tz}))).unwrap_err();
        assert_eq!(e.code(), "QT-INPUT-002", "{e}");
        assert!(
            e.to_string().contains(&format!(
                "timestamp 9223372036854775000 ns is in the {tz} session day 2262-04-11, which ends after the last \
                 i64 nanosecond (2262-04-11T23:47:16Z)"
            )),
            "{e}"
        );
    }
    // The day before is fine: 2262-04-10 is a complete, representable session day.
    std::fs::write(&f, format!("{header}{}", row(9_223_286_400_000_000 - 1))).unwrap();
    let p = prepare(&spec("tardis_l2", &f, json!({}))).unwrap();
    assert_eq!(p.instruments.len(), 1);
    assert_eq!(p.instruments[0].session_date, 106_750);
    std::fs::remove_dir_all(d).ok();
}

/// A `session_date` whose midnight is outside the `i64` ns range is a config error (it used to overflow).
#[test]
fn session_date_beyond_i64_range_is_a_config_error() {
    let d = tmpdir("fuzz_session_date");
    let f = d.join("trades.csv");
    std::fs::write(
        &f,
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n\
         binance-futures,BTCUSDT,1700000000001999,1700000000002000,1,sell,100.0,0.5\n",
    )
    .unwrap();
    let e = prepare(&spec("tardis_l2", &f, json!({"session_date": "9999-12-31"}))).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001", "{e}");
    // The last day `i64` ns can start, with a record inside it: its end (midnight + 1 day) used to overflow.
    let last_midnight = 106_751 * 86_400_000_000_000i64;
    std::fs::write(
        &f,
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n\
         binance-futures,BTCUSDT,9223372036854775,9223372036854775,1,sell,100.0,0.5\n",
    )
    .unwrap();
    let range = "does not fit in i64 nanoseconds (UTC instants 1677-09-21T00:12:43Z..2262-04-11T23:47:16Z): the \
                 day must start and end inside that range";
    for (extra, what) in [
        (
            json!({"session_date": "2262-04-11"}),
            "session_date: the UTC session day 2262-04-11".to_owned(),
        ),
        (
            json!({"session_date": "2262-04-11", "tz": "America/New_York"}),
            "session_date: the America/New_York session day 2262-04-11".to_owned(),
        ),
        (
            json!({"session_date": "2262-04-11", "midnight_utc_ns": last_midnight}),
            format!("midnight_utc_ns: the session day 2262-04-11 starting at {last_midnight} ns"),
        ),
        // In UTC the first day whose midnight `i64` ns can hold is 1677-09-22.
        (
            json!({"session_date": "1677-09-21"}),
            "session_date: the UTC session day 1677-09-21".to_owned(),
        ),
    ] {
        let e = prepare(&spec("tardis_l2", &f, extra.clone())).unwrap_err();
        assert_eq!(e.code(), "QT-CONFIG-001", "{extra}: {e}");
        assert_eq!(e.to_string(), format!("QT-CONFIG-001: {what} {range}"), "{extra}");
    }
    // The last complete day is accepted as a session (the record then lies outside it: an ordinary session error).
    let e = prepare(&spec("tardis_l2", &f, json!({"session_date": "2262-04-10"}))).unwrap_err();
    assert!(e.to_string().contains("is outside 2262-04-10"), "{e}");
    // Alone, that midnight is refused by `qt_ingest::time` (its date is past the supported range).
    let e = prepare(&spec("tardis_l2", &f, json!({"midnight_utc_ns": last_midnight}))).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001", "{e}");
    std::fs::remove_dir_all(d).ok();
}
