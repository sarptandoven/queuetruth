"""Biller interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class UsageEvent:
    """One unpushed `usage_events` row with the tenant's billing customer."""

    identifier: str
    tenant_id: str
    meter: str
    quantity: float
    unit: str
    created_at: datetime
    customer_id: str | None


@dataclass(frozen=True)
class PushResult:
    """Outcome per identifier: `ok` = acknowledged (never sent again)."""

    identifier: str
    ok: bool
    error: str | None = None


class Biller(Protocol):
    """Sends usage events to a billing system."""

    name: str

    def push(self, events: list[UsageEvent]) -> list[PushResult]: ...
