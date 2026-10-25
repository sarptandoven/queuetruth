#!/bin/bash
# Runs fuzz targets at low priority: fuzz/run.sh [seconds per target] [target ...] (default 600 s, every target).
# A target that finds a crash does not stop the loop: the summary at the end lists it and the script exits 1.
#
# Environment:
#   QT_FUZZ_CORPUS   working corpus root (default <repo>/.scratch/fuzz-corpus; never committed)
#   QT_FUZZ_EXTRA    extra read-only seed root with <target>/ subdirectories (e.g. local real-data excerpts)
#   QT_FUZZ_TARGET   cargo target directory (default <repo>/.scratch/fuzz-target)
#   QT_FUZZ_JOBS     cargo build jobs (default 3)
#   QT_FUZZ_ARGS     extra libFuzzer flags (default "-fork=1", as in every published run; add -ignore_crashes=1 to keep
#                    fuzzing a target after its first crash)
#   QT_FUZZ_TMP      parent of this run's scratch directory (default <repo>/.scratch/fuzz-tmp). The run creates its
#                    own directory inside it, clears it after each target and removes it on exit; nothing else in
#                    QT_FUZZ_TMP is touched.
# Crashes land in fuzz/artifacts/<target>/ (git-ignored); minimize with
#   cargo +nightly fuzz tmin <target> <crash file>
# and commit the minimized input under fuzz/regressions/<target>/ with a regression test in the fixed crate.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
secs=${1:-600}
shift || true
corpus=${QT_FUZZ_CORPUS:-$repo/.scratch/fuzz-corpus}
target_dir=${QT_FUZZ_TARGET:-$repo/.scratch/fuzz-target}
export CARGO_BUILD_JOBS=${QT_FUZZ_JOBS:-3}
tmp_parent=${QT_FUZZ_TMP:-$repo/.scratch/fuzz-tmp}
mkdir -p "$tmp_parent"
run_tmp=$(mktemp -d "$tmp_parent/run.XXXXXX") || exit 1
export QT_FUZZ_TMP=$run_tmp
trap 'rm -rf "$run_tmp"' EXIT
targets=("$@")
if [ $# -eq 0 ]; then
  while IFS= read -r t; do targets+=("$t"); done < <(cd "$here" && cargo +nightly fuzz list)
fi
cd "$here" || exit 1
failed=()
for t in "${targets[@]}"; do
  mkdir -p "$corpus/$t"
  dirs=("$corpus/$t" "$here/seeds/$t")
  if [ -n "${QT_FUZZ_EXTRA:-}" ] && [ -d "$QT_FUZZ_EXTRA/$t" ]; then dirs+=("$QT_FUZZ_EXTRA/$t"); fi
  if [ -d "$here/regressions/$t" ]; then dirs+=("$here/regressions/$t"); fi
  # The DBN decoder's known up-front metadata allocation (up to 256 MiB) is filtered out of these two targets
  # (qt_fuzz::DBN_KNOWN_ALLOC_BYTES, 16 MiB); the tighter limit catches any other allocation of that class.
  case "$t" in
    dbn | ingest) malloc_mb=64 ;;
    *) malloc_mb=512 ;;
  esac
  echo "== $t (${secs}s, -malloc_limit_mb=$malloc_mb)"
  # shellcheck disable=SC2086 # QT_FUZZ_ARGS is a list of flags
  if ! nice -n 15 cargo +nightly fuzz run --target-dir "$target_dir" "$t" "${dirs[@]}" -- \
    -max_total_time="$secs" -timeout=10 -rss_limit_mb=2048 -malloc_limit_mb="$malloc_mb" -max_len=65536 \
    -print_final_stats=1 ${QT_FUZZ_ARGS--fork=1}; then
    failed+=("$t")
  fi
  rm -rf "${run_tmp:?}"/qt-fuzz-*
done
if [ ${#failed[@]} -gt 0 ]; then
  echo "== targets with findings or errors: ${failed[*]} (see fuzz/artifacts/)"
  exit 1
fi
echo "== all ${#targets[@]} targets clean"
