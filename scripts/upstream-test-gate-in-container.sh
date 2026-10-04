#!/usr/bin/env bash
# Run one sweep of the upstream test gate in the manylinux_2_28 container the
# tree was built in, as scripts/build-in-container.sh runs the build.
#
#   scripts/upstream-test-gate-in-container.sh units|regression
#
# The report lands in $BUILD_CACHE/gate (./.build-cache/gate by default).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MANYLINUX_IMAGE:-quay.io/pypa/manylinux_2_28_x86_64}"
cache_dir="${BUILD_CACHE:-$repo_root/.build-cache}"

exec docker run --rm \
  -v "$repo_root:/repo" \
  -v "$cache_dir:/build" \
  -e BUILD_ROOT=/build \
  -e JOBS="${JOBS:-$(nproc)}" \
  -w /repo \
  "$image" \
  /repo/scripts/upstream-test-gate.sh "$1" /build/gate
