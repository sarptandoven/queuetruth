"""Rate limits (SPEC 8.7, O-API-6): token buckets in Postgres, 429 with `Retry-After` and draft-11 headers."""

from __future__ import annotations

import pytest
from svc_support import make_svc, seed_dataset

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def limited(db, tmp_path):
    yield from make_svc(db, tmp_path, rate_limits={"read": (120, 60), "write": (30, 10), "submit": (10, 5)})


def test_o_api_6_submit_bucket_429_with_headers(limited):
    ds = seed_dataset(limited)
    codes, last = [], None
    for i in range(7):
        last = limited.client.post(
            "/v1/replays", json={"dataset": {"dataset_id": ds}, "seed": i}, headers=limited.h(Idempotency_Key=f"rl{i}")
        )
        codes.append(last.status_code)
    assert codes[:5] == [202] * 5 and codes[5] == 429, codes
    assert last is not None and last.status_code == 429
    assert last.headers["content-type"] == "application/problem+json"
    assert last.json()["type"].endswith("/rate-limited")
    assert int(last.headers["retry-after"]) >= 1
    assert last.headers["ratelimit-policy"] == '"submit";q=10;w=60'
    assert last.headers["ratelimit"].startswith('"submit";r=0;t=')
    # other buckets are independent
    r = limited.client.get("/v1/jobs", headers=limited.h())
    assert r.status_code == 200 and r.headers["ratelimit-policy"] == '"read";q=120;w=60'
    assert r.headers["ratelimit"].startswith('"read";r=')


@pytest.fixture
def slow(db, tmp_path):
    yield from make_svc(db, tmp_path, rate_limits={"read": (1, 5), "write": (1, 5), "submit": (1, 5)})


def test_o_api_6_read_burst_and_per_key(slow):
    limited = slow
    codes = [limited.client.get("/v1/tenant", headers=limited.h()).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    assert limited.client.get("/v1/tenant", headers=limited.h("B")).status_code == 200  # B's key has its own bucket
    # refill: pretend a minute passed
    limited.db.q("update rate_limit_buckets set updated_at = updated_at - interval '1 minute'")
    assert limited.client.get("/v1/tenant", headers=limited.h()).status_code == 200


def test_unauthenticated_requests_never_touch_buckets(limited):
    for _ in range(5):
        limited.client.get("/v1/tenant")
    assert limited.db.q("select count(*) from rate_limit_buckets") == [(0,)]
