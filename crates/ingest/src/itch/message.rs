//! Nasdaq TotalView-ITCH 5.0 message layouts (port of et `crates/itch/src/message.rs`).
//!
//! Every offset and length is transcribed from the specification ("Nasdaq TotalView-ITCH 5.0", message formats in
//! sections 1.1 to 1.8). Common header: Message Type (0, 1), Stock Locate (1, 2), Tracking Number (3, 2), Timestamp
//! (5, 6) "nanoseconds since midnight". Integers are big endian and unsigned; alpha fields are ASCII, left
//! justified, space padded; `Price (4)` has four implied decimals, `Price (8)` eight. Known types shorter than their
//! layout are `ParseError::Short`; unknown types decode the header and `Body::Unknown`. Parsing never panics.

use std::fmt;

/// 8-byte space-padded symbol as transmitted.
pub type Stock = [u8; 8];

/// Symbol without the right padding.
pub fn stock_str(s: &Stock) -> &str {
    std::str::from_utf8(s).unwrap_or("").trim_end_matches(' ')
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
/// Common message header.
pub struct Header {
    /// Message type.
    pub kind: u8,
    /// Stock locate.
    pub locate: u16,
    /// Tracking number.
    pub tracking: u16,
    /// Nanoseconds since midnight (48-bit field).
    pub ts_ns: u64,
}

/// Stock Directory (`R`, section 1.2.1), 39 bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StockDirectory {
    /// Stock symbol, space padded.
    pub stock: Stock,
    /// Market category.
    pub market_category: u8,
    /// Financial status.
    pub financial_status: u8,
    /// Round lot size.
    pub round_lot_size: u32,
    /// Round lots only.
    pub round_lots_only: u8,
    /// Issue classification.
    pub issue_classification: u8,
    /// Issue sub type.
    pub issue_sub_type: [u8; 2],
    /// Authenticity.
    pub authenticity: u8,
    /// Short sale threshold.
    pub short_sale_threshold: u8,
    /// Ipo flag.
    pub ipo_flag: u8,
    /// Luld tier.
    pub luld_tier: u8,
    /// Etp flag.
    pub etp_flag: u8,
    /// Etp leverage factor.
    pub etp_leverage_factor: u32,
    /// Inverse indicator.
    pub inverse_indicator: u8,
}

/// Net Order Imbalance Indicator (`I`, section 1.6), 50 bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Noii {
    /// Paired shares.
    pub paired_shares: u64,
    /// Imbalance shares.
    pub imbalance_shares: u64,
    /// Imbalance direction.
    pub imbalance_direction: u8,
    /// Stock symbol, space padded.
    pub stock: Stock,
    /// Far price.
    pub far_price: u32,
    /// Near price.
    pub near_price: u32,
    /// Reference price.
    pub reference_price: u32,
    /// Cross type.
    pub cross_type: u8,
    /// Price variation.
    pub price_variation: u8,
}

/// Direct Listing with Capital Raise (`O`, section 1.8), 48 bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DirectListing {
    /// Stock symbol, space padded.
    pub stock: Stock,
    /// Open eligibility.
    pub open_eligibility: u8,
    /// Min price.
    pub min_price: u32,
    /// Max price.
    pub max_price: u32,
    /// Near execution price.
    pub near_execution_price: u32,
    /// Near execution time.
    pub near_execution_time: u64,
    /// Lower collar.
    pub lower_collar: u32,
    /// Upper collar.
    pub upper_collar: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
/// Type-specific fields of a message.
// Variant fields carry the specification field names given in each variant's doc.
#[allow(missing_docs)]
pub enum Body {
    /// `S` (1.1), 12 bytes. Event codes O, S, Q, M, E, C.
    SystemEvent { event: u8 },
    /// `R` (1.2.1), 39 bytes.
    StockDirectory(StockDirectory),
    /// `H` (1.2.2), 25 bytes. State H, P, Q, T.
    TradingAction { stock: Stock, state: u8, reason: [u8; 4] },
    /// `Y` (1.2.3), 20 bytes.
    RegSho { stock: Stock, action: u8 },
    /// `L` (1.2.4), 26 bytes.
    MarketParticipant {
        /// MPID attribution (F only).
        mpid: [u8; 4],
        /// Stock symbol, space padded.
        stock: Stock,
        /// Primary.
        primary: u8,
        /// Mode.
        mode: u8,
        /// State.
        state: u8,
    },
    /// `V` (1.2.5.1), 35 bytes; levels are `Price (8)`.
    MwcbLevels { level1: u64, level2: u64, level3: u64 },
    /// `W` (1.2.5.2), 12 bytes.
    MwcbStatus { level: u8 },
    /// `K` (1.2.6), 28 bytes.
    IpoQuoting {
        /// Stock symbol, space padded.
        stock: Stock,
        /// Release time s.
        release_time_s: u32,
        /// Qualifier.
        qualifier: u8,
        /// Price.
        price: u32,
    },
    /// `J` (1.2.7), 35 bytes.
    LuldCollar {
        /// Stock symbol, space padded.
        stock: Stock,
        /// Reference.
        reference: u32,
        /// Upper.
        upper: u32,
        /// Lower.
        lower: u32,
        /// Extension.
        extension: u32,
    },
    /// `h` (1.2.8), 21 bytes.
    OperationalHalt { stock: Stock, market: u8, action: u8 },
    /// `A` (1.3.1), 36 bytes, or `F` (1.3.2), 40 bytes with `mpid`.
    AddOrder {
        /// Order reference number.
        order_ref: u64,
        /// `B` buy, `S` sell.
        buy_sell: u8,
        /// Shares.
        shares: u32,
        /// Stock symbol, space padded.
        stock: Stock,
        /// Price.
        price: u32,
        /// MPID attribution (F only).
        mpid: Option<[u8; 4]>,
    },
    /// `E` (1.4.1), 31 bytes.
    Executed { order_ref: u64, shares: u32, match_no: u64 },
    /// `C` (1.4.2), 36 bytes. `printable` is `Y`/`N`.
    ExecutedWithPrice {
        /// Order reference number.
        order_ref: u64,
        /// Shares.
        shares: u32,
        /// Match number.
        match_no: u64,
        /// Printable flag (Y/N).
        printable: u8,
        /// Price.
        price: u32,
    },
    /// `X` (1.4.3), 23 bytes: shares removed from the display size.
    Cancel { order_ref: u64, shares: u32 },
    /// `D` (1.4.4), 19 bytes: all remaining shares removed.
    Delete { order_ref: u64 },
    /// `U` (1.4.5), 35 bytes: `old_ref` is dead; `new_ref` carries the old side and stock.
    Replace {
        /// Original order reference number.
        old_ref: u64,
        /// New order reference number.
        new_ref: u64,
        /// Shares.
        shares: u32,
        /// Price.
        price: u32,
    },
    /// `P` (1.5.1), 44 bytes: non-displayable match; `order_ref` is 0 since 2010-12-06
    /// and `buy_sell` is always `B` since 2014-07-14.
    Trade {
        /// Order reference number.
        order_ref: u64,
        /// Buy/Sell indicator (B or S).
        buy_sell: u8,
        /// Shares.
        shares: u32,
        /// Stock symbol, space padded.
        stock: Stock,
        /// Price.
        price: u32,
        /// Match number.
        match_no: u64,
    },
    /// `Q` (1.5.2), 40 bytes; shares may be 0; cross type O, C, H.
    CrossTrade {
        /// Shares.
        shares: u64,
        /// Stock symbol, space padded.
        stock: Stock,
        /// Price.
        price: u32,
        /// Match number.
        match_no: u64,
        /// Cross type.
        cross_type: u8,
    },
    /// `B` (1.5.3), 19 bytes.
    BrokenTrade { match_no: u64 },
    /// `I` (1.6), 50 bytes.
    Noii(Noii),
    /// `N` (1.7), 20 bytes. Interest flag B, S, A, N.
    Rpii { stock: Stock, interest: u8 },
    /// `O` (1.8), 48 bytes.
    DirectListing(DirectListing),
    /// A message type this crate does not know; skipped via the length prefix.
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
/// One decoded message.
pub struct Message {
    /// Common header.
    pub header: Header,
    /// Type-specific fields.
    pub body: Body,
}

/// Fixed length of each known message type (header included), from the spec.
pub const fn message_len(kind: u8) -> Option<usize> {
    Some(match kind {
        b'S' => 12,
        b'R' => 39,
        b'H' => 25,
        b'Y' => 20,
        b'L' => 26,
        b'V' => 35,
        b'W' => 12,
        b'K' => 28,
        b'J' => 35,
        b'h' => 21,
        b'A' => 36,
        b'F' => 40,
        b'E' => 31,
        b'C' => 36,
        b'X' => 23,
        b'D' => 19,
        b'U' => 35,
        b'P' => 44,
        b'Q' => 40,
        b'B' => 19,
        b'I' => 50,
        b'N' => 20,
        b'O' => 48,
        _ => return None,
    })
}

/// Every message type this crate decodes.
pub const KNOWN_TYPES: &[u8] = b"SRHYLVWKJhAFECXDUPQBINO";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
/// Why a message could not be parsed.
pub enum ParseError {
    /// Empty input.
    Empty,
    /// A known type arrived shorter than its fixed layout.
    Short {
        /// Message type.
        kind: u8,
        /// Bytes received.
        got: usize,
        /// Bytes the layout needs.
        want: usize,
    },
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => write!(f, "empty message"),
            Self::Short { kind, got, want } => write!(
                f,
                "message type {:?} is {got} bytes, spec layout needs {want}",
                *kind as char
            ),
        }
    }
}

impl std::error::Error for ParseError {}

/// Largest known message (Noii, 50 bytes) rounded up; parse reads fixed offsets from a zero-padded copy.
const PAD: usize = 64;

#[inline]
fn u16_at(b: &[u8; PAD], o: usize) -> u16 {
    u16::from_be_bytes([b[o], b[o + 1]])
}
#[inline]
fn u32_at(b: &[u8; PAD], o: usize) -> u32 {
    u32::from_be_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]])
}
#[inline]
fn u48_at(b: &[u8; PAD], o: usize) -> u64 {
    u64::from_be_bytes([0, 0, b[o], b[o + 1], b[o + 2], b[o + 3], b[o + 4], b[o + 5]])
}
#[inline]
fn u64_at(b: &[u8; PAD], o: usize) -> u64 {
    u64::from_be_bytes([
        b[o],
        b[o + 1],
        b[o + 2],
        b[o + 3],
        b[o + 4],
        b[o + 5],
        b[o + 6],
        b[o + 7],
    ])
}
#[inline]
fn arr<const N: usize>(b: &[u8; PAD], o: usize) -> [u8; N] {
    let mut a = [0u8; N];
    a.copy_from_slice(&b[o..o + N]);
    a
}

/// Parses one message (without its 2-byte length prefix).
pub fn parse(b: &[u8]) -> Result<Message, ParseError> {
    let Some(&kind) = b.first() else {
        return Err(ParseError::Empty);
    };
    let want = message_len(kind);
    if let Some(want) = want
        && b.len() < want
    {
        return Err(ParseError::Short {
            kind,
            got: b.len(),
            want,
        });
    }
    let raw_len = b.len();
    let mut pad = [0u8; PAD];
    let n = raw_len.min(PAD);
    pad[..n].copy_from_slice(&b[..n]);
    let b = &pad;
    let header = if raw_len >= 11 {
        Header {
            kind,
            locate: u16_at(b, 1),
            tracking: u16_at(b, 3),
            ts_ns: u48_at(b, 5),
        }
    } else {
        Header {
            kind,
            locate: 0,
            tracking: 0,
            ts_ns: 0,
        }
    };
    let body = match kind {
        b'S' => Body::SystemEvent { event: b[11] },
        b'R' => Body::StockDirectory(StockDirectory {
            stock: arr(b, 11),
            market_category: b[19],
            financial_status: b[20],
            round_lot_size: u32_at(b, 21),
            round_lots_only: b[25],
            issue_classification: b[26],
            issue_sub_type: arr(b, 27),
            authenticity: b[29],
            short_sale_threshold: b[30],
            ipo_flag: b[31],
            luld_tier: b[32],
            etp_flag: b[33],
            etp_leverage_factor: u32_at(b, 34),
            inverse_indicator: b[38],
        }),
        b'H' => Body::TradingAction {
            stock: arr(b, 11),
            state: b[19],
            reason: arr(b, 21),
        },
        b'Y' => Body::RegSho {
            stock: arr(b, 11),
            action: b[19],
        },
        b'L' => Body::MarketParticipant {
            mpid: arr(b, 11),
            stock: arr(b, 15),
            primary: b[23],
            mode: b[24],
            state: b[25],
        },
        b'V' => Body::MwcbLevels {
            level1: u64_at(b, 11),
            level2: u64_at(b, 19),
            level3: u64_at(b, 27),
        },
        b'W' => Body::MwcbStatus { level: b[11] },
        b'K' => Body::IpoQuoting {
            stock: arr(b, 11),
            release_time_s: u32_at(b, 19),
            qualifier: b[23],
            price: u32_at(b, 24),
        },
        b'J' => Body::LuldCollar {
            stock: arr(b, 11),
            reference: u32_at(b, 19),
            upper: u32_at(b, 23),
            lower: u32_at(b, 27),
            extension: u32_at(b, 31),
        },
        b'h' => Body::OperationalHalt {
            stock: arr(b, 11),
            market: b[19],
            action: b[20],
        },
        b'A' | b'F' => Body::AddOrder {
            order_ref: u64_at(b, 11),
            buy_sell: b[19],
            shares: u32_at(b, 20),
            stock: arr(b, 24),
            price: u32_at(b, 32),
            mpid: (kind == b'F').then(|| arr(b, 36)),
        },
        b'E' => Body::Executed {
            order_ref: u64_at(b, 11),
            shares: u32_at(b, 19),
            match_no: u64_at(b, 23),
        },
        b'C' => Body::ExecutedWithPrice {
            order_ref: u64_at(b, 11),
            shares: u32_at(b, 19),
            match_no: u64_at(b, 23),
            printable: b[31],
            price: u32_at(b, 32),
        },
        b'X' => Body::Cancel {
            order_ref: u64_at(b, 11),
            shares: u32_at(b, 19),
        },
        b'D' => Body::Delete {
            order_ref: u64_at(b, 11),
        },
        b'U' => Body::Replace {
            old_ref: u64_at(b, 11),
            new_ref: u64_at(b, 19),
            shares: u32_at(b, 27),
            price: u32_at(b, 31),
        },
        b'P' => Body::Trade {
            order_ref: u64_at(b, 11),
            buy_sell: b[19],
            shares: u32_at(b, 20),
            stock: arr(b, 24),
            price: u32_at(b, 32),
            match_no: u64_at(b, 36),
        },
        b'Q' => Body::CrossTrade {
            shares: u64_at(b, 11),
            stock: arr(b, 19),
            price: u32_at(b, 27),
            match_no: u64_at(b, 31),
            cross_type: b[39],
        },
        b'B' => Body::BrokenTrade {
            match_no: u64_at(b, 11),
        },
        b'I' => Body::Noii(Noii {
            paired_shares: u64_at(b, 11),
            imbalance_shares: u64_at(b, 19),
            imbalance_direction: b[27],
            stock: arr(b, 28),
            far_price: u32_at(b, 36),
            near_price: u32_at(b, 40),
            reference_price: u32_at(b, 44),
            cross_type: b[48],
            price_variation: b[49],
        }),
        b'N' => Body::Rpii {
            stock: arr(b, 11),
            interest: b[19],
        },
        b'O' => Body::DirectListing(DirectListing {
            stock: arr(b, 11),
            open_eligibility: b[19],
            min_price: u32_at(b, 20),
            max_price: u32_at(b, 24),
            near_execution_price: u32_at(b, 28),
            near_execution_time: u64_at(b, 32),
            lower_collar: u32_at(b, 40),
            upper_collar: u32_at(b, 44),
        }),
        _ => Body::Unknown,
    };
    Ok(Message { header, body })
}

/// Encodes a message in the spec layout, with its 2-byte big-endian length
/// prefix (the emi.nasdaq.com file framing). Used for fixtures and benchmarks;
/// `parse(encode(m)) == m` for every known type.
pub fn encode(m: &Message, out: &mut Vec<u8>) {
    let start = out.len();
    out.extend_from_slice(&[0, 0]);
    let h = &m.header;
    out.push(h.kind);
    out.extend_from_slice(&h.locate.to_be_bytes());
    out.extend_from_slice(&h.tracking.to_be_bytes());
    out.extend_from_slice(&h.ts_ns.to_be_bytes()[2..]);
    match &m.body {
        Body::SystemEvent { event } => out.push(*event),
        Body::StockDirectory(d) => {
            out.extend_from_slice(&d.stock);
            out.push(d.market_category);
            out.push(d.financial_status);
            out.extend_from_slice(&d.round_lot_size.to_be_bytes());
            out.push(d.round_lots_only);
            out.push(d.issue_classification);
            out.extend_from_slice(&d.issue_sub_type);
            out.push(d.authenticity);
            out.push(d.short_sale_threshold);
            out.push(d.ipo_flag);
            out.push(d.luld_tier);
            out.push(d.etp_flag);
            out.extend_from_slice(&d.etp_leverage_factor.to_be_bytes());
            out.push(d.inverse_indicator);
        }
        Body::TradingAction { stock, state, reason } => {
            out.extend_from_slice(stock);
            out.push(*state);
            out.push(b' ');
            out.extend_from_slice(reason);
        }
        Body::RegSho { stock, action } => {
            out.extend_from_slice(stock);
            out.push(*action);
        }
        Body::MarketParticipant {
            mpid,
            stock,
            primary,
            mode,
            state,
        } => {
            out.extend_from_slice(mpid);
            out.extend_from_slice(stock);
            out.extend_from_slice(&[*primary, *mode, *state]);
        }
        Body::MwcbLevels { level1, level2, level3 } => {
            for l in [level1, level2, level3] {
                out.extend_from_slice(&l.to_be_bytes());
            }
        }
        Body::MwcbStatus { level } => out.push(*level),
        Body::IpoQuoting {
            stock,
            release_time_s,
            qualifier,
            price,
        } => {
            out.extend_from_slice(stock);
            out.extend_from_slice(&release_time_s.to_be_bytes());
            out.push(*qualifier);
            out.extend_from_slice(&price.to_be_bytes());
        }
        Body::LuldCollar {
            stock,
            reference,
            upper,
            lower,
            extension,
        } => {
            out.extend_from_slice(stock);
            for v in [reference, upper, lower, extension] {
                out.extend_from_slice(&v.to_be_bytes());
            }
        }
        Body::OperationalHalt { stock, market, action } => {
            out.extend_from_slice(stock);
            out.extend_from_slice(&[*market, *action]);
        }
        Body::AddOrder {
            order_ref,
            buy_sell,
            shares,
            stock,
            price,
            mpid,
        } => {
            out.extend_from_slice(&order_ref.to_be_bytes());
            out.push(*buy_sell);
            out.extend_from_slice(&shares.to_be_bytes());
            out.extend_from_slice(stock);
            out.extend_from_slice(&price.to_be_bytes());
            if let Some(m) = mpid {
                out.extend_from_slice(m);
            }
        }
        Body::Executed {
            order_ref,
            shares,
            match_no,
        } => {
            out.extend_from_slice(&order_ref.to_be_bytes());
            out.extend_from_slice(&shares.to_be_bytes());
            out.extend_from_slice(&match_no.to_be_bytes());
        }
        Body::ExecutedWithPrice {
            order_ref,
            shares,
            match_no,
            printable,
            price,
        } => {
            out.extend_from_slice(&order_ref.to_be_bytes());
            out.extend_from_slice(&shares.to_be_bytes());
            out.extend_from_slice(&match_no.to_be_bytes());
            out.push(*printable);
            out.extend_from_slice(&price.to_be_bytes());
        }
        Body::Cancel { order_ref, shares } => {
            out.extend_from_slice(&order_ref.to_be_bytes());
            out.extend_from_slice(&shares.to_be_bytes());
        }
        Body::Delete { order_ref } => out.extend_from_slice(&order_ref.to_be_bytes()),
        Body::Replace {
            old_ref,
            new_ref,
            shares,
            price,
        } => {
            out.extend_from_slice(&old_ref.to_be_bytes());
            out.extend_from_slice(&new_ref.to_be_bytes());
            out.extend_from_slice(&shares.to_be_bytes());
            out.extend_from_slice(&price.to_be_bytes());
        }
        Body::Trade {
            order_ref,
            buy_sell,
            shares,
            stock,
            price,
            match_no,
        } => {
            out.extend_from_slice(&order_ref.to_be_bytes());
            out.push(*buy_sell);
            out.extend_from_slice(&shares.to_be_bytes());
            out.extend_from_slice(stock);
            out.extend_from_slice(&price.to_be_bytes());
            out.extend_from_slice(&match_no.to_be_bytes());
        }
        Body::CrossTrade {
            shares,
            stock,
            price,
            match_no,
            cross_type,
        } => {
            out.extend_from_slice(&shares.to_be_bytes());
            out.extend_from_slice(stock);
            out.extend_from_slice(&price.to_be_bytes());
            out.extend_from_slice(&match_no.to_be_bytes());
            out.push(*cross_type);
        }
        Body::BrokenTrade { match_no } => out.extend_from_slice(&match_no.to_be_bytes()),
        Body::Noii(n) => {
            out.extend_from_slice(&n.paired_shares.to_be_bytes());
            out.extend_from_slice(&n.imbalance_shares.to_be_bytes());
            out.push(n.imbalance_direction);
            out.extend_from_slice(&n.stock);
            for v in [n.far_price, n.near_price, n.reference_price] {
                out.extend_from_slice(&v.to_be_bytes());
            }
            out.extend_from_slice(&[n.cross_type, n.price_variation]);
        }
        Body::Rpii { stock, interest } => {
            out.extend_from_slice(stock);
            out.push(*interest);
        }
        Body::DirectListing(d) => {
            out.extend_from_slice(&d.stock);
            out.push(d.open_eligibility);
            for v in [d.min_price, d.max_price, d.near_execution_price] {
                out.extend_from_slice(&v.to_be_bytes());
            }
            out.extend_from_slice(&d.near_execution_time.to_be_bytes());
            out.extend_from_slice(&d.lower_collar.to_be_bytes());
            out.extend_from_slice(&d.upper_collar.to_be_bytes());
        }
        Body::Unknown => {}
    }
    // Every known layout is at most 50 bytes; an `Unknown` body adds nothing, so the length always fits.
    let len = u16::try_from(out.len() - start - 2).unwrap_or(u16::MAX);
    out[start..start + 2].copy_from_slice(&len.to_be_bytes());
}

/// Pads a symbol to the 8-byte alpha field.
pub fn stock(sym: &str) -> Stock {
    let mut s = *b"        ";
    for (o, b) in s.iter_mut().zip(sym.bytes()) {
        *o = b;
    }
    s
}
