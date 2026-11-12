"""Drift test: the formats the service accepts are the source format registry (`_core.formats()`)."""

from __future__ import annotations

import json
from typing import get_args

import pytest

from queuetruth.schemas import specs as sp
from queuetruth_service.api_models import INGEST_FORMATS


@pytest.mark.requires_core
def test_service_formats_match_the_registry() -> None:
    from queuetruth import _core

    registry = [f["name"] for f in json.loads(_core.formats())]
    assert sorted(INGEST_FORMATS) == sorted(registry)
    assert list(INGEST_FORMATS) == list(get_args(sp.IngestSpec.model_fields["format"].annotation))
