"""Venue importers (SPEC 5.7.1) and simulator-output importers; every importer maps
`(input paths, ImportOptions) -> ImportResult` (see `queuetruth.live.builder` for the shared semantics)."""

from __future__ import annotations

from queuetruth.live.builder import ImportResult
from queuetruth.live.importers._base import Importer, ImportOptions
from queuetruth.live.importers.binance import import_binance
from queuetruth.live.importers.bybit import import_bybit
from queuetruth.live.importers.coinbase import import_coinbase
from queuetruth.live.importers.deribit import import_deribit
from queuetruth.live.importers.fix44 import import_fix44
from queuetruth.live.importers.hyperliquid import import_hyperliquid
from queuetruth.live.importers.ibkr_flex import import_ibkr_flex
from queuetruth.live.importers.kraken import import_kraken
from queuetruth.live.importers.okx import import_okx
from queuetruth.live.importers.sim import import_hftbacktest, import_nautilus

#: The nine venue importers of SPEC 5.7.1 (`LiveImportSpec.venue` values).
VENUE_IMPORTERS: dict[str, Importer] = {
    "binance": import_binance,
    "bybit": import_bybit,
    "okx": import_okx,
    "coinbase": import_coinbase,
    "kraken": import_kraken,
    "deribit": import_deribit,
    "hyperliquid": import_hyperliquid,
    "ibkr_flex": import_ibkr_flex,
    "fix44": import_fix44,
}
#: Simulator-output importers (not `LiveImportSpec` venues; see `queuetruth.live.import_sim`).
SIM_IMPORTERS: dict[str, Importer] = {"hftbacktest": import_hftbacktest, "nautilus": import_nautilus}

__all__ = ["SIM_IMPORTERS", "VENUE_IMPORTERS", "ImportOptions", "ImportResult", "Importer"]
