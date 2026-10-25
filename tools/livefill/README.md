# livefill: own-account live-fill kit

Tools for putting real own-account orders through `queuetruth score` with exact queue truth. Guide:
[Score my own live fills](../../docs/how-to/own-account-live-fills.md). Design notes:
[docs/design/extensions/livefill-kit.md](../../docs/design/extensions/livefill-kit.md).

```text
export PYTHONPATH="$PWD/tools/livefill"
python -m livefill capture   --market usdtusd --duration 2m --out cap      # public feed -> Parquet, gap detection
python -m livefill check     cap                                         # replay vs the venue's snapshots
python -m livefill stand-in  cap --out stand-in.jsonl                    # rehearse on other traders' orders
python -m livefill harness   --market usdtusd --log dry.jsonl --fast     # DRY RUN unless --live and --confirm
python -m livefill pipeline  cap --log stand-in.jsonl --out run          # match, live log, ingest, score, pool
python -m livefill synth     --out syn                                   # synthetic capture + own orders
```

The harness never places an order without `--live`, `--confirm place-real-orders-on-<market>`, API keys in the
environment and explicit notional caps. `--book-seed` picks the snapshot a segment is replayed from; `--seed` is a
random seed. Tests: `pytest tools/livefill/tests` (the `realdata` test runs only with `QT_REALDATA=1` and the RD-12
Bitstamp capture at `.realdata/bitstamp/20261012T020742Z`, or `LIVEFILL_RD12_CAPTURE` pointing at it).
