//! Deterministic randomness (SPEC 3.1, 7.2): `stable_seed` (FNV-1a 64 + splitmix64), `splitmix64`, xoshiro256**.
//! The Python mirrors (`queuetruth.stats.stable_seed`, `python/tests/reference/rng.py`) must produce identical values.

const GOLDEN_GAMMA: u64 = 0x9e37_79b9_7f4a_7c15;
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

/// One splitmix64 step: adds the golden gamma to `x`, then mixes (wrapping arithmetic).
pub fn splitmix64(x: u64) -> u64 {
    let mut z = x.wrapping_add(GOLDEN_GAMMA);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

/// FNV-1a 64 over `run_seed` (8 LE bytes), then per part its length (4 LE bytes) and bytes; finally one splitmix64.
pub fn stable_seed(run_seed: u64, parts: &[&[u8]]) -> u64 {
    let mut h = FNV_OFFSET;
    let mut feed = |bytes: &[u8]| {
        for &b in bytes {
            h ^= u64::from(b);
            h = h.wrapping_mul(FNV_PRIME);
        }
    };
    feed(&run_seed.to_le_bytes());
    for part in parts {
        let len = u32::try_from(part.len()).unwrap_or(u32::MAX);
        feed(&len.to_le_bytes());
        feed(part);
    }
    splitmix64(h)
}

/// xoshiro256** (Blackman-Vigna), seeded from four successive splitmix64 outputs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Xoshiro256StarStar {
    s: [u64; 4],
}

impl Xoshiro256StarStar {
    /// State = `splitmix64(seed)`, `splitmix64(seed + g)`, `splitmix64(seed + 2g)`, `splitmix64(seed + 3g)`.
    pub fn new(seed: u64) -> Self {
        let mut x = seed;
        let mut s = [0u64; 4];
        for slot in &mut s {
            *slot = splitmix64(x);
            x = x.wrapping_add(GOLDEN_GAMMA);
        }
        Self { s }
    }

    /// Next 64-bit output.
    pub fn next_u64(&mut self) -> u64 {
        let result = self.s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
        let t = self.s[1] << 17;
        self.s[2] ^= self.s[0];
        self.s[3] ^= self.s[1];
        self.s[1] ^= self.s[2];
        self.s[0] ^= self.s[3];
        self.s[2] ^= t;
        self.s[3] = self.s[3].rotate_left(45);
        result
    }

    /// Uniform in `[0, 1)`: `(next_u64() >> 11) * 2^-53`.
    pub fn next_f64(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    /// `lo + next_u64() % (hi - lo + 1)` (modulo bias accepted). The full range `0..=u64::MAX` returns
    /// `next_u64()`; `hi < lo` returns `lo` without drawing.
    pub fn range_inclusive(&mut self, lo: u64, hi: u64) -> u64 {
        if hi < lo {
            return lo;
        }
        let span = (hi - lo).wrapping_add(1);
        let x = self.next_u64();
        if span == 0 { x } else { lo + x % span }
    }
}

#[cfg(test)]
impl Xoshiro256StarStar {
    /// Builds a generator from a raw state (tests only).
    pub(crate) fn from_state(s: [u64; 4]) -> Self {
        Self { s }
    }
}
