"""Metering and billing (SPEC 8.9, O-BILL-1): unique `identifier = <job_id>:<meter>`, push replays never duplicate,
`NoopBiller` default (forced in zero-egress mode), `StripeBiller` request shape against `httpx.MockTransport`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import psycopg
import pytest
from svc_support import insert_job

from queuetruth_service.billing import NoopBiller, UsageEvent, make_biller
from queuetruth_service.billing.stripe import StripeBiller
from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.maintenance import push_usage, storage_meter
from queuetruth_service.settings import Settings
from queuetruth_service.worker import ClaimedJob, Outcome, Worker

FAKE_KEY = "sk_test_FAKE_NOT_A_REAL_KEY"


def test_o_bill_1_noop_default_and_forced_in_zero_egress():
    assert isinstance(make_biller(Settings()), NoopBiller)
    s = Settings(biller="stripe", stripe_api_key=FAKE_KEY, zero_egress=True)
    assert s.effective_biller == "noop" and isinstance(make_biller(s), NoopBiller)
    assert isinstance(make_biller(Settings(biller="stripe", stripe_api_key=FAKE_KEY)), StripeBiller)
    with pytest.raises(ValueError):
        make_biller(Settings(biller="stripe"))


def test_o_bill_1_stripe_request_shape():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        form = parse_qs(req.content.decode())
        if form["identifier"][0].endswith(":dup"):
            return httpx.Response(400, json={"error": {"code": "resource_already_exists"}})
        if form["identifier"][0].endswith(":bad"):
            return httpx.Response(500, json={"error": {"code": "api_error"}})
        return httpx.Response(200, json={"object": "billing.meter_event"})

    b = StripeBiller(FAKE_KEY, "https://api.stripe.test", transport=httpx.MockTransport(handler))
    ts = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    ev = [
        UsageEvent("job_1:replay_records", "ten_1", "replay_records", 1234.0, "records", ts, "cus_123"),
        UsageEvent("job_1:dup", "ten_1", "cpu_ms", 1.5, "ms", ts, "cus_123"),
        UsageEvent("job_1:bad", "ten_1", "cpu_ms", 1.0, "ms", ts, "cus_123"),
        UsageEvent("job_2:cpu_ms", "ten_2", "cpu_ms", 1.0, "ms", ts, None),
    ]
    res = b.push(ev)
    assert [(r.identifier, r.ok) for r in res] == [
        ("job_1:replay_records", True),
        ("job_1:dup", True),
        ("job_1:bad", False),
        ("job_2:cpu_ms", False),
    ]
    assert len(seen) == 3  # no customer id -> not sent
    req = seen[0]
    assert req.method == "POST" and str(req.url) == "https://api.stripe.test/v1/billing/meter_events"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert req.headers["idempotency-key"] == "job_1:replay_records"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(req.content.decode()) == {
        "event_name": ["replay_records"],
        "identifier": ["job_1:replay_records"],
        "payload[stripe_customer_id]": ["cus_123"],
        "payload[value]": ["1234"],
        "timestamp": [str(int(ts.timestamp()))],
    }
    assert parse_qs(seen[1].content.decode())["payload[value]"] == ["2"]  # ceil to an integer


@pytest.mark.requires_postgres
def test_o_bill_1_identifiers_unique_and_push_never_duplicates(svc):
    job = insert_job(svc, state="queued")
    tenant = svc.tenants["A"]
    svc.db.q("update tenants set stripe_customer_id = 'cus_A' where id = %s", (tenant,))
    sent: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(parse_qs(req.content.decode())["identifier"][0])
        return httpx.Response(200, json={})

    stripe = StripeBiller(FAKE_KEY, "https://api.stripe.test", transport=httpx.MockTransport(handler))

    async def go() -> None:
        pool = await make_async_pool(svc.db.worker)
        try:
            w = Worker(svc.settings, pool, svc.store, None)
            claimed = await w.claim(job)
            assert claimed is not None
            usage = {"replay_records": (100.0, "records"), "cpu_ms": (7.0, "ms")}
            await w.finish(claimed, Outcome("succeeded", summary={}, usage=usage))
            # a second finish of the same attempt is fenced; identifiers stay unique anyway
            with pytest.raises(psycopg.errors.UniqueViolation):
                svc.db.q(
                    "insert into usage_events (id, tenant_id, job_id, meter, quantity, unit, identifier) "
                    "values ('use_x', %s, %s, 'cpu_ms', 1, 'ms', %s)",
                    (tenant, job, f"{job}:cpu_ms"),
                )
            assert sorted(await push_usage(pool, stripe)) == [f"{job}:cpu_ms", f"{job}:replay_records"]
            assert await push_usage(pool, stripe) == []  # replay of the push loop: nothing re-sent
            # a crash after Stripe acknowledged but before pushed_at was written: the retry reuses the identifier
            svc.db.q("update usage_events set pushed_at = null where identifier = %s", (f"{job}:cpu_ms",))
            assert await push_usage(pool, stripe) == [f"{job}:cpu_ms"]
            await storage_meter(pool, svc.store, "2026-10-18")
            await storage_meter(pool, svc.store, "2026-10-18")
        finally:
            await pool.close()

    asyncio.run(go())
    assert sorted(sent) == sorted([f"{job}:cpu_ms", f"{job}:replay_records", f"{job}:cpu_ms"])
    assert len(set(sent)) == 2  # only identifiers, never new ones, for the same usage
    rows = svc.db.q("select identifier from usage_events where meter = 'storage_byte_days' order by identifier")
    assert [r[0] for r in rows] == sorted(f"storage:{t}:2026-10-18:storage_byte_days" for t in svc.tenants.values())
    rep = svc.client.get("/v1/usage?group_by=meter&from=2026-01-01", headers=svc.h()).json()
    by_meter = {r["meter"]: r["quantity"] for r in rep["rows"]}
    assert by_meter["replay_records"] == 100.0 and by_meter["cpu_ms"] == 7.0
    rep = svc.client.get("/v1/usage?group_by=job&from=2026-01-01", headers=svc.h()).json()
    assert {r["key"] for r in rep["rows"]} == {job, "none"}
    assert svc.client.get("/v1/usage?from=notadate", headers=svc.h()).status_code == 422


@pytest.mark.requires_postgres
def test_zero_egress_tenant_uses_noop(svc):
    job = insert_job(svc)
    svc.db.q("update tenants set zero_egress = true, stripe_customer_id = 'cus_A' where id = %s", (svc.tenants["A"],))

    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("zero-egress tenant must not reach Stripe")

    stripe = StripeBiller(FAKE_KEY, "https://api.stripe.test", transport=httpx.MockTransport(handler))

    async def go() -> list[str]:
        pool = await make_async_pool(svc.db.worker)
        try:
            w = Worker(svc.settings, pool, svc.store, None)
            c = await w.claim(job)
            assert isinstance(c, ClaimedJob)
            await w.finish(c, Outcome("succeeded", summary={}, usage={"cpu_ms": (1.0, "ms")}))
            return await push_usage(pool, stripe)
        finally:
            await pool.close()

    assert asyncio.run(go()) == [f"{job}:cpu_ms"]
    assert svc.client.get("/v1/meta", headers=svc.h()).json()["zero_egress"] is True
