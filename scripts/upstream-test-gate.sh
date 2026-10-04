#!/usr/bin/env bash
# Run one sweep of the upstream test gate: Palace's own test suite, as built
# and installed by the superbuild (wheelbuild/superbuild.py).
#
#   scripts/upstream-test-gate.sh units|regression OUT_DIR
#
# Runs where the build ran: inside the manylinux container on Linux, on the
# runner on macOS, and under MSYS2 UCRT64 bash on Windows, as
# scripts/build-windows.sh does.
#
# Environment:
#   BUILD_ROOT   the build root the driver used (default /build; on Windows a
#                Windows path is accepted, and D:\b is the default)
#   MSMPI_DIR    Windows only: where the driver fetched MS-MPI (default
#                <BUILD_ROOT>-msmpi), whose mpiexec the tests were registered
#                with and whose msmpi.dll they load
#   JOBS         ctest parallelism (default: the core count)
#
# Writes OUT_DIR/tests.json (every registered ctest entry) and
# OUT_DIR/<sweep>.xml (ctest's JUnit report). It exits 0 when ctest ran,
# whatever the tests did: the verdict is `python -m wheelbuild.upstream_gate
# judge`, which knows the test exclusions.
#
# Every entry runs, excluded ones included. ctest gives each its own process,
# so an MFEM abort in one ends only that one.
set -euo pipefail

sweep="${1:?usage: upstream-test-gate.sh units|regression OUT_DIR}"
out="${2:?usage: upstream-test-gate.sh units|regression OUT_DIR}"
case "$(uname -s)" in
  MINGW* | MSYS*)
    build_root="$(cygpath -u "${BUILD_ROOT:-D:\\b}")"
    out="$(cygpath -u "$out")"
    ;;
  *) build_root="${BUILD_ROOT:-/build}" ;;
esac
prefix="$build_root/install"
test_dir="$build_root/superbuild/palace-build/test/unit"
# ctest is native on Windows, and the forward-slash form is the one both it
# and the MSYS2 tools read. Elsewhere a path is already native.
native() { printf '%s\n' "$1"; }

case "$(uname -s)" in
  Linux)
    jobs="${JOBS:-$(nproc)}"
    # The tests load the prefix's libraries, as the build's own test listing
    # did, and the container's toolset runtimes, which is why this runs in
    # the image and not on the runner.
    export LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    ;;
  Darwin)
    jobs="${JOBS:-$(sysctl -n hw.logicalcpu)}"
    # ctest from the CMake the driver pinned inside 3.31, in its venv.
    export PATH="$build_root/venv/bin:$PATH"
    ;;
  MINGW* | MSYS*)
    jobs="${JOBS:-$(nproc)}"
    native() { cygpath -m "$1"; }
    msmpi_dir="$(cygpath -u "${MSMPI_DIR:-$(cygpath -w "$build_root")-msmpi}")"
    # ctest from the CMake scripts/build-windows.sh pinned, in the build root.
    # The tests load the DLLs the build's own test listing did: OpenBLAS from
    # the prefix's bin, libCEED and LIBXSMM from its lib, msmpi.dll, and the
    # GCC runtimes from /ucrt64/bin, already on PATH.
    export PATH="$build_root/cmake-3.31.8-windows-x86_64/bin:$prefix/lib:$msmpi_dir:$PATH"
    # MS-MPI's mpiexec, which the [Parallel] and regression entries run under,
    # then starts its ranks on this machine only.
    export MSMPI_LOCAL_ONLY=1
    ;;
  *)
    echo "the upstream test gate does not run on $(uname -s)" >&2
    exit 1
    ;;
esac
export PATH="$prefix/bin:$PATH"

# Ceilings from ticket 04 of the palace-tests effort. The step that runs this
# sets the sweep's own (5 and 60 minutes); these stop one hung entry from
# spending all of it, and name the entry that hung.
case "$sweep" in
  units) selection=(-LE '^(regression|long)$') timeout=300 ;;
  regression) selection=(-L '^regression$') timeout=1200 ;;
  *)
    echo "unknown sweep $sweep" >&2
    exit 2
    ;;
esac

mkdir -p "$out"
ctest --test-dir "$(native "$test_dir")" --show-only=json-v1 >"$out/tests.json"
report="$out/$sweep.xml"
rm -f "$report"
ctest --test-dir "$(native "$test_dir")" "${selection[@]}" -j "$jobs" --timeout "$timeout" \
  --output-on-failure --output-junit "$(native "$report")" || true
if [[ ! -s "$report" ]]; then
  echo "ctest wrote no report to $report" >&2
  exit 1
fi
