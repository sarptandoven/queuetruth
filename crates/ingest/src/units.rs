//! Exact fixed-point conversions: rescaling integer prices between exponents and parsing decimal strings at a
//! given exponent. Anything not exactly representable is `QT-INPUT-004`.

use qt_types::{QtError, QtResult, UNDEF_PRICE};

/// `10^k` for `k` in `0..=18`.
pub fn pow10(k: u32) -> Option<i64> {
    10i64.checked_pow(k)
}

/// Rescales `v` (at `10^from_exp`) to `10^to_exp`; `None` if not exact or out of range. `UNDEF_PRICE` passes through.
pub fn rescale(v: i64, from_exp: i8, to_exp: i8) -> Option<i64> {
    if v == UNDEF_PRICE || from_exp == to_exp {
        return Some(v);
    }
    let diff = i32::from(from_exp) - i32::from(to_exp);
    if diff > 0 {
        v.checked_mul(pow10(u32::try_from(diff).ok()?)?)
    } else {
        let p = pow10(u32::try_from(-diff).ok()?)?;
        (v % p == 0).then_some(v / p)
    }
}

/// Parses a plain decimal string (`"0.01"`, `"25"`, `"-1.5"`) as an integer at `10^exp`, exactly.
pub fn decimal_to_fixed(s: &str, exp: i8) -> QtResult<i64> {
    let err = |m: &str| QtError::Units(format!("{s:?} at 10^{exp}: {m}"));
    let t = s.trim();
    let (neg, body) = match t.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    let (int, frac) = body.split_once('.').unwrap_or((body, ""));
    if int.is_empty() && frac.is_empty() {
        return Err(err("not a decimal number"));
    }
    if !int.bytes().all(|b| b.is_ascii_digit()) || !frac.bytes().all(|b| b.is_ascii_digit()) {
        return Err(err("not a decimal number"));
    }
    let frac = frac.trim_end_matches('0');
    // value = digits * 10^(-frac.len()); target units = value * 10^(-exp)
    let digits = format!("{int}{frac}");
    let digits = digits.trim_start_matches('0');
    let mut v: i64 = 0;
    for b in digits.bytes() {
        v = v
            .checked_mul(10)
            .and_then(|x| x.checked_add(i64::from(b - b'0')))
            .ok_or_else(|| err("out of range"))?;
    }
    let src_exp = -i32::try_from(frac.len()).map_err(|_| err("too many digits"))?;
    let src_exp = i8::try_from(src_exp).map_err(|_| err("too many digits"))?;
    let out = rescale(v, src_exp, exp).ok_or_else(|| err("not exactly representable"))?;
    Ok(if neg { -out } else { out })
}

/// `decimal_to_fixed` for a non-negative quantity.
pub fn decimal_to_units(s: &str, exp: i8) -> QtResult<u64> {
    let v = decimal_to_fixed(s, exp)?;
    u64::try_from(v).map_err(|_| QtError::Units(format!("{s:?}: negative quantity")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rescale_and_decimals_are_exact() {
        assert_eq!(rescale(1_852_500, -4, -9), Some(185_250_000_000));
        assert_eq!(rescale(185_250_000_000, -9, -4), Some(1_852_500));
        assert_eq!(rescale(185_250_000_001, -9, -4), None);
        assert_eq!(rescale(i64::MAX / 2, -4, -9), None);
        assert_eq!(rescale(UNDEF_PRICE, -9, -2), Some(UNDEF_PRICE));
        assert_eq!(decimal_to_fixed("0.01", -9).unwrap(), 10_000_000);
        assert_eq!(decimal_to_fixed("0.25", -2).unwrap(), 25);
        assert_eq!(decimal_to_fixed("25", 0).unwrap(), 25);
        assert_eq!(decimal_to_fixed("-1.50", -1).unwrap(), -15);
        assert_eq!(decimal_to_fixed(".5", -1).unwrap(), 5);
        assert_eq!(decimal_to_fixed("100", 2).unwrap(), 1);
        assert_eq!(decimal_to_units("1", 0).unwrap(), 1);
        assert_eq!(decimal_to_fixed("0.001", -2).unwrap_err().code(), "QT-INPUT-004");
        assert!(decimal_to_fixed("1e-3", -9).is_err());
        assert!(decimal_to_fixed("", -9).is_err());
        assert!(decimal_to_fixed("1.2.3", -9).is_err());
        assert!(decimal_to_units("-1", 0).is_err());
        assert!(decimal_to_fixed("99999999999999999999", 0).is_err());
    }
}
