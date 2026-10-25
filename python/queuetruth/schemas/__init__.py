"""Shared contracts: JSON document models, spec models, Arrow table schemas and run identity (SPEC 3, 4.2, 7.3)."""

import queuetruth.forecast.schemas  # noqa: F401  (adds the qt.forecast_* documents to DOC_MODELS)
from queuetruth.schemas import arrow
from queuetruth.schemas.arrow import ADDED_COLUMNS, DICT_VALUES, FULL_SCHEMAS, SCHEMAS, features_schema
from queuetruth.schemas.models import *  # noqa: F403
from queuetruth.schemas.models import DOC_MODELS, parse_document
from queuetruth.schemas.runid import (
    V1_1_DEFAULTS,
    canonical_json,
    drop_v1_1_defaults,
    identity_document,
    is_ref,
    run_id,
    spec_echo,
)
from queuetruth.schemas.specs import *  # noqa: F403
from queuetruth.schemas.specs import SPEC_MODELS

# isort: split
# Documents and specs of extension packages that import `queuetruth.schemas` themselves: registered here, after every
# module of this package is complete, so that `DOC_MODELS` / `SPEC_MODELS` hold them wherever they are imported from.
from queuetruth.ledger.models import DOCUMENTS as _LEDGER_DOC_MODELS
from queuetruth.sandbox.models import SANDBOX_DOC_MODELS, SANDBOX_SPEC_MODELS

DOC_MODELS.update(_LEDGER_DOC_MODELS)
DOC_MODELS.update(SANDBOX_DOC_MODELS)
SPEC_MODELS.update(SANDBOX_SPEC_MODELS)

__all__ = [
    "ADDED_COLUMNS",
    "DICT_VALUES",
    "DOC_MODELS",
    "FULL_SCHEMAS",
    "SCHEMAS",
    "SPEC_MODELS",
    "V1_1_DEFAULTS",
    "arrow",
    "canonical_json",
    "drop_v1_1_defaults",
    "features_schema",
    "identity_document",
    "is_ref",
    "parse_document",
    "run_id",
    "spec_echo",
]
