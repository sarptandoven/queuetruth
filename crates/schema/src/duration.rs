//! Duration strings of the CLI (SPEC 4.4): `<integer>` (ns) or `<integer><unit>` with unit `ns`, `us`, `ms`, `s`,
//! `m`; e.g. `250us`, `100ms`, `1s`, `10s`, `1m`, `5m`. Values are non-negative; overflow is an error.

use qt_types::{Dur, QtError, QtResult};

/// Parses a duration string into ns (`QT-CONFIG-001` on malformed input or overflow).
pub fn parse(s: &str) -> QtResult<Dur> {
    let t = s.trim();
    let split = t.find(|c: char| !c.is_ascii_digit()).unwrap_or(t.len());
    let (num, unit) = t.split_at(split);
    if num.is_empty() {
        return Err(QtError::Config(format!(
            "duration {s:?}: expected <integer>[ns|us|ms|s|m]"
        )));
    }
    let mult: i64 = match unit {
        "" | "ns" => 1,
        "us" => 1_000,
        "ms" => 1_000_000,
        "s" => 1_000_000_000,
        "m" => 60_000_000_000,
        _ => {
            return Err(QtError::Config(format!(
                "duration {s:?}: unknown unit {unit:?} (ns|us|ms|s|m)"
            )));
        }
    };
    let v: i64 = num
        .parse()
        .map_err(|_| QtError::Config(format!("duration {s:?}: integer out of range")))?;
    v.checked_mul(mult)
        .ok_or_else(|| QtError::Config(format!("duration {s:?}: overflows i64 ns")))
}

/// Parses a comma-separated list of durations (empty items rejected).
pub fn parse_list(s: &str) -> QtResult<Vec<Dur>> {
    s.split(',').map(parse).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn units() {
        let cases = [
            ("0", 0),
            ("1500", 1500),
            ("7ns", 7),
            ("250us", 250_000),
            ("100ms", 100_000_000),
            ("1s", 1_000_000_000),
            ("10s", 10_000_000_000),
            ("1m", 60_000_000_000),
            ("5m", 300_000_000_000),
            (" 2s ", 2_000_000_000),
        ];
        for (s, v) in cases {
            assert_eq!(parse(s).ok(), Some(v), "{s}");
        }
        assert_eq!(parse_list("0,1s,5s").ok(), Some(vec![0, 1_000_000_000, 5_000_000_000]));
    }

    #[test]
    fn rejects() {
        for s in [
            "",
            "s",
            "-1s",
            "1.5s",
            "1h",
            "1 s",
            "1e9",
            "99999999999999999999",
            "200000000m",
            "1s,",
        ] {
            let r = if s.contains(',') {
                parse_list(s).map(|_| 0)
            } else {
                parse(s)
            };
            assert_eq!(r.err().map(|e| e.code()), Some("QT-CONFIG-001"), "{s:?}");
        }
    }
}
