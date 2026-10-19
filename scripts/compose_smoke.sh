#!/usr/bin/env bash
# Compose smoke test: builds and starts the self-host stack with
# throwaway secrets under a private project name, then checks end to end through the published ports:
#   - health: GET /v1/health (no key), the SeaweedFS S3 gateway, compose health of api and worker, 401 without a key;
#   - tenants: two tenants created through `migrate create-tenant`, a narrower write key and a read key per scope;
#   - presigned upload through SeaweedFS: part URLs point at QT_S3_PUBLIC_ENDPOINT and are signed, a tampered
#     signature is refused by the store, the real PUT returns an ETag, the worker verifies the sha256;
#   - data: `queuetruth fixtures synth` uploaded as one .tar.gz archive and registered as a parquet dataset;
#   - replay: a job with shadows and the rank oracle; every output downloaded with sha256 verification; the summary
#     (without `timings` and the spec echo), the run_id and every output file except summary.json are identical to a
#     local `queuetruth ingest` + `queuetruth replay` of the same data;
#   - isolation: the second tenant gets 404 for every id of the first, cannot reference its dataset or uploads, and
#     lists nothing; a read key cannot submit; a revoked key gets 401.
# Then the stack and its volumes are removed. Skips (exit 0) when the Docker daemon is not running. Synthetic data only.
# QT_SMOKE_KEEP=1 keeps the stack (and prints how to remove it) when a check fails. QT_SMOKE_BUILD=0 reuses an existing
# image queuetruth:$QT_SMOKE_TAG (default tag `smoke`) instead of building it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yml"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
QT="${QT_CLI:-$(dirname "$PY")/queuetruth}"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "compose_smoke: skipped (Docker daemon not running)"
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/qt-compose-smoke.XXXXXX")"
PROJECT="qtsmoke$$"
API_PORT="${QT_API_PORT:-18080}"
S3_PORT="${QT_S3_PORT:-18333}"
TAG="${QT_SMOKE_TAG:-smoke}"
secret() { "$PY" -c "import secrets; print(secrets.token_urlsafe(32))"; }
cat >"$WORK/env" <<EOF
QT_PG_PASSWORD=$(secret)
QT_APP_PASSWORD=$(secret)
QT_WORKER_PASSWORD=$(secret)
QT_S3_ACCESS_KEY_ID=$(secret)
QT_S3_SECRET_ACCESS_KEY=$(secret)
QT_KEY_PEPPER=$(secret)
QT_SIGNING_KEY=$(secret)
QT_VERSION=$TAG
QT_API_PORT=$API_PORT
QT_S3_PORT=$S3_PORT
QT_PUBLIC_BASE_URL=http://127.0.0.1:$API_PORT
QT_S3_PUBLIC_ENDPOINT=http://127.0.0.1:$S3_PORT
QT_WORKER_MEM_LIMIT=${QT_WORKER_MEM_LIMIT:-6g}
QT_JOB_MEMORY_BYTES=${QT_JOB_MEMORY_BYTES:-4294967296}
EOF
chmod 600 "$WORK/env"
dc() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" --env-file "$WORK/env" "$@"; }
cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "compose_smoke: FAILED (exit $rc); service logs follow" >&2
    dc logs --no-color --tail 80 migrate api worker seaweedfs >&2 2>&1 || true
    if [ "${QT_SMOKE_KEEP:-0}" = 1 ]; then
      echo "compose_smoke: stack kept; remove with: docker compose -p $PROJECT -f $COMPOSE_FILE --env-file $WORK/env down -v" >&2
      return
    fi
  fi
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

dc config -q
t0=$(date +%s)
if [ "${QT_SMOKE_BUILD:-1}" = 1 ]; then dc build; else docker image inspect "queuetruth:$TAG" >/dev/null; fi
echo "compose_smoke: build $(( $(date +%s) - t0 )) s; image $(docker image inspect -f '{{.Size}}' "queuetruth:$TAG" | awk '{printf "%.0f MB", $1/1e6}')"
t0=$(date +%s)
dc up -d --no-build --wait --wait-timeout 300 api worker
echo "compose_smoke: stack healthy after $(( $(date +%s) - t0 )) s"
tenant() { dc run --rm -T migrate queuetruth-service create-tenant --name "$1" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["key"])'; }
KEY_A="$(tenant smoke-a)"
KEY_B="$(tenant smoke-b)"
"$QT" fixtures synth --out "$WORK/synth" --quiet >/dev/null

QT_API_URL="http://127.0.0.1:$API_PORT" QT_SMOKE_S3="http://127.0.0.1:$S3_PORT" KEY_A="$KEY_A" KEY_B="$KEY_B" QT_CLI="$QT" \
  "$PY" - "$WORK/synth" "$WORK" <<'PYEOF'
import hashlib, json, os, subprocess, sys, tarfile, time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from queuetruth.client import Client
from queuetruth.errors import QtApiError

synth, work = Path(sys.argv[1]), Path(sys.argv[2])
api, s3 = os.environ["QT_API_URL"], os.environ["QT_SMOKE_S3"]
ok = lambda msg: print(f"compose_smoke: ok   {msg}", flush=True)


def status_of(fn, *a, **k):
    try:
        fn(*a, **k)
    except QtApiError as e:
        return e.status
    return 200


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---- health -----------------------------------------------------------------------------------------------------
r = httpx.get(f"{api}/v1/health", timeout=10)
h = r.json()
assert r.status_code == 200 and h["status"] == "ok" and h["schema"] == "qt.health", r.text
ok(f"GET /v1/health -> 200 {{status: ok, db: {h['db']}, store: {h['store']}, queue: {h['queue']}, version: {h['version']}}}")
assert httpx.get(f"{s3}/healthz", timeout=10).status_code == 200
ok("SeaweedFS S3 gateway /healthz -> 200")
assert httpx.get(f"{api}/v1/openapi.json", timeout=10).json()["openapi"].startswith("3.1")
r = httpx.get(f"{api}/v1/datasets", timeout=10)
assert r.status_code == 401 and r.headers["content-type"].startswith("application/problem+json"), r.status_code
assert r.headers.get("X-Request-Id"), "no X-Request-Id"
ok("no key -> 401 problem+json with X-Request-Id; openapi 3.1 served")

admin_a, admin_b = Client(api_key=os.environ["KEY_A"], timeout=60), Client(api_key=os.environ["KEY_B"], timeout=60)
ta, tb = admin_a.tenant(), admin_b.tenant()
assert ta.id != tb.id
created = admin_a.create_key(["write"], "smoke-ci")
reader = admin_a.create_key(["read"], "smoke-agent")
c, ro = Client(api_key=created.secret, timeout=60), Client(api_key=reader.secret, timeout=60)
ok(f"tenants {ta.id} and {tb.id}; write key {created.id} and read key {reader.id} created")

# ---- presigned upload through SeaweedFS (by hand, to see the URLs) --------------------------------------------
shadows = synth / "truth" / "shadows.parquet"
data = shadows.read_bytes()
up = c.create_upload("shadow_spec", "shadows.parquet", len(data), hashlib.sha256(data).hexdigest())
assert len(up.parts) == 1
url = c.url(up.parts[0].url)
u = urlsplit(url)
assert f"{u.scheme}://{u.netloc}" == s3 and "X-Amz-Signature=" in u.query, url.split("?")[0]
tampered = url.replace("X-Amz-Signature=", "X-Amz-Signature=0")
r = httpx.put(tampered, content=data, timeout=60)
assert r.status_code == 403, (r.status_code, r.text[:300])
r = httpx.put(url.split("?")[0], content=data, timeout=60)
assert r.status_code in (403, 401), (r.status_code, r.text[:300])
r = httpx.put(url, content=data, timeout=60)
assert r.status_code == 200 and r.headers.get("ETag"), (r.status_code, r.text[:300])
c.complete_upload(up.id, [{"part_number": 1, "etag": r.headers["ETag"]}])
sh_up = c.wait_upload(up.id, timeout=300)
assert sh_up.status == "verified", sh_up
ok(f"presigned PUT to {u.netloc}{u.path} accepted (tampered/unsigned -> 403); upload {sh_up.id} verified by the worker")

# ---- dataset from a .tar.gz of `queuetruth fixtures synth` -------------------------------------------------------
archive = work / "synth.tar.gz"
with tarfile.open(archive, "w:gz") as t:
    t.add(synth, arcname="synth")
up = c.wait_upload(c.upload_file(archive, "market_data").id, timeout=300)
assert up.status == "verified", up
ds = c.create_dataset("synth", "mbo", "parquet", "cme", [up.id], ingest={"venue": "cme"})
dj = c.jobs.wait(ds.job_id, timeout=600)
assert dj.state == "succeeded", dj.error
ds = c.get_dataset(ds.id)
assert ds.status == "ready", ds
ok(f"archive upload {up.id} ({archive.stat().st_size} B) verified; dataset {ds.id} ready")

# ---- replay job vs a local `queuetruth replay` ------------------------------------------------------------------
assert status_of(ro.submit, "replay", {"dataset": {"dataset_id": ds.id}}) == 403
ok("read key cannot submit (403)")
t0 = time.monotonic()
sub = c.submit("replay", {"dataset": {"dataset_id": ds.id}, "shadows": {"upload_id": sh_up.id}, "rank_oracle": True})
job = c.jobs.wait(sub.job.id, timeout=900)
assert job.state == "succeeded", job.error
remote = c.get_run("replay", sub.id).summary.model_dump(mode="json")
out = work / "remote"
paths = c.jobs.download_all(job.id, out)
man = {f.name: f for f in c.jobs.files(job.id).files}
for p in paths:
    f = man[p.relative_to(out).as_posix()]
    assert sha(p) == f.sha256 and p.stat().st_size == f.bytes, p
ok(f"replay job {job.id} succeeded in {time.monotonic() - t0:.1f} s; {len(paths)} files downloaded, sha256 verified")

# The service ingests the uploaded files into a new dataset (format parquet), so the local reference is the same
# two steps: `queuetruth ingest` of the synth directory, then `queuetruth replay` on that dataset. (A replay directly
# on the synth directory has another dataset_id and therefore another run_id; its records are the same.)
qt = os.environ["QT_CLI"]
local_ds, local_out = work / "local_ds", work / "local"
subprocess.run([qt, "ingest", "--format", "parquet", "--in", str(synth), "--venue", "cme", "--out", str(local_ds),
                "--quiet"], capture_output=True, text=True, check=True)
local_id = json.loads((local_ds / "manifest.json").read_text())["dataset_id"]
assert ds.manifest is not None and ds.manifest.dataset_id == local_id, (ds.manifest and ds.manifest.dataset_id, local_id)
cp = subprocess.run(
    [qt, "replay", "--data", str(local_ds), "--shadows", str(shadows), "--rank-oracle",
     "--out", str(local_out), "--output-format", "json", "--quiet"],
    capture_output=True, text=True, check=True,
)
local = json.loads(cp.stdout)
assert local["run_id"] == remote["run_id"], (local["run_id"], remote["run_id"])
for d in (local, remote):
    d.pop("timings", None)
    d.pop("spec", None)
a, b = json.dumps(local, sort_keys=True), json.dumps(remote, sort_keys=True)
assert a == b, [k for k in local if local.get(k) != remote.get(k)]
lf = {p.relative_to(local_out).as_posix(): sha(p) for p in local_out.rglob("*") if p.is_file()}
rf = {p.relative_to(out).as_posix(): sha(p) for p in out.rglob("*") if p.is_file()}
assert set(lf) == set(rf), (sorted(set(lf) ^ set(rf)))
diff = [n for n in lf if n != "summary.json" and lf[n] != rf[n]]
assert not diff, diff
ok(f"dataset_id {local_id[:16]}.. equals local ingest; run_id {remote['run_id']} and summary equal local replay; "
   f"{len(lf) - 1} output files byte-identical")

# ---- tenant isolation -------------------------------------------------------------------------------------------
checks = {
    "dataset": status_of(admin_b.get_dataset, ds.id),
    "upload": status_of(admin_b.get_upload, sh_up.id),
    "job": status_of(admin_b.jobs.get, job.id),
    "job files": status_of(admin_b.jobs.files, job.id),
    "replay": status_of(admin_b.get_run, "replay", sub.id),
    "download": httpx.get(f"{api}/v1/jobs/{job.id}/files/summary.json",
                          headers={"Authorization": f"Bearer {os.environ['KEY_B']}"}, timeout=30).status_code,
    "delete dataset": status_of(admin_b.delete_dataset, ds.id),
}
assert all(v == 404 for v in checks.values()), checks
sb = status_of(admin_b.submit, "replay", {"dataset": {"dataset_id": ds.id}, "shadows": {"upload_id": sh_up.id}})
assert sb in (404, 409, 422), sb
assert not admin_b.list_datasets().data and not admin_b.jobs.list().data
ok(f"tenant B: 404 on {', '.join(checks)}; submit with A's ids -> {sb}; B lists no datasets or jobs")
assert c.get_dataset(ds.id).status == "ready"

admin_a.revoke_key(created.id)
assert status_of(c.get_dataset, ds.id) == 401
ok("revoked key -> 401")
PYEOF
echo "compose_smoke: pass"
