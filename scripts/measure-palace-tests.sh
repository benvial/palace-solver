#!/usr/bin/env bash
# Throwaway measurement for ticket 03 of .scratch/palace-tests: build Palace's
# unit-tests on a warm tree, install them into the prefix, run the sweeps.
# Never merged. Runs where the build ran: inside the manylinux container, on
# the macOS runner, or under MSYS2 UCRT64.
#
#   scripts/measure-palace-tests.sh BUILD_ROOT OUT_DIR
#
# Always exits 0; every outcome is recorded under OUT_DIR.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  MINGW* | MSYS*) os=windows ;;
  *) echo "unknown OS $(uname -s)" >&2; exit 0 ;;
esac

if [[ $os == windows ]]; then
  build_root="$(cygpath -u "$1")"
  out="$(cygpath -u "$2")"
  nat() { cygpath -m "$1"; }
  PY="$(cygpath -u "$pythonLocation")/python.exe"
else
  build_root="$1"
  out="$2"
  nat() { printf '%s\n' "$1"; }
  PY=python3
  [[ -x /opt/python/cp312-cp312/bin/python3 ]] && PY=/opt/python/cp312-cp312/bin/python3
fi
mkdir -p "$out"
superbuild="$build_root/superbuild"
palace_build="$superbuild/palace-build"
prefix="$build_root/install"
test_dir="$palace_build/test/unit"
helper="$(nat "$repo_root/scripts/measure_palace_tests.py")"

export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="*"
export CCACHE_MAXSIZE=2G

case $os in
  linux)
    jobs=$(nproc)
    dnf install -y ccache >/dev/null
    export CCACHE_DIR="$build_root/ccache"
    export PATH="$prefix/bin:$PATH"
    export LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    ;;
  macos)
    jobs=$(sysctl -n hw.logicalcpu)
    export CCACHE_DIR="$build_root/ccache"
    export PATH="$build_root/toolchain-bin:$build_root/venv/bin:$prefix/bin:$PATH"
    ;;
  windows)
    jobs=$(nproc)
    export CCACHE_DIR="$(nat "$build_root/ccache")"
    msmpi_dir="$(cygpath -u "$(cygpath -w "$build_root")-msmpi")"
    export PATH="$build_root/cmake-3.31.8-windows-x86_64/bin:$build_root/posix-python:$prefix/bin:$prefix/lib:$msmpi_dir:$PATH"
    export CMAKE_GENERATOR="MSYS Makefiles"
    export MSMPI_LOCAL_ONLY=1
    ;;
esac

times="$out/times.tsv"
printf 'step\tseconds\texit\n' >"$times"
timed() {
  local name="$1"; shift
  local t0 t1 rc
  t0=$(date +%s)
  "$@" >"$out/$name.log" 2>&1
  rc=$?
  t1=$(date +%s)
  printf '%s\t%s\t%s\n' "$name" "$((t1 - t0))" "$rc" >>"$times"
  echo "==> $name: $((t1 - t0)) s, exit $rc"
  return $rc
}

{
  echo "os=$os jobs=$jobs"
  echo "python=$("$PY" --version 2>&1)"
  cmake --version | head -1
  ctest --version | head -1
  command -v ccache || echo "no ccache"
  grep -E '^MPIEXEC' "$palace_build/CMakeCache.txt"
  grep -E '^PALACE_(TESTS|REGRESSION)' "$palace_build/CMakeCache.txt"
  grep -E '^CMAKE_(C|CXX|Fortran)_COMPILER_LAUNCHER' "$palace_build/CMakeCache.txt"
  if [[ $os == windows ]]; then
    echo "symlinks in the Palace source test tree: $(find "$build_root"/palace-*/test -type l | wc -l)"
  fi
  echo "unit-tests already built: $(ls "$test_dir"/palace-unit-tests* 2>/dev/null || echo no)"
} >"$out/facts.txt" 2>&1
cat "$out/facts.txt"

"$PY" "$helper" snapshot "$(nat "$build_root")" "$(nat "$out/snapshot-before.json")"

# The two commands of upstream's `tests` step, with -j; -k so a Windows compile
# lists every failing site rather than the first.
# macOS: the build-tree palace-unit-tests carries no rpath to the prefix, so
# Catch2's POST_BUILD discovery cannot load @rpath/libparpack.2.dylib and make
# deletes the binary (run 37125409604). Give palace-build a build rpath.
if [[ $os == macos ]]; then
  timed reconfigure-build-rpath cmake -DCMAKE_BUILD_RPATH="$prefix/lib" "$palace_build"
fi
build_ok=0
timed build-unit-tests cmake --build "$(nat "$palace_build")" --target unit-tests -j "$jobs" -- -k && build_ok=1
installed=0
if (( build_ok )); then
  timed install-unit-tests cmake --install "$(nat "$test_dir")" --prefix "$(nat "$prefix")" && installed=1
  # The cost on a later run whose restored tree already holds the tests.
  timed rebuild-unit-tests-noop cmake --build "$(nat "$palace_build")" --target unit-tests -j "$jobs"
fi
grep -nE 'error|Error' "$out/build-unit-tests.log" | grep -v 'Werror' | head -200 >"$out/build-errors.txt" || true

"$PY" "$helper" snapshot "$(nat "$build_root")" "$(nat "$out/snapshot-after-install.json")"
"$PY" "$helper" delta "$(nat "$out/snapshot-before.json")" "$(nat "$out/snapshot-after-install.json")" \
  "$(nat "$out/delta-install")"

if (( installed )); then
  echo yes >"$out/installed"
  ls -la "$prefix/bin" "$prefix/share/palace/test" >"$out/prefix-listing.txt" 2>&1
  ctest --test-dir "$(nat "$test_dir")" -N >"$out/ctest-N.txt" 2>&1
  ctest --test-dir "$(nat "$test_dir")" --show-only=json-v1 >"$out/ctest.json" 2>/dev/null
  sweep() {
    local name="$1"; shift
    timed "$name" ctest --test-dir "$(nat "$test_dir")" --output-on-failure \
      --output-junit "$(nat "$out/$name.xml")" -j "$jobs" "$@"
  }
  sweep unit-serial -R '^serial-' --timeout 900
  sweep unit-mpi -R '^mpi-' --timeout 900
  sweep regression-2ranks -L '^regression$' --timeout 1800
  timed regression-serial "$PY" "$helper" serial-regression "$(nat "$out/ctest.json")" \
    "$(nat "$out/regression-serial.json")" "$jobs" 1800
  "$PY" "$helper" snapshot "$(nat "$build_root")" "$(nat "$out/snapshot-after-sweeps.json")"
  "$PY" "$helper" delta "$(nat "$out/snapshot-after-install.json")" "$(nat "$out/snapshot-after-sweeps.json")" \
    "$(nat "$out/delta-sweeps")"
fi
rm -f "$out"/snapshot-*.json
exit 0
