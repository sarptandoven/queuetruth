#!/usr/bin/env bash
# Reproduces the hftbacktest #312 evidence in UPSTREAM.md from public sources. Nothing is submitted anywhere.
#
#   1. downloads hftbacktest at master 5f3ec40 and tag py-v2.4.4 (GitHub tarballs) and the crates.io crate 0.9.4
#      (for its published Cargo.lock), and checks the sha256 of the files the patch touches;
#   2. runs the 7 Rust unit tests of the patch on three builds of the crate at 5f3ec40: unpatched, the reporter's
#      one-line `>` -> `>=` change, and the patch (expected: 5, 2 and 0 failures);
#   3. builds the py-hftbacktest 2.4.4 wheel with the patch (tag py-v2.4.4 + patch, pinned Cargo.lock), installs the
#      released 2.4.4 wheel and the patched one into two venvs (numba, llvmlite and numpy pinned to the versions of
#      the evidence), and runs the patch's Python tests (expected: 4 failures on 2.4.4, none on the patched build)
#      and repro_312.py (exit 3 on 2.4.4, 0 patched);
#   4. with both venvs: evidence/synthetic_regression.py on the 83 O-ZOO-3 scenarios (expected: 2,988 runs, 177
#      stopped by error 14 on 2.4.4, 0 patched, 2,811 unstopped runs identical, 177 stopped runs a prefix of the
#      patched run; hbt_driver.py identical to the per-row driver on 2,988 of 2,988 runs per build) and
#      evidence/pos316.py (#316: position 1.0 after fills of 1 + 1 on both builds).
#
# The real-data numbers of UPSTREAM.md and README.md need the Nasdaq ITCH datasets; README.md "Reproduce the
# documented run" rebuilds them (reconcile.py, then evidence/readme_tables.py).
#
# Usage: integrations/hftbacktest/upstream/verify.sh [--skip-wheel]
# Env: HBT312_WORK (default <repo>/.scratch/hbt312), PYTHON_BIN (default python3.13), CARGO_BUILD_JOBS (default 3),
# MATURIN (default <repo>/.venv/bin/maturin).
# Needs: curl, patch, cargo (stable >= 1.91), uv, maturin (the repo venv's), network access to github.com,
# crates.io and PyPI. The wheel build compiles about 400 crates (aws-sdk-s3 included): 10-20 minutes.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
W=${HBT312_WORK:-$REPO/.scratch/hbt312}
PY=${PYTHON_BIN:-python3.13}
MATURIN=${MATURIN:-$REPO/.venv/bin/maturin}
export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-3}
PATCH=$HERE/0001-partialfillexchange-remove-completed-orders-312.patch
MASTER=5f3ec40b2afb764e0fea112f941ed85523ef4e88
TAG=py-v2.4.4
SHA_MASTER_PFE=c05f8b81da248dca3675258307bbf45ee6a4b2cfe421365dd083adc920cfabc2
SHA_TAG_PFE=0c9791d59238c97ed4dbcf104519590f46bf0fc0b9220164ef916c9805ca7045
SHA_CRATE=9f8b0bec3c589d21814bee7c83680273e4395804bcd9a526dd5b90d44e808830
PFE=hftbacktest/src/backtest/proc/partialfillexchange.rs
SKIP_WHEEL=0
[ "${1:-}" = "--skip-wheel" ] && SKIP_WHEEL=1

say() { printf '\n== %s\n' "$*"; }
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
check_sha() { [ "$(sha "$1")" = "$2" ] || { echo "sha256 mismatch: $1" >&2; exit 1; }; }

mkdir -p "$W/dl"
cd "$W"
say "download sources into $W/dl"
[ -f dl/master.tar.gz ] || curl -fsSL -o dl/master.tar.gz "https://codeload.github.com/nkaz001/hftbacktest/tar.gz/$MASTER"
[ -f dl/tag.tar.gz ] || curl -fsSL -o dl/tag.tar.gz "https://codeload.github.com/nkaz001/hftbacktest/tar.gz/refs/tags/$TAG"
[ -f dl/hftbacktest-0.9.4.crate ] || curl -fsSL -o dl/hftbacktest-0.9.4.crate \
  "https://static.crates.io/crates/hftbacktest/hftbacktest-0.9.4.crate"
check_sha dl/hftbacktest-0.9.4.crate "$SHA_CRATE"
rm -rf src && mkdir -p src/master src/tag src/crate
tar xzf dl/master.tar.gz -C src/master --strip-components 1
tar xzf dl/tag.tar.gz -C src/tag --strip-components 1
tar xzf dl/hftbacktest-0.9.4.crate -C src/crate --strip-components 1
check_sha "src/master/$PFE" "$SHA_MASTER_PFE"
check_sha "src/tag/$PFE" "$SHA_TAG_PFE"

say "apply the patch to master $MASTER and to tag $TAG"
rm -rf patched-master patched-tag
cp -R src/master patched-master
(cd patched-master && patch -p1 -s -N <"$PATCH")
cp -R src/tag patched-tag
(cd patched-tag && patch -p1 -s -N <"$PATCH")

say "Rust unit tests (crate 0.9.4 manifest and Cargo.lock, sources of $MASTER)"
mk_crate() { # name pfe-file
  rm -rf "crate-$1"
  cp -R src/crate "crate-$1"
  rm -rf "crate-$1/src"
  cp -R src/master/hftbacktest/src "crate-$1/src"
  cp "$2" "crate-$1/src/backtest/proc/partialfillexchange.rs"
  echo '[workspace]' >>"crate-$1/Cargo.toml"
}
# the patch's test module appended to the unpatched file, and to the reporter's `>=` variant
"$PY" - "$W" <<'EOF'
import sys
from pathlib import Path
w = Path(sys.argv[1])
pfe = "hftbacktest/src/backtest/proc/partialfillexchange.rs"
patched = (w / "patched-master" / pfe).read_text()
tests = patched[patched.index("\n#[cfg(test)]\nmod tests"):]
orig = (w / "src/master" / pfe).read_text()
(w / "pfe-testsonly.rs").write_text(orig + tests)
old = "let exec_qty = if filled_qty > order.leaves_qty {"
assert orig.count(old) == 2
(w / "pfe-gte.rs").write_text(orig.replace(old, "let exec_qty = if filled_qty >= order.leaves_qty {") + tests)
EOF
mk_crate unpatched pfe-testsonly.rs
mk_crate reporter-gte pfe-gte.rs
mk_crate patched "patched-master/$PFE"
want() { case $1 in unpatched) echo 5 ;; reporter-gte) echo 2 ;; patched) echo 0 ;; esac; }
fail=0
for v in unpatched reporter-gte patched; do
  out=$(cd "crate-$v" && CARGO_TARGET_DIR="$W/target-$v" nice -n 10 cargo test --locked --no-default-features \
    --features backtest --lib partialfillexchange 2>&1 || true)
  got=$(printf '%s\n' "$out" | sed -n 's/^test result: .* \([0-9]*\) failed;.*/\1/p' | tail -1)
  printf '%s\n' "$out" | grep -E '^test backtest' || true
  echo "$v: ${got:-?} failed (expected $(want "$v"))"
  [ "${got:-x}" = "$(want "$v")" ] || fail=1
done
(cd crate-patched && CARGO_TARGET_DIR="$W/target-patched" nice -n 10 cargo test --locked --no-default-features \
  --features backtest --lib 2>&1 | grep 'test result') || fail=1

if [ "$SKIP_WHEEL" = 0 ]; then
  say "build the patched py-hftbacktest 2.4.4 wheel"
  "$PY" - "$W/patched-tag/Cargo.toml" <<'EOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = re.sub(r"members = \[[^\]]*\]", 'members = ["hftbacktest", "hftbacktest-derive", "py-hftbacktest"]', s)
open(p, "w").write(s)
EOF
  cp "$HERE/py-hftbacktest-2.4.4-build.Cargo.lock" patched-tag/Cargo.lock
  rm -rf wheel && mkdir wheel
  (cd patched-tag/py-hftbacktest && CARGO_TARGET_DIR="$W/target-wheel" CARGO_PROFILE_RELEASE_LTO=false \
    CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 nice -n 10 "$MATURIN" build --release --locked -i "$(command -v "$PY")" \
    -o "$W/wheel" >"$W/wheel.log" 2>&1) || { tail -30 "$W/wheel.log"; exit 1; }
  for v in stock patched; do
    [ -x "venv-$v/bin/python" ] || uv venv -q "venv-$v" --python "$PY"
    uv pip install -q --python "venv-$v" numba==0.68.0 llvmlite==0.50.0 numpy==2.5.3
  done
  uv pip install -q --python venv-stock --no-deps --reinstall hftbacktest==2.4.4
  uv pip install -q --python venv-patched --no-deps --reinstall wheel/*.whl
  say "Python tests and reproduction"
  mkdir -p run
  T=$W/patched-tag/py-hftbacktest/tests/test_partial_fill_312.py
  for v in stock patched; do
    res=$(cd run && "../venv-$v/bin/python" -I "$T" 2>&1 | tail -1 || true)
    echo "python tests on $v: $res"
    set +e
    (cd run && "../venv-$v/bin/python" -I "$HERE/repro_312.py")
    rc=$?
    set -e
    echo "repro_312.py on $v: exit $rc"
    if [ "$v" = stock ]; then
      [ "$res" = "FAILED (failures=4)" ] && [ $rc = 3 ] || fail=1
    else
      [ "$res" = "OK" ] && [ $rc = 0 ] || fail=1
    fi
  done
  echo "patched wheel: $(ls wheel/*.whl) sha256 $(sha wheel/*.whl)"

  say "synthetic regression, driver parity and #316 (evidence/)"
  rm -rf synth && mkdir synth
  "$W/venv-stock/bin/python" -I "$HERE/../evidence/synthetic_regression.py" --stock "$W/venv-stock/bin/python" \
    --patched "$W/venv-patched/bin/python" --work "$W/synth" --out "$W/synth/summary.json" >/dev/null || fail=1
  "$W/venv-stock/bin/python" - "$W/synth/summary.json" <<'EOF2' || fail=1
import json, sys
s = json.load(open(sys.argv[1]))
b, p = s["builds"], s["hbt_driver_vs_per_row_driver"]
got = (b["runs"], b["stock_stopped"], b["stock_error_codes"], b["patched_stopped"], b["unstopped_runs_identical"],
       b["unstopped_runs_changed"], b["stopped_runs_with_fills_a_prefix_of_patched"],
       p["stock"]["identical"], p["patched"]["identical"])
want = (2988, 177, {"14": 177}, 0, 2811, 0, 177, 2988, 2988)
print("synthetic regression:", got, "(expected", want, ")")
sys.exit(0 if got == want else 1)
EOF2
  for v in stock patched; do
    pos=$(cd run && "../venv-$v/bin/python" -I "$HERE/../evidence/pos316.py" | tail -1)
    echo "#316 on $v: $pos"
    printf '%s' "$pos" | grep -q '"position": 1.0' || fail=1
  done
fi

if [ $fail = 0 ]; then say "all expectations met"; else say "EXPECTATION MISMATCH (see above)"; exit 1; fi
