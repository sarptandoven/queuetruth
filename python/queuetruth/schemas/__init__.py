"""Shared contracts: JSON document models, spec models, Arrow table schemas and run identity (SPEC 3, 4.2, 7.3)."""

import queuetruth.forecast.schemas  # noqa: F401  (adds the qt.forecast_* documents to DOC_MODELS)
from queuetruth.schemas import arrow
from queuetruth.schemas.arrow import ADDED_COLUMNS, DICT_VALUES, FULL_SCHEMAS, SCHEMAS, features_schema
from queuetruth.schemas.models import *  # noqa: F403
from queuetruth.schemas.models import DOC_MODELS, parse_document
from queuetruth.schemas.runid import (
    V1_1_DEFAULTS,
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
from queuetruth.atlas._registry import doc_models as _atlas_doc_models
from queuetruth.ledger.models import DOCUMENTS as _LEDGER_DOC_MODELS
from queuetruth.sandbox.models import SANDBOX_DOC_MODELS, SANDBOX_SPEC_MODELS

DOC_MODELS.update(_atlas_doc_models())  # the Fill-Realism Atlas data API (queuetruth.atlas, one module per file)
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
    "drop_v1_1_defaults",
    "features_schema",
    "identity_document",
    "is_ref",
    "parse_document",
    "run_id",
    "spec_echo",
]


def __getattr__(name: str) -> object:
    if name == "canonical_json":  # moved to queuetruth.integrity; the old name keeps working with a warning
        import warnings

        from queuetruth.integrity import canonical_json

        warnings.warn(
            "queuetruth.schemas.canonical_json moved to queuetruth.integrity.canonical_json; the old name will be "
            "removed in a future release",
            DeprecationWarning,
            stacklevel=2,
        )
        return canonical_json
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
