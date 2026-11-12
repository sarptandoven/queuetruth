"""`atlas/v1/configs.json` (`qt.atlas_configs`): per library and liquidity tier, the setting closest to the real
queue, whether it is still clearly off, and the config to paste."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from queuetruth.atlas._parts import Interval, Library, Verdict
from queuetruth.schemas.models import QtDoc, QtModel, _tag


class Config(QtModel):
    """The closest setting of one library in one tier, at the document's horizon, mode and population."""

    library: Library
    library_name: str
    tier: str
    tier_name: str
    label: str
    model_id: str
    #: how many of the library's settings the atlas has in this tier
    n_settings: int
    fill_rate_bias_pp: Interval
    verdict: Verdict
    verdict_text: str
    #: settings whose 95% intervals overlap this one's: the atlas cannot rank them by closeness
    overlapping: list[str]
    overlapping_labels: list[str]
    pnl_bias_ticks: Interval | None
    #: the text to paste (null when no released library setting reproduces the model)
    config: str | None
    #: where it goes, e.g. "NautilusTrader BacktestEngine.add_venue keywords"
    config_kind: str | None


class AtlasConfigs(QtDoc):
    """`configs.json`: one closest setting per (tier, library), `ALL` first."""

    SCHEMA: ClassVar[str] = "qt.atlas_configs"
    schema_: Literal["qt.atlas_configs"] = _tag("qt.atlas_configs")
    schema_version: Literal[1] = 1
    venue: str
    atlas_root: str
    mode: str
    shadow_population: str
    horizon: str
    #: notes that apply to every config of a library (versions, imports), by library key
    notes: dict[str, str] = Field(default_factory=dict)
    configs: list[Config] = Field(default_factory=list)


DOC_MODELS = {AtlasConfigs.SCHEMA: AtlasConfigs}

__all__ = ["DOC_MODELS", "AtlasConfigs", "Config"]
