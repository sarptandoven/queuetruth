//! The `nyse` rule set: the five NYSE Group Pillar equities markets of the TAQ Integrated Feed and how each one
//! allocates an execution among the resting orders at a price. This is the documentation behind
//! `qt_types::rules_for(RuleSet::Nyse)` and behind the ingest label `nyse_parity_ranks_approximate`.
//!
//! # Record semantics (all five markets)
//!
//! The reader writes Nasdaq-style records (`RuleSet::Nyse.venue()` is `Venue::Nasdaq`): no `M` records (a Modify
//! Order becomes a partial `C` when it keeps priority, else `C` + `A`), `T` prints with the aggressor side for
//! displayed executions, side-`N` prints for non-displayed trades, and displayed interest ahead of non-displayed
//! interest at a price (NYSE Rule 7.36(e): Priority 2 Display Orders before Priority 3 Non-Display Orders; the same
//! priority categories exist on the other four markets). Working time (NYSE Rule 7.36(f)): an order keeps it when
//! its size decreases and gets a new one when its price changes or its size increases. On Pillar a cancel/replace
//! keeps its time priority and its Order ID only when it reduces the quantity (or changes the side from S to SS or
//! SSE); every other cancel/replace gets a new time priority and a new Order ID, and so does the replenished part of
//! a reserve order (NYSE Pillar Trader Update, 2015-03-02, "Cancel and Cancel Replace Handling" and "Reserve
//! Orders"). The reader expects the first case as a Modify Order of the same Order ID (kept in place) and the second
//! as a Replace Order or an Add Order with a new Order ID (back of the queue; a cancel/replace also moves a
//! Participant to the back of its allocation wheel, Rule 7.37(b)(2)(D)). The TAQ specification does not settle the
//! first case: its section 6 sends a Modify Order for a change "due to an event other than a cancel-replace", and
//! its section 9 gives every Replace Order a new Order ID. If real files publish a priority-keeping reduction as a
//! Replace Order, the reader puts that order at the back of the queue on all five markets; the INFO counter
//! `nyse_replace_reduce_same_price` (Replace Orders at the old side and price with a smaller size) shows on the
//! first licensed file which case occurs. The record `sequence` is the
//! channel SequenceNumber, which has holes for the control messages the TAQ product omits (the Source Time Reference
//! message is sent once a second per channel, Pillar Equities Common Client Specification v2.4p section 4.2), so the
//! rule set has `sequence_meaningful = false` and the reader checks each symbol's SymbolSeqNum instead (section 3.4
//! of the same specification).
//!
//! # Allocation: parity on NYSE (market id 1)
//!
//! NYSE does not allocate by time across the book. At each price an Aggressing Order is allocated (NYSE Rule
//! 7.37(b)(1)): first to unexecuted Market Orders by time; then to the order with Setter Priority (Rule 7.36(h): the
//! first round-lot displayed order that set a new NBBO at that price), which since SR-NYSE-2020-55 (approved
//! 2020-08-06, Release 34-89499) trades in full before anyone else; then to Priority 2 Display Orders on parity by
//! Participant; then to non-displayed orders on parity; then to the remaining categories. A Participant (Rule
//! 7.36(a)(5), with the DMM added by SR-NYSE-2019-05, approved 2019-05-29, Release 34-85962) is each Floor broker
//! trading license, the DMM assigned to the security, or the Book Participant: every other order on the book,
//! collectively. Parity (Rule 7.37(b)(2) to (5)): the Participants at a price sit on an allocation wheel in the order
//! of their first order there; a pointer names the next one; each turn of the pointer allocates one round lot; an
//! odd-lot remainder stays with its Participant; within the Book Participant the orders are allocated by working
//! time. Text read: the NYSE Rules as hosted on nyse.com (a 2019 reproduction), the two SEC releases above and the
//! NYSE Parity FAQs (2024: an order with Setter Priority trades its full displayed quantity before the other
//! parity Participants get shares, and Setter Priority is not kept in the Closing Auction or a reopening auction).
//!
//! What this means for queuetruth: the TAQ Integrated Feed shows each displayed order with its Order ID, but not
//! which Participant entered it (FirmID is blank unless the order is attributed; the NYSE Parity FAQs: "There is no
//! parity identifier on NYSE feeds"). The exact replay keeps each price level in time order, so a FIFO rank on NYSE
//! is not the allocation order: an order behind a Floor broker's or the DMM's earlier order can be filled first, and
//! a shadow order joins the Book Participant, whose share of each execution depends on how many Participants rest at
//! the price. The error has no fixed sign. Every NYSE dataset therefore carries the non-INFO counter
//! [`PARITY_COUNTER`] (the number of instrument-days, dataset partitions, of a market that allocated by parity on
//! that session date, whether or not they traded; `nyse_parity_executions`, INFO, counts their displayed
//! executions), which makes its data-quality verdict `warnings`; replay repeats the reason in its own data quality
//! (and `--strict` fails), `queuetruth dq` reports it, and the replay rank oracle measures how far the real
//! executions were from the head of the FIFO queue. The zoo, calibration and the features engine read the dataset's
//! feed gaps but not its verdict, and per-shadow `dq_flags` of a replay do not carry it.
//!
//! # Allocation: price-time on the other four markets
//!
//! NYSE American (market id 9) moved its equities from a floor-based parity model to fully automated price-time
//! priority on Pillar (SR-NYSEMKT-2017-01: notice Release 34-79993, 2017-02-09; approval as amended Release 34-80590,
//! 2017-05-04; Rules 7.36E / 7.37E) on 2017-07-24, when NYSE MKT became NYSE American. Its TAQ history before that day,
//! the `EQY_US_MKT_IBF_*` files of 2015-11-16 to 2017-07-21, records the Floor-based NYSE MKT, so the rule set treats
//! those session dates as parity ([`Market::allocation_on`], [`AMERICAN_PILLAR_DATE`]) and labels them like NYSE. NYSE
//! Arca (3) has been price-time since its Pillar rules (Rules 7.36-E / 7.37-E). NYSE National (10, SR-NYSENAT-2018-02,
//! approved 2018-05-17, Release 34-83289) and NYSE Texas (11, formerly NYSE Chicago, SR-NYSECHX-2019-08, approved
//! 2019-10-09, Release 34-87264) relaunched on Pillar with fully automated price-time allocation (from the SEC notices'
//! summaries; their rule text was not re-read here). On these markets a FIFO rank is the allocation order and the label
//! is not set.

use std::fmt;

/// `source_specific` counter that labels a dataset's queue ranks approximate: the number of its partitions
/// (instrument-days) whose market allocated by parity on that session date, traded or not (non-INFO: the verdict is
/// at least `warnings`, and replay repeats it as `ingest: nyse_parity_ranks_approximate = N`).
pub const PARITY_COUNTER: &str = "nyse_parity_ranks_approximate";

/// First session date (days since 1970-01-01) of NYSE American on Pillar, 2017-07-24: until then the market was
/// NYSE MKT, a Floor-based market with a parity allocation model (SR-NYSEMKT-2017-01), whose TAQ history files are
/// `EQY_US_MKT_IBF_*` (2015-11-16 to 2017-07-21, TAQ Products Client Specification v2.4b, Appendix B).
pub const AMERICAN_PILLAR_DATE: i32 = 17_371;

/// How a market allocates an execution among the resting orders at one price.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Allocation {
    /// Setter priority, then round lots per Participant around an allocation wheel (NYSE Rule 7.37(b)).
    Parity,
    /// Price, then priority category, then time.
    PriceTime,
}

impl Allocation {
    /// True when a FIFO rank is the allocation order.
    pub const fn fifo_ranks_exact(self) -> bool {
        matches!(self, Self::PriceTime)
    }

    /// `parity` or `price_time`.
    pub const fn name(self) -> &'static str {
        match self {
            Self::Parity => "parity",
            Self::PriceTime => "price_time",
        }
    }
}

/// An NYSE Group equities market of the TAQ Integrated Feed.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Market {
    /// NYSE (market id 1, files `EQY_US_NYSE_IBF_*`).
    Nyse,
    /// NYSE Arca (3, `EQY_US_ARCA_IBF_*`).
    Arca,
    /// NYSE American (9, `EQY_US_AMEX_IBF_*`, before 2017-07-24 `EQY_US_MKT_IBF_*`).
    American,
    /// NYSE National (10, `EQY_US_NATIONAL_IBF_*`).
    National,
    /// NYSE Texas, formerly NYSE Chicago (11, `EQY_US_TEXAS_IBF_*`, earlier `EQY_US_CHICAGO_IBF_*`).
    Texas,
}

impl Market {
    /// Every market, in market-id order.
    pub const ALL: [Self; 5] = [Self::Nyse, Self::Arca, Self::American, Self::National, Self::Texas];

    /// The market of a Symbol Index Mapping `Market ID` (1, 3, 9, 10, 11); `None` for the options and bond markets
    /// (4, 5, 8) and unknown ids.
    pub const fn from_market_id(id: u32) -> Option<Self> {
        match id {
            1 => Some(Self::Nyse),
            3 => Some(Self::Arca),
            9 => Some(Self::American),
            10 => Some(Self::National),
            11 => Some(Self::Texas),
            _ => None,
        }
    }

    /// The market of a file-name exchange token (`NYSE`, `ARCA`, `AMEX`, `MKT`, `NATIONAL`, `TEXAS`, `CHICAGO`;
    /// case-insensitive).
    pub fn from_file_token(t: &str) -> Option<Self> {
        match t.to_ascii_uppercase().as_str() {
            "NYSE" => Some(Self::Nyse),
            "ARCA" => Some(Self::Arca),
            "AMEX" | "MKT" => Some(Self::American),
            "NATIONAL" => Some(Self::National),
            "TEXAS" | "CHICAGO" => Some(Self::Texas),
            _ => None,
        }
    }

    /// Symbol Index Mapping `Market ID`.
    pub const fn market_id(self) -> u32 {
        match self {
            Self::Nyse => 1,
            Self::Arca => 3,
            Self::American => 9,
            Self::National => 10,
            Self::Texas => 11,
        }
    }

    /// `source_venue` of the dataset and its instrument rows.
    pub const fn source_venue(self) -> &'static str {
        match self {
            Self::Nyse => "nyse",
            Self::Arca => "nyse_arca",
            Self::American => "nyse_american",
            Self::National => "nyse_national",
            Self::Texas => "nyse_texas",
        }
    }

    /// The allocation rule of the market today, on Pillar (module docs).
    pub const fn allocation(self) -> Allocation {
        match self {
            Self::Nyse => Allocation::Parity,
            Self::Arca | Self::American | Self::National | Self::Texas => Allocation::PriceTime,
        }
    }

    /// The allocation rule in force on `session_date` (days since 1970-01-01): [`Market::allocation`], except that
    /// NYSE American allocated on parity, as the Floor-based NYSE MKT, before [`AMERICAN_PILLAR_DATE`].
    pub const fn allocation_on(self, session_date: i32) -> Allocation {
        match self {
            Self::American if session_date < AMERICAN_PILLAR_DATE => Allocation::Parity,
            m => m.allocation(),
        }
    }

    /// Where the allocation rule comes from.
    pub const fn allocation_source(self) -> &'static str {
        match self {
            Self::Nyse => {
                "NYSE Rules 7.36(a)(5), 7.36(h), 7.37(b); SR-NYSE-2019-05 (DMM Participant); SR-NYSE-2020-55 (Setter \
                 Priority trades in full)"
            }
            Self::Arca => "NYSE Arca Rules 7.36-E, 7.37-E (Pillar price-time)",
            Self::American => {
                "SR-NYSEMKT-2017-01: floor-based parity (NYSE MKT) replaced by price-time on Pillar on 2017-07-24"
            }
            Self::National => "SR-NYSENAT-2018-02: Pillar relaunch with price-time allocation",
            Self::Texas => "SR-NYSECHX-2019-08: Pillar transition with price-time allocation",
        }
    }
}

impl fmt::Display for Market {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Nyse => "NYSE",
            Self::Arca => "NYSE Arca",
            Self::American => "NYSE American",
            Self::National => "NYSE National",
            Self::Texas => "NYSE Texas",
        })
    }
}

/// The ingest warning of a dataset whose instruments trade on a parity market (`market` on `session_date`, the
/// earliest parity partition of the dataset): `days` instrument-day(s), `executions` displayed executions allocated
/// by parity.
pub fn parity_warning(market: Market, session_date: i32, days: usize, executions: u64) -> String {
    let rule = if market == Market::American && session_date < AMERICAN_PILLAR_DATE {
        "NYSE MKT (NYSE American before its move to Pillar on 2017-07-24) allocated executions on parity among the \
         book, the DMM and each Floor broker, not by time (a Floor-based market with a parity allocation model, \
         SR-NYSEMKT-2017-01)"
            .to_owned()
    } else {
        format!(
            "{market} allocates executions by Setter Priority and then round lots per Participant (each Floor broker, \
             the DMM, the Book Participant), not by time (NYSE Rule 7.37(b); Rule 72 before Pillar)"
        )
    };
    format!(
        "{rule}: FIFO queue ranks, queue ahead and shadow fill times of its {days} instrument-day(s) \
         ({PARITY_COUNTER} = {days}) are approximations, since the feed does not say which Participant entered an \
         order; {executions} displayed execution(s) were allocated this way (nyse_parity_executions; `replay \
         --rank-oracle` measures how far they were from the head of the FIFO queue)"
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use qt_types::{RuleSet, ValidationStatus, Venue, rules, rules_for};

    #[test]
    fn markets_ids_tokens_and_allocations() {
        for m in Market::ALL {
            assert_eq!(Market::from_market_id(m.market_id()), Some(m));
            assert!(m.source_venue().starts_with("nyse"));
            assert!(!m.allocation_source().is_empty());
        }
        for id in [0, 2, 4, 5, 8, 12] {
            assert_eq!(Market::from_market_id(id), None, "{id}");
        }
        assert_eq!(Market::from_file_token("nyse"), Some(Market::Nyse));
        assert_eq!(Market::from_file_token("AMEX"), Some(Market::American));
        assert_eq!(Market::from_file_token("MKT"), Some(Market::American));
        assert_eq!(Market::from_file_token("Chicago"), Some(Market::Texas));
        assert_eq!(Market::from_file_token("NASDAQ"), None);
        let parity: Vec<Market> = Market::ALL
            .into_iter()
            .filter(|m| m.allocation() == Allocation::Parity)
            .collect();
        assert_eq!(parity, [Market::Nyse], "only NYSE allocates on parity");
        assert!(!Allocation::Parity.fifo_ranks_exact() && Allocation::PriceTime.fifo_ranks_exact());
        // NYSE American was the Floor-based NYSE MKT (parity) before its move to Pillar on 2017-07-24.
        let day = |y, m, d| crate::time::Date::new(y, m, d).unwrap().days_from_civil();
        assert_eq!(AMERICAN_PILLAR_DATE, day(2017, 7, 24));
        assert_eq!(Market::American.allocation_on(day(2017, 7, 21)), Allocation::Parity);
        assert_eq!(Market::American.allocation_on(day(2015, 11, 16)), Allocation::Parity);
        assert_eq!(
            Market::American.allocation_on(AMERICAN_PILLAR_DATE),
            Allocation::PriceTime
        );
        for m in Market::ALL {
            for d in [day(2016, 1, 4), day(2026, 7, 1)] {
                if m != Market::American || d >= AMERICAN_PILLAR_DATE {
                    assert_eq!(m.allocation_on(d), m.allocation(), "{m} {d}");
                }
            }
        }
        assert_eq!(Market::American.to_string(), "NYSE American");
        assert_eq!(
            (Allocation::Parity.name(), Allocation::PriceTime.name()),
            ("parity", "price_time")
        );
    }

    /// The `nyse` rule set of `qt_types` is the `nasdaq` record semantics with unchecked record sequences and a
    /// synthetic-only validation label (the module docs give the citations).
    #[test]
    fn rule_set_values() {
        let r = rules_for(RuleSet::Nyse);
        assert_eq!((r.rule_set, r.venue), (RuleSet::Nyse, Venue::Nasdaq));
        assert_eq!(RuleSet::Nyse.venue(), Venue::Nasdaq);
        assert_eq!(r.validation, ValidationStatus::SyntheticOnly);
        assert!(!r.sequence_meaningful, "the TAQ files omit sequenced control messages");
        assert!(!r.modify_records_present && r.trades_have_aggressor && r.supports_hidden_orders);
        assert!(r.hidden_lower_priority && r.fill_removal_shares_sequence);
        let n = rules(Venue::Nasdaq);
        let same = qt_types::VenueRules {
            rule_set: RuleSet::Nasdaq,
            validation: n.validation,
            sequence_meaningful: true,
            ..r
        };
        assert_eq!(
            same, n,
            "only the name, the validation label and sequence_meaningful differ from nasdaq"
        );
        for s in ["nyse", "NYSE", " xnys "] {
            assert_eq!(s.parse::<RuleSet>().ok(), Some(RuleSet::Nyse), "{s}");
        }
        // Databento's dataset code is not an alias: its DBN files are read by `--format dbn`, which does not resolve
        // this rule set (docs/how-to/nyse-order-level.md, Limits).
        assert!("xnys.pillar".parse::<RuleSet>().is_err());
        assert!(
            "nyse".parse::<Venue>().is_err(),
            "nyse is a rule set, not a record venue"
        );
        assert_eq!(RuleSet::Nyse.to_string(), "nyse");
        assert_eq!(serde_json::to_string(&RuleSet::Nyse).ok().as_deref(), Some("\"nyse\""));
        assert_eq!(serde_json::from_str::<RuleSet>("\"nyse\"").ok(), Some(RuleSet::Nyse));
    }

    #[test]
    fn parity_warning_names_the_rule_and_the_counter() {
        let w = parity_warning(Market::Nyse, 20_458, 2, 17);
        assert!(w.starts_with("NYSE allocates executions by Setter Priority"), "{w}");
        assert!(
            w.contains("2 instrument-day(s) (nyse_parity_ranks_approximate = 2)")
                && w.contains("17 displayed execution(s)"),
            "{w}"
        );
        assert!(w.contains("NYSE Rule 7.37(b)"), "{w}");
        let w = parity_warning(Market::American, AMERICAN_PILLAR_DATE - 3, 1, 0);
        assert!(
            w.starts_with("NYSE MKT (NYSE American before its move to Pillar on 2017-07-24)"),
            "{w}"
        );
        assert!(
            w.contains("SR-NYSEMKT-2017-01")
                && w.contains("nyse_parity_ranks_approximate = 1")
                && w.contains("0 displayed execution(s)"),
            "{w}"
        );
    }
}
