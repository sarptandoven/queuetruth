"""`run_id` of a spec, computed by the Rust core."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from queuetruth.ops._call import _call, _dump


def run_id(spec: BaseModel | dict[str, Any]) -> str:
    """`run_id` of a spec (SPEC 7.3) computed by the Rust core; pass a Spec model (fully defaulted) or its dump."""
    doc = _dump(spec) if isinstance(spec, BaseModel) else spec
    return str(_call("run_id", json.dumps(doc)))
