"""Stripe Billing meter events (`POST /v1/billing/meter_events`, form-encoded): `event_name` = meter, `identifier`
= `<job_id>:<meter>` (Stripe dedupes on it), `payload[stripe_customer_id]`, `payload[value]` (integer quantity),
`timestamp` (unix seconds). The identifier is also sent as the `Idempotency-Key`. A 2xx, or an error saying the
identifier already exists, counts as acknowledged; rows without a customer id are not sent."""

from __future__ import annotations

import math
from typing import Any

import httpx

from queuetruth_service.billing.base import PushResult, UsageEvent

DUPLICATE_CODES = ("resource_already_exists", "idempotency_key_in_use")


class StripeBiller:
    """Posts meter events with httpx."""

    name = "stripe"

    def __init__(self, api_key: str, api_base: str = "https://api.stripe.com", transport: Any = None) -> None:
        self._client = httpx.Client(
            base_url=api_base,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
            transport=transport,
        )

    def close(self) -> None:
        """Closes the HTTP client."""
        self._client.close()

    @staticmethod
    def form(e: UsageEvent) -> dict[str, str]:
        """Form body of one meter event."""
        return {
            "event_name": e.meter,
            "identifier": e.identifier,
            "payload[stripe_customer_id]": str(e.customer_id),
            "payload[value]": str(math.ceil(e.quantity)),
            "timestamp": str(int(e.created_at.timestamp())),
        }

    def push(self, events: list[UsageEvent]) -> list[PushResult]:
        """Sends each event once; failures stay unpushed (retried later with the same identifier)."""
        out = []
        for e in events:
            if not e.customer_id:
                out.append(PushResult(e.identifier, False, "tenant has no stripe_customer_id"))
                continue
            try:
                r = self._client.post(
                    "/v1/billing/meter_events", data=self.form(e), headers={"Idempotency-Key": e.identifier}
                )
            except httpx.HTTPError as exc:
                out.append(PushResult(e.identifier, False, f"transport error ({type(exc).__name__})"))
                continue
            if 200 <= r.status_code < 300:
                out.append(PushResult(e.identifier, True))
                continue
            code = ""
            try:
                code = str(r.json().get("error", {}).get("code", ""))
            except ValueError:
                pass
            if code in DUPLICATE_CODES:
                out.append(PushResult(e.identifier, True))
            else:
                out.append(PushResult(e.identifier, False, f"HTTP {r.status_code} {code}".strip()))
        return out
