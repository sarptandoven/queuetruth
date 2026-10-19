"""Per-process service context shared by routers: settings, the API pool, the object store, the enqueue hook and the
failed-authentication throttle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Request
from psycopg_pool import ConnectionPool

from queuetruth_service.ratelimit import FailureThrottle
from queuetruth_service.settings import Settings
from queuetruth_service.store import ObjectStore


@dataclass
class ServiceContext:
    """Everything a request handler needs."""

    settings: Settings
    pool: ConnectionPool | None
    store: ObjectStore
    enqueue: Callable[[str], None] | None = None
    auth_throttle: FailureThrottle | None = None
    started: bool = field(default=False)

    def db(self) -> ConnectionPool:
        """The pool (503 when the service has no database)."""
        if self.pool is None:
            from queuetruth_service.problems import ProblemError

            raise ProblemError(503, "unavailable", "Service unavailable", "QT-ENGINE-001", "database not configured")
        return self.pool


def ctx(request: Request) -> ServiceContext:
    """The context of the app serving `request`."""
    return request.app.state.ctx
