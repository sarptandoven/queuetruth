"""Default biller: acknowledges every event without sending anything."""

from __future__ import annotations

from queuetruth_service.billing.base import PushResult, UsageEvent


class NoopBiller:
    """Marks events pushed; no network."""

    name = "noop"

    def push(self, events: list[UsageEvent]) -> list[PushResult]:
        """Acknowledges all events."""
        return [PushResult(e.identifier, True) for e in events]
