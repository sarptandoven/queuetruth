//! Minimal JSON reader that keeps every number as its source text, so float-looking prices and amounts (Bitfinex
//! frames carry `61200.5` and `0.30000000000000004` as JSON numbers) convert to fixed point exactly or fail.

/// A parsed JSON value; numbers keep their literal text.
#[derive(Clone, Debug, PartialEq)]
pub enum J {
    /// `null`.
    Null,
    /// `true` / `false`.
    Bool(bool),
    /// A number, exactly as written.
    Num(String),
    /// A string (escapes decoded).
    Str(String),
    /// An array.
    Arr(Vec<J>),
    /// An object, keys in source order.
    Obj(Vec<(String, J)>),
}

impl J {
    /// Object member by key (first match).
    pub fn get(&self, key: &str) -> Option<&J> {
        match self {
            J::Obj(m) => m.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// String content.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            J::Str(s) => Some(s),
            _ => None,
        }
    }

    /// Array items.
    pub fn as_arr(&self) -> Option<&[J]> {
        match self {
            J::Arr(a) => Some(a),
            _ => None,
        }
    }

    /// Number text, or the content of a string (Bitstamp sends some numbers as strings).
    pub fn num_text(&self) -> Option<&str> {
        match self {
            J::Num(s) | J::Str(s) => Some(s),
            _ => None,
        }
    }

    /// Integer value of a number or a numeric string (no fraction, no exponent).
    pub fn as_i128(&self) -> Option<i128> {
        let t = self.num_text()?.trim();
        if t.is_empty() || t.contains(['.', 'e', 'E']) {
            return None;
        }
        t.parse::<i128>().ok()
    }

    /// `as_i128` narrowed to i64.
    pub fn as_i64(&self) -> Option<i64> {
        self.as_i128().and_then(|v| i64::try_from(v).ok())
    }

    /// `as_i128` narrowed to u64.
    pub fn as_u64(&self) -> Option<u64> {
        self.as_i128().and_then(|v| u64::try_from(v).ok())
    }

    /// True for `null`.
    pub fn is_null(&self) -> bool {
        matches!(self, J::Null)
    }
}

/// Parses one JSON document (trailing whitespace allowed).
pub fn parse(s: &str) -> Result<J, String> {
    let mut p = P {
        b: s.as_bytes(),
        i: 0,
        depth: 0,
    };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return Err(format!("trailing characters at byte {}", p.i));
    }
    Ok(v)
}

const MAX_DEPTH: u32 = 128;

struct P<'a> {
    b: &'a [u8],
    i: usize,
    depth: u32,
}

impl P<'_> {
    fn ws(&mut self) {
        while let Some(&c) = self.b.get(self.i) {
            if matches!(c, b' ' | b'\t' | b'\n' | b'\r') {
                self.i += 1;
            } else {
                break;
            }
        }
    }

    fn err<T>(&self, what: &str) -> Result<T, String> {
        Err(format!("{what} at byte {}", self.i))
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn lit(&mut self, word: &[u8], v: J) -> Result<J, String> {
        if self.b.get(self.i..self.i + word.len()) == Some(word) {
            self.i += word.len();
            Ok(v)
        } else {
            self.err("invalid literal")
        }
    }

    fn value(&mut self) -> Result<J, String> {
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => self.string().map(J::Str),
            Some(b't') => self.lit(b"true", J::Bool(true)),
            Some(b'f') => self.lit(b"false", J::Bool(false)),
            Some(b'n') => self.lit(b"null", J::Null),
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            Some(_) => self.err("unexpected character"),
            None => self.err("unexpected end of input"),
        }
    }

    fn enter(&mut self) -> Result<(), String> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return self.err("nesting too deep");
        }
        Ok(())
    }

    fn object(&mut self) -> Result<J, String> {
        self.enter()?;
        self.i += 1;
        let mut m = Vec::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            self.depth -= 1;
            return Ok(J::Obj(m));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return self.err("expected object key");
            }
            let k = self.string()?;
            self.ws();
            if self.peek() != Some(b':') {
                return self.err("expected ':'");
            }
            self.i += 1;
            self.ws();
            let v = self.value()?;
            m.push((k, v));
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    self.depth -= 1;
                    return Ok(J::Obj(m));
                }
                _ => return self.err("expected ',' or '}'"),
            }
        }
    }

    fn array(&mut self) -> Result<J, String> {
        self.enter()?;
        self.i += 1;
        let mut a = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            self.depth -= 1;
            return Ok(J::Arr(a));
        }
        loop {
            self.ws();
            a.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    self.depth -= 1;
                    return Ok(J::Arr(a));
                }
                _ => return self.err("expected ',' or ']'"),
            }
        }
    }

    fn number(&mut self) -> Result<J, String> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        let digits = |p: &mut Self| {
            let s = p.i;
            while p.peek().is_some_and(|c| c.is_ascii_digit()) {
                p.i += 1;
            }
            p.i - s
        };
        if digits(self) == 0 {
            return self.err("invalid number");
        }
        if self.peek() == Some(b'.') {
            self.i += 1;
            if digits(self) == 0 {
                return self.err("invalid number fraction");
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.i += 1;
            }
            if digits(self) == 0 {
                return self.err("invalid number exponent");
            }
        }
        let text = std::str::from_utf8(&self.b[start..self.i]).map_err(|e| e.to_string())?;
        Ok(J::Num(text.to_string()))
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let h = self
            .b
            .get(self.i..self.i + 4)
            .ok_or_else(|| format!("short \\u escape at byte {}", self.i))?;
        let s = std::str::from_utf8(h).map_err(|e| e.to_string())?;
        let v = u32::from_str_radix(s, 16).map_err(|_| format!("bad \\u escape at byte {}", self.i))?;
        self.i += 4;
        Ok(v)
    }

    fn string(&mut self) -> Result<String, String> {
        self.i += 1;
        let mut out = String::new();
        loop {
            let start = self.i;
            while let Some(&c) = self.b.get(self.i) {
                if c == b'"' || c == b'\\' || c < 0x20 {
                    break;
                }
                self.i += 1;
            }
            out.push_str(std::str::from_utf8(&self.b[start..self.i]).map_err(|e| e.to_string())?);
            match self.peek() {
                Some(b'"') => {
                    self.i += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.i += 1;
                    let c = self.peek().ok_or_else(|| "unterminated escape".to_string())?;
                    self.i += 1;
                    match c {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            let cp = if (0xD800..0xDC00).contains(&hi) {
                                if self.b.get(self.i..self.i + 2) != Some(b"\\u") {
                                    return self.err("unpaired surrogate");
                                }
                                self.i += 2;
                                let lo = self.hex4()?;
                                if !(0xDC00..0xE000).contains(&lo) {
                                    return self.err("invalid low surrogate");
                                }
                                0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
                            } else {
                                hi
                            };
                            out.push(char::from_u32(cp).ok_or_else(|| "invalid code point".to_string())?);
                        }
                        _ => return self.err("invalid escape"),
                    }
                }
                _ => return self.err("unterminated string"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_keep_their_text() {
        let v = parse(r#"[1,[[101,61200.5,0.30000000000000004],-2e-3],"xé\n",null,true,{"a":{"b":[]}}]"#)
            .unwrap_or(J::Null);
        let a = v.as_arr().unwrap_or(&[]);
        assert_eq!(a.len(), 6);
        let inner = a[1].as_arr().unwrap_or(&[]);
        let first = inner[0].as_arr().unwrap_or(&[]);
        assert_eq!(first[1].num_text(), Some("61200.5"));
        assert_eq!(first[2].num_text(), Some("0.30000000000000004"));
        assert_eq!(inner[1].num_text(), Some("-2e-3"));
        assert_eq!(a[2].as_str(), Some("xé\n"));
        assert!(a[3].is_null());
        assert_eq!(a[5].get("a").and_then(|x| x.get("b")), Some(&J::Arr(vec![])));
        assert_eq!(J::Num("2060025400799234".into()).as_u64(), Some(2_060_025_400_799_234));
        assert_eq!(J::Str("1791770863525000".into()).as_i64(), Some(1_791_770_863_525_000));
        assert_eq!(J::Num("1.5".into()).as_i64(), None);
    }

    #[test]
    fn malformed_inputs_fail() {
        for bad in ["", "[1,", "{\"a\" 1}", "[01x]", "\"abc", "[1] x", "-", "1.", "1e"] {
            assert!(parse(bad).is_err(), "{bad}");
        }
        let deep = "[".repeat(200) + &"]".repeat(200);
        assert!(parse(&deep).is_err());
    }
}
