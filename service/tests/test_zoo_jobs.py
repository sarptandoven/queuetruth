"""Zoo runs over HTTP (`/v1/zoos`, migration 0008) and the list bounds of zoo and calibration specs: submit, estimate,
list and get; validation (references, `out`, compute knobs, model ids, horizons); tenancy (404 across tenants, RLS on
`zoos`); the runner target and usage meters; retention clears zoo summaries; per-kind timeouts accept `zoo`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import psycopg
import pytest
from svc_support import insert_job, seed_dataset, succeed_job, upload_bytes, verify_uploads

from queuetruth.schemas import models as m
from queuetruth_service import keys, runners
from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.estimate import QUEUE_OVERHEAD_S, REPLAY_RPS, ZOO_PAIR_S
from queuetruth_service.ids import ID_RE
from queuetruth_service.maintenance import run_retention
from queuetruth_service.routers.runs import (
    BOOTSTRAP_DRAWS_PER_PAIR,
    MAX_HORIZONS,
    MAX_ZOO_MODELS,
    PARTICLE_FAMILIES,
    PARTICLES_PER_PAIR,
    ZOO_ALL_MODELS,
    engine_models,
    zoo_models_est,
)
from queuetruth_service.settings import Settings

PG = pytest.mark.requires_postgres
REPO = Path(__file__).resolve().parents[2]


def shadows_upload(svc, who: str = "A", data: bytes = b"PAR1" + b"s" * 6400) -> str:
    """A verified upload standing for a shadow spec (6404 bytes: 100 shadows by the estimate heuristic)."""
    up = upload_bytes(svc, data, who, filename="shadows.parquet", purpose="shadow_spec")
    assert verify_uploads(svc)[up] == "verified"
    return up


def zoo_spec(ds: str, up: str, **kw) -> dict:
    return {"dataset": {"dataset_id": ds}, "shadows": {"upload_id": up}, **kw}


# ---- submit, list, get --------------------------------------------------------------------------------------------


@PG
def test_submit_zoo_creates_a_zoo_job_and_record(svc):
    ds, up = seed_dataset(svc), shadows_upload(svc)
    spec = zoo_spec(ds, up, models=["hbt_power3[n=3]", "hbt_risk_adverse"], bootstrap_b=50)
    r = svc.client.post("/v1/zoos", json=spec, headers=svc.h(Idempotency_Key="z1"))
    assert r.status_code == 202, r.text
    body = r.json()
    zid, job = body["id"], body["job"]
    assert zid.startswith("zoo_") and ID_RE.match(zid)
    assert job["kind"] == "zoo" and job["state"] == "queued" and job["resource"] == {"type": "zoo", "id": zid}
    assert r.headers["location"] == f"/v1/jobs/{job['id']}"
    m.Job.model_validate(job)
    assert svc.enqueued == [job["id"]]
    # idempotent replay: same response, no second job
    again = svc.client.post("/v1/zoos", json=spec, headers=svc.h(Idempotency_Key="z1"))
    assert again.status_code == 202 and again.json() == body and again.headers["idempotent-replayed"] == "true"
    assert svc.db.q("select count(*) from jobs where kind = 'zoo'") == [(1,)]
    rec = svc.client.get(f"/v1/zoos/{zid}", headers=svc.h()).json()
    assert rec["job_id"] == job["id"] and rec["summary"] is None
    # stored with SPEC 4.2 defaults; `out` stays null (the worker assigns it)
    assert rec["spec"]["models"] == spec["models"] and rec["spec"]["mode"] == "faithful" and rec["spec"]["out"] is None
    assert rec["spec"]["particles"] == 100 and rec["spec"]["write_fills"] is True
    page = svc.client.get("/v1/zoos", headers=svc.h()).json()
    assert page["schema"] == "qt.page" and [x["id"] for x in page["data"]] == [zid] and not page["has_more"]
    jobs = svc.client.get("/v1/jobs?kind=zoo", headers=svc.h()).json()["data"]
    assert [j["id"] for j in jobs] == [job["id"]]
    assert svc.client.get("/v1/jobs?kind=replay", headers=svc.h()).json()["data"] == []
    stored = svc.db.q("select kind, resource_type, resource_id, spec->'models' from jobs where id = %s", (job["id"],))
    assert stored == [("zoo", "zoo", zid, spec["models"])]


@PG
def test_zoo_summary_and_pagination(svc):
    ds, up = seed_dataset(svc), shadows_upload(svc)
    ids = []
    for i in range(3):
        r = svc.client.post("/v1/zoos", json=zoo_spec(ds, up, seed=i), headers=svc.h(Idempotency_Key=f"p{i}"))
        assert r.status_code == 202, r.text
        ids.append(r.json()["id"])
    p1 = svc.client.get("/v1/zoos?limit=2", headers=svc.h()).json()
    assert [x["id"] for x in p1["data"]] == ids[::-1][:2] and p1["has_more"] and p1["next_cursor"] == ids[1]
    p2 = svc.client.get(f"/v1/zoos?limit=2&starting_after={p1['next_cursor']}", headers=svc.h()).json()
    assert [x["id"] for x in p2["data"]] == [ids[0]] and not p2["has_more"]
    sample = {
        "schema": "qt.zoo_summary",
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "spec": {},
        "n_shadows": 0,
        "models": [],
        "leaderboard": [],
        "files": [],
        "warnings": [],
    }
    svc.db.q("update zoos set summary = %s where id = %s", (json.dumps(sample), ids[0]))
    rec = svc.client.get(f"/v1/zoos/{ids[0]}", headers=svc.h())
    assert rec.status_code == 200 and rec.json()["summary"]["run_id"] == "0123456789abcdef"
    assert rec.json()["summary"]["hbt_312_aborts"] == {}  # the response model fills additive defaults
    assert svc.client.get("/v1/zoos/zoo_01ARZ3NDEKTSV4RRFFQ69G5FAV", headers=svc.h()).status_code == 404


# ---- validation ---------------------------------------------------------------------------------------------------


@PG
@pytest.mark.parametrize(
    ("patch", "loc"),
    [
        ({"models": []}, ["body", "models"]),
        ({"models": ["all"] * 2}, ["body", "models", 1]),
        ({"models": [f"hbt_power[n={i}]" for i in range(MAX_ZOO_MODELS + 1)]}, ["body", "models"]),
        ({"models": ["hbt power3"]}, ["body", "models", 0]),
        ({"models": ["../../etc/passwd"]}, ["body", "models", 0]),
        ({"models": ["x" * 65]}, ["body", "models", 0]),
        ({"models": ["hbt_log2\n"]}, ["body", "models", 0]),
        ({"models": ["all", "nosuchfamily[n=1]"]}, ["body", "models", 1]),
        ({"models": ["hbt_powr3[n=3]"]}, ["body", "models", 0]),
        ({"horizons_ns": list(range(MAX_HORIZONS + 1))}, ["body", "horizons_ns"]),
        ({"particles": 10_001}, ["body", "particles"]),
        ({"threads": 17}, ["body", "threads"]),
        ({"bootstrap_b": 10_001}, ["body", "bootstrap_b"]),
    ],
)
def test_zoo_limits_are_422(svc, patch, loc):
    if "family" in str(patch) or "powr" in str(patch) or "nosuch" in str(patch):
        pytest.importorskip("queuetruth._core")  # families come from the engine's registry
    ds, up = seed_dataset(svc), shadows_upload(svc)
    for path in ("/v1/zoos", "/v1/zoos/estimate"):
        r = svc.client.post(path, json=zoo_spec(ds, up) | patch, headers=svc.h(Idempotency_Key=f"v{path}"))
        assert r.status_code == 422, (path, r.text)
        body = r.json()
        assert body["code"] == "QT-CONFIG-001" and body["type"].endswith("/validation-error")
        assert loc in [e["loc"] for e in body["errors"]], body["errors"]
    assert svc.db.q("select count(*) from jobs") == [(0,)] and svc.enqueued == []


@PG
def test_compute_and_list_violations_are_reported_together(svc):
    ds, up = seed_dataset(svc), shadows_upload(svc)
    bad = zoo_spec(ds, up, particles=10_001, models=[], horizons_ns=list(range(MAX_HORIZONS + 1)))
    for path in ("/v1/zoos", "/v1/zoos/estimate"):
        r = svc.client.post(path, json=bad, headers=svc.h(Idempotency_Key="both"))
        assert r.status_code == 422, r.text
        body = r.json()
        assert [e["loc"] for e in body["errors"]] == [
            ["body", "particles"],
            ["body", "horizons_ns"],
            ["body", "models"],
        ]
        assert "QT_COMPUTE_LIMITS" in body["detail"] and "model or horizon lists" in body["detail"]
    r = svc.client.post("/v1/zoos/estimate", json=zoo_spec(ds, up, models=[]), headers=svc.h())
    assert r.json()["detail"] == "model or horizon lists are outside this service's bounds"
    r = svc.client.post("/v1/zoos/estimate", json=zoo_spec(ds, up, threads=17), headers=svc.h())
    assert r.json()["detail"] == "compute parameters exceed this service's limits (QT_COMPUTE_LIMITS)"


@PG
def test_zoo_model_ids_of_the_engine_grammar_pass(svc):
    ds, up = seed_dataset(svc), shadows_upload(svc)
    models = [
        "all",
        "hbt_power[exch=partial;mode=faithful;n=2]",
        "hbt_power3[n=3]",
        "nt_queue_l2_prob_touch[p=0.25]",
        "hbt_log2",
    ]
    r = svc.client.post("/v1/zoos", json=zoo_spec(ds, up, models=models), headers=svc.h(Idempotency_Key="ok"))
    assert r.status_code == 202, r.text
    many = [f"hbt_power[n={i}]" for i in range(MAX_ZOO_MODELS)]
    r = svc.client.post("/v1/zoos", json=zoo_spec(ds, up, models=many), headers=svc.h(Idempotency_Key="many"))
    assert r.status_code == 202, r.text
    r = svc.client.post(
        "/v1/zoos",
        json=zoo_spec(ds, up, horizons_ns=list(range(1, MAX_HORIZONS + 1))),
        headers=svc.h(Idempotency_Key="hz"),
    )
    assert r.status_code == 202, r.text


@PG
def test_zoo_references_are_checked(svc):
    ds, up = seed_dataset(svc), shadows_upload(svc)
    cases = [
        ({"dataset": {"path": "/data/ds"}, "shadows": {"upload_id": up}}, 422, ["body", "dataset"]),
        ({"dataset": {"dataset_id": ds}, "shadows": {"path": "/x.parquet"}}, 422, ["body", "shadows"]),
        (zoo_spec(ds, up, out="/tmp/zoo"), 422, ["body", "out"]),
        (zoo_spec(ds, up, amends={"path": "/a.parquet"}), 422, ["body", "amends"]),
        (zoo_spec("ds_01ARZ3NDEKTSV4RRFFQ69G5FAV", up), 404, None),
        (zoo_spec(ds, "up_01ARZ3NDEKTSV4RRFFQ69G5FAV"), 404, None),
        ({"dataset": {"dataset_id": ds}}, 422, ["body", "shadows"]),  # required by ZooSpec
        (zoo_spec(ds, up, unknown_field=1), 422, ["body", "unknown_field"]),
        (zoo_spec(ds, up, mode="bogus"), 422, ["body", "mode"]),
    ]
    for i, (spec, status, loc) in enumerate(cases):
        r = svc.client.post("/v1/zoos", json=spec, headers=svc.h(Idempotency_Key=f"ref{i}"))
        assert r.status_code == status, (spec, r.text)
        if loc is not None:
            assert any(e["loc"][: len(loc)] == loc for e in r.json()["errors"]), r.json()
    pending = seed_dataset(svc, status="validating")
    r = svc.client.post("/v1/zoos", json=zoo_spec(pending, up), headers=svc.h(Idempotency_Key="pend"))
    assert r.status_code == 409 and r.json()["type"].endswith("/dataset-not-ready")
    raw = upload_bytes(svc, b"unverified" * 10, filename="u.parquet", purpose="shadow_spec")
    r = svc.client.post("/v1/zoos", json=zoo_spec(ds, raw), headers=svc.h(Idempotency_Key="unv"))
    assert r.status_code == 409 and r.json()["type"].endswith("/upload-not-verified")
    assert svc.db.q("select count(*) from jobs") == [(0,)]


@PG
def test_calibration_horizon_bound(svc):
    ds, up = seed_dataset(svc), shadows_upload(svc)
    spec = {"kind": "alloc", "family": "power3", "dataset": {"dataset_id": ds}, "shadows": {"upload_id": up}}
    too_many = spec | {"horizons_ns": list(range(MAX_HORIZONS + 1))}
    for path in ("/v1/calibrations", "/v1/calibrations/estimate"):
        r = svc.client.post(path, json=too_many, headers=svc.h(Idempotency_Key="c-many"))
        assert r.status_code == 422 and r.json()["errors"] == [
            {"loc": ["body", "horizons_ns"], "msg": f"at most {MAX_HORIZONS} horizons on this service"}
        ]
    ok = svc.client.post("/v1/calibrations", json=spec, headers=svc.h(Idempotency_Key="c-ok"))
    assert ok.status_code == 202 and ok.json()["job"]["kind"] == "calibration"


@PG
def test_submit_needs_submit_scope_and_estimate_read(svc):
    ds, up = seed_dataset(svc), shadows_upload(svc)
    r = svc.client.post("/v1/keys", json={"scopes": ["read"], "name": "ro"}, headers=svc.h(Idempotency_Key="k-ro"))
    read = r.json()["secret"]
    hdr = {"Authorization": f"Bearer {read}", "Idempotency-Key": "s"}
    assert svc.client.post("/v1/zoos", json=zoo_spec(ds, up), headers=hdr).status_code == 403
    assert svc.client.post("/v1/zoos/estimate", json=zoo_spec(ds, up), headers=hdr).status_code == 200
    assert svc.client.get("/v1/zoos", headers=hdr).status_code == 200
    assert svc.client.post("/v1/zoos", json=zoo_spec(ds, up)).status_code == 401
    r = svc.client.post("/v1/zoos", json=zoo_spec(ds, up), headers=svc.h())
    assert r.status_code == 422  # Idempotency-Key is required on job POSTs


# ---- estimate -----------------------------------------------------------------------------------------------------


@PG
def test_zoo_estimate(svc):
    ds, up = seed_dataset(svc), shadows_upload(svc)  # manifest: 1000 rows; upload 6404 bytes -> 100 shadows
    est = svc.client.post("/v1/zoos/estimate", json=zoo_spec(ds, up, models=["all"]), headers=svc.h())
    assert est.status_code == 200, est.text
    doc = est.json()
    m.Estimate.model_validate(doc)
    n, n_particle = zoo_models_est({"models": ["all"]})
    pairs = 100 * n
    steps = (n - n_particle) + n_particle * (1 + 100 / PARTICLES_PER_PAIR) + (n + 1) * 1000 / BOOTSTRAP_DRAWS_PER_PAIR
    cpu = round(2 * 1000 / REPLAY_RPS + 100 * steps * ZOO_PAIR_S, 3)
    assert doc["kind"] == "zoo" and doc["records_est"] == 1000 and doc["shadows_est"] == 100
    assert doc["cpu_seconds_est"] == cpu and doc["cost_units"] == cpu
    assert doc["eta_seconds"] == round(cpu + QUEUE_OVERHEAD_S, 3) and doc["bytes_out_est"] == pairs * 512
    two = svc.client.post(
        "/v1/zoos/estimate",
        json=zoo_spec(ds, up, models=["hbt_power3[n=3]", "hbt_log2"], write_trajectory=True, bootstrap_b=1),
        headers=svc.h(),
    ).json()
    assert two["cpu_seconds_est"] == round(2 * 1000 / REPLAY_RPS + 100 * (2 + 3 / 1000) * ZOO_PAIR_S, 3)
    assert two["bytes_out_est"] == 200 * 512 + 1000 * 24

    def cpu_of(**kw) -> float:
        r = svc.client.post("/v1/zoos/estimate", json=zoo_spec(ds, up, **kw), headers=svc.h())
        assert r.status_code == 200, r.text
        return r.json()["cpu_seconds_est"]

    # the knobs QT_COMPUTE_LIMITS allows at 100x (particles) and 10x (bootstrap_b) their defaults move the estimate
    mc = ["databento_mc[k=-0.8]"]
    assert cpu_of(models=mc, particles=10_000, bootstrap_b=1) == round(
        2 * 1000 / REPLAY_RPS + 100 * (1 + 10_000 / PARTICLES_PER_PAIR + 2 / 1000) * ZOO_PAIR_S, 3
    )
    assert cpu_of(models=mc, particles=10_000, bootstrap_b=1) > 10 * cpu_of(models=mc, particles=100, bootstrap_b=1)
    assert cpu_of(models=["front_of_queue"], bootstrap_b=10_000) > 4 * cpu_of(models=["front_of_queue"])
    assert cpu_of(models=["front_of_queue"], particles=10_000) == cpu_of(models=["front_of_queue"])  # no particles
    assert svc.db.q("select count(*) from jobs") == [(0,)]  # estimates create nothing


def test_zoo_models_est():
    n_all = zoo_models_est({"models": ["all"]})[0]
    assert n_all == ZOO_ALL_MODELS
    assert zoo_models_est({"models": ["all", "nt_queue_l2_prob_touch[p=0.5]"]})[0] == ZOO_ALL_MODELS + 1
    assert zoo_models_est({"models": ["a", "b", "c"]}) == (3, 0)
    assert zoo_models_est({"models": ["databento_mc[k=0.1]", "hbt_log"]}) == (2, 1)
    assert zoo_models_est({"models": []}) == (0, 0)


@pytest.mark.requires_core
def test_zoo_estimate_follows_the_engine_registry(tmp_path):
    """`all` and the particle families come from the engine (`queuetruth zoo --list-models`); the fallback constant
    equals the engine's count, and an explicit id that `all` holds counts once, as the engine dedupes it."""
    registry = engine_models()
    assert registry is not None
    ids, families = registry
    assert len(ids) == ZOO_ALL_MODELS and PARTICLE_FAMILIES <= families
    assert zoo_models_est({"models": ["all", "hbt_power3[n=3]"]}) == zoo_models_est({"models": ["all"]})
    assert zoo_models_est({"models": ["all"]})[1] == sum(1 for i in ids if i.split("[")[0] in PARTICLE_FAMILIES) == 2
    import queuetruth as qt

    fixture = REPO / "tests" / "fixtures" / "synth_small"
    if not (fixture / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    shadows = tmp_path / "s.parquet"
    qt.sample_shadows({"dataset": {"path": str(fixture)}, "n": 4, "seed": 1, "out": str(shadows)})
    for models in (["all"], ["all", "hbt_power3[n=3]"]):
        spec = {"dataset": {"path": str(fixture)}, "shadows": {"path": str(shadows)}, "models": models}
        ran = qt.run_zoo(spec | {"bootstrap_b": 1, "out": str(tmp_path / f"z{len(models)}")})
        assert len(ran.models) == zoo_models_est(spec)[0], models


# ---- tenancy ------------------------------------------------------------------------------------------------------


@PG
def test_zoo_tenant_isolation(svc):
    ds, up = seed_dataset(svc, "A"), shadows_upload(svc, "A")
    r = svc.client.post("/v1/zoos", json=zoo_spec(ds, up), headers=svc.h("A", Idempotency_Key="a"))
    zid, job = r.json()["id"], r.json()["job"]["id"]
    succeed_job(svc, job, {"summary.json": b"{}", "zoo_predictions.parquet": b"PAR1"})
    b = svc.h("B")
    for path in (f"/v1/zoos/{zid}", f"/v1/jobs/{job}", f"/v1/jobs/{job}/files", f"/v1/jobs/{job}/files/summary.json"):
        res = svc.client.get(path, headers=b)
        assert res.status_code == 404, path
        assert "PAR1" not in res.text and ds not in res.text  # the problem names only the requested path
    assert svc.client.get("/v1/zoos", headers=b).json()["data"] == []
    assert svc.client.get("/v1/jobs?kind=zoo", headers=b).json()["data"] == []
    # B cannot use A's dataset or upload, nor cancel A's job
    b_up = shadows_upload(svc, "B", b"PAR1" + b"b" * 640)
    b_ds = seed_dataset(svc, "B")
    for i, spec in enumerate((zoo_spec(ds, b_up), zoo_spec(b_ds, up))):
        res = svc.client.post("/v1/zoos", json=spec, headers=svc.h("B", Idempotency_Key=f"b{i}"))
        assert res.status_code == 404, res.text
        res = svc.client.post("/v1/zoos/estimate", json=spec, headers=b)
        assert res.status_code == 404, res.text
    assert svc.client.post(f"/v1/jobs/{job}/cancel", headers=svc.h("B", Idempotency_Key="cx")).status_code == 404
    # RLS: the app role sees no zoo row unbound or with a hand-set GUC, none bound to B and one bound to A
    with psycopg.connect(svc.db.app) as conn, conn.transaction():
        assert conn.execute("select count(*) from zoos").fetchone() == (0,)
        conn.execute("select set_config('app.tenant_id', %s, true)", (svc.tenants["A"],))
        assert conn.execute("select count(*) from zoos").fetchone() == (0,)
    for who, n in (("B", 0), ("A", 1)):
        key = svc.keys[who]
        with psycopg.connect(svc.db.app) as conn, conn.transaction():
            bound = conn.execute(
                "select tenant_id from qt_authenticate(%s, %s)",
                (keys.parse(key), keys.key_hash(svc.settings.pepper, key)),
            ).fetchone()
            assert bound == (svc.tenants[who],)
            assert conn.execute("select count(*) from zoos").fetchone() == (n,), who
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(
                    "insert into zoos (id, tenant_id, job_id, spec) values ('zoo_evil', %s, %s, '{}')",
                    (svc.tenants["A" if who == "B" else "B"], job),
                )


@PG
def test_zoos_table_forces_rls_and_job_kind_constraint(db):
    rows = db.q("select relrowsecurity, relforcerowsecurity from pg_class where relname = 'zoos'")
    assert rows == [(True, True)]
    assert db.q("select tableowner from pg_tables where tablename = 'zoos'")[0][0] not in ("qt_app", "qt_worker")
    grants = {
        (r, p)
        for r, p in db.q(
            "select grantee, privilege_type from information_schema.role_table_grants where table_name = 'zoos'"
        )
    }
    for role in ("qt_app", "qt_worker"):
        assert {(role, p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")} <= grants
    (con,) = db.q("select pg_get_constraintdef(oid) from pg_constraint where conname = 'jobs_kind_check'")[0]
    for kind in ("ingest", "replay", "calibration", "scorecard", "report", "dataset_validate", "deletion", "zoo"):
        assert f"'{kind}'" in con
    db.q("insert into tenants (id, name) values ('ten_x', 'x')")
    insert = "insert into jobs (id, tenant_id, kind, resource_type, resource_id) values (%s, 'ten_x', %s, 'x', 'y')"
    with pytest.raises(psycopg.errors.CheckViolation):
        db.q(insert, ("j1", "bogus"))
    db.q(insert, ("j2", "zoo"))


# ---- retention, runners, settings ---------------------------------------------------------------------------------


@PG
def test_retention_purge_clears_zoo_summaries(svc):
    a = svc.tenants["A"]
    job = insert_job(svc, kind="zoo")
    succeed_job(svc, job, {"summary.json": b"{}"})
    svc.db.q("insert into zoos (id, tenant_id, job_id, spec, summary) values ('zoo_old', %s, %s, '{}', '{}')", (a, job))
    svc.db.q("update jobs set finished_at = now() - interval '40 days' where id = %s", (job,))

    async def go():
        pool = await make_async_pool(svc.db.worker)
        try:
            return await run_retention(pool, svc.store)
        finally:
            await pool.close()

    rep = asyncio.run(go())[a]
    assert rep.jobs == [job]
    assert svc.db.q("select summary from zoos where id = 'zoo_old'") == [(None,)]
    assert svc.db.q("select state from jobs where id = %s", (job,)) == [("expired",)]


def test_zoo_runner_target_and_meters():
    assert runners.TARGETS["zoo"] == "queuetruth_service.runners:zoo"
    assert runners.TARGETS["calibration"] == "queuetruth_service.runners:calibrate"
    meters = runners.usage_meters("zoo", {"n_shadows": 40, "models": ["a", "b", "c"]}, 1234)
    assert meters == {"cpu_ms": (1234.0, "ms"), "shadow_orders": (40.0, "orders"), "zoo_pairs": (120.0, "pairs")}
    assert runners.usage_meters("zoo", {"n_shadows": 0, "models": []}, 5) == {
        "cpu_ms": (5.0, "ms"),
        "shadow_orders": (0.0, "orders"),
    }
    assert runners.usage_meters("zoo", None, 0) == {"cpu_ms": (0.0, "ms"), "shadow_orders": (0.0, "orders")}


def test_job_timeouts_accept_zoo(monkeypatch):
    s = Settings(_env_file=None, job_timeouts_s={"zoo": 900, "calibration": 14400})
    assert s.job_timeout_for("zoo") == 900 and s.job_timeout_for("calibration") == 14400
    assert s.job_timeout_for("replay") == s.job_timeout_s
    with pytest.raises(ValueError, match="unknown job kind 'zoos'"):
        Settings(_env_file=None, job_timeouts_s={"zoos": 1})


def test_openapi_documents_the_zoo_resource():
    from queuetruth_service.export_openapi import document

    doc = document()
    for method, path in (
        ("post", "/v1/zoos"),
        ("get", "/v1/zoos"),
        ("post", "/v1/zoos/estimate"),
        ("get", "/v1/zoos/{zoo_id}"),
    ):
        assert method in doc["paths"][path], (method, path)
    post = doc["paths"]["/v1/zoos"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/ZooSpec")
    assert any(p["name"] == "Idempotency-Key" and p["required"] for p in post["parameters"])
    rec = doc["components"]["schemas"]["ZooRecord"]
    assert {"$ref": "#/components/schemas/ZooSummary"} in rec["properties"]["summary"]["anyOf"]
    kinds = doc["components"]["schemas"]["Job"]["properties"]["kind"]["enum"]
    assert "zoo" in kinds and "calibration" in kinds
    jobs_kind = next(p for p in doc["paths"]["/v1/jobs"]["get"]["parameters"] if p["name"] == "kind")
    assert "zoo" in str(jobs_kind["schema"])
