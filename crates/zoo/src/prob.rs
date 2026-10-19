//! Probability functions of the hftbacktest ProbQueueModel family and the Databento estimator (SPEC 5.3.3 b, g).
//!
//! Ported from hftbacktest `queue.rs` at commit 5f3ec40b2afb764e0fea112f941ed85523ef4e88 (MIT, see NOTICE and
//! third_party/licenses/hftbacktest-MIT.txt). Every expression keeps the exact f64 operation order of SPEC 5.3.3 so
//! faithful mode reproduces hbt bit for bit. `pi` is the probability that a level decrease is **behind** the order.

/// hbt `PowerProbQueueFunc`: `b^n / (b^n + f^n)`.
pub fn power(n: f64, front: f64, back: f64) -> f64 {
    back.powf(n) / (back.powf(n) + front.powf(n))
}

/// hbt `PowerProbQueueFunc2`: `b^n / (b + f)^n`.
pub fn power2(n: f64, front: f64, back: f64) -> f64 {
    back.powf(n) / (back + front).powf(n)
}

/// hbt `PowerProbQueueFunc3`: `1 - (f / (f + b))^n`.
pub fn power3(n: f64, front: f64, back: f64) -> f64 {
    1.0 - (front / (front + back)).powf(n)
}

/// hbt `LogProbQueueFunc`: `ln(1 + b) / (ln(1 + b) + ln(1 + f))`.
pub fn log(front: f64, back: f64) -> f64 {
    (1.0 + back).ln() / ((1.0 + back).ln() + (1.0 + front).ln())
}

/// hbt `LogProbQueueFunc2`: `ln(1 + b) / ln(1 + (b + f))`.
pub fn log2(front: f64, back: f64) -> f64 {
    (1.0 + back).ln() / (1.0 + (back + front)).ln()
}

/// Databento tutorial: probability mass of a decrease allocated to the front, `x = front / (front + back)`,
/// `k` in (-1, 1): `x^(1 / (1 + k))` for `k <= 0`, `1 - (1 - x)^(1 / (1 - k))` for `k > 0`.
pub fn db_p_front(k: f64, x: f64) -> f64 {
    if k <= 0.0 {
        x.powf(1.0 / (1.0 + k))
    } else {
        1.0 - (1.0 - x).powf(1.0 / (1.0 - k))
    }
}

/// hbt execution lots: `(-front / lot).round() as i64` (half away from zero; saturating cast, NaN -> 0).
pub fn hbt_exec_lots(front: f64, lot: f64) -> i64 {
    (-front / lot).round() as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Faithful hbt ProbQueueModel depth update (SPEC 5.3.3 b), private test helper: returns (pi, F').
    /// `pi` is NaN when the update returned before computing it.
    fn faithful_depth(prob: impl Fn(f64, f64) -> f64, f: f64, t: f64, prev: f64, new: f64) -> (f64, f64) {
        let mut chg = prev - new;
        chg -= t;
        if chg < 0.0 {
            return (f64::NAN, f.min(new));
        }
        let front = f;
        let back = prev - front;
        let mut pi = prob(front, back);
        if pi.is_infinite() {
            pi = 1.0;
        }
        let est = front - (1.0 - pi) * chg + (back - pi * chg).min(0.0);
        (pi, est.min(new))
    }

    fn bits_eq(a: f64, b: f64) -> bool {
        a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan())
    }

    /// O-ZOO-1: golden table of SPEC 5.3.3(b), bit for bit (`f64::to_bits`) on macOS arm64, within 4 ulp elsewhere.
    #[test]
    fn o_zoo_1_golden_prob_and_depth_update() {
        type Prob = Box<dyn Fn(f64, f64) -> f64>;
        /// (name, prob, F, T, prev, new, expected pi, expected F').
        type Case = (&'static str, Prob, f64, f64, f64, f64, Option<f64>, f64);
        let cases: Vec<Case> = vec![
            (
                "power n=2",
                Box::new(|f, b| power(2.0, f, b)),
                30.0,
                0.0,
                100.0,
                80.0,
                Some(0.8448275862068966),
                26.896551724137932,
            ),
            (
                "power2 n=2",
                Box::new(|f, b| power2(2.0, f, b)),
                30.0,
                0.0,
                100.0,
                80.0,
                Some(0.49),
                19.8,
            ),
            (
                "power3 n=2",
                Box::new(|f, b| power3(2.0, f, b)),
                30.0,
                0.0,
                100.0,
                80.0,
                Some(0.91),
                28.2,
            ),
            (
                "log",
                Box::new(log),
                30.0,
                0.0,
                100.0,
                80.0,
                Some(0.5538345145878271),
                21.07669029175654,
            ),
            (
                "log2",
                Box::new(log2),
                30.0,
                0.0,
                100.0,
                80.0,
                Some(0.9236334915818913),
                28.472669831637827,
            ),
            (
                "power3 n=3 (back overflow)",
                Box::new(|f, b| power3(3.0, f, b)),
                90.0,
                0.0,
                100.0,
                40.0,
                Some(0.2709999999999999),
                40.0,
            ),
            (
                "power n=2, trade netting",
                Box::new(|f, b| power(2.0, f, b)),
                15.0,
                15.0,
                100.0,
                80.0,
                Some(0.9697986577181208),
                14.848993288590604,
            ),
            (
                "power n=1.5, negative F (NaN)",
                Box::new(|f, b| power(1.5, f, b)),
                -0.4,
                0.0,
                50.0,
                40.0,
                Some(f64::NAN),
                40.0,
            ),
            (
                "power3 n=2, increase",
                Box::new(|f, b| power3(2.0, f, b)),
                30.0,
                0.0,
                100.0,
                120.0,
                None,
                30.0,
            ),
        ];
        let exact = cfg!(all(target_os = "macos", target_arch = "aarch64"));
        for (name, prob, f, t, prev, new, pi_exp, f_exp) in cases {
            let (pi, f_new) = faithful_depth(&prob, f, t, prev, new);
            if let Some(p) = pi_exp {
                if exact {
                    assert!(bits_eq(pi, p), "{name}: pi {pi:?} != {p:?}");
                } else {
                    assert!(
                        (p.is_nan() && pi.is_nan()) || (pi - p).abs() <= 4.0 * f64::EPSILON * p.abs(),
                        "{name}: pi"
                    );
                }
            }
            if exact {
                assert!(bits_eq(f_new, f_exp), "{name}: F' {f_new:?} != {f_exp:?}");
            } else {
                assert!(
                    (f_new - f_exp).abs() <= 4.0 * f64::EPSILON * f_exp.abs().max(1.0),
                    "{name}: F'"
                );
            }
        }
    }

    /// O-ZOO-1: exec-lots list of SPEC 5.3.3.
    #[test]
    fn o_zoo_1_exec_lots() {
        let cases = [
            (0.0, 1.0, 0),
            (-0.49, 1.0, 0),
            (-0.5, 1.0, 1),
            (-1.0, 1.0, 1),
            (-2.5, 1.0, 3),
            (-0.25, 0.5, 1),
            (-0.6, 0.5, 1),
            (3.0, 1.0, -3),
            (f64::NAN, 1.0, 0),
            (f64::NEG_INFINITY, 1.0, i64::MAX),
        ];
        for (front, lot, lots) in cases {
            assert_eq!(hbt_exec_lots(front, lot), lots, "front {front}, lot {lot}");
        }
    }

    #[test]
    fn db_p_front_shapes() {
        assert_eq!(db_p_front(0.0, 0.3).to_bits(), 0.3f64.to_bits());
        let k = -0.8;
        assert_eq!(db_p_front(k, 0.25).to_bits(), 0.25f64.powf(1.0 / (1.0 + k)).to_bits());
        let k = 0.5;
        assert_eq!(
            db_p_front(k, 0.25).to_bits(),
            (1.0 - 0.75f64.powf(1.0 / (1.0 - k))).to_bits()
        );
        for x in [0.0, 0.1, 0.5, 0.9, 1.0] {
            for k in [-0.95, -0.5, 0.0, 0.5, 0.95] {
                let p = db_p_front(k, x);
                assert!((0.0..=1.0).contains(&p), "k {k} x {x}: {p}");
            }
        }
    }

    #[test]
    fn prob_complements_and_specials() {
        // power3 with n = 1 is back / (front + back).
        assert!((power3(1.0, 30.0, 70.0) - 0.7).abs() < 1e-15);
        // Infinite or NaN inputs propagate exactly as f64 does (hbt semantics; callers handle them).
        assert!(power(2.0, 0.0, 0.0).is_nan());
        assert!(log(0.0, 0.0).is_nan());
        assert_eq!(power2(2.0, 0.0, 5.0), 1.0);
    }
}
