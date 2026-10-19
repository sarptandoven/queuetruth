# Score my QuickFIX-driven strategy

**Goal:** take the files your QuickFIX engine already writes, turn them into a canonical live log, measure your
latency from them, and score your simulator against those live orders, order by order.

**You need:** your session's `FileLogPath` directory (message logs) and, if you have it, its `FileStorePath`
directory; an instrument map for the symbols you traded; and market data for the same venue and period:
order-level data (Nasdaq ITCH, Databento DBN MBO) for the exact arm, or L2 data for the zoo models and markouts.

queuetruth reads the logs that QuickFIX C++ (including the `quickfix` Python package), QuickFIX/J, QuickFIX/n and
QuickFIX/Go write, for FIX 4.2, 4.4 and 5.0SP2, through the `fix44` importer (`queuetruth.live.import_quickfix` in
Python). Like every importer in v0.1 it is tested on fixtures written from the engines' sources and the FIX
specification with invented values (O-LIVE-1), not on a real trading log; compare the import summary with your own
records the first time.

## 1. What to point it at

- **The directories.** Pass `FileLogPath` and `FileStorePath` (or individual files). The importer recognises the
  engines' file names: `<session>.messages.current.log` and `.messages.backup.<n>.log` (C++, n, Go),
  `<session>.messages.log` (QuickFIX/J), rotated and gzip-compressed copies, FileStore `.body` and `.header` files and
  the sequence-number files. Event logs are summarised; FileStore bodies fill outbound messages missing from the log.
- **Timestamps.** Each engine stamps every log line in UTC, and that stamp is your send or receive time. QuickFIX/J
  writes no stamp on message lines by default: set `FileIncludeTimeStampForMessages=Y` and
  `FileIncludeMilliseconds=Y` in the session settings, or inbound messages get no receive time. The exchange time is
  TransactTime (60) of the execution report.
- **The session's venue.** The file name `FIX.4.4-<SENDER>-<TARGET>` tells the importer which side is yours.
  `--fix-venue-name` names the exchange (for example `cme`), which sets the canonical `venue`, the `order_key` prefix
  and the instrument-map rows used; with several sessions, `--fix-session-venue SESSION=VENUE` maps each one.
- **Message checks.** Every message is framed by BodyLength (9) and checked against CheckSum (10); failing,
  truncated or malformed messages are excluded and counted (`--fix-checksum keep` imports them anyway, for logs
  re-encoded after capture). MsgSeqNum gaps, PossDupFlag resends, SequenceReset and session resets are tracked per
  direction; a resend of a message already seen is dropped and counted.
- **Custom tags.** `--fix-data-dictionary FIX44-venue.xml` (repeatable) reads your QuickFIX data dictionary, so
  custom tags and enum values map by name. `--fix-instrument-tag 107` takes the instrument from another tag than
  Symbol (55).

What each FIX message becomes, and the remaining rules (order identity, fills hidden by gaps, unsolicited cancels,
drop-copy deduplication), are listed in [Import fills from my venue or broker](import-live-logs.md#fix44) and in the
module documentation of `queuetruth.live.importers.fix44`.

## 2. Try it on a small synthetic log

The blocks below run as written in one empty directory. The first one writes a QuickFIX C++ style message log with
correct BodyLength and CheckSum fields: a logon, one resting buy order for 100 that is acknowledged, partly filled
(40), sent again as a possible duplicate, and cancelled. The firm, symbol, ids and prices are invented.

<!-- check-docs: run -->
```python
import os

SOH = "\x01"


def fix(fields):
    """One FIX 4.4 message with a correct BodyLength (9) and CheckSum (10)."""
    body = "".join(f"{t}={v}{SOH}" for t, v in fields)
    head = f"8=FIX.4.4{SOH}9={len(body.encode())}{SOH}"
    return head + body + f"10={sum((head + body).encode()) % 256:03d}{SOH}"


def out(seq, t, typ, *f):  # this firm -> broker
    return [(35, typ), (49, "MYFIRM"), (56, "BROKER"), (34, seq), (52, t), *f]


def inc(seq, t, typ, *f):  # broker -> this firm
    return [(35, typ), (49, "BROKER"), (56, "MYFIRM"), (34, seq), (52, t), *f]


order = [(1, "ACCT1"), (55, "ACME"), (54, 1), (38, 100), (40, 2), (44, "101.25"), (59, 0)]
fill = [(37, "7001"), (11, "ORD-1"), (17, "E-2"), (150, "F"), (39, 1), *order, (32, 40), (31, "101.25"),
        (151, 60), (14, 40), (6, "101.25"), (851, 1), (60, "20261015-14:30:05.001500")]
lines = [
    ("20261015-14:30:00.000000000", out(1, "20261015-14:30:00.000", "A", (98, 0), (108, 30))),
    ("20261015-14:30:00.001000000", inc(1, "20261015-14:30:00.001", "A", (98, 0), (108, 30))),
    ("20261015-14:30:01.000000000", out(2, "20261015-14:30:01.000", "D", (11, "ORD-1"), *order,
                                        (60, "20261015-14:30:01.000"))),
    ("20261015-14:30:01.002000000", inc(2, "20261015-14:30:01.001", "8", (37, "7001"), (11, "ORD-1"),
                                        (17, "E-1"), (150, 0), (39, 0), *order, (151, 100), (14, 0), (6, 0),
                                        (60, "20261015-14:30:01.000800"))),
    ("20261015-14:30:05.003000000", inc(3, "20261015-14:30:05.002", "8", *fill)),
    ("20261015-14:30:05.010000000", inc(3, "20261015-14:30:05.009", "8", (43, "Y"),
                                        (122, "20261015-14:30:05.002"), *fill)),
    ("20261015-14:30:20.000000000", out(3, "20261015-14:30:20.000", "F", (11, "ORD-1-C"), (41, "ORD-1"),
                                        (37, "7001"), (55, "ACME"), (54, 1), (38, 100),
                                        (60, "20261015-14:30:20.000"))),
    ("20261015-14:30:20.002000000", inc(4, "20261015-14:30:20.001", "8", (37, "7001"), (11, "ORD-1-C"),
                                        (41, "ORD-1"), (17, "E-3"), (150, 4), (39, 4), *order, (151, 0),
                                        (14, 40), (6, "101.25"), (60, "20261015-14:30:20.000900"))),
]
os.makedirs("quickfix-logs", exist_ok=True)
with open("quickfix-logs/FIX.4.4-MYFIRM-BROKER.messages.current.log", "w") as f:
    for ts, fields in lines:
        f.write(f"{ts} : {fix(fields)}\n")
with open("instrument_map.csv", "w") as f:
    f.write("venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\n")
    f.write("cme,ACME,201,,-4,0,0.01,1\n")
```

The instrument map gives each symbol its `instrument_id` and fixed-point scales; its `venue` column is the name you
pass as `--fix-venue-name`. When you score against a dataset, use the same `instrument_id` as the dataset's
`instruments.parquet`, or the orders will not find their market data. An instrument missing from the map is named in
the summary's `unmapped_instruments` and its orders cannot be scheduled. Decimal prices convert exactly or fail with
`QT-INPUT-004`.

<!-- check-docs: run -->
```sh
queuetruth live import --venue fix44 --fix-venue-name cme --account desk1 \
    --instrument-map instrument_map.csv --in quickfix-logs --out live
queuetruth live validate --live live
```

The warnings report the session (application messages, one possible-duplicate resend dropped), the log format it
recognised and the session messages that have no canonical event. The same import from Python:

<!-- check-docs: run -->
```python
import pyarrow.parquet as pq

from queuetruth.live import import_quickfix

summary = import_quickfix(["quickfix-logs"], "live-py", venue_name="cme", account="desk1",
                          instrument_map="instrument_map.csv")
print("orders", summary.n_orders, "events", summary.n_events, "fills", summary.n_fills)
cols = ["event_type", "qty", "ts_local_send", "ts_exchange", "ts_exchange_kind", "ts_local_recv", "liquidity"]
for row in pq.read_table("live-py/events.parquet").select(cols).to_pylist():
    print(row)
```

Every event carries your send or receive time from the log line next to the exchange's TransactTime.

**Latency and the schedule.** The live log feeds the latency fit, hftbacktest's latency file and the fixed schedule
the scorer replays:

<!-- check-docs: run -->
```sh
python -c "from queuetruth.live import latency_stats; s = latency_stats('live'); print(s['entry_ns'], s['ack_rtt_ns'])"
queuetruth calibrate latency --live live --out cal-latency
queuetruth export latency-npz --live live --out order_latency.npz
queuetruth shadows from-live --live live --out schedule.parquet
```

`entry_ns` is the ack's TransactTime minus your send time and `response_ns` your receive time minus the ack's
TransactTime; both assume your clock and the venue's are synchronized. `ack_rtt_ns` (ack received minus order sent)
and `cancel_rtt_ns` use only your own clock, so they hold even when the clocks are not synchronized
([Calibration](../concepts/calibration.md#latency)). With one order these are single values; on a real log you get
p10, p50, p90 and p99.

## 3. Score it against your market data

These steps need your data, so the docs check only checks their syntax.

### Import the whole session

<!-- check-docs: skip (needs your QuickFIX files) -->
```python
from queuetruth.live import import_quickfix

summary = import_quickfix(
    ["/var/log/quickfix", "/var/lib/quickfix/store"],  # FileLogPath and FileStorePath
    "live",
    venue_name="cme",
    account="desk1",
    instrument_map="instrument_map.csv",
    data_dictionary="FIX44-venue.xml",  # optional: your QuickFIX data dictionary
)
for w in summary.warnings:  # gaps, resends, garbled messages, sessions, unmapped instruments
    print(w)
```

The same from the command line:

<!-- check-docs: skip (needs your QuickFIX files) -->
```sh
queuetruth live import --venue fix44 --fix-venue-name cme --account desk1 --instrument-map instrument_map.csv \
    --fix-data-dictionary FIX44-venue.xml --in /var/log/quickfix --in /var/lib/quickfix/store --out live
queuetruth live validate --live live
```

Read the warnings before scoring. An inbound sequence gap that was never resent is reported; a CumQty jump across it
becomes a `derived` fill (no fill id, liquidity unknown) so cumulative quantities stay true. If you import an
order-entry session and a drop-copy session of the same venue together, duplicate fills are dropped by ExecID, or by
the reported state when the drop copy uses its own ExecIDs. `live validate` exits 2 on errors and lists warnings.
Market, IOC and FOK orders are imported but not scheduled.

### Ingest the market data

<!-- check-docs: skip (needs market data) -->
```sh
# order-level, for example a CME product from Databento DBN MBO
queuetruth ingest --format dbn --in glbx-mdp3-20261015.mbo.dbn.zst --venue cme --out ds
# or L2 only, for example a crypto venue through Tardis
queuetruth ingest --format tardis-l2 --venue generic --in book_l2.csv.gz --in trades.csv.gz --out ds
```

Then make the instrument map's `instrument_id` match `ds/instruments.parquet`, and record the data vintage (see
[Data and licensing](../data-and-licensing.md#record-the-data-vintage)).

### Score

<!-- check-docs: skip (needs your live log and market data) -->
```sh
queuetruth score --live live --data ds --sim exact --out sc-exact                    # order-level data only
queuetruth score --live live --data ds --sim-model "hbt_power3[n=2]" --out sc-power3  # any zoo model
queuetruth score --live live --data ds --sim-log my-backtest-log --out sc-mine        # your simulator's output
queuetruth report --in sc-exact --in sc-power3 --in sc-mine --out scorecards.html
```

- `--sim exact` replays your orders through the exact FIFO engine. On order-level data your own orders are in the
  tape, so the scorer first removes them, matching each order by its OrderID (37) when that equals the feed's order
  id (true for CME, where the MBO order id is the exchange order id) and otherwise by side, price, size and time
  within 1 ms of entry ([own-order exclusion](../concepts/scorecard.md#the-exact-arm-and-your-own-orders)). Check
  the match rate in the warnings.
- `--entry exchange-ack` (the default) starts each scheduled order at its ack's TransactTime, which isolates queue
  and fill logic from your entry latency; `--entry send-plus-latency` starts it at your send time plus the measured
  latency.
- `--sim-log` takes your backtester's output as a canonical log with the same `order_key`s.
  `queuetruth export replay-schedule --target hftbacktest|nautilus --live live --data ds --out FILE` writes a
  program that replays this schedule in your simulator, and `live import --venue sim-hftbacktest|sim-nautilus`
  reads its output back ([Score my Nautilus backtest](score-nautilus-vs-binance.md)).

The terminal shows the result first (direction and size of the bias with its interval and the cluster-robust p),
then the order counts and the 2x2 table; [Scorecard statistics](../concepts/scorecard.md) explains each number.

### Put it in CI

<!-- check-docs: skip (needs your live log and market data) -->
```sh
queuetruth score --live live --data ds --sim-log my-backtest-log --out sc \
    --fail-abs-bias 0.02 --fail-not-equivalent
```

Exit code 1 means the simulator's fill rate is more than 2 percentage points from live, or not shown to be within the
equivalence margin; 2 means the check could not run.

## Limits

- The FIX log knows your orders, not the queue: queue position at entry comes from the market data, exact only on
  order-level data.
- FIX does not say whether a replace kept priority. The schedule applies the venue's rule: on `binance`, `kraken`,
  `deribit` and `cme` a size decrease keeps priority, and on `coinbase` the importer marks a same-price size decrease
  as priority-keeping; on other venues, including the default name `fix44`, a replace starts a new leg.
- Your live orders changed the book and other traders may have reacted to them; the scorer's shadows do not. The
  report counts live orders that overlapped another of yours at the same price and side.
- Your FIX log is confidential: the `raw` column of the canonical log keeps every original message. Drop it before
  sharing ([Import fills from my venue or broker](import-live-logs.md#privacy)).
