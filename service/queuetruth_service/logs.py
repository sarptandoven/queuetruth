"""Structured JSON logs (SPEC 8.10): one object per line with `request_id`, `tenant_id`, `job_id` from context
variables; API keys, signed-URL credentials and DSN passwords are redacted (`keys.redact`); no request bodies or data
rows are ever logged. `setup` also routes uvicorn's own loggers (including `uvicorn.access`, whose lines carry the
query string of `/v1/_store` URLs) through this formatter instead of uvicorn's unredacted handlers (L5)."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

from queuetruth_service.keys import redact

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("qt_request_id", default=None)
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("qt_tenant_id", default=None)
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("qt_job_id", default=None)


class JsonFormatter(logging.Formatter):
    """Formats a record as one JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "request_id": request_id_var.get(),
            "tenant_id": tenant_id_var.get(),
            "job_id": job_id_var.get(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            doc["exc_type"] = record.exc_info[0].__name__
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            doc.update({k: redact(str(v)) if isinstance(v, str) else v for k, v in extra.items()})
        return json.dumps(doc, separators=(",", ":"), default=str)


def setup(level: str = "INFO") -> None:
    """Installs the JSON formatter on the root logger (stderr)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True


UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
