//! Exact decimal-string to fixed-point conversion, ISO-8601 / epoch-unit timestamps and session-date helpers.
//! Every conversion is exact or fails with `QT-INPUT-004` (units) / `QT-INPUT-002` (format).

use qt_types::{QtError, QtResult, Ts};

const NS_PER_DAY: i64 = 86_400_000_000_000;

/// Exact value of decimal text `s` (optional sign, digits, optional fraction, optional exponent) in units of
/// `10^exp`, as i128. Fails if the value is not an integer multiple of `10^exp`.
pub fn dec_units(s: &str, exp: i32) -> Result<i128, String> {
    let t = s.trim();
    let (neg, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let (mant, e10) = match body.find(['e', 'E']) {
        Some(k) => {
            let e: i32 = body[k + 1..].parse().map_err(|_| format!("bad exponent in {s:?}"))?;
            (&body[..k], e)
        }
        None => (body, 0),
    };
    let (int_part, frac_part) = mant.split_once('.').unwrap_or((mant, ""));
    if (int_part.is_empty() && frac_part.is_empty())
        || !int_part.bytes().all(|c| c.is_ascii_digit())
        || !frac_part.bytes().all(|c| c.is_ascii_digit())
    {
        return Err(format!("not a decimal number: {s:?}"));
    }
    let digits: String = format!("{int_part}{frac_part}");
    let digits = digits.trim_start_matches('0');
    let frac_len = i32::try_from(frac_part.len()).map_err(|_| format!("number too long: {s:?}"))?;
    // value = digits * 10^(e10 - frac_len); result = value / 10^exp = digits * 10^shift
    let shift = e10
        .checked_sub(frac_len)
        .and_then(|v| v.checked_sub(exp))
        .ok_or_else(|| format!("exponent out of range: {s:?}"))?;
    let (keep, shift) = if shift < 0 {
        let drop = usize::try_from(shift.unsigned_abs()).map_err(|_| format!("exponent out of range: {s:?}"))?;
        if drop > digits.len() {
            if digits.is_empty() {
                ("", 0)
            } else {
                return Err(format!("{s:?} is not a multiple of 10^{exp}"));
            }
        } else {
            let (k, tail) = digits.split_at(digits.len() - drop);
            if tail.bytes().any(|c| c != b'0') {
                return Err(format!("{s:?} is not a multiple of 10^{exp}"));
            }
            (k, 0)
        }
    } else {
        (digits, shift)
    };
    let mut v: i128 = 0;
    for c in keep.bytes() {
        v = v
            .checked_mul(10)
            .and_then(|x| x.checked_add(i128::from(c - b'0')))
            .ok_or_else(|| format!("number too large: {s:?}"))?;
    }
    for _ in 0..shift {
        if v == 0 {
            break;
        }
        v = v.checked_mul(10).ok_or_else(|| format!("number too large: {s:?}"))?;
    }
    Ok(if neg { -v } else { v })
}

/// True when decimal text is numerically zero (`0`, `0.0`, `-0e5`).
pub fn is_zero(s: &str) -> bool {
    let t = s.trim().trim_start_matches(['-', '+']);
    let mant = t.split(['e', 'E']).next().unwrap_or("");
    !mant.is_empty() && mant.bytes().any(|c| c.is_ascii_digit()) && mant.bytes().all(|c| c == b'0' || c == b'.')
}

/// Price text -> i64 fixed point at `10^price_exp` (`QT-INPUT-004` when not exact or out of range).
pub fn price(s: &str, price_exp: i8) -> QtResult<i64> {
    let v = dec_units(s, i32::from(price_exp)).map_err(|m| QtError::Units(format!("price {m}")))?;
    i64::try_from(v)
        .ok()
        .filter(|p| *p != i64::MAX)
        .ok_or_else(|| QtError::Units(format!("price {s:?} out of range at 10^{price_exp}")))
}

/// Non-negative size text -> u64 size units at `10^size_exp` (`QT-INPUT-004` when not exact, negative or too big).
pub fn size(s: &str, size_exp: i8) -> QtResult<u64> {
    let v = dec_units(s, i32::from(size_exp)).map_err(|m| QtError::Units(format!("size {m}")))?;
    u64::try_from(v).map_err(|_| QtError::Units(format!("size {s:?} negative or out of range at 10^{size_exp}")))
}

/// Signed amount text -> (sign, |amount| in size units). Sign is -1, 0 or 1.
pub fn signed_size(s: &str, size_exp: i8) -> QtResult<(i8, u64)> {
    let v = dec_units(s, i32::from(size_exp)).map_err(|m| QtError::Units(format!("amount {m}")))?;
    let mag = u64::try_from(v.unsigned_abs())
        .map_err(|_| QtError::Units(format!("amount {s:?} out of range at 10^{size_exp}")))?;
    Ok((v.signum() as i8, mag))
}

/// Days since 1970-01-01 of a proleptic Gregorian date (Howard Hinnant's `days_from_civil`).
pub fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let m = i64::from(m);
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + i64::from(d) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// (year, month, day) of a day number (inverse of `days_from_civil`).
pub fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// `YYYY-MM-DD` -> days since epoch.
pub fn parse_date(s: &str) -> QtResult<i32> {
    let bad = || QtError::Config(format!("session_date: expected YYYY-MM-DD, got {s:?}"));
    let p: Vec<&str> = s.trim().split('-').collect();
    if p.len() != 3 || p[0].len() != 4 || p[1].len() != 2 || p[2].len() != 2 {
        return Err(bad());
    }
    let y: i64 = p[0].parse().map_err(|_| bad())?;
    let m: u32 = p[1].parse().map_err(|_| bad())?;
    let d: u32 = p[2].parse().map_err(|_| bad())?;
    if !(1..=12).contains(&m) || d == 0 || d > days_in_month(y, m) {
        return Err(bad());
    }
    i32::try_from(days_from_civil(y, m, d)).map_err(|_| bad())
}

fn days_in_month(y: i64, m: u32) -> u32 {
    match m {
        2 if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// `YYYY-MM-DDTHH:MM:SS[.f{1,9}](Z|+00:00)` -> ns since epoch (fractions longer than 9 digits are truncated, as et's
/// `iso_ns`).
pub fn iso_ns(s: &str) -> QtResult<Ts> {
    let bad = || QtError::InputFormat {
        location: "time".into(),
        msg: format!("not an ISO-8601 UTC time: {s:?}"),
    };
    let t = s.trim();
    let t = t
        .strip_suffix('Z')
        .or_else(|| t.strip_suffix("+00:00"))
        .ok_or_else(bad)?;
    let (date, time) = t.split_once('T').ok_or_else(bad)?;
    let days = i64::from(parse_date(date).map_err(|_| bad())?);
    let (hms, frac) = time.split_once('.').unwrap_or((time, ""));
    let p: Vec<&str> = hms.split(':').collect();
    if p.len() != 3 || p.iter().any(|x| x.len() != 2) || !frac.bytes().all(|c| c.is_ascii_digit()) {
        return Err(bad());
    }
    let h: i64 = p[0].parse().map_err(|_| bad())?;
    let mi: i64 = p[1].parse().map_err(|_| bad())?;
    let se: i64 = p[2].parse().map_err(|_| bad())?;
    if h > 23 || mi > 59 || se > 60 {
        return Err(bad());
    }
    let mut f = frac
        .as_bytes()
        .iter()
        .take(9)
        .fold(0i64, |a, c| a * 10 + i64::from(c - b'0'));
    for _ in frac.len().min(9)..9 {
        f *= 10;
    }
    let secs = days * 86_400 + h * 3600 + mi * 60 + se;
    secs.checked_mul(1_000_000_000)
        .and_then(|x| x.checked_add(f))
        .ok_or_else(bad)
}

/// Integer count of `unit_ns` (e.g. microseconds = 1_000) -> ns, checked.
pub fn scaled_ns(v: i64, unit_ns: i64, what: &str) -> QtResult<Ts> {
    v.checked_mul(unit_ns)
        .filter(|x| *x >= 0)
        .ok_or_else(|| QtError::InputFormat {
            location: what.into(),
            msg: format!("timestamp {v} out of range"),
        })
}

/// Session clock used to cut session dates.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionClock {
    /// UTC days (crypto default).
    Utc,
    /// US Eastern local days (DST rule since 2007).
    Eastern,
}

/// UTC ns of local midnight of `date` (days since epoch), exact in `i128` for every `i32` date.
fn midnight_wide(clock: SessionClock, date: i32) -> i128 {
    let base = i128::from(date) * i128::from(NS_PER_DAY);
    match clock {
        SessionClock::Utc => base,
        SessionClock::Eastern => base + i128::from(eastern_offset_h(date)) * 3_600_000_000_000,
    }
}

/// UTC ns of local midnight of `date` (days since epoch); `None` when that instant does not fit in `i64` (outside
/// 1677-09-21T00:12:43Z..2262-04-11T23:47:16Z: in UTC the first representable midnight is 1677-09-22's, in US
/// Eastern 1677-09-21's; in both the last is 2262-04-11's).
pub fn checked_midnight_ns(clock: SessionClock, date: i32) -> Option<Ts> {
    Ts::try_from(midnight_wide(clock, date)).ok()
}

/// UTC ns of local midnight of `date` (days since epoch), saturating at `i64::MIN` / `i64::MAX` outside the
/// representable range (use [`checked_midnight_ns`] to reject such dates).
pub fn midnight_ns(clock: SessionClock, date: i32) -> Ts {
    checked_midnight_ns(clock, date).unwrap_or(if date < 0 { Ts::MIN } else { Ts::MAX })
}

/// Hours behind UTC at local midnight of `date` in US Eastern: DST from the second Sunday of March to the first
/// Sunday of November (switches at 02:00 local, so midnight of the March switch day is still standard time).
fn eastern_offset_h(date: i32) -> i64 {
    let d = i64::from(date);
    let (y, _, _) = civil_from_days(d);
    // weekday: 1970-01-01 was a Thursday (0 = Monday convention -> 3)
    let weekday = |day: i64| (day + 3).rem_euclid(7); // 0 = Monday .. 6 = Sunday
    let march1 = days_from_civil(y, 3, 1);
    let dst_start = march1 + (6 - weekday(march1)).rem_euclid(7) + 7;
    let nov1 = days_from_civil(y, 11, 1);
    let dst_end = nov1 + (6 - weekday(nov1)).rem_euclid(7);
    if dst_start < d && d <= dst_end { 4 } else { 5 }
}

/// Local session date of `ts` under `clock`.
pub fn session_date_of(clock: SessionClock, ts: Ts) -> i32 {
    let guess = ts.div_euclid(NS_PER_DAY);
    let mut d = i32::try_from(guess).unwrap_or(if guess < 0 { i32::MIN } else { i32::MAX });
    // Eastern midnight is 4-5 h after UTC midnight: step back at most one day, forward never needed.
    // Compared in i128: near `i64::MAX` / `i64::MIN` the neighbouring midnights do not fit in `i64`.
    let t = i128::from(ts);
    while d > i32::MIN && midnight_wide(clock, d) > t {
        d -= 1;
    }
    while d < i32::MAX && midnight_wide(clock, d + 1) <= t {
        d += 1;
    }
    d
}

/// Greatest common divisor.
pub fn gcd(a: u64, b: u64) -> u64 {
    let (mut a, mut b) = (a, b);
    while b != 0 {
        (a, b) = (b, a % b);
    }
    a
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decimal_exactness() {
        assert_eq!(price("61200.12345678", -9).unwrap(), 61_200_123_456_780);
        assert_eq!(size("0.12345678", -8).unwrap(), 12_345_678);
        assert_eq!(size("0.00000001", -8).unwrap(), 1);
        assert_eq!(price("100.5", -9).unwrap(), 100_500_000_000);
        assert_eq!(price("0.000000001", -9).unwrap(), 1);
        assert_eq!(price("1e-05", -9).unwrap(), 10_000);
        assert_eq!(price("-1.5E2", -9).unwrap(), -150_000_000_000);
        assert_eq!(size("0", -8).unwrap(), 0);
        assert_eq!(size("0.0", -8).unwrap(), 0);
        assert_eq!(size("50", -8).unwrap(), 5_000_000_000);
        assert_eq!(signed_size("-0.2", -8).unwrap(), (-1, 20_000_000));
        assert_eq!(signed_size("0", -8).unwrap(), (0, 0));
        for (s, e) in [
            ("0.000000015", -8),
            ("61200.0000000001", -9),
            ("0.30000000000000004", -8),
        ] {
            let err = dec_units(s, e);
            assert!(err.is_err(), "{s}");
        }
        assert_eq!(size("0.000000015", -8).unwrap_err().code(), "QT-INPUT-004");
        assert_eq!(price("1.0000000005", -9).unwrap_err().code(), "QT-INPUT-004");
        assert!(size("-1", -8).is_err());
        assert!(price("abc", -9).is_err());
        assert!(price("1.2.3", -9).is_err());
        assert!(price("99999999999999999999", -9).is_err());
    }

    #[test]
    fn iso_times() {
        assert_eq!(
            iso_ns("2014-11-07T08:19:27.028459Z").unwrap(),
            1_415_348_367_028_459_000
        );
        assert_eq!(iso_ns("2014-11-07T08:19:27Z").unwrap(), 1_415_348_367_000_000_000);
        assert_eq!(
            iso_ns("2014-11-07T08:19:27.1234567891+00:00").unwrap(),
            1_415_348_367_123_456_789
        );
        assert!(iso_ns("2014-11-07 08:19:27Z").is_err());
        assert!(iso_ns("2014-13-07T08:19:27Z").is_err());
    }

    #[test]
    fn dates_and_midnights() {
        assert_eq!(parse_date("1970-01-01").unwrap(), 0);
        assert_eq!(parse_date("2026-10-12").unwrap(), 20_738);
        assert_eq!(civil_from_days(20_738), (2026, 10, 12));
        assert!(parse_date("2026-02-29").is_err());
        // et eastern_midnight_ns cases: 12302019 (EST) and the DST switch days of 2019
        let d = parse_date("2019-12-30").unwrap();
        assert_eq!(midnight_ns(SessionClock::Eastern, d), 1_577_682_000_000_000_000);
        let d = parse_date("2020-01-30").unwrap();
        assert_eq!(midnight_ns(SessionClock::Eastern, d), 1_580_360_400_000_000_000);
        let mar = parse_date("2019-03-10").unwrap();
        assert_eq!(
            midnight_ns(SessionClock::Eastern, mar) - i64::from(mar) * NS_PER_DAY,
            5 * 3_600_000_000_000
        );
        assert_eq!(
            midnight_ns(SessionClock::Eastern, mar + 1) - i64::from(mar + 1) * NS_PER_DAY,
            4 * 3_600_000_000_000
        );
        let nov = parse_date("2019-11-03").unwrap();
        assert_eq!(
            midnight_ns(SessionClock::Eastern, nov) - i64::from(nov) * NS_PER_DAY,
            4 * 3_600_000_000_000
        );
        assert_eq!(
            midnight_ns(SessionClock::Eastern, nov + 1) - i64::from(nov + 1) * NS_PER_DAY,
            5 * 3_600_000_000_000
        );
        let ts = 1_577_682_000_000_000_000;
        assert_eq!(
            session_date_of(SessionClock::Eastern, ts),
            parse_date("2019-12-30").unwrap()
        );
        assert_eq!(
            session_date_of(SessionClock::Eastern, ts - 1),
            parse_date("2019-12-29").unwrap()
        );
        assert_eq!(
            session_date_of(SessionClock::Utc, ts),
            parse_date("2019-12-30").unwrap()
        );
        assert_eq!(session_date_of(SessionClock::Utc, 0), 0);
        assert_eq!(gcd(10_000_000, 25_000_000), 5_000_000);
    }

    /// Fuzz regression (`fuzz/regressions/crypto_prepare/tardis_trade_in_last_i64_day`): the session date of a
    /// timestamp within a day of `i64::MAX` / `i64::MIN` needs a midnight that `i64` cannot hold; it used to overflow.
    #[test]
    fn extreme_timestamps_have_a_session_date() {
        let last = i32::try_from(i64::MAX / NS_PER_DAY).unwrap();
        for clock in [SessionClock::Utc, SessionClock::Eastern] {
            let d = session_date_of(clock, i64::MAX);
            assert_eq!(checked_midnight_ns(clock, d + 1), None, "{clock:?}");
            assert!(checked_midnight_ns(clock, d).is_some(), "{clock:?}");
            let d = session_date_of(clock, i64::MIN);
            assert!(checked_midnight_ns(clock, d).is_none_or(|m| m == i64::MIN), "{clock:?}");
            assert!(checked_midnight_ns(clock, d + 1).is_some(), "{clock:?}");
        }
        assert_eq!(session_date_of(SessionClock::Utc, i64::MAX), last);
        assert_eq!(
            checked_midnight_ns(SessionClock::Utc, last),
            Some(i64::from(last) * NS_PER_DAY)
        );
        // midnight_ns saturates outside the representable range.
        assert_eq!(midnight_ns(SessionClock::Utc, last + 1), i64::MAX);
        assert_eq!(midnight_ns(SessionClock::Eastern, i32::MAX), i64::MAX);
        assert_eq!(midnight_ns(SessionClock::Utc, i32::MIN), i64::MIN);
        // A year-9999 session date (accepted by parse_date) has no representable midnight.
        assert_eq!(
            checked_midnight_ns(SessionClock::Utc, parse_date("9999-12-31").unwrap()),
            None
        );
    }
}
