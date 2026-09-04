#!/usr/bin/env bash
# Run the wheel build in a manylinux_2_28 container on this machine.
#
#   scripts/build-in-container.sh [PALACE_VERSION]
#
# The build cache (sources, superbuild tree, ccache) lives in ./.build-cache so
# a second run skips the 30-60 minute dependency compile.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MANYLINUX_IMAGE:-quay.io/pypa/manylinux_2_28_x86_64}"
cache_dir="${BUILD_CACHE:-$repo_root/.build-cache}"
mkdir -p "$cache_dir"

exec docker run --rm \
  -v "$repo_root:/repo" \
  -v "$cache_dir:/build" \
  -e BUILD_ROOT=/build \
  -e JOBS="${JOBS:-$(nproc)}" \
  -w /repo \
  "$image" \
  /repo/scripts/build-wheel.sh "$@"
