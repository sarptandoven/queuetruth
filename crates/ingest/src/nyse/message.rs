//! TAQ Integrated Feed CSV layouts (TAQ Products Client Specification v2.4b, 2026-09-08, sections 2 to 25): one
//! message per line, comma-separated ASCII fields in the documented field order. The spec's data-type rules (section
//! 2.2) apply: numeric fields are ASCII digits, prices are plain decimals (`25.222`, `0.125`), SourceTime is
//! `HH:MM:SS.nnnnnnnnn`, and every default value of 0 or space in the real-time feed is a blank field, so a blank
//! numeric field reads as 0 here.
//!
//! Field positions follow the spec tables (1-based in the docs below). Two tables, Imbalance (105) and Add Order
//! Refresh (106), number their fields 1, 2, 3, 5, 6, ... (no field 4). Without a sample to settle whether a CSV line
//! carries an empty field 4, the parser accepts both layouts: field 5 is numeric (SymbolSeqNum) in the contiguous
//! layout and alphabetic (Symbol) in the other. `encode` writes the contiguous layout.
//!
//! Fields the mapper does not use (the informational codes and numbers of types 3, 34, 111 and 114) never reject a
//! line: an unexpected value reads as 0. Trailing fields beyond a layout are ignored (newer spec versions append
//! fields), and a missing optional trailing field reads as blank: Modify / Replace `Side` was `Reserved` before spec
//! 2.3g (2022-05-16) and Order Execution / Non-Displayed Trade carried `DBExecID` instead of the four trade
//! conditions before 2.3e (2022-01-28).

use std::fmt;

/// Fields kept per line (the longest Integrated layout, Imbalance, has 25).
pub const MAX_FIELDS: usize = 32;

/// Message types of the TAQ Integrated Feed product (spec section 2.1).
pub const INTEGRATED_TYPES: [u16; 15] = [3, 34, 100, 101, 102, 103, 104, 105, 106, 110, 111, 112, 113, 114, 223];

/// Message types that belong only to the TAQ BBO (140), Trades (220, 221, 222) and TRF (210 to 219) products.
pub const OTHER_PRODUCT_TYPES: [u16; 13] = [140, 210, 212, 213, 214, 215, 216, 217, 218, 219, 220, 221, 222];

/// Nanoseconds per second.
const NS: u64 = 1_000_000_000;

/// The comma-separated fields of one line (at most `MAX_FIELDS`; `truncated` when the line had more).
#[derive(Clone, Copy, Debug)]
pub struct Fields<'a> {
    f: [&'a [u8]; MAX_FIELDS],
    n: usize,
    /// The line had more than `MAX_FIELDS` fields (the rest are dropped).
    pub truncated: bool,
}

impl<'a> Fields<'a> {
    /// Splits `line` (no line terminator) at every comma.
    pub fn split(line: &'a [u8]) -> Self {
        let mut f: [&'a [u8]; MAX_FIELDS] = [&[]; MAX_FIELDS];
        let mut n = 0;
        let mut start = 0;
        let mut truncated = false;
        for (i, &b) in line.iter().enumerate() {
            if b == b',' {
                if n < MAX_FIELDS {
                    f[n] = &line[start..i];
                    n += 1;
                } else {
                    truncated = true;
                }
                start = i + 1;
            }
        }
        if n < MAX_FIELDS {
            f[n] = &line[start..];
            n += 1;
        } else {
            truncated = true;
        }
        Self { f, n, truncated }
    }

    /// Number of fields kept.
    pub fn len(&self) -> usize {
        self.n
    }

    /// True for a line with no fields (never: an empty line has one empty field).
    pub fn is_empty(&self) -> bool {
        self.n == 0
    }

    /// Field `i` (0-based); empty when absent.
    #[inline]
    pub fn get(&self, i: usize) -> &'a [u8] {
        if i < self.n { self.f[i] } else { &[] }
    }
}

/// Why a line could not be parsed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ParseError {
    /// Empty line.
    Empty,
    /// Field 1 (message type) is not a number.
    BadType,
    /// Fewer fields than the layout needs.
    TooFewFields {
        /// Message type.
        msg_type: u16,
        /// Fields needed.
        need: u8,
        /// Fields present.
        got: u8,
    },
    /// A field does not hold what the layout says.
    BadField {
        /// Message type.
        msg_type: u16,
        /// 1-based field number of the spec table.
        field: u8,
        /// What was expected.
        what: &'static str,
    },
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("empty line"),
            Self::BadType => f.write_str("field 1 (message type) is not a number"),
            Self::TooFewFields { msg_type, need, got } => {
                write!(
                    f,
                    "message type {msg_type}: {got} field(s), the layout needs at least {need}"
                )
            }
            Self::BadField { msg_type, field, what } => {
                write!(f, "message type {msg_type}: field {field} is not {what}")
            }
        }
    }
}

/// Fields shared by the symbol-sequenced messages: SequenceNumber (field 2), SourceTime (3), Symbol (4) and
/// SymbolSeqNum (5).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Head<'a> {
    /// Channel message sequence number.
    pub seq: u64,
    /// SourceTime as nanoseconds after midnight.
    pub time_ns: u64,
    /// Symbol in NYSE symbology (`BRK A`), trimmed.
    pub symbol: &'a [u8],
    /// Sequence number of the message among all messages of its symbol.
    pub symbol_seq: u64,
}

/// Symbol Index Mapping (type 3, section 3): no SourceTime and no SymbolSeqNum.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SymbolIndexMapping<'a> {
    /// Channel message sequence number.
    pub seq: u64,
    /// Symbol.
    pub symbol: &'a [u8],
    /// Market ID of the originating market (1 NYSE, 3 NYSE Arca, 9 NYSE American, 10 NYSE National, 11 NYSE Texas).
    pub market_id: u32,
    /// Matching engine server.
    pub system_id: u32,
    /// Listing exchange code (`N`, `P`, `Q`, `A`, `Z`, `F`; 0 when blank).
    pub exchange_code: u8,
    /// Security type (0 when blank).
    pub security_type: u8,
    /// Round lot size in shares (0 when blank).
    pub lot_size: u32,
    /// Price resolution (0 all penny, 1 penny / nickel, 5 nickel / dime).
    pub price_resolution: u8,
    /// Round lots accepted (`Y` / `N`, 0 when blank).
    pub round_lot: u8,
    /// Minimum trade price increment in hundredths of a cent.
    pub mpv: u32,
    /// Unit of trade in shares.
    pub unit_of_trade: u32,
}

/// Security Status (type 34, section 4).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SecurityStatus<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Field 6: halt (`4` halt, `5` resume, `6` suspend), short-sale (`A`, `C`, `D`), session (`P`, `B`, `N`, `E`,
    /// `O`, `L`, `X`) or price indication (`I`, `G`) code.
    pub status: u8,
    /// Field 7 (0 when blank).
    pub halt_condition: u8,
    /// Field 13 (0 when blank).
    pub ssr_state: u8,
    /// Field 14 (0 when blank).
    pub market_state: u8,
}

/// Add Order (type 100, section 5) and Add Order Refresh (type 106, section 11).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AddOrder<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Order ID (unique per symbol for the day).
    pub order_id: u64,
    /// Price at 10^-9.
    pub price: i64,
    /// Shares.
    pub volume: u64,
    /// `B` or `S` as written (0 when blank).
    pub side: u8,
    /// FirmID, empty when the order is not attributed.
    pub firm_id: &'a [u8],
}

/// Modify Order (type 101, section 6): the new price and volume of a resting order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ModifyOrder<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Order ID.
    pub order_id: u64,
    /// New price at 10^-9.
    pub price: i64,
    /// New shares.
    pub volume: u64,
    /// Field 9: 1 = lost position in the book (the spec says it is currently defaulted to 0).
    pub position_change: u8,
    /// Field 10 (`B` / `S`; 0 when blank, as in files written before spec 2.3g).
    pub side: u8,
}

/// Delete Order (type 102, section 7).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DeleteOrder<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Order ID.
    pub order_id: u64,
}

/// Order Execution (type 103, section 8).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OrderExecution<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Resting order executed.
    pub order_id: u64,
    /// Execution id (referenced by Trade Cancel).
    pub trade_id: u64,
    /// Execution price at 10^-9 (0 when blank).
    pub price: i64,
    /// Executed shares.
    pub volume: u64,
    /// PrintableFlag: printed to the SIP.
    pub printable: bool,
    /// Trade conditions 1 to 4 (0 when blank or absent).
    pub conditions: [u8; 4],
}

/// Replace Order (type 104, section 9): the resting order is replaced by a new order id.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReplaceOrder<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Order replaced.
    pub order_id: u64,
    /// Replacement order id.
    pub new_order_id: u64,
    /// New price at 10^-9.
    pub price: i64,
    /// New shares.
    pub volume: u64,
    /// Field 10 (`B` / `S`; 0 when blank).
    pub side: u8,
}

/// Non-Displayed Trade (type 110, section 12): two non-displayed orders matched.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NonDisplayedTrade<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Trade id.
    pub trade_id: u64,
    /// Price at 10^-9.
    pub price: i64,
    /// Shares.
    pub volume: u64,
    /// Printed to the SIP.
    pub printable: bool,
    /// Trade conditions 1 to 4 (0 when blank or absent).
    pub conditions: [u8; 4],
}

/// Cross Trade (type 111, section 13): the bulk volume of a crossing auction.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CrossTrade<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Cross id.
    pub cross_id: u64,
    /// Price at 10^-9.
    pub price: i64,
    /// Shares.
    pub volume: u64,
    /// `E`, `O`, `5` or `6` (0 when blank or absent).
    pub cross_type: u8,
}

/// Trade Cancel (type 112, section 14).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TradeCancel<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// TradeID of the cancelled execution or trade.
    pub trade_id: u64,
}

/// Cross Correction (type 113, section 15).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CrossCorrection<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// Cross id corrected.
    pub cross_id: u64,
    /// Corrected volume.
    pub volume: u64,
}

/// Retail Price Improvement (type 114, section 16).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RetailPriceImprovement<'a> {
    /// Shared fields.
    pub head: Head<'a>,
    /// `A`, `B`, `C` (0 when blank: no retail interest).
    pub indicator: u8,
}

/// Stock Summary (type 223, section 25): no SymbolSeqNum.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StockSummary<'a> {
    /// Channel message sequence number.
    pub seq: u64,
    /// SourceTime as nanoseconds after midnight.
    pub time_ns: u64,
    /// Symbol.
    pub symbol: &'a [u8],
    /// Total volume of the day.
    pub total_volume: u64,
}

/// One parsed line.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Message<'a> {
    /// Type 3.
    SymbolIndexMapping(SymbolIndexMapping<'a>),
    /// Type 34.
    SecurityStatus(SecurityStatus<'a>),
    /// Type 100.
    AddOrder(AddOrder<'a>),
    /// Type 101.
    ModifyOrder(ModifyOrder<'a>),
    /// Type 102.
    DeleteOrder(DeleteOrder<'a>),
    /// Type 103.
    OrderExecution(OrderExecution<'a>),
    /// Type 104.
    ReplaceOrder(ReplaceOrder<'a>),
    /// Type 105 (only the shared fields are read).
    Imbalance(Head<'a>),
    /// Type 106.
    AddOrderRefresh(AddOrder<'a>),
    /// Type 110.
    NonDisplayedTrade(NonDisplayedTrade<'a>),
    /// Type 111.
    CrossTrade(CrossTrade<'a>),
    /// Type 112.
    TradeCancel(TradeCancel<'a>),
    /// Type 113.
    CrossCorrection(CrossCorrection<'a>),
    /// Type 114.
    RetailPriceImprovement(RetailPriceImprovement<'a>),
    /// Type 223.
    StockSummary(StockSummary<'a>),
    /// A type of the BBO, Trades or TRF products (`OTHER_PRODUCT_TYPES`).
    OtherProduct {
        /// Message type.
        msg_type: u16,
    },
    /// A type that no TAQ product lists.
    Unknown {
        /// Message type.
        msg_type: u16,
    },
}

impl<'a> Message<'a> {
    /// Message type number.
    pub fn msg_type(&self) -> u16 {
        match self {
            Self::SymbolIndexMapping(_) => 3,
            Self::SecurityStatus(_) => 34,
            Self::AddOrder(_) => 100,
            Self::ModifyOrder(_) => 101,
            Self::DeleteOrder(_) => 102,
            Self::OrderExecution(_) => 103,
            Self::ReplaceOrder(_) => 104,
            Self::Imbalance(_) => 105,
            Self::AddOrderRefresh(_) => 106,
            Self::NonDisplayedTrade(_) => 110,
            Self::CrossTrade(_) => 111,
            Self::TradeCancel(_) => 112,
            Self::CrossCorrection(_) => 113,
            Self::RetailPriceImprovement(_) => 114,
            Self::StockSummary(_) => 223,
            Self::OtherProduct { msg_type } | Self::Unknown { msg_type } => *msg_type,
        }
    }

    /// The shared fields of a symbol-sequenced message (types 34, 100 to 106, 110 to 114).
    pub fn head(&self) -> Option<&Head<'a>> {
        match self {
            Self::SecurityStatus(m) => Some(&m.head),
            Self::AddOrder(m) | Self::AddOrderRefresh(m) => Some(&m.head),
            Self::ModifyOrder(m) => Some(&m.head),
            Self::DeleteOrder(m) => Some(&m.head),
            Self::OrderExecution(m) => Some(&m.head),
            Self::ReplaceOrder(m) => Some(&m.head),
            Self::Imbalance(h) => Some(h),
            Self::NonDisplayedTrade(m) => Some(&m.head),
            Self::CrossTrade(m) => Some(&m.head),
            Self::TradeCancel(m) => Some(&m.head),
            Self::CrossCorrection(m) => Some(&m.head),
            Self::RetailPriceImprovement(m) => Some(&m.head),
            Self::SymbolIndexMapping(_) | Self::StockSummary(_) | Self::OtherProduct { .. } | Self::Unknown { .. } => {
                None
            }
        }
    }

    /// Channel message sequence number (`None` for other-product and unknown types, whose layout is not read).
    pub fn seq(&self) -> Option<u64> {
        match self {
            Self::SymbolIndexMapping(m) => Some(m.seq),
            Self::StockSummary(m) => Some(m.seq),
            _ => self.head().map(|h| h.seq),
        }
    }

    /// Symbol (types 3, 223 and the symbol-sequenced types).
    pub fn symbol(&self) -> Option<&'a [u8]> {
        match self {
            Self::SymbolIndexMapping(m) => Some(m.symbol),
            Self::StockSummary(m) => Some(m.symbol),
            _ => self.head().map(|h| h.symbol),
        }
    }

    /// SourceTime as nanoseconds after midnight (every type but 3, other-product and unknown).
    pub fn time_ns(&self) -> Option<u64> {
        match self {
            Self::StockSummary(m) => Some(m.time_ns),
            _ => self.head().map(|h| h.time_ns),
        }
    }
}

// ---- field parsers ----------------------------------------------------------------------------------------------

/// Unsigned decimal integer; blank = 0 (spec 2.2 rule 6). `None`: a non-digit or overflow.
#[inline]
pub fn number(b: &[u8]) -> Option<u64> {
    let mut v: u64 = 0;
    for &c in b {
        if !c.is_ascii_digit() {
            return None;
        }
        v = v.checked_mul(10)?.checked_add(u64::from(c - b'0'))?;
    }
    Some(v)
}

/// True for a non-empty run of ASCII digits.
#[inline]
fn is_digits(b: &[u8]) -> bool {
    !b.is_empty() && b.iter().all(u8::is_ascii_digit)
}

/// Plain non-negative decimal price (`25.222`, `100.6`, `.5`, `25`) as an integer at 10^-9; blank = 0. `None`: not a
/// decimal with at most 9 fraction digits, or out of range.
#[inline]
pub fn price_nanos(b: &[u8]) -> Option<i64> {
    if b.is_empty() {
        return Some(0);
    }
    let (int, frac) = match b.iter().position(|&c| c == b'.') {
        Some(i) => (&b[..i], &b[i + 1..]),
        None => (b, &[][..]),
    };
    if (int.is_empty() && frac.is_empty()) || frac.len() > 9 {
        return None;
    }
    let mut v: i64 = 0;
    for &c in int {
        if !c.is_ascii_digit() {
            return None;
        }
        v = v.checked_mul(10)?.checked_add(i64::from(c - b'0'))?;
    }
    let mut f: i64 = 0;
    for &c in frac {
        if !c.is_ascii_digit() {
            return None;
        }
        f = f * 10 + i64::from(c - b'0');
    }
    // frac.len() <= 9, so the scale is at most 10^9 and f < 10^9.
    let scale = 10i64.pow(9 - frac.len() as u32);
    v.checked_mul(NS as i64)?.checked_add(f * scale)
}

/// `HH:MM:SS` with an optional `.` and 1 to 9 fraction digits, as nanoseconds after midnight (hours 0 to 23).
#[inline]
pub fn time_ns(b: &[u8]) -> Option<u64> {
    if b.len() < 8 || b[2] != b':' || b[5] != b':' {
        return None;
    }
    let two = |i: usize| -> Option<u64> {
        let (x, y) = (b[i], b[i + 1]);
        (x.is_ascii_digit() && y.is_ascii_digit()).then(|| u64::from(x - b'0') * 10 + u64::from(y - b'0'))
    };
    let (h, m, s) = (two(0)?, two(3)?, two(6)?);
    if h > 23 || m > 59 || s > 59 {
        return None;
    }
    let mut ns = 0;
    if b.len() > 8 {
        let frac = &b[9..];
        if b[8] != b'.' || frac.is_empty() || frac.len() > 9 || !frac.iter().all(u8::is_ascii_digit) {
            return None;
        }
        ns = number(frac)? * 10u64.pow(9 - frac.len() as u32);
    }
    Some(((h * 60 + m) * 60 + s) * NS + ns)
}

/// Symbol: printable ASCII after trimming spaces, never empty and never all digits.
#[inline]
fn symbol(b: &[u8]) -> Option<&[u8]> {
    let t = b.trim_ascii();
    (!t.is_empty() && t.iter().all(|&c| (0x20..0x7f).contains(&c)) && !is_digits(t)).then_some(t)
}

/// One-byte code: the first byte of a field of one non-space character, 0 when blank or a space.
#[inline]
fn code(b: &[u8]) -> Option<u8> {
    match b.trim_ascii() {
        [] => Some(0),
        [c] => Some(*c),
        _ => None,
    }
}

/// Parser state of one line: the fields and the message type.
struct Line<'a> {
    f: Fields<'a>,
    t: u16,
}

impl<'a> Line<'a> {
    fn need(&self, n: usize) -> Result<(), ParseError> {
        if self.f.len() < n {
            return Err(ParseError::TooFewFields {
                msg_type: self.t,
                need: n as u8,
                got: self.f.len().min(255) as u8,
            });
        }
        Ok(())
    }

    fn bad(&self, field: usize, what: &'static str) -> ParseError {
        ParseError::BadField {
            msg_type: self.t,
            field: field as u8,
            what,
        }
    }

    /// Unsigned number at 1-based spec field `field`, stored at index `i`.
    fn num(&self, i: usize, field: usize) -> Result<u64, ParseError> {
        number(self.f.get(i)).ok_or_else(|| self.bad(field, "a number"))
    }

    fn num32(&self, i: usize, field: usize) -> Result<u32, ParseError> {
        u32::try_from(self.num(i, field)?).map_err(|_| self.bad(field, "a 32-bit number"))
    }

    fn price(&self, i: usize, field: usize) -> Result<i64, ParseError> {
        price_nanos(self.f.get(i)).ok_or_else(|| self.bad(field, "a decimal price"))
    }

    fn time(&self, i: usize, field: usize) -> Result<u64, ParseError> {
        time_ns(self.f.get(i)).ok_or_else(|| self.bad(field, "a HH:MM:SS.nnnnnnnnn time"))
    }

    fn symbol(&self, i: usize, field: usize) -> Result<&'a [u8], ParseError> {
        symbol(self.f.get(i)).ok_or_else(|| self.bad(field, "a symbol"))
    }

    fn code(&self, i: usize, field: usize) -> Result<u8, ParseError> {
        code(self.f.get(i)).ok_or_else(|| self.bad(field, "a one-character code"))
    }

    fn flag(&self, i: usize, field: usize) -> Result<bool, ParseError> {
        match self.f.get(i) {
            b"" | b"0" => Ok(false),
            b"1" => Ok(true),
            _ => Err(self.bad(field, "0 or 1")),
        }
    }

    /// An informational one-character code: 0 when blank, a space or not one character (never an error, so an
    /// unexpected value in a field the mapper does not use cannot reject the line).
    fn info_code(&self, i: usize) -> u8 {
        code(self.f.get(i)).unwrap_or(0)
    }

    /// An informational number: 0 when blank or not a 32-bit number.
    fn info_num32(&self, i: usize) -> u32 {
        number(self.f.get(i)).and_then(|v| u32::try_from(v).ok()).unwrap_or(0)
    }

    fn conditions(&self, first: usize) -> [u8; 4] {
        // Conditions are informational; a value that is not one character (an old DBExecID) reads as blank.
        std::array::from_fn(|k| code(self.f.get(first + k)).unwrap_or(0))
    }

    /// Shared fields at indices `1, 2, s, s + 1` (spec fields 2, 3, 4, 5 of the contiguous layout).
    fn head(&self, s: usize) -> Result<Head<'a>, ParseError> {
        Ok(Head {
            seq: self.num(1, 2)?,
            time_ns: self.time(2, 3)?,
            symbol: self.symbol(s, 4)?,
            symbol_seq: self.num(s + 1, 5)?,
        })
    }

    /// Index of Symbol in types 105 / 106: 3 in the contiguous layout, 4 when the line carries the field 4 that the
    /// spec tables skip (field 5 is then the Symbol, not a number).
    fn gap_layout_symbol(&self) -> usize {
        if is_digits(self.f.get(4)) { 3 } else { 4 }
    }
}

/// Parses one line (without its terminator).
pub fn parse_line(line: &[u8]) -> Result<Message<'_>, ParseError> {
    if line.is_empty() {
        return Err(ParseError::Empty);
    }
    let f = Fields::split(line);
    let t = number(f.get(0))
        .filter(|&t| t > 0)
        .and_then(|t| u16::try_from(t).ok())
        .ok_or(ParseError::BadType)?;
    let l = Line { f, t };
    Ok(match t {
        3 => {
            l.need(4)?;
            Message::SymbolIndexMapping(SymbolIndexMapping {
                seq: l.num(1, 2)?,
                symbol: l.symbol(2, 3)?,
                market_id: l.num32(3, 4)?,
                system_id: l.info_num32(4),
                exchange_code: l.info_code(5),
                security_type: l.info_code(6),
                lot_size: l.info_num32(7),
                price_resolution: l.info_code(10),
                round_lot: l.info_code(11),
                mpv: l.info_num32(12),
                unit_of_trade: l.info_num32(13),
            })
        }
        34 => {
            l.need(6)?;
            Message::SecurityStatus(SecurityStatus {
                head: l.head(3)?,
                status: l.info_code(5),
                halt_condition: l.info_code(6),
                ssr_state: l.info_code(12),
                market_state: l.info_code(13),
            })
        }
        100 | 106 => {
            let s = if t == 106 { l.gap_layout_symbol() } else { 3 };
            l.need(s + 6)?;
            let a = AddOrder {
                head: l.head(s)?,
                order_id: l.num(s + 2, 6)?,
                price: l.price(s + 3, 7)?,
                volume: l.num(s + 4, 8)?,
                side: l.code(s + 5, 9)?,
                firm_id: l.f.get(s + 6).trim_ascii(),
            };
            if t == 100 {
                Message::AddOrder(a)
            } else {
                Message::AddOrderRefresh(a)
            }
        }
        101 => {
            l.need(8)?;
            let position_change = match l.f.get(8) {
                b"" | b"0" => 0,
                b"1" => 1,
                _ => return Err(l.bad(9, "0 or 1")),
            };
            Message::ModifyOrder(ModifyOrder {
                head: l.head(3)?,
                order_id: l.num(5, 6)?,
                price: l.price(6, 7)?,
                volume: l.num(7, 8)?,
                position_change,
                side: l.code(9, 10).unwrap_or(0),
            })
        }
        102 => {
            l.need(6)?;
            Message::DeleteOrder(DeleteOrder {
                head: l.head(3)?,
                order_id: l.num(5, 6)?,
            })
        }
        103 => {
            l.need(10)?;
            Message::OrderExecution(OrderExecution {
                head: l.head(3)?,
                order_id: l.num(5, 6)?,
                trade_id: l.num(6, 7)?,
                price: l.price(7, 8)?,
                volume: l.num(8, 9)?,
                printable: l.flag(9, 10)?,
                conditions: l.conditions(11),
            })
        }
        104 => {
            l.need(9)?;
            Message::ReplaceOrder(ReplaceOrder {
                head: l.head(3)?,
                order_id: l.num(5, 6)?,
                new_order_id: l.num(6, 7)?,
                price: l.price(7, 8)?,
                volume: l.num(8, 9)?,
                side: l.code(9, 10).unwrap_or(0),
            })
        }
        105 => {
            let s = l.gap_layout_symbol();
            l.need(s + 2)?;
            Message::Imbalance(l.head(s)?)
        }
        110 => {
            l.need(9)?;
            Message::NonDisplayedTrade(NonDisplayedTrade {
                head: l.head(3)?,
                trade_id: l.num(5, 6)?,
                price: l.price(6, 7)?,
                volume: l.num(7, 8)?,
                printable: l.flag(8, 9)?,
                conditions: l.conditions(9),
            })
        }
        111 => {
            l.need(8)?;
            Message::CrossTrade(CrossTrade {
                head: l.head(3)?,
                cross_id: l.num(5, 6)?,
                price: l.price(6, 7)?,
                volume: l.num(7, 8)?,
                cross_type: l.info_code(8),
            })
        }
        112 => {
            l.need(6)?;
            Message::TradeCancel(TradeCancel {
                head: l.head(3)?,
                trade_id: l.num(5, 6)?,
            })
        }
        113 => {
            l.need(7)?;
            Message::CrossCorrection(CrossCorrection {
                head: l.head(3)?,
                cross_id: l.num(5, 6)?,
                volume: l.num(6, 7)?,
            })
        }
        114 => {
            l.need(5)?;
            Message::RetailPriceImprovement(RetailPriceImprovement {
                head: l.head(3)?,
                indicator: l.info_code(5),
            })
        }
        223 => {
            l.need(4)?;
            Message::StockSummary(StockSummary {
                seq: l.num(1, 2)?,
                time_ns: l.time(2, 3)?,
                symbol: l.symbol(3, 4)?,
                total_volume: l.num(8, 9)?,
            })
        }
        t if OTHER_PRODUCT_TYPES.contains(&t) => Message::OtherProduct { msg_type: t },
        t => Message::Unknown { msg_type: t },
    })
}

// ---- encoder (synthetic files and tests) ------------------------------------------------------------------------

/// Appends `v`, or nothing when it is 0 (blank default, spec 2.2 rule 6).
fn put_num(out: &mut Vec<u8>, v: u64) {
    if v != 0 {
        out.extend_from_slice(v.to_string().as_bytes());
    }
}

/// Appends a price at 10^-9 as a decimal with at least two fraction digits (blank when 0).
pub fn put_price(out: &mut Vec<u8>, nanos: i64) {
    if nanos == 0 {
        return;
    }
    let (int, frac) = (nanos / NS as i64, nanos % NS as i64);
    let mut s = format!("{int}.{frac:09}");
    while s.ends_with('0') && s.len() - s.find('.').unwrap_or(0) > 3 {
        s.pop();
    }
    out.extend_from_slice(s.as_bytes());
}

/// Appends `HH:MM:SS.nnnnnnnnn`.
pub fn put_time(out: &mut Vec<u8>, ns: u64) {
    let secs = ns / NS;
    let (h, m, s) = (secs / 3600, secs / 60 % 60, secs % 60);
    out.extend_from_slice(format!("{h:02}:{m:02}:{s:02}.{:09}", ns % NS).as_bytes());
}

fn put_code(out: &mut Vec<u8>, c: u8) {
    if c != 0 && c != b' ' {
        out.push(c);
    }
}

fn put_head(out: &mut Vec<u8>, t: u16, h: &Head<'_>) {
    out.extend_from_slice(t.to_string().as_bytes());
    out.push(b',');
    put_num(out, h.seq);
    out.push(b',');
    put_time(out, h.time_ns);
    out.push(b',');
    out.extend_from_slice(h.symbol);
    out.push(b',');
    put_num(out, h.symbol_seq);
}

/// Appends `m` as one CSV line (contiguous layout, `\n` terminated). Other-product and unknown types are written
/// as their type and an empty sequence field.
pub fn encode(m: &Message<'_>, out: &mut Vec<u8>) {
    let c = |out: &mut Vec<u8>| out.push(b',');
    match m {
        Message::SymbolIndexMapping(s) => {
            out.extend_from_slice(b"3,");
            put_num(out, s.seq);
            c(out);
            out.extend_from_slice(s.symbol);
            c(out);
            put_num(out, u64::from(s.market_id));
            c(out);
            put_num(out, u64::from(s.system_id));
            c(out);
            put_code(out, s.exchange_code);
            c(out);
            put_code(out, s.security_type);
            c(out);
            put_num(out, u64::from(s.lot_size));
            out.extend_from_slice(b",,,");
            put_code(out, s.price_resolution);
            c(out);
            put_code(out, s.round_lot);
            c(out);
            put_num(out, u64::from(s.mpv));
            c(out);
            put_num(out, u64::from(s.unit_of_trade));
        }
        Message::SecurityStatus(s) => {
            put_head(out, 34, &s.head);
            c(out);
            put_code(out, s.status);
            c(out);
            put_code(out, s.halt_condition);
            out.extend_from_slice(b",,,,,,");
            put_code(out, s.ssr_state);
            c(out);
            put_code(out, s.market_state);
        }
        Message::AddOrder(a) | Message::AddOrderRefresh(a) => {
            put_head(out, m.msg_type(), &a.head);
            c(out);
            put_num(out, a.order_id);
            c(out);
            put_price(out, a.price);
            c(out);
            put_num(out, a.volume);
            c(out);
            put_code(out, a.side);
            c(out);
            out.extend_from_slice(a.firm_id);
            c(out);
        }
        Message::ModifyOrder(x) => {
            put_head(out, 101, &x.head);
            c(out);
            put_num(out, x.order_id);
            c(out);
            put_price(out, x.price);
            c(out);
            put_num(out, x.volume);
            c(out);
            put_num(out, u64::from(x.position_change));
            c(out);
            put_code(out, x.side);
            c(out);
        }
        Message::DeleteOrder(d) => {
            put_head(out, 102, &d.head);
            c(out);
            put_num(out, d.order_id);
            c(out);
        }
        Message::OrderExecution(e) => {
            put_head(out, 103, &e.head);
            c(out);
            put_num(out, e.order_id);
            c(out);
            put_num(out, e.trade_id);
            c(out);
            put_price(out, e.price);
            c(out);
            put_num(out, e.volume);
            c(out);
            out.push(if e.printable { b'1' } else { b'0' });
            c(out);
            for k in e.conditions {
                c(out);
                put_code(out, k);
            }
        }
        Message::ReplaceOrder(r) => {
            put_head(out, 104, &r.head);
            c(out);
            put_num(out, r.order_id);
            c(out);
            put_num(out, r.new_order_id);
            c(out);
            put_price(out, r.price);
            c(out);
            put_num(out, r.volume);
            c(out);
            put_code(out, r.side);
            c(out);
        }
        Message::Imbalance(h) => {
            put_head(out, 105, h);
            // Fields 7 to 25 of the table (19 imbalance fields), blank: the mapper reads only the shared fields.
            out.extend_from_slice(&[b','; 19]);
        }
        Message::NonDisplayedTrade(n) => {
            put_head(out, 110, &n.head);
            c(out);
            put_num(out, n.trade_id);
            c(out);
            put_price(out, n.price);
            c(out);
            put_num(out, n.volume);
            c(out);
            out.push(if n.printable { b'1' } else { b'0' });
            for k in n.conditions {
                c(out);
                put_code(out, k);
            }
        }
        Message::CrossTrade(x) => {
            put_head(out, 111, &x.head);
            c(out);
            put_num(out, x.cross_id);
            c(out);
            put_price(out, x.price);
            c(out);
            put_num(out, x.volume);
            c(out);
            put_code(out, x.cross_type);
        }
        Message::TradeCancel(x) => {
            put_head(out, 112, &x.head);
            c(out);
            put_num(out, x.trade_id);
        }
        Message::CrossCorrection(x) => {
            put_head(out, 113, &x.head);
            c(out);
            put_num(out, x.cross_id);
            c(out);
            put_num(out, x.volume);
        }
        Message::RetailPriceImprovement(x) => {
            put_head(out, 114, &x.head);
            c(out);
            put_code(out, x.indicator);
        }
        Message::StockSummary(x) => {
            out.extend_from_slice(b"223,");
            put_num(out, x.seq);
            c(out);
            put_time(out, x.time_ns);
            c(out);
            out.extend_from_slice(x.symbol);
            out.extend_from_slice(b",,,,,");
            put_num(out, x.total_volume);
        }
        Message::OtherProduct { msg_type } | Message::Unknown { msg_type } => {
            out.extend_from_slice(msg_type.to_string().as_bytes());
            c(out);
        }
    }
    out.push(b'\n');
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(s: &str) -> Message<'_> {
        parse_line(s.as_bytes()).unwrap_or_else(|e| panic!("{s}: {e}"))
    }

    fn err(s: &str) -> ParseError {
        parse_line(s.as_bytes()).err().unwrap_or_else(|| panic!("{s} parsed"))
    }

    #[test]
    fn numbers_prices_and_times() {
        assert_eq!(number(b""), Some(0));
        assert_eq!(number(b"18446744073709551615"), Some(u64::MAX));
        assert_eq!(number(b"18446744073709551616"), None);
        assert_eq!(number(b"12a"), None);
        assert_eq!(number(b" 1"), None);
        assert_eq!(price_nanos(b"25.222"), Some(25_222_000_000));
        assert_eq!(price_nanos(b"0.125"), Some(125_000_000));
        assert_eq!(price_nanos(b"100.6"), Some(100_600_000_000));
        assert_eq!(price_nanos(b"2.30"), Some(2_300_000_000));
        assert_eq!(price_nanos(b"25"), Some(25_000_000_000));
        assert_eq!(price_nanos(b".5"), Some(500_000_000));
        assert_eq!(price_nanos(b"0.0001"), Some(100_000));
        assert_eq!(price_nanos(b"1.123456789"), Some(1_123_456_789));
        assert_eq!(price_nanos(b""), Some(0));
        for bad in [
            &b"1.1234567891"[..],
            b"-1.5",
            b"1e3",
            b"1.2.3",
            b".",
            b"1,5",
            b"9223372036.9",
        ] {
            assert_eq!(price_nanos(bad), None, "{:?}", String::from_utf8_lossy(bad));
        }
        assert_eq!(time_ns(b"00:00:00"), Some(0));
        assert_eq!(time_ns(b"09:30:00.000000001"), Some(34_200 * NS + 1));
        assert_eq!(time_ns(b"23:59:59.999999999"), Some(86_400 * NS - 1));
        assert_eq!(time_ns(b"12:32:44.123"), Some(45_164 * NS + 123_000_000));
        assert_eq!(time_ns(b"12:32:44.123456"), Some(45_164 * NS + 123_456_000));
        for bad in [
            &b"24:00:00"[..],
            b"12:60:00",
            b"12:00:60",
            b"12:00:00.",
            b"12:00:00.1234567890",
            b"12:00:00,1",
            b"1:00:00.0",
            b"12-00-00",
            b"",
            b"12:00:0a",
        ] {
            assert_eq!(time_ns(bad), None, "{:?}", String::from_utf8_lossy(bad));
        }
    }

    #[test]
    fn fields_split_keeps_blanks_and_caps_the_count() {
        let f = Fields::split(b"100,,a,");
        assert_eq!(
            (f.len(), f.get(0), f.get(1), f.get(2), f.get(3), f.get(9)),
            (4, &b"100"[..], &b""[..], &b"a"[..], &b""[..], &b""[..])
        );
        let long = vec![b','; 40];
        let f = Fields::split(&long);
        assert_eq!(f.len(), MAX_FIELDS);
        assert!(f.truncated);
        assert!(!Fields::split(&[b','; 31]).truncated);
        assert!(!Fields::split(b"").is_empty());
    }

    #[test]
    fn every_integrated_layout_parses() {
        assert_eq!(
            p("3,2,BRK A,1,7,N,C,100,410000.25,1200,0,Y,1,100"),
            Message::SymbolIndexMapping(SymbolIndexMapping {
                seq: 2,
                symbol: b"BRK A",
                market_id: 1,
                system_id: 7,
                exchange_code: b'N',
                security_type: b'C',
                lot_size: 100,
                price_resolution: b'0',
                round_lot: b'Y',
                mpv: 1,
                unit_of_trade: 100,
            })
        );
        let h = |seq, sym: &'static str, ss| Head {
            seq,
            time_ns: 34_200 * NS + 5,
            symbol: sym.as_bytes(),
            symbol_seq: ss,
        };
        assert_eq!(
            p("34,9,09:30:00.000000005,QTX,4,O,~,,,,,,~,O"),
            Message::SecurityStatus(SecurityStatus {
                head: h(9, "QTX", 4),
                status: b'O',
                halt_condition: b'~',
                ssr_state: b'~',
                market_state: b'O',
            })
        );
        assert_eq!(
            p("100,10,09:30:00.000000005,QTX,5,77,25.10,300,B,,"),
            Message::AddOrder(AddOrder {
                head: h(10, "QTX", 5),
                order_id: 77,
                price: 25_100_000_000,
                volume: 300,
                side: b'B',
                firm_id: b"",
            })
        );
        assert_eq!(
            p("100,10,09:30:00.000000005,QTX,5,77,25.10,300,S,ABCD,"),
            Message::AddOrder(AddOrder {
                head: h(10, "QTX", 5),
                order_id: 77,
                price: 25_100_000_000,
                volume: 300,
                side: b'S',
                firm_id: b"ABCD",
            })
        );
        assert_eq!(
            p("101,11,09:30:00.000000005,QTX,6,77,25.1,200,,B,"),
            Message::ModifyOrder(ModifyOrder {
                head: h(11, "QTX", 6),
                order_id: 77,
                price: 25_100_000_000,
                volume: 200,
                position_change: 0,
                side: b'B',
            })
        );
        // Before spec 2.3g field 10 was Reserved (blank or 0 in old files): side reads 0.
        match p("101,11,09:30:00.000000005,QTX,6,77,25.1,200,1,0,0") {
            Message::ModifyOrder(m) => assert_eq!((m.position_change, m.side), (1, b'0')),
            other => panic!("{other:?}"),
        }
        assert_eq!(
            p("102,12,09:30:00.000000005,QTX,7,77,"),
            Message::DeleteOrder(DeleteOrder {
                head: h(12, "QTX", 7),
                order_id: 77,
            })
        );
        assert_eq!(
            p("103,13,09:30:00.000000005,QTX,8,77,9001,25.10,100,1,,@,,,I"),
            Message::OrderExecution(OrderExecution {
                head: h(13, "QTX", 8),
                order_id: 77,
                trade_id: 9001,
                price: 25_100_000_000,
                volume: 100,
                printable: true,
                conditions: [b'@', 0, 0, b'I'],
            })
        );
        // Pre-2.3e layout: field 11 DBExecID and no trade conditions.
        match p("103,13,09:30:00.000000005,QTX,8,77,9001,25.10,100,0,123456789") {
            Message::OrderExecution(e) => assert_eq!((e.printable, e.conditions), (false, [0; 4])),
            other => panic!("{other:?}"),
        }
        assert_eq!(
            p("104,14,09:30:00.000000005,QTX,9,77,78,25.12,500,B,"),
            Message::ReplaceOrder(ReplaceOrder {
                head: h(14, "QTX", 9),
                order_id: 77,
                new_order_id: 78,
                price: 25_120_000_000,
                volume: 500,
                side: b'B',
            })
        );
        assert_eq!(
            p("105,15,09:30:00.000000005,QTX,10,25.10,1000,200,,0930,M,B,,,,,,,0,0,0,0,,"),
            Message::Imbalance(h(15, "QTX", 10))
        );
        assert_eq!(
            p("110,16,09:30:00.000000005,QTX,11,9002,25.11,40,1,@,,,I"),
            Message::NonDisplayedTrade(NonDisplayedTrade {
                head: h(16, "QTX", 11),
                trade_id: 9002,
                price: 25_110_000_000,
                volume: 40,
                printable: true,
                conditions: [b'@', 0, 0, b'I'],
            })
        );
        assert_eq!(
            p("111,17,09:30:00.000000005,QTX,12,55,25.10,12000,O"),
            Message::CrossTrade(CrossTrade {
                head: h(17, "QTX", 12),
                cross_id: 55,
                price: 25_100_000_000,
                volume: 12_000,
                cross_type: b'O',
            })
        );
        assert_eq!(
            p("112,18,09:30:00.000000005,QTX,13,9001"),
            Message::TradeCancel(TradeCancel {
                head: h(18, "QTX", 13),
                trade_id: 9001,
            })
        );
        assert_eq!(
            p("113,19,09:30:00.000000005,QTX,14,55,11900"),
            Message::CrossCorrection(CrossCorrection {
                head: h(19, "QTX", 14),
                cross_id: 55,
                volume: 11_900,
            })
        );
        assert_eq!(
            p("114,20,09:30:00.000000005,QTX,15,C"),
            Message::RetailPriceImprovement(RetailPriceImprovement {
                head: h(20, "QTX", 15),
                indicator: b'C',
            })
        );
        assert_eq!(
            p("114,20,09:30:00.000000005,QTX,15,"),
            Message::RetailPriceImprovement(RetailPriceImprovement {
                head: h(20, "QTX", 15),
                indicator: 0,
            })
        );
        assert_eq!(
            p("223,21,16:00:01.000000000,QTX,25.50,24.90,25.00,25.40,987654"),
            Message::StockSummary(StockSummary {
                seq: 21,
                time_ns: 57_601 * NS,
                symbol: b"QTX",
                total_volume: 987_654,
            })
        );
        assert_eq!(p("140,22,x"), Message::OtherProduct { msg_type: 140 });
        assert_eq!(p("220"), Message::OtherProduct { msg_type: 220 });
        assert_eq!(p("999,1"), Message::Unknown { msg_type: 999 });
    }

    #[test]
    fn refresh_and_imbalance_accept_both_documented_layouts() {
        let contiguous = p("106,30,10:00:00,QTX,40,501,25.05,700,S,,");
        let gapped = p("106,30,10:00:00,,QTX,40,501,25.05,700,S,,");
        let want = AddOrder {
            head: Head {
                seq: 30,
                time_ns: 36_000 * NS,
                symbol: b"QTX",
                symbol_seq: 40,
            },
            order_id: 501,
            price: 25_050_000_000,
            volume: 700,
            side: b'S',
            firm_id: b"",
        };
        assert_eq!(contiguous, Message::AddOrderRefresh(want));
        assert_eq!(gapped, Message::AddOrderRefresh(want));
        let i1 = p("105,31,10:00:00,QTX,41,25.05,100,0,0,1600,C,,0,0,0,0,0,0,0,0,0,0,,");
        let i2 = p("105,31,10:00:00,,QTX,41,25.05,100,0,0,1600,C,,0,0,0,0,0,0,0,0,0,0,,");
        assert_eq!(i1, i2);
        assert_eq!(i1.head().map(|h| (h.symbol, h.symbol_seq)), Some((&b"QTX"[..], 41)));
    }

    #[test]
    fn malformed_lines_name_the_field() {
        assert_eq!(err(""), ParseError::Empty);
        assert_eq!(err("abc,1"), ParseError::BadType);
        assert_eq!(err("0,1"), ParseError::BadType);
        assert_eq!(err("70000,1"), ParseError::BadType);
        assert_eq!(
            err("100,10,09:30:00,QTX,5,77,25.10,300"),
            ParseError::TooFewFields {
                msg_type: 100,
                need: 9,
                got: 8
            }
        );
        let bad = |s: &str, field: u8| {
            assert!(
                matches!(err(s), ParseError::BadField { field: f, .. } if f == field),
                "{s}: {:?}",
                err(s)
            );
        };
        bad("100,x,09:30:00,QTX,5,77,25.10,300,B,,", 2);
        bad("100,10,9:30:00,QTX,5,77,25.10,300,B,,", 3);
        bad("100,10,09:30:00,,5,77,25.10,300,B,,", 4);
        bad("100,10,09:30:00,1234,5,77,25.10,300,B,,", 4);
        bad("100,10,09:30:00,QTX,5x,77,25.10,300,B,,", 5);
        bad("100,10,09:30:00,QTX,5,77,25.1.0,300,B,,", 7);
        bad("100,10,09:30:00,QTX,5,77,25.10,3e2,B,,", 8);
        bad("100,10,09:30:00,QTX,5,77,25.10,300,BS,,", 9);
        bad("101,10,09:30:00,QTX,5,77,25.10,300,2,B,", 9);
        bad("103,10,09:30:00,QTX,5,77,1,25.10,300,Y", 10);
        bad("3,2,QTX,x", 4);
        // Informational fields never reject a line: odd values read as 0.
        match p("3,2,QTX,1,x,NN,CS,1e2,,,PP,YES,,") {
            Message::SymbolIndexMapping(m) => assert_eq!(
                (
                    m.market_id,
                    m.system_id,
                    m.exchange_code,
                    m.security_type,
                    m.lot_size,
                    m.price_resolution,
                    m.round_lot
                ),
                (1, 0, 0, 0, 0, 0, 0)
            ),
            other => panic!("{other:?}"),
        }
        match p("34,9,09:30:00,QTX,4,OO,ZZ,,,,,,EE,OO") {
            Message::SecurityStatus(s) => {
                assert_eq!((s.status, s.halt_condition, s.ssr_state, s.market_state), (0, 0, 0, 0))
            }
            other => panic!("{other:?}"),
        }
        let e = err("100,10,9:30:00,QTX,5,77,25.10,300,B,,");
        assert_eq!(
            e.to_string(),
            "message type 100: field 3 is not a HH:MM:SS.nnnnnnnnn time"
        );
    }

    #[test]
    fn encode_round_trips_every_layout() {
        let imbalance = format!("105,15,09:30:00.000000005,QTX,10{}", ",".repeat(19));
        let lines = [
            "3,2,BRK A,1,7,N,C,100,,,0,Y,1,100",
            "34,9,09:30:00.000000005,QTX,4,O,~,,,,,,~,O",
            "100,10,09:30:00.000000005,QTX,5,77,25.10,300,B,,",
            "100,10,09:30:00.000000005,QTX,5,77,0.0001,300,S,ABCD,",
            "106,10,09:30:00.000000005,QTX,5,77,25.125,300,S,,",
            "101,11,09:30:00.000000005,QTX,6,77,25.10,200,1,B,",
            "102,12,09:30:00.000000005,QTX,7,77,",
            "103,13,09:30:00.000000005,QTX,8,77,9001,25.10,100,1,,@,F,T,I",
            "104,14,09:30:00.000000005,QTX,9,77,78,25.12,500,B,",
            imbalance.as_str(),
            "110,16,09:30:00.000000005,QTX,11,9002,25.11,40,0,@,,,",
            "111,17,09:30:00.000000005,QTX,12,55,25.10,12000,O",
            "112,18,09:30:00.000000005,QTX,13,9001",
            "113,19,09:30:00.000000005,QTX,14,55,11900",
            "114,20,09:30:00.000000005,QTX,15,C",
            "223,21,16:00:01.000000000,QTX,,,,,987654",
        ];
        for line in lines {
            let m = p(line);
            let mut out = Vec::new();
            encode(&m, &mut out);
            assert_eq!(out.pop(), Some(b'\n'));
            assert_eq!(String::from_utf8_lossy(&out), line, "encode(parse(line))");
            assert_eq!(parse_line(&out).ok(), Some(m), "parse(encode(m))");
        }
        let mut out = Vec::new();
        put_price(&mut out, 25_000_000_000);
        out.push(b' ');
        put_price(&mut out, 100_000);
        out.push(b' ');
        put_price(&mut out, 1_123_456_789);
        assert_eq!(out, b"25.00 0.0001 1.123456789");
        assert!(INTEGRATED_TYPES.iter().all(|t| !OTHER_PRODUCT_TYPES.contains(t)));
    }
}
