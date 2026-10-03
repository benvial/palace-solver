#!/usr/bin/env bash
# Run one sweep of the upstream test gate: Palace's own test suite, as built
# and installed by the superbuild (wheelbuild/superbuild.py).
#
#   scripts/upstream-test-gate.sh units|regression OUT_DIR
#
# Runs where the build ran: inside the manylinux container on Linux, on the
# runner on macOS. Windows does not build the tests yet.
#
# Environment:
#   BUILD_ROOT   the build root the driver used (default /build)
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
build_root="${BUILD_ROOT:-/build}"
prefix="$build_root/install"
test_dir="$build_root/superbuild/palace-build/test/unit"

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
  *)
    echo "the upstream test gate does not run on $(uname -s) yet" >&2
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
ctest --test-dir "$test_dir" --show-only=json-v1 >"$out/tests.json"
report="$out/$sweep.xml"
rm -f "$report"
ctest --test-dir "$test_dir" "${selection[@]}" -j "$jobs" --timeout "$timeout" \
  --output-on-failure --output-junit "$report" || true
if [[ ! -s "$report" ]]; then
  echo "ctest wrote no report to $report" >&2
  exit 1
fi
