# Read NYSE order-by-order data

**Goal:** turn NYSE Group's order-by-order historical files (TAQ NYSE Integrated Feed) into a queuetruth dataset,
and replay them knowing where a FIFO queue rank is the real allocation order and where it is not.

**You need:** Integrated Feed files you are licensed to use (`EQY_US_<market>_IBF_<channel>_<date>.gz`, one per
data channel and day), or the files in `tests/fixtures/nyse/` to try every command below, and queuetruth built from
this repository.

## Read the data terms first

NYSE posts sample files of this product at
[ftp.nyse.com, Historical Data Samples](https://ftp.nyse.com/Historical%20Data%20Samples/) (listed 2026-10-24:
2026-07-01 and 2026-07-02 for each of the five markets, two NYSE Texas days of 2025 under its former name
`EQY_US_CHICAGO_IBF_*`, and five NYSE Arca days of 2020 to 2022; the NYSE Arca folder also holds
`EQY_US_ALL_ARCA_BOOK_*` days of 2009 to 2015, an older format this reader does not read). The directory has no
terms file. The
[TAQ Integrated Feed product page](https://www.nyse.com/market-data/historical/taq-integrated-feed) links to it, and
the only terms we found are ICE's [website Terms of Use](https://www.ice.com/privacy-security-center/terms-of-use),
which govern www.nyse.com and the content available through it: they license downloads for personal,
non-commercial use and, without prior written permission, rule out adapting, extracting, compiling or creating
derivative works from the content. An ingest and a replay do all of those. NYSE's
[market data policies](https://www.nyse.com/publicdocs/nyse/data/NYSE_Market_Data_Complete_Policy_Package.pdf) also
bar redistributing its market data externally on a historical basis, in any form, without a specific licence (the
Historical Use policy, written for vendors of its real-time data).

So queuetruth downloads no NYSE file and ships none. The reader was built from NYSE's published specifications and
tested on hand-written and synthetic files, and the `nyse` rule set is labelled `synthetic_only`. Run it on files you
are licensed for (the purchased TAQ product under NYSE's agreements, a vendor's NYSE order-by-order data under that
vendor's terms, or the samples with NYSE's written permission) and keep the outputs inside your licence. This page
is not legal advice; see [Data and licensing](../data-and-licensing.md).

## How queuetruth reads the files

The format is the CSV of the
[TAQ Products Client Specification](https://www.nyse.com/publicdocs/nyse/data/TAQ_XDP_Products_Client_Spec_v2.4b.pdf)
(v2.4b, 2026-09-08): one line per real-time message, in feed order, fields in the documented order, blank for the
default values 0 and space, prices as decimals and SourceTime as `HH:MM:SS.nnnnnnnnn`. Every displayed order is
added, modified, deleted, replaced or executed under its Order ID, so the displayed book is exact:

| Integrated Feed message | queuetruth records |
|---|---|
| Add Order (100) | `A` |
| Modify Order (101), same price, smaller size | `C` of the decrease: the order keeps its place (NYSE Rule 7.36(f)) |
| Modify Order (101), new price, larger size, or PositionChange 1 | `C` of the remaining size, then `A` with the same id at the back of the queue |
| Delete Order (102) | `C` of the remaining size |
| Order Execution (103) | `T` (aggressor = the opposite side) + `F` + `C`; when it is not printable (an auction allocation) no `T`, and the `F` and `C` carry the flag `PUBLISHER_SPECIFIC` |
| Replace Order (104) | `C` of the old order, then `A` with the new Order ID at the back of the queue |
| Add Order Refresh (106) | the book is cleared (`R`) and rebuilt from the refresh, inside a feed gap |
| Non-Displayed Trade (110) | `T` with side `N`: a hidden print that consumes no displayed queue |
| Cross Trade (111) | `T` with side `N`, kind `cross` |
| Symbol Index Mapping (3), Security Status (34), Imbalance (105), Trade Cancel (112), Cross Correction (113), Retail Price Improvement (114), Stock Summary (223) | no records (counters) |

A few things the reader does that matter for exactness:

- **Which changes keep the queue place.** On Pillar a cancel/replace keeps its time priority and its Order ID only
  when it reduces the quantity (or changes a sell to a short sale); every other cancel/replace gets a new time
  priority and a new Order ID, and so does the replenished part of a reserve order
  ([Pillar trader update, 2015-03-02](https://www.nyse.com/publicdocs/nyse/markets/nyse/Pillar_Trader_Update_Mar_2015.pdf)).
  The reader expects the first case as a Modify Order of the same Order ID, which it keeps in place, and the others
  as a Replace Order or an Add Order with a new Order ID, which go to the back of the queue. The TAQ specification
  does not settle the first case: its section 6 sends a Modify Order for a change "due to an event other than a
  cancel-replace", and its section 9 gives every Replace Order a new Order ID. If real files publish a reduction as
  a Replace Order, those orders go to the back of the queue on every market, the price-time ones included. The INFO
  counter `nyse_replace_reduce_same_price` (Replace Orders at the old side and price with a smaller size) shows on
  your first file which case occurs.
- **Dates and channels from the file names.** `EQY_US_NYSE_IBF_3_20260105.csv.gz` is NYSE, channel 3, 2026-01-05.
  Give every channel file of a day in one ingest (a symbol lives on one channel); the files are read in date and
  channel order whatever order you pass them in, so instrument ids are stable. A renamed file needs `--date`.
- **Symbol sequence numbers, not channel sequence numbers.** The TAQ files leave out sequenced control messages
  (Pillar sends a Source Time Reference message once a second per channel), so holes in the channel numbering are
  normal. Every message of a symbol carries the symbol's own sequence number, and a jump there is a lost message: it
  becomes a `feed_gaps` entry, and replay censors that instrument's shadows across it.
- **Failovers.** After a publisher failover the backup clears every symbol of the channel (a Symbol Clear message)
  and re-sends each order still resting as an Add Order Refresh (TAQ specification section 11). The Symbol Clear is
  not in the TAQ product, so the first refresh message of a symbol clears its book. A symbol whose resting orders
  were all removed during the outage gets no refresh message at all, so the reader treats a refresh as the failover
  of the whole channel: every other symbol of the channel with resting orders waits for its next order message. A
  refresh of its own rebuilds it; a modify, delete, execution or replace of one of its resting orders shows that its
  book survived (counted `nyse_failover_books_kept`); an add would land on a book the backup cleared, so the reader
  clears it first (`R`, counted `nyse_failover_books_cleared`, with a feed gap from the symbol's last message before
  the failover). A symbol with no order message after the failover is cleared at the end of the file.
- **Time zone.** The specification gives SourceTime no zone. The reader reads it as US/Eastern (`--tz UTC` if your
  files are UTC) and checks: the core session starts at 09:30 Eastern, so when the Security Status messages that
  open it (`O`) sit at 14:30 or 13:30 in the file, the ingest warns that the clock is UTC and says what to pass.
- **Symbols** are in NYSE symbology: share classes take a space (`BRK A`), so filter with `--symbols "BRK A"`.

## The `nyse` rule set: parity on NYSE, price-time on its sister markets

The records follow the `nasdaq` record semantics (displayed interest ahead of hidden interest at a price, hidden
prints, aggressor sides), so a dataset carries `venue = nasdaq` (the record semantics, also on the HTML report's
dataset line), `rule_set = nyse` and a `source_venue` naming the market. What differs is how each market allocates
an execution among the orders resting at a price:

| Market | Market ID | Allocation | Source |
|---|---|---|---|
| NYSE | 1 | parity | NYSE Rules 7.36 and 7.37; SR-NYSE-2019-05; SR-NYSE-2020-55 |
| NYSE Arca | 3 | price-time | Rules 7.36-E and 7.37-E |
| NYSE American | 9 | price-time since 2017-07-24; parity before, as NYSE MKT (`EQY_US_MKT_IBF_*` files) | SR-NYSEMKT-2017-01 (notice Release 34-79993, approval Release 34-80590): it left floor-based parity when it moved to Pillar |
| NYSE National | 10 | price-time | SR-NYSENAT-2018-02 |
| NYSE Texas (formerly NYSE Chicago) | 11 | price-time | SR-NYSECHX-2019-08 |

On NYSE an incoming order is not matched against the oldest order first. It goes first to the order with Setter
Priority (the round-lot displayed order that set a new national best bid or offer), which trades its full displayed
size; the rest is dealt out one round lot at a time around an allocation wheel of Participants: each Floor broker,
the DMM, and the Book Participant, which is every other order together, filled by time. The feed shows every order
but not which Participant entered it (NYSE's [parity FAQ](https://nyse.com/publicdocs/nyse/NYSE_Parity_FAQs.pdf):
"There is no parity identifier on NYSE feeds"), so queuetruth cannot replay the wheel. Its exact replay keeps each
price in time order, and on NYSE that FIFO rank is an approximation whose error can go either way: a shadow order
joins the Book Participant, which may be served before older Floor broker and DMM orders, or after a long Book
queue.

queuetruth says so instead of pretending. A dataset of an NYSE market carries the counter
`nyse_parity_ranks_approximate`, the number of its instrument-days (partitions) on a parity market, whether they
traded or not, and an ingest warning in plain words; the INFO counter `nyse_parity_executions` counts their displayed
executions. The label makes the data-quality verdict `warnings`; replay repeats it (`ingest:
nyse_parity_ranks_approximate = N` in its `data_quality.reasons`, with a verdict of at least `warnings`), `queuetruth
dq` reports it, and `--strict` fails on both. Datasets of the four price-time markets carry no such label, except
NYSE American days before 2017-07-24: the market was then the Floor-based NYSE MKT, with a parity allocation model,
and those days are labelled too.

Two consequences. First, `--strict` cannot gate an NYSE dataset, because the label is always there; gate on the
other reasons instead (the next section shows how). Second, the label does not reach every output yet: the zoo,
calibration and the features engine read the dataset's feed gaps but not its verdict, so `queuetruth zoo` on NYSE
data still prints "exact fill rate" and "in the exact replay" for what is a FIFO approximation, and each shadow
row's `dq_flags` in a replay reflects only its partition's book counters.

## Try it on the fixtures

The fixtures are invented: an NYSE channel with two symbols, `QTN` and `QTN B`, written message by message from the
specification (`tests/fixtures/nyse/README.txt` walks through it). At 09:31:30 a sell of 800 shares meets four bids
at 25.02: an odd lot of 50 from the book (order 1010), then 500 from a Floor broker that set the round-lot quote
(1011, Setter Priority), 300 from the book (1012) and 200 from the DMM (1013). NYSE fills 1011 in full first, then a
round lot to the Book Participant (1010 and 1012), one to the DMM (1013) and one more to the Book (1012). By time it
would have been 1010, 1011 and 250 of 1012.

<!-- check-docs: run -->
```sh
queuetruth ingest --format nyse-ibf --venue nyse \
  --in "$QT_FIXTURES/nyse/EQY_US_NYSE_IBF_3_20260105.csv" --out nyse-ds
queuetruth dq --data nyse-ds --out nyse-dq --fail-on fail
queuetruth replay --data nyse-ds --out nyse-replay --rank-oracle
```

<!-- check-docs: run -->
```python
import json

s = json.load(open("nyse-replay/summary.json"))
dq = s["data_quality"]
print(dq["verdict"], dq["reasons"])
assert dq["verdict"] == "warnings" and "ingest: nyse_parity_ranks_approximate = 2" in dq["reasons"]
print("FIFO rank of each execution (0, 1, 2, 3, 4, 5+):", s["rank_oracle"]["fifo_rank_histogram"])
```

The replay's verdict is `warnings` only because of the label (2 instrument-days, `QTN` and `QTN B`; 10 displayed
executions); its two reasons are `ingest: nyse_parity_ranks_approximate = 2` and `ingest verdict: warnings`. The
book is clean (every invariant 0, `queuetruth dq` passes its book, sequence and trade-parity checks), and the rank
oracle finds 2 of the 10 executions behind the head of their FIFO queue, `[8, 2, 0, 0, 0, 0]`: 1011, filled before
1010, and 1013, filled before the rest of 1012.

The same day on NYSE Arca, where the sell fills by time, ingests `clean` with no label:

<!-- check-docs: run -->
```sh
queuetruth ingest --format nyse-ibf --venue nyse \
  --in "$QT_FIXTURES/nyse/EQY_US_ARCA_IBF_1_20260105.csv" --out arca-ds --strict
```

`EQY_US_NYSE_IBF_4_20260105.csv` is a channel with a publisher failover at 10:00:05, a busted trade, a cross
correction, two lost messages and an unknown message type. The backup re-sends the two `QTF` orders still resting.
`QTG` lost both its orders during the outage, so nothing is re-sent for it; its next message, at 10:02, is an offer
at 29.90 that would cross its stale 30.00 bid, and the reader clears its book first:

<!-- check-docs: run -->
```sh
queuetruth ingest --format nyse-ibf --venue nyse \
  --in "$QT_FIXTURES/nyse/EQY_US_NYSE_IBF_4_20260105.csv" --out failover-ds
queuetruth replay --data failover-ds --out failover-replay
```

<!-- check-docs: run -->
```python
import json

ingest = json.load(open("failover-ds/data_quality.json"))
gaps = [(g["instrument_id"], g["reason"]) for g in ingest["feed_gaps"]]
print(gaps)
assert (2, "nyse failover: symbol cleared without refresh") in gaps
assert "nyse_failover_books_cleared = 1" in ingest["reasons"]
replay = json.load(open("failover-replay/summary.json"))["data_quality"]
assert replay["crossed_books_after_last"] == 0, replay
```

## Gate on the other reasons

Every NYSE dataset is `warnings` because of the label, so `--strict` cannot tell it from a dataset with real faults,
and `queuetruth dq --fail-on fail` lets feed gaps and sequence gaps through (they are warnings). This gate fails on
everything `--strict` would, except the label: a counter reason of the ingest data quality other than
`nyse_parity_ranks_approximate` (the `warning:` lines are informational, as for `--strict`), a reason of the replay's
own book counters, or an `unusable` verdict.

<!-- check-docs: run -->
```python
import json


def faults(dataset, replay_dir):
    """What --strict would fail on, the parity label aside."""
    ingest = json.load(open(f"{dataset}/data_quality.json"))
    replay = json.load(open(f"{replay_dir}/summary.json"))["data_quality"]
    found = [r for r in ingest["reasons"] if not r.startswith(("nyse_parity_ranks_approximate = ", "warning: "))]
    found += [r for r in replay["reasons"] if not r.startswith("ingest")]
    if "unusable" in (ingest["verdict"], replay["verdict"]):
        found.append("verdict unusable")
    return found


assert faults("nyse-ds", "nyse-replay") == []
print(faults("failover-ds", "failover-replay"))
assert "feed_gaps = 3" in faults("failover-ds", "failover-replay")
```

## Run it on your files

Pass every channel file of a day (or, with `--symbols`, at least the channel files that carry your symbols) and let
the reader take the market, channels and date from the names:

<!-- check-docs: skip (needs licensed NYSE files) -->
```sh
queuetruth ingest --format nyse-ibf --venue nyse \
  --in EQY_US_NYSE_IBF_1_20260701.gz --in EQY_US_NYSE_IBF_2_20260701.gz \
  --symbols "BRK A" --out nyse-20260701
queuetruth dq --data nyse-20260701 --out nyse-20260701-dq --fail-on fail
queuetruth replay --data nyse-20260701 --out nyse-20260701-replay --rank-oracle
```

Read the ingest warnings first: the clock check, any feed gaps and failovers, and on NYSE the parity label. Then run
the gate above, and read the rank oracle as the next section describes.

## Measure the allocation with the rank oracle

The rank oracle records, for every execution, how many orders stood ahead of the executed order in its price's FIFO
queue. It measures the order of allocation, which bounds how far a FIFO model's allocation order is off; it is not
the error in a shadow order's fill time.

On real price-time data the head of the FIFO queue is not always the order executed. On Nasdaq, queuetruth's
real-data runs find 0.3% to 4.6% of executions behind the head (a rank-0 share of 0.954 to 0.997, raw, per liquidity
tier and day over 36 names and two days; see [Results](../RESULTS.md), sections 1 and 4b). On the RD-2 day 4,270 of
176,103 executions were behind the head raw and 2,249 after the sweep adjustment (which ignores orders ahead
executed in the same sweep: fills at one price within a short gap of each other), and 617 of the 4,270 came in the
first 60 s after the open. So on a real NYSE day the raw histogram mixes the parity effect with that price-time
baseline. Run a same-day control on a price-time market, the NYSE Arca or NYSE National channel files of the same
symbols, and read the excess of NYSE's behind-the-head share over the control's as the parity signal, with the raw
(`fifo_rank_histogram`) and sweep-adjusted (`fifo_rank_histogram_sweep_adjusted`) histograms side by side.

The two synthetic files in `tests/fixtures/nyse/` show the mechanics. queuetruth's generator
(`qt_ingest::nyse::synth`) wrote them: it fills orders by a model of NYSE Rule 7.37(b) on the NYSE file and by time
on the NYSE Arca file, and knows the FIFO rank of every execution it wrote:

<!-- check-docs: run -->
```sh
for m in NYSE ARCA; do
  queuetruth ingest --format nyse-ibf --venue nyse \
    --in "$QT_FIXTURES/nyse/EQY_US_${m}_IBF_7_20260106.csv" --out "synth-$m"
  queuetruth replay --data "synth-$m" --out "synth-$m-replay" --rank-oracle
done
```

<!-- check-docs: run -->
```python
import json

want = {
    "NYSE": ([2665, 444, 218, 107, 60, 184], [2706, 420, 214, 98, 59, 181]),
    "ARCA": ([1982, 0, 0, 0, 0, 0], [1982, 0, 0, 0, 0, 0]),
}
for market, (raw, adjusted) in want.items():
    ro = json.load(open(f"synth-{market}-replay/summary.json"))["rank_oracle"]
    got = (ro["fifo_rank_histogram"], ro["fifo_rank_histogram_sweep_adjusted"])
    print(market, "raw", got[0], "sweep-adjusted", got[1])
    assert got == (raw, adjusted), (market, got)
```

The replay reproduces the generator's ground truth exactly: on the simulated NYSE day 1,013 of 3,678 executions
(27.5%) were filled behind the head of their FIFO queue, on the simulated NYSE Arca day none of 1,982. That share
comes from the generator's parameters (three Floor brokers, the DMM, a random mix of Participants per order, seed
20260106) and says nothing about real NYSE; it only shows that the reader and the replay recover what the generator
did. The simulated NYSE Arca day fills strictly by time, each execution at the head of its queue when it prints,
so its rank is 0 for every execution, unlike a real price-time day.

Read both histograms. The sweep adjustment does not count orders ahead that were executed in the same sweep, which
is right for a feed that splits one match into several messages, but parity deals one aggressing order out across
several orders of a price in exactly such a sweep: on the fixture day it turns `[8, 2, 0, 0, 0, 0]` into ten
executions at the head, and on the synthetic NYSE day it moves 41 executions to rank 0 (`[2706, 420, 214, 98, 59,
181]`). On real data the raw histogram overstates parity (it includes the price-time baseline) and the adjusted one
can understate it; compare each with the same histogram of the control.

## Results

Every number below comes from the runs above (2026-10-24) or from the tests in `crates/ingest/src/nyse/`.

| Input | Result |
|---|---|
| `EQY_US_NYSE_IBF_3_20260105.csv` (52 lines) | 55 records (QTN 46, QTN B 9), 11 prints; 0 unknown references, 0 duplicate adds, 0 sequence gaps or regressions, 0 out-of-order timestamps; verdict `warnings` with the one reason `nyse_parity_ranks_approximate = 2` (`nyse_parity_executions` 10) |
| its replay | 36 events, every invariant 0; rank oracle 10 executions, `[8, 2, 0, 0, 0, 0]`; verdict `warnings`, reasons `ingest: nyse_parity_ranks_approximate = 2` and `ingest verdict: warnings` |
| its `queuetruth dq` | 11 checks pass, 1 warns (`dq_counters`: the label), 0 fail, 2 skipped; 11 replay trades against 11 source prints, 0 mismatches; 0 crossed or locked event ends |
| `EQY_US_ARCA_IBF_1_20260105.csv` (50 lines) | verdict `clean`, no label; `--strict` passes |
| `EQY_US_NYSE_IBF_4_20260105.csv` (28 lines) | 22 records (QTF 17, QTG 5); 1 refresh run (`R` at sequence 24), 1 book cleared without a refresh (`QTG`, `R` at sequence 29), 1 trade cancel and 1 cross correction (INFO), 1 symbol-sequence gap of 2 messages, 1 unknown message type, 3 feed gaps; its replay: 0 crossed books |
| An NYSE day without executions (test `the_label_follows_the_market_not_the_executions`) | labelled: `nyse_parity_ranks_approximate = 1`, verdict `warnings`, `--strict` fails |
| An `EQY_US_MKT_IBF_*` day of 2017 and the same lines on an NYSE American day of 2026 (test `nyse_mkt_days_before_pillar_are_labelled_parity`) | the NYSE MKT day carries `nyse_parity_ranks_approximate = 1` and the NYSE MKT warning; the 2026 day is `clean` |
| Synthetic NYSE day (8,468 lines) | 3,678 executions; rank oracle 2,665 / 444 / 218 / 107 / 60 / 184 at FIFO rank 0 / 1 / 2 / 3 / 4 / 5+, equal to the generator's truth |
| Synthetic NYSE Arca day (6,798 lines) | 1,982 executions, all at FIFO rank 0, equal to the generator's truth |
| Property tests (`props` in `crates/ingest/src/nyse/tests.rs`: 48 random cases each per test run, 2,000 each in a one-off run) | on every generated day of any of the five markets (1 to 3 symbols, 50 to 600 events per symbol, 0 to 4 Floor brokers), the FIFO rank of every execution recomputed from the reader's records alone equals the generator's truth, with no anomaly; any mix of valid, truncated and garbage lines maps to a self-consistent record stream (every `C` and `F` names a live order and never exceeds it), failovers included (in the 2,000-case run at least one book was cleared in 1,811 cases; per case 3.9 books cleared and 1.7 kept on average) |
| Large synthetic NYSE day (4 symbols, 400,000 events each: 2,349,888 lines, 155.4 MB of CSV, 42.1 MB gzipped; written by the ignored test `write_large_synthetic_day`, not committed) | 4,570,547 records; rank oracle on 1,070,418 executions `[765675, 101464, 51083, 33068, 24364, 94764]`, equal to the generator's truth; every invariant 0, 0 price-priority violations; the gzipped and the raw file give the same `dataset_id` |

Speed, measured on the large day with the release build of the CLI, on the 8-core reference laptop while it was
shared (load average 15 to 23); medians of three runs, CPU = user + system: ingest takes 4.4 s of CPU (2.5 s wall)
for the raw file and 4.4 s (3.0 s wall) for the gzipped one. Two fixed costs are inside those numbers: Python
start-up (importing `queuetruth._core` alone takes 1.1 s of CPU there) and the sha256 of the input recorded in the
manifest (`shasum -a 256` takes 0.7 s of CPU on the file). With a symbol filter that keeps nothing, so that the
ingest only parses, it takes 2.3 s of CPU. The replay with the rank oracle takes 3.3 s of CPU (2.4 s wall) in all;
its engine reports 1.15 s for the 4,570,547 records, 4.0 million records a second.

The large day is not committed; this writes it again (about 155 MB) for your own run:

<!-- check-docs: skip (writes a 155 MB file and needs a Rust toolchain) -->
```sh
QT_NYSE_SYNTH_OUT=$PWD/EQY_US_NYSE_IBF_1_20260106.csv \
  cargo test -p queuetruth-ingest --lib nyse::tests::write_large_synthetic_day -- --ignored --nocapture
queuetruth ingest --format nyse-ibf --venue nyse --in EQY_US_NYSE_IBF_1_20260106.csv --out large
queuetruth replay --data large --out large-replay --rank-oracle
```

## Limits

- **No real NYSE file has been replayed** (the terms above), so the rule set is `synthetic_only`. These assumptions
  wait for a real file: that the symbol sequence numbers of the TAQ files have no expected holes; that SourceTime is
  US/Eastern (the clock check catches the opposite); the layout of Imbalance and Add Order Refresh lines (their
  specification tables skip field 4; both readings are accepted); how the overnight session that specification
  2.4b introduces shows up in the files (a clock that passes midnight is counted and warned about); which message a
  priority-keeping reduction uses (Modify or Replace, `nyse_replace_reduce_same_price` above); and how a failover
  shows up. The
  [XDP Common Client Specification](https://www.nyse.com/publicdocs/nyse/data/XDP_Common_Client_Specification_v2.4.pdf)
  (v2.4, section 9.2) has the backup reset the channel sequence number and send, per symbol, its Symbol Index
  Mapping, the Symbol Clear, its last Security Status and the refresh; the reader handles that order but has not
  seen it in a TAQ file. NYSE Operations can also refresh a single symbol (XDP Integrated Feed Client Specification
  v2.3b, section 13); the reader then clears the books of the other symbols of the channel at their next add,
  unless they first reference one of their resting orders. Compare `nyse_failover_books_cleared` with
  `nyse_failover_books_kept` on your first file with a refresh.
- **Parity is labelled, not replayed.** An allocation-wheel replay needs each order's Participant, which the feed
  does not carry (only attributed orders show a FirmID, counted `nyse_attributed_adds`).
- **Where the label does not reach yet.** In a replay with shadow orders the label is in the run's `data_quality`;
  each shadow row's `dq_flags` reflects only its partition's book counters. The zoo, calibration and features
  outputs do not carry it either, and `queuetruth zoo` calls the replay "exact" on NYSE data too.
- **Auction allocations.** The opening, reopening and closing crosses allocate their volume by auction rules, not by
  the continuous book's parity or time. Their executions of displayed orders arrive as non-printable Order
  Executions (`F` and `C` flagged `PUBLISHER_SPECIFIC`), and replay infers shadow fills from them by FIFO as from
  any execution: a shadow filled at a cross is a guess. Censor or drop shadows still resting at the crosses.
- **Databento's NYSE data is not read under this rule set.** Databento's XNYS.PILLAR MBO files are read by
  `--format dbn`, which does not resolve the `nyse` rule set (`--venue nyse` is refused there), so they ingest as
  `--venue nasdaq`: rule set `nasdaq`, validation `real_data` and no parity label. Their FIFO queue ranks are
  approximations all the same; treat them as on this page.
- **Before the NYSE open** orders of NYSE-listed symbols wait for the opening auction; if the displayed book locks or
  crosses meanwhile, replay counts those events in `crossed_books_after_last`, which can turn the verdict
  `unusable` on a busy pre-opening. Check the `crossed_book` result of `queuetruth dq` on your first real day.
- Non-displayed orders, the hidden part of reserve orders and auction-only interest are not on the feed; their
  executions arrive as hidden and cross prints.
- The XDP binary captures that NYSE also publishes are not read, only the TAQ CSV product; the hosted service does
  not accept `nyse_ibf`.
