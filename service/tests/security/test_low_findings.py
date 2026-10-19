"""Low-severity findings L1-L7 of the security review, inverted (L3, the MCP local tools, lives in
`python/tests/mcp/test_mcp_local_security.py`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pickle
import socket
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from svc_support import make_svc

from queuetruth_service import keys, logs, netguard, runners
from queuetruth_service.executor import run_child

SERVICE = Path(__file__).resolve().parents[2] / "queuetruth_service"
COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "compose" / "docker-compose.yml"

# ---- L1: read-scoped keys get no PUT URLs ------------------------------------------------------------------------


@pytest.mark.requires_postgres
def test_l1_read_key_gets_no_part_urls(svc):
    r = svc.client.post("/v1/keys", json={"scopes": ["read"], "name": "dash"}, headers=svc.h(Idempotency_Key="rk"))
    read_key = r.json()["secret"]
    data = b"a" * 2048
    body = {
        "purpose": "other",
        "filename": "x.bin",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    up = svc.client.post("/v1/uploads", json=body, headers=svc.h(Idempotency_Key="u1")).json()
    assert len(up["parts"]) == 2
    got = svc.client.get(f"/v1/uploads/{up['id']}", headers={"Authorization": f"Bearer {read_key}"})
    assert got.status_code == 200 and got.json()["status"] == "pending" and got.json()["parts"] == []
    writer = svc.client.get(f"/v1/uploads/{up['id']}", headers=svc.h()).json()  # the write key refreshes its URLs
    u = urlsplit(writer["parts"][0]["url"])
    assert svc.client.put(f"{u.path}?{u.query}", content=data[:1024]).status_code == 200


# ---- L2: netguard is a tripwire; the boundary is the network -----------------------------------------------------


def test_l2_sendmsg_is_guarded_and_the_limits_are_enforced_by_the_network():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    netguard.install([("127.0.0.1", 1)])
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with pytest.raises(netguard.EgressBlocked):
            s.sendmsg([b"exfil"], [], 0, rx.getsockname())
        with pytest.raises(netguard.EgressBlocked):
            s.sendto(b"x", rx.getsockname())
        s.close()
    finally:
        netguard.uninstall()
        rx.close()
    doc = netguard.__doc__ or ""
    assert "_socket.socket.connect" in doc and "internal" in doc  # the in-process limits are documented ...
    net = yaml.safe_load(COMPOSE.read_text())
    assert net["networks"]["backend"]["internal"] is True  # ... and egress is cut at the container network
    assert net["services"]["worker"]["networks"] == ["backend"]
    assert net["services"]["postgres"]["networks"] == ["backend"]


# ---- L4: unauthenticated requests are throttled before the database ----------------------------------------------


class CountingPool:
    def __init__(self, inner):
        self.inner, self.n = inner, 0

    def connection(self, *a, **kw):
        self.n += 1
        return self.inner.connection(*a, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.mark.requires_postgres
def test_l4_failed_authentications_are_throttled_before_the_database(db, tmp_path):
    gen = make_svc(db, tmp_path, auth_failure_limit=(30, 10))
    svc = next(gen)
    try:
        ctx = svc.client.app.state.ctx
        counting = CountingPool(ctx.pool)
        ctx.pool = counting
        try:
            responses = [
                svc.client.get("/v1/jobs", headers={"Authorization": f"Bearer {keys.generate('prod')[0]}"})
                for _ in range(100)
            ]
        finally:
            ctx.pool = counting.inner
        codes = [r.status_code for r in responses]
        assert codes[:10] == [401] * 10 and codes[10:] == [429] * 90
        assert counting.n == 10  # one lookup per allowed attempt; the 90 throttled ones never reach the database
        assert int(responses[-1].headers["retry-after"]) >= 1
        assert svc.client.get("/v1/jobs", headers={"Authorization": "Bearer nope"}).status_code == 429
    finally:
        gen.close()


def test_l4_throttle_is_per_client_and_refills():
    from queuetruth_service.ratelimit import FailureThrottle

    t = FailureThrottle(per_min=60, burst=2)
    for _ in range(2):
        assert t.retry_after("a") is None
        t.failed("a")
    assert t.retry_after("a") == 1 and t.retry_after("b") is None
    small = FailureThrottle(per_min=1, burst=1, max_clients=3)
    for c in "abcd":
        small.failed(c)
    assert len(small._buckets) == 3  # bounded memory


# ---- L5: signed query strings and secrets are redacted in access logs --------------------------------------------


def test_l5_access_log_lines_are_redacted():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        logs.setup("INFO")
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lg = logging.getLogger(name)
            assert lg.propagate is True and lg.handlers == []  # through the JSON formatter, not uvicorn's handler
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            (
                "203.0.113.9:5555",
                "GET",
                "/v1/_store/t/ten_A/jobs/job_1/fills.parquet?exp=1999999999&sig=deadbeefcafe",
                "1.1",
                200,
            ),
            None,
        )
        line = logs.JsonFormatter().format(record)
        assert "deadbeefcafe" not in line and "sig=<redacted>" in json.loads(line)["msg"]
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
    s3 = "/b/k?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIAXX%2F20261018&X-Amz-Signature=abc123def"
    assert "abc123def" not in keys.redact(s3) and "AKIAXX" not in keys.redact(s3)
    assert keys.redact("postgresql://qt_app:hunter2@pg/q") == "postgresql://qt_app:<redacted>@pg/q"


# ---- L6: no unpickling in the service; the model loader checks the hash first ------------------------------------


class _Boom:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __reduce__(self):
        return (os.mkdir, (self.marker,))


def test_l6_service_never_unpickles_user_data(tmp_path):
    banned = ("import pickle", "pickle.load", "joblib", "cloudpickle", "HazardModel", "allow_pickle=True", "dill")
    for p in SERVICE.rglob("*.py"):
        if p.name == "testing.py":  # test fakes (fake_unpickle proves the guard below)
            continue
        text = p.read_text()
        assert not [b for b in banned if b in text], p
    marker = tmp_path / "PWNED"
    payload = pickle.dumps(_Boom(str(marker))).hex()
    res = asyncio.run(
        run_child("queuetruth_service.testing:fake_unpickle", {"payload_hex": payload}, str(tmp_path), timeout_s=60)
    )
    assert res.status == "error" and res.error["code"] == "QT-ENGINE-001"
    assert not marker.exists()  # job processes refuse pickle global lookups (audit hook)


def _model_dir(tmp_path: Path, blob: bytes, sha: str) -> Path:
    d = tmp_path / "evil_model"
    d.mkdir()
    (d / "model.pkl").write_bytes(blob)
    card = {
        "schema": "qt.hazard_model_card", "schema_version": 1, "model_id": "m", "format_version": 1, "spec": {},
        "grid_ns": [1], "causes": ["fill"], "feature_names": ["f"], "privileged": False,
        "split": {"scheme": "days", "train": {"dates": ["2020-01-01"]}, "calib": {"dates": ["2020-01-02"]},
                  "test": {"dates": ["2020-01-03"]}},
        "n_orders": {"train": 0, "calib": 0, "test": 0}, "n_rows_train": 0,
        "scores_test": {"rcll": 0, "ipcw_logloss": {}, "brier": {}, "bss_vs_climatology": {}, "ibs": 0,
                        "calib_slope": {}, "calib_intercept": {}, "spiegelhalter_z": {}, "hl_p": {},
                        "c_index_uno": None, "auc": {}},
        "recalibration": "none", "warnings": [],
        "files": [{"name": "model.pkl", "bytes": len(blob), "sha256": sha}],
    }  # fmt: skip
    (d / "model.json").write_text(json.dumps(card))
    return d


def test_l6_model_loader_checks_the_sha256_before_unpickling(tmp_path, monkeypatch):
    from queuetruth.model.hazard import HazardModel

    marker = tmp_path / "PWNED"
    blob = pickle.dumps(_Boom(str(marker)))
    d = _model_dir(tmp_path, blob, "0" * 64)  # a card that does not match the pickle
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match="sha256"):
        HazardModel.load(str(d))
    assert not marker.exists()


def test_l6_model_loader_does_not_trust_a_card_from_the_same_directory(tmp_path, monkeypatch):
    from queuetruth.model.hazard import HazardModel

    marker = tmp_path / "PWNED"
    blob = pickle.dumps(_Boom(str(marker)))
    d = _model_dir(tmp_path, blob, hashlib.sha256(blob).hexdigest())  # the attacker writes both files
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception):  # noqa: B017
        HazardModel.load(str(d))
    assert not marker.exists()


# ---- L7: tar extraction refuses unpatched Python versions --------------------------------------------------------


def test_l7_tar_extraction_requires_a_patched_python(tmp_path, monkeypatch):
    patched = runners.tar_filter_patched
    for v, ok in (
        ((3, 11, 12), False),
        ((3, 11, 13), True),
        ((3, 12, 10), False),
        ((3, 12, 11), True),
        ((3, 13, 3), False),
        ((3, 13, 4), True),
        ((3, 14, 0), True),
    ):
        assert patched(v) is ok, v
    arc = tmp_path / "a.tar"
    src = tmp_path / "f.txt"
    src.write_text("x")
    with tarfile.open(arc, "w") as t:
        t.add(src, arcname="d/f.txt")
    assert (runners.extract_archive(arc, tmp_path / "ok") / "f.txt").read_text() == "x"  # this interpreter
    monkeypatch.setattr(runners, "tar_filter_patched", lambda version=None: False)
    with pytest.raises(runners.ArchiveError, match=r"3\.12\.11") as e:
        runners.extract_archive(arc, tmp_path / "old")
    assert e.value.code == "QT-INPUT-002" and not (tmp_path / "old" / "d").exists()
