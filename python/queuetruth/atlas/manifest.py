"""`atlas/v1/manifest.json` (`qt.atlas_api_manifest`): what the data API holds, which atlas it was built from, and
the sha256 of every other file of the API, so a reader can check what it downloaded."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from queuetruth.schemas.models import QtDoc, QtModel, Sha256, _tag


class Window(QtModel):
    """The trading days the atlas covers (ISO dates)."""

    from_: str = Field(alias="from")
    to: str


class ApiFile(QtModel):
    """One file of the API, relative to the directory of `manifest.json` (`summary.json`, `symbols/A.json`)."""

    path: str
    bytes: int
    sha256: Sha256


class AtlasApiManifest(QtDoc):
    SCHEMA: ClassVar[str] = "qt.atlas_api_manifest"
    schema_: Literal["qt.atlas_api_manifest"] = _tag("qt.atlas_api_manifest")
    schema_version: Literal[1] = 1
    #: the version of the API (the `v1` of the path)
    api_version: Literal[1] = 1
    #: when the site was built (ISO 8601): `--built-at`, else SOURCE_DATE_EPOCH, else the newest day of the window
    built_at: str
    #: Merkle root over the day roots (scripts/scale/atlas.py); every number traces back to it
    atlas_root: str
    #: `iex` or `nasdaq`, and its plain name
    venue: str
    venue_name: str
    window: Window
    days: list[str]
    symbol_days: int
    symbols: int
    mode: str
    shadow_population: str
    headline_horizon: str
    #: the data owner's required attribution line, when it has one (IEX)
    attribution: str | None
    #: how to check a number against the atlas root
    verify: str
    files: list[ApiFile]


DOC_MODELS = {AtlasApiManifest.SCHEMA: AtlasApiManifest}

__all__ = ["DOC_MODELS", "ApiFile", "AtlasApiManifest", "Window"]
