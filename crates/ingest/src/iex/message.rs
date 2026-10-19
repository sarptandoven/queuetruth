//! DEEP+ message layouts (IEX DEEP+ Specification v1.06, "Administrative Message Formats" pp.13-20 and "Trading
//! Message Formats" pp.21-27). All fields little endian; Price = 8-byte signed fixed point with 4 decimals;
//! Timestamp = 8-byte signed nanoseconds since the POSIX epoch (UTC); Symbol = 8 ASCII bytes, space padded.
//!
//! The spec (p.4, "Architecture") reserves the right to add message types and to grow any message at its end:
//! `parse` reads the documented prefix of a known type and ignores trailing bytes (the caller counts them as
//! over-length), returns `Unknown` for a type it does not know and `ParseError::Short` for a known type shorter than
//! its layout. Nothing here panics on any input.

/// 8-byte space-padded symbol (Nasdaq Integrated symbology).
pub type Symbol = [u8; 8];

/// One DEEP+ message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Message {
    /// `S` (0x53) System Event, 10 bytes.
    SystemEvent {
        /// Event: `O` start of messages, `S` start of system hours, `R` start of regular market hours, `M` end of
        /// regular market hours, `E` end of system hours, `C` end of messages.
        event: u8,
        /// Timestamp.
        ts: i64,
    },
    /// `D` (0x44) Security Directory, 31 bytes.
    SecurityDirectory {
        /// Flags (0x80 test security, 0x40 when issued, 0x20 ETP).
        flags: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Round lot size.
        round_lot: u32,
        /// Adjusted previous official closing price (4 decimals).
        adjusted_poc: i64,
        /// LULD tier (0, 1, 2).
        luld_tier: u8,
    },
    /// `H` (0x48) Trading Status, 22 bytes.
    TradingStatus {
        /// `H` halted, `O` order acceptance period, `P` paused, `T` trading.
        status: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Reason (4 bytes).
        reason: [u8; 4],
    },
    /// `I` (0x49) Retail Liquidity Indicator, 18 bytes.
    RetailLiquidity {
        /// Indicator (space, `A`, `B`, `C`).
        indicator: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
    },
    /// `O` (0x4f) Operational Halt Status, 18 bytes.
    OperationalHalt {
        /// `O` halted on IEX, `N` not halted.
        status: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
    },
    /// `P` (0x50) Short Sale Price Test Status, 19 bytes.
    ShortSalePriceTest {
        /// 0 not in effect, 1 in effect.
        status: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Detail (space, `A`, `C`, `D`, `N`).
        detail: u8,
    },
    /// `E` (0x45) Security Event, 18 bytes.
    SecurityEvent {
        /// `O` opening process complete, `C` closing process complete.
        event: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
    },
    /// `a` (0x61) Add Order, 38 bytes: a displayed order added to the IEX book.
    AddOrder {
        /// `8` (0x38) buy, `5` (0x35) sell.
        side: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Order ID.
        order_id: i64,
        /// Quoted size.
        size: u32,
        /// Booking price (4 decimals).
        price: i64,
    },
    /// `M` (0x4D) Order Modify, 38 bytes: price, size or priority of a displayed order changed.
    OrderModify {
        /// Bit 0: 0 = reset priority, 1 = maintain priority; bits 1-7 reserved.
        flags: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Order ID reference.
        order_id: i64,
        /// New total quoted size.
        size: u32,
        /// Booking price (4 decimals).
        price: i64,
    },
    /// `R` (0x52) Order Delete, 26 bytes.
    OrderDelete {
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Order ID reference.
        order_id: i64,
    },
    /// `L` (0x4C) Order Executed, 46 bytes: a displayed order executed against.
    OrderExecuted {
        /// Sale condition flags (Appendix A).
        sale_flags: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Order ID reference.
        order_id: i64,
        /// Trade volume.
        size: u32,
        /// Trade price (4 decimals; may differ from the booked price: price improvement or sliding).
        price: i64,
        /// IEX trade id.
        trade_id: i64,
    },
    /// `T` (0x54) Trade, 38 bytes: non-displayed against non-displayed (no displayed quantity changes).
    Trade {
        /// Sale condition flags.
        sale_flags: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Trade volume.
        size: u32,
        /// Trade price.
        price: i64,
        /// IEX trade id.
        trade_id: i64,
    },
    /// `B` (0x42) Trade Break, 38 bytes.
    TradeBreak {
        /// Sale condition flags.
        sale_flags: u8,
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
        /// Trade break volume.
        size: u32,
        /// Trade break price.
        price: i64,
        /// Trade id of the broken `L` / `T`.
        trade_id: i64,
    },
    /// `C` (0x43) Clear Book, 18 bytes: the book of the symbol was cleared of all orders.
    ClearBook {
        /// Timestamp.
        ts: i64,
        /// Symbol.
        symbol: Symbol,
    },
    /// A type this reader does not know (skipped, counted).
    Unknown {
        /// Message type byte.
        kind: u8,
    },
}

/// Sale condition flag bits (Appendix A).
pub mod sale {
    /// Intermarket sweep order.
    pub const ISO: u8 = 0x80;
    /// Extended hours (Form T).
    pub const EXTENDED_HOURS: u8 = 0x40;
    /// Odd lot.
    pub const ODD_LOT: u8 = 0x20;
    /// Trade-through exempt.
    pub const TRADE_THROUGH_EXEMPT: u8 = 0x10;
    /// Single-price cross (auction).
    pub const SINGLE_PRICE_CROSS: u8 = 0x08;
}

/// Order Modify flag bit 0: 1 = maintain priority.
pub const MODIFY_MAINTAIN_PRIORITY: u8 = 0x01;

/// Every message type of the v1.06 spec.
pub const KNOWN_TYPES: [u8; 14] = *b"SDHIOPEaMRLTBC";

/// Documented length of a known type (`None`: unknown).
pub const fn message_len(kind: u8) -> Option<usize> {
    Some(match kind {
        b'S' => 10,
        b'D' => 31,
        b'H' => 22,
        b'I' | b'O' | b'E' | b'C' => 18,
        b'P' => 19,
        b'a' | b'M' | b'T' | b'B' => 38,
        b'R' => 26,
        b'L' => 46,
        _ => return None,
    })
}

/// A known type shorter than its layout, or an empty message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ParseError {
    /// Zero-length message data.
    Empty,
    /// Known type with fewer bytes than its layout.
    Short {
        /// Message type.
        kind: u8,
        /// Bytes present.
        len: usize,
        /// Bytes required.
        need: usize,
    },
}

#[inline]
fn u32_at(b: &[u8], o: usize) -> u32 {
    let mut a = [0u8; 4];
    a.copy_from_slice(&b[o..o + 4]);
    u32::from_le_bytes(a)
}

#[inline]
fn i64_at(b: &[u8], o: usize) -> i64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[o..o + 8]);
    i64::from_le_bytes(a)
}

#[inline]
fn sym_at(b: &[u8], o: usize) -> Symbol {
    let mut s = [0u8; 8];
    s.copy_from_slice(&b[o..o + 8]);
    s
}

/// Parses one message (its Message Data, without the 2-byte length).
pub fn parse(b: &[u8]) -> Result<Message, ParseError> {
    let Some(&kind) = b.first() else {
        return Err(ParseError::Empty);
    };
    let Some(need) = message_len(kind) else {
        return Ok(Message::Unknown { kind });
    };
    if b.len() < need {
        return Err(ParseError::Short {
            kind,
            len: b.len(),
            need,
        });
    }
    let (f1, ts) = (b[1], i64_at(b, 2));
    Ok(match kind {
        b'S' => Message::SystemEvent { event: f1, ts },
        b'D' => Message::SecurityDirectory {
            flags: f1,
            ts,
            symbol: sym_at(b, 10),
            round_lot: u32_at(b, 18),
            adjusted_poc: i64_at(b, 22),
            luld_tier: b[30],
        },
        b'H' => {
            let mut reason = [0u8; 4];
            reason.copy_from_slice(&b[18..22]);
            Message::TradingStatus {
                status: f1,
                ts,
                symbol: sym_at(b, 10),
                reason,
            }
        }
        b'I' => Message::RetailLiquidity {
            indicator: f1,
            ts,
            symbol: sym_at(b, 10),
        },
        b'O' => Message::OperationalHalt {
            status: f1,
            ts,
            symbol: sym_at(b, 10),
        },
        b'P' => Message::ShortSalePriceTest {
            status: f1,
            ts,
            symbol: sym_at(b, 10),
            detail: b[18],
        },
        b'E' => Message::SecurityEvent {
            event: f1,
            ts,
            symbol: sym_at(b, 10),
        },
        b'a' => Message::AddOrder {
            side: f1,
            ts,
            symbol: sym_at(b, 10),
            order_id: i64_at(b, 18),
            size: u32_at(b, 26),
            price: i64_at(b, 30),
        },
        b'M' => Message::OrderModify {
            flags: f1,
            ts,
            symbol: sym_at(b, 10),
            order_id: i64_at(b, 18),
            size: u32_at(b, 26),
            price: i64_at(b, 30),
        },
        b'R' => Message::OrderDelete {
            ts,
            symbol: sym_at(b, 10),
            order_id: i64_at(b, 18),
        },
        b'L' => Message::OrderExecuted {
            sale_flags: f1,
            ts,
            symbol: sym_at(b, 10),
            order_id: i64_at(b, 18),
            size: u32_at(b, 26),
            price: i64_at(b, 30),
            trade_id: i64_at(b, 38),
        },
        b'T' => Message::Trade {
            sale_flags: f1,
            ts,
            symbol: sym_at(b, 10),
            size: u32_at(b, 18),
            price: i64_at(b, 22),
            trade_id: i64_at(b, 30),
        },
        b'B' => Message::TradeBreak {
            sale_flags: f1,
            ts,
            symbol: sym_at(b, 10),
            size: u32_at(b, 18),
            price: i64_at(b, 22),
            trade_id: i64_at(b, 30),
        },
        b'C' => Message::ClearBook {
            ts,
            symbol: sym_at(b, 10),
        },
        _ => Message::Unknown { kind },
    })
}

impl Message {
    /// Message type byte.
    pub fn kind(&self) -> u8 {
        match self {
            Self::SystemEvent { .. } => b'S',
            Self::SecurityDirectory { .. } => b'D',
            Self::TradingStatus { .. } => b'H',
            Self::RetailLiquidity { .. } => b'I',
            Self::OperationalHalt { .. } => b'O',
            Self::ShortSalePriceTest { .. } => b'P',
            Self::SecurityEvent { .. } => b'E',
            Self::AddOrder { .. } => b'a',
            Self::OrderModify { .. } => b'M',
            Self::OrderDelete { .. } => b'R',
            Self::OrderExecuted { .. } => b'L',
            Self::Trade { .. } => b'T',
            Self::TradeBreak { .. } => b'B',
            Self::ClearBook { .. } => b'C',
            Self::Unknown { kind } => *kind,
        }
    }

    /// Timestamp (`None` for `Unknown`).
    pub fn ts(&self) -> Option<i64> {
        match *self {
            Self::SystemEvent { ts, .. }
            | Self::SecurityDirectory { ts, .. }
            | Self::TradingStatus { ts, .. }
            | Self::RetailLiquidity { ts, .. }
            | Self::OperationalHalt { ts, .. }
            | Self::ShortSalePriceTest { ts, .. }
            | Self::SecurityEvent { ts, .. }
            | Self::AddOrder { ts, .. }
            | Self::OrderModify { ts, .. }
            | Self::OrderDelete { ts, .. }
            | Self::OrderExecuted { ts, .. }
            | Self::Trade { ts, .. }
            | Self::TradeBreak { ts, .. }
            | Self::ClearBook { ts, .. } => Some(ts),
            Self::Unknown { .. } => None,
        }
    }

    /// Symbol (`None` for `SystemEvent` and `Unknown`).
    pub fn symbol(&self) -> Option<&Symbol> {
        match self {
            Self::SecurityDirectory { symbol, .. }
            | Self::TradingStatus { symbol, .. }
            | Self::RetailLiquidity { symbol, .. }
            | Self::OperationalHalt { symbol, .. }
            | Self::ShortSalePriceTest { symbol, .. }
            | Self::SecurityEvent { symbol, .. }
            | Self::AddOrder { symbol, .. }
            | Self::OrderModify { symbol, .. }
            | Self::OrderDelete { symbol, .. }
            | Self::OrderExecuted { symbol, .. }
            | Self::Trade { symbol, .. }
            | Self::TradeBreak { symbol, .. }
            | Self::ClearBook { symbol, .. } => Some(symbol),
            Self::SystemEvent { .. } | Self::Unknown { .. } => None,
        }
    }
}

/// Symbol without its space padding (and anything after a NUL), as text.
pub fn symbol_str(s: &Symbol) -> String {
    let end = s.iter().position(|&b| b == 0).unwrap_or(8);
    String::from_utf8_lossy(&s[..end]).trim_end().to_owned()
}

/// Space-padded symbol from text (longer names are cut at 8 bytes).
pub fn symbol(name: &str) -> Symbol {
    let mut s = [b' '; 8];
    for (d, b) in s.iter_mut().zip(name.bytes()) {
        *d = b;
    }
    s
}

/// Appends the Message Data of `m` (no length prefix) in the documented layout; `Unknown` writes its type byte.
pub fn encode(m: &Message, out: &mut Vec<u8>) {
    let start = out.len();
    out.push(m.kind());
    let le64 = |out: &mut Vec<u8>, v: i64| out.extend_from_slice(&v.to_le_bytes());
    let le32 = |out: &mut Vec<u8>, v: u32| out.extend_from_slice(&v.to_le_bytes());
    match *m {
        Message::SystemEvent { event, ts } => {
            out.push(event);
            le64(out, ts);
        }
        Message::SecurityDirectory {
            flags,
            ts,
            symbol,
            round_lot,
            adjusted_poc,
            luld_tier,
        } => {
            out.push(flags);
            le64(out, ts);
            out.extend_from_slice(&symbol);
            le32(out, round_lot);
            le64(out, adjusted_poc);
            out.push(luld_tier);
        }
        Message::TradingStatus {
            status,
            ts,
            symbol,
            reason,
        } => {
            out.push(status);
            le64(out, ts);
            out.extend_from_slice(&symbol);
            out.extend_from_slice(&reason);
        }
        Message::RetailLiquidity {
            indicator: f1,
            ts,
            symbol,
        }
        | Message::OperationalHalt { status: f1, ts, symbol }
        | Message::SecurityEvent { event: f1, ts, symbol } => {
            out.push(f1);
            le64(out, ts);
            out.extend_from_slice(&symbol);
        }
        Message::ShortSalePriceTest {
            status,
            ts,
            symbol,
            detail,
        } => {
            out.push(status);
            le64(out, ts);
            out.extend_from_slice(&symbol);
            out.push(detail);
        }
        Message::AddOrder {
            side: f1,
            ts,
            symbol,
            order_id,
            size,
            price,
        }
        | Message::OrderModify {
            flags: f1,
            ts,
            symbol,
            order_id,
            size,
            price,
        } => {
            out.push(f1);
            le64(out, ts);
            out.extend_from_slice(&symbol);
            le64(out, order_id);
            le32(out, size);
            le64(out, price);
        }
        Message::OrderDelete { ts, symbol, order_id } => {
            out.push(0);
            le64(out, ts);
            out.extend_from_slice(&symbol);
            le64(out, order_id);
        }
        Message::OrderExecuted {
            sale_flags,
            ts,
            symbol,
            order_id,
            size,
            price,
            trade_id,
        } => {
            out.push(sale_flags);
            le64(out, ts);
            out.extend_from_slice(&symbol);
            le64(out, order_id);
            le32(out, size);
            le64(out, price);
            le64(out, trade_id);
        }
        Message::Trade {
            sale_flags,
            ts,
            symbol,
            size,
            price,
            trade_id,
        }
        | Message::TradeBreak {
            sale_flags,
            ts,
            symbol,
            size,
            price,
            trade_id,
        } => {
            out.push(sale_flags);
            le64(out, ts);
            out.extend_from_slice(&symbol);
            le32(out, size);
            le64(out, price);
            le64(out, trade_id);
        }
        Message::ClearBook { ts, symbol } => {
            out.push(0);
            le64(out, ts);
            out.extend_from_slice(&symbol);
        }
        Message::Unknown { .. } => {}
    }
    debug_assert!(message_len(m.kind()).is_none_or(|n| out.len() - start == n));
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The worked examples of the spec (pp.13-27), byte for byte.
    #[test]
    fn spec_examples() {
        let hex = |s: &str| -> Vec<u8> {
            s.split_whitespace()
                .map(|h| u8::from_str_radix(h, 16).unwrap())
                .collect()
        };
        let ziext = symbol("ZIEXT");
        let ts = 0x146d_86ab_a0a5_8fb2_i64; // 2016-08-23 15:32:04.912754610
        let add = hex(
            "61 38 b2 8f a5 a0 ab 86 6d 14 5a 49 45 58 54 20 20 20 96 8f 06 00 00 00 00 00 64 00 00 00 24 1d 0f 00 00 00 00 00",
        );
        assert_eq!(
            parse(&add),
            Ok(Message::AddOrder {
                side: b'8',
                ts,
                symbol: ziext,
                order_id: 429_974,
                size: 100,
                price: 990_500,
            })
        );
        let exe = hex(
            "4c 00 b2 8f a5 a0 ab 86 6d 14 5a 49 45 58 54 20 20 20 96 8f 06 00 00 00 00 00 64 00 00 00 24 1d 0f 00 00 00 00 00 96 8f 02 00 00 00 00 00",
        );
        assert_eq!(
            parse(&exe),
            Ok(Message::OrderExecuted {
                sale_flags: 0,
                ts,
                symbol: ziext,
                order_id: 429_974,
                size: 100,
                price: 990_500,
                trade_id: 167_830,
            })
        );
        let sys = hex("53 45 00 a0 99 97 e9 3d b6 14");
        assert_eq!(
            parse(&sys),
            Ok(Message::SystemEvent {
                event: b'E',
                ts: 0x14b6_3de9_9799_a000,
            })
        );
        for b in [add, exe, sys] {
            let m = parse(&b).unwrap();
            let mut out = Vec::new();
            encode(&m, &mut out);
            assert_eq!(out, b, "{m:?}");
        }
        assert_eq!(symbol_str(&ziext), "ZIEXT");
    }

    #[test]
    fn short_unknown_and_overlength() {
        assert_eq!(parse(&[]), Err(ParseError::Empty));
        assert_eq!(
            parse(&[b'a', b'8', 0]),
            Err(ParseError::Short {
                kind: b'a',
                len: 3,
                need: 38
            })
        );
        assert_eq!(parse(&[0xfe, 1, 2]), Ok(Message::Unknown { kind: 0xfe }));
        let mut b = Vec::new();
        encode(
            &Message::ClearBook {
                ts: 7,
                symbol: symbol("SPY"),
            },
            &mut b,
        );
        b.extend_from_slice(&[9; 5]);
        assert_eq!(
            parse(&b),
            Ok(Message::ClearBook {
                ts: 7,
                symbol: symbol("SPY")
            })
        );
    }
}
