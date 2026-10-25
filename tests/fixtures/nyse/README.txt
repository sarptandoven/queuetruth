nyse: NYSE Group TAQ Integrated Feed files for the nyse_ibf reader (qt_ingest::nyse). No NYSE data: every symbol,
order, price and size is invented. The line layouts follow the TAQ Products Client Specification (Pillar Integrated,
BBO, Trades and Imbalance Feeds) v2.4b (NYSE Group, 2026-09-08); the files use the product's names,
EQY_US_<EXCH>_IBF_<channel>_<YYYYMMDD>, so the reader takes the market, channel and date from them. They are plain
CSV (the real files are gzipped; the tests gzip a copy to read that path too).

Hand-written (every message chosen by hand; the channel SequenceNumber skips one number at each new second, as the
omitted Source Time Reference message would, and SymbolSeqNum counts each symbol's messages):

EQY_US_NYSE_IBF_3_20260105.csv  NYSE (Market ID 1, parity allocation), channel 3, 2026-01-05 (EST), symbols QTN and
  QTN B. Pre-opening orders and imbalances; the core session at 09:30:00 (Security Status O); the opening crosses
  (Cross Trade 111 with a non-printable Order Execution of displayed bid 1005 and a non-printable Non-Displayed
  Trade); at 25.02 an odd lot from the book (1010), then 500 shares from a Floor broker (1011) that set the round-lot
  quote and so have Setter Priority, then 300 from the book (1012) and 200 from the DMM (1013) (the feed does not
  carry these Participants; they are the story behind the allocation). A sell of 800 at 09:31:30 is allocated as
  NYSE Rule 7.37(b) does: 1011 in full (500, though 1010 is ahead of it), then round lots by Participant from the
  pointer: 1010 50 + 1012 50 (the Book Participant), 1013 100 (the DMM), 1012 100 (the Book again). A FIFO book
  would have filled 1010, 1011 and 250 of 1012. Then a size decrease that keeps priority (1012), a size increase
  that loses it (1013), a hidden print at 25.035, a cancel/replace (1004 -> 1014), an execution, Retail Price
  Improvement messages, the closing imbalance and cross, the end-of-session deletes, Security Status X. A FirmID on
  1003 (an attributed order).
EQY_US_ARCA_IBF_1_20260105.csv  The same day on NYSE Arca (Market ID 3, price-time): the sell of 800 fills 1010,
  1011 and 250 of 1012 by time, and the later lines follow from that.
EQY_US_NYSE_IBF_4_20260105.csv  NYSE, channel 4, symbols QTF and QTG: a publisher failover at 10:00:05 (two Add
  Order Refresh messages re-send QTF's book; order 4004, deleted during the outage, is not in it; QTG's two orders,
  5001 bid 30.00 and 5002 offer 30.10, were both deleted during the outage, so the backup re-sends nothing for QTG
  and its deletes, SymbolSeqNum 5 and 6, are lost), QTG's next message an offer at 29.90 at 10:02 (it would cross
  the stale bid: the reader clears QTG's book first), a Trade Cancel of the 09:41 execution, two lost QTF messages
  (SymbolSeqNum 11 -> 14), a message type no TAQ product lists (199), the closing cross and a Cross Correction of
  its volume.

Synthetic (qt_ingest::nyse::synth::generate, seed 20260106, symbols QTS and QTT A, 3,000 events per symbol, 3
Floor broker Participants; regenerate with
QT_BLESS=1 cargo test -p queuetruth-ingest --lib nyse::tests::synthetic_fixtures_are_current):

EQY_US_NYSE_IBF_7_20260106.csv  NYSE: executions allocated by the generator's model of Rule 7.37(b) (Setter
  Priority, then round-lot parity over the Book Participant, the DMM and the Floor brokers). Ground truth:
  8,468 lines, 3,678 executions, of which 2,665 at the head of their FIFO queue and 1,013 behind it.
EQY_US_ARCA_IBF_7_20260106.csv  NYSE Arca: the same generator filling by time. 6,798 lines, 1,982 executions, all
  at the head of their FIFO queue.

MANIFEST.sha256 lists every file (shasum -a 256 -c MANIFEST.sha256).
