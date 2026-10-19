"""Metering and billing (SPEC 8.9): usage events are inserted with the job's terminal transition
(`identifier = "<job_id>:<meter>"`, unique); a periodic pusher sends unpushed rows through `Biller.push`.
`NoopBiller` is the default and is forced in zero-egress mode; `StripeBiller` posts Stripe Billing meter events.
No real Stripe key is used anywhere in this repository."""

from __future__ import annotations

from queuetruth_service.billing.base import Biller, PushResult, UsageEvent
from queuetruth_service.billing.noop import NoopBiller
from queuetruth_service.settings import Settings


def make_biller(settings: Settings, transport: object | None = None) -> Biller:
    """The configured biller (`noop` whenever zero-egress is on)."""
    if settings.effective_biller == "stripe":
        from queuetruth_service.billing.stripe import StripeBiller

        if settings.stripe_api_key is None:
            raise ValueError("BILLER=stripe requires QT_STRIPE_API_KEY")
        return StripeBiller(settings.stripe_api_key.get_secret_value(), settings.stripe_api_base, transport=transport)
    return NoopBiller()


__all__ = ["Biller", "NoopBiller", "PushResult", "UsageEvent", "make_biller"]
