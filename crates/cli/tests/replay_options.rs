//! `ops::replay_options` / `merged_replay_options`: `ExcludeOrderId.session_date` (schema 1.1) reaches
//! `ReplayOptions.exclude_order_ids_dated` (that date only) instead of being dropped into the all-date list.

use qt_cli::ops::{merged_replay_options, replay_options};
use qt_schema::specs::ReplaySpec;
use serde_json::json;

const DAY_2019_12_30: i32 = 18_260;

#[test]
fn session_dates_map_to_dated_exclusions() {
    let o = replay_options(
        &json!({"exclude_order_ids": [
            {"instrument_id": 7, "order_id": 1},
            {"instrument_id": 7, "order_id": 2, "session_date": "2019-12-30"},
            {"instrument_id": 7, "order_id": 3, "session_date": null},
            {"order_id": 4, "session_date": "2019-12-30"},
            5
        ]}),
        &[7, 9],
    )
    .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(o.exclude_order_ids, vec![(7, 1), (7, 3), (7, 5), (9, 5)]);
    assert_eq!(
        o.exclude_order_ids_dated,
        vec![(7, 2, DAY_2019_12_30), (7, 4, DAY_2019_12_30), (9, 4, DAY_2019_12_30)]
    );
    for bad in [
        json!({"exclude_order_ids": [{"instrument_id": 7, "order_id": 1, "session_date": "30/12/2019"}]}),
        json!({"exclude_order_ids": [{"instrument_id": 7, "order_id": 1, "session_date": 20191230}]}),
    ] {
        let e = replay_options(&bad, &[7]).unwrap_err();
        assert_eq!(e.code(), "QT-CONFIG-001", "{e}");
        assert!(e.to_string().contains("session_date"), "{e}");
    }
}

/// The spec's own dated exclusions travel through the merged options (the CLI and Python paths) with their date.
#[test]
fn merged_options_keep_the_spec_session_date() {
    let spec: ReplaySpec = serde_json::from_value(json!({
        "dataset": {"path": "/nonexistent"},
        "exclude_order_ids": [{"instrument_id": 7, "order_id": 2, "session_date": "2019-12-30"}]
    }))
    .unwrap_or_else(|e| panic!("{e}"));
    // Only bare ids open the dataset; fully specified items do not need it.
    let o = merged_replay_options(&spec, &serde_json::Value::Null).unwrap_or_else(|e| panic!("{e}"));
    assert!(o.exclude_order_ids.is_empty(), "{o:?}");
    assert_eq!(o.exclude_order_ids_dated, vec![(7, 2, DAY_2019_12_30)]);
}
