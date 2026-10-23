#!/usr/bin/env bash
# Stranger test of one built wheel: a fresh venv holding only the wheel and its declared dependencies from PyPI, an
# empty working directory outside any checkout, then the steps of docs/getting-started.md (sections 1 to 6) and the
# extras. Used by .github/workflows/release.yml on every published platform and by hand before a release.
#
#   scripts/release/smoke.sh PYTHON WHEEL WORKDIR
#
# PYTHON is the interpreter for the venv (any CPython >= 3.11; the wheel is abi3). WORKDIR must not exist; it is
# left in place for inspection and the caller deletes it. Exits non-zero at the first failing step.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 PYTHON WHEEL WORKDIR" >&2
    exit 2
fi
PY=$1
WHEEL=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
WORK=$3
if [ -e "$WORK" ]; then
    echo "smoke: $WORK already exists" >&2
    exit 2
fi
mkdir -p "$WORK/run"
WORK=$(cd "$WORK" && pwd)
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV

step() { printf '\n== %s\n' "$*"; }
SECONDS=0

step "venv ($("$PY" -c 'import platform, sys; print(sys.version.split()[0], platform.machine())'))"
"$PY" -m venv "$WORK/venv"
if [ -d "$WORK/venv/Scripts" ]; then BIN="$WORK/venv/Scripts"; else BIN="$WORK/venv/bin"; fi
export PATH="$BIN:$PATH"
python -m pip install --no-cache-dir --disable-pip-version-check -q "$WHEEL"
python -m pip list --disable-pip-version-check --format freeze

cd "$WORK/run"
WANT=$(basename "$WHEEL" | cut -d- -f2)

step "version and help"
queuetruth --version | tee version.txt
grep -qx "queuetruth $WANT" version.txt
queuetruth --help > /dev/null
python -c "import queuetruth, queuetruth._core as c; assert queuetruth.__version__ == c.version() == '$WANT', c.version()"

step "doctor"
queuetruth doctor

step "fixtures synth"
queuetruth fixtures synth --out synth
python - <<'EOF'
import hashlib
from pathlib import Path

root = Path("synth")
n = 0
for line in (root / "MANIFEST.sha256").read_text().splitlines():
    digest, name = line.split(maxsplit=1)
    name = name.lstrip("*")
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    n += 1
print(f"synth: all {n} files verified")
EOF

step "shadows sample, zoo, leaderboard"
queuetruth shadows sample --data synth --n 1000 --out qt-shadows.parquet
queuetruth zoo --data synth --shadows qt-shadows.parquet --out qt-zoo --quiet
python - <<'EOF'
import json

s = json.load(open("qt-zoo/summary.json"))
h = str(10_000_000_000)
rows = sorted(s["leaderboard"], key=lambda r: r["fill_rate_bias"][h]["value"])
assert len(rows) == 16, len(rows)  # 0.1.1: --models all drops hbt_power[n=1] (duplicate of hbt_power3[n=1])
for r in rows:
    b = r["fill_rate_bias"][h]
    print(f'{r["model_id"]:52s} {b["value"]:+.4f}  [{b["ci_lo"]:+.4f}, {b["ci_hi"]:+.4f}]')
EOF

step "report"
queuetruth report --in qt-zoo --out qt-report.html
python - <<'EOF'
import re

html = open("qt-report.html", encoding="utf-8").read()
external = sorted(set(re.findall(r'(?:src|href)\s*=\s*["\']((?:https?:)?//[^"\']+)', html)))
assert not external, external
print(f"qt-report.html: {len(html):,} characters, no external references")
EOF

step "replay and why"
queuetruth replay --data synth --shadows qt-shadows.parquet --out qt-replay
SID=$(python -c "
import pyarrow.compute as pc, pyarrow.parquet as pq
t = pq.read_table('qt-replay/shadow_results.parquet')
print(t.filter(pc.equal(t['end_state'], 'filled'))['shadow_id'][0])
")
queuetruth why --data synth --shadows qt-shadows.parquet --shadow-id "$SID" --format json > why.json
python -c "
import json
w = json.load(open('why.json'))
assert w['outcome']['end_state'] == 'filled', w['outcome']
print(w['subject'], w['outcome'], len(w['events']), 'events')
"

step "Python API"
python - <<'EOF'
import json

import queuetruth as qt

qt.sample_shadows({"dataset": {"path": "synth"}, "n": 1000, "out": "api-shadows.parquet"})
zoo = qt.run_zoo(
    {
        "dataset": {"path": "synth"},
        "shadows": {"path": "api-shadows.parquet"},
        "out": "api-zoo",
        "models": ["hbt_power3[n=2]", "hbt_risk_adverse", "front_of_queue", "trade_through_only"],
    }
)
assert len(zoo.model_dump()["leaderboard"]) == 4
fills = qt.read_table("api-zoo/zoo_fills.parquet", expect="qt.zoo_fills")
print(fills.num_rows, "model fills")
part = json.load(open("synth/manifest.json"))["partitions"][0]
s = qt.Session("synth")
s.advance_to((part["ts_min"] + part["ts_max"]) // 2)
bbo = s.bbo(part["instrument_id"])
print(bbo, s.queue_ahead(part["instrument_id"], "B", bbo["bid"][0]))
EOF

step "score gate"
set +e
queuetruth score --live synth/own_orders/live --data synth --sim-model "hbt_power3[n=2]" --out sc \
    --fail-abs-bias 0.5 --format json --quiet > sc.json
rc=$?
set -e
echo "exit code $rc"
python -c "import json; print(json.load(open('sc.json'))['verdict']['headline'])"
test "$rc" -le 1

step "schema list / show"
queuetruth schema list --format json | python -c "
import json, sys
d = json.load(sys.stdin)
assert d['json'] and d['arrow'], d
print(len(d['json']), 'JSON Schemas,', len(d['arrow']), 'Arrow schemas')
"
queuetruth schema show qt.doctor --format json > /dev/null

step "mcp without the extra fails cleanly"
set +e
queuetruth mcp serve --local < /dev/null > mcp-noextra.out 2> mcp-noextra.err
rc=$?
set -e
cat mcp-noextra.err
test "$rc" -eq 2

step "extras: mcp, client, polars"
python -m pip install --no-cache-dir --disable-pip-version-check -q "${WHEEL}[mcp,client,polars]"
queuetruth mcp serve --help > /dev/null
cat > "$WORK/mcp_probe.py" <<'EOF'
"""stdio MCP probe: initialize, tools/list, one local read tool."""

import json
import subprocess
import sys

proc = subprocess.Popen(
    ["queuetruth", "mcp", "serve", "--local"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)


def rpc(i, method, params):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            sys.exit("server closed stdout: " + proc.stderr.read())
        msg = json.loads(line)
        if msg.get("id") == i:
            return msg


info = {"name": "smoke", "version": "0"}
init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": info})
print("server:", init["result"]["serverInfo"])
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
names = [t["name"] for t in rpc(2, "tools/list", {})["result"]["tools"]]
assert names == ["qt_local_read_summary", "qt_local_list_outputs", "qt_local_why"], names
res = rpc(3, "tools/call", {"name": "qt_local_read_summary", "arguments": {"path": "qt-zoo"}})["result"]
assert not res.get("isError") and res["structuredContent"]["schema"] == "qt.zoo_summary", res
proc.stdin.close()
proc.wait(timeout=30)
print("tools:", names, "read_summary ok, exit", proc.returncode)
EOF
python "$WORK/mcp_probe.py"
python -c "
import polars as pl
import pyarrow.parquet as pq
from queuetruth.client import Client
print('client import ok; polars', pl.__version__, pl.from_arrow(pq.read_table('qt-zoo/zoo_fills.parquet')).shape)
"

step "ok in ${SECONDS}s"
