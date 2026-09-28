#!/usr/bin/env bash
# Spike (wayfinder ticket 06), run as cibuildwheel's `before-all`.
#
# Drives the production recipe — scripts/build-wheel.sh, stopped just before
# wheel assembly, which cibuildwheel owns — with BUILD_ROOT inside the /host
# mount, so the superbuild reads and writes the runner's /build directory and
# the GitHub Actions cache there is what makes the second run warm.
set -euo pipefail

build_root="${BUILD_ROOT:?BUILD_ROOT must point into /host}"
started=$(date +%s)

# cibuildwheel puts /opt/python/cp39-cp39/bin first on PATH for before-all, so
# `python3` there is 3.9 rather than the container python a plain `container:`
# job gets. The production recipe is written against the latter, so the image's
# own interpreter is put back in front.
export PATH="/usr/local/bin:$PATH"
echo "==> python3 is $(command -v python3) ($(python3 -V 2>&1))"

echo "==> before-all starting at $(date -Is) as $(id -un) in $(pwd)"
echo "==> /host/build before the build"
if [[ -d "$build_root" ]]; then
  find "$build_root" -maxdepth 1 -printf '%M %u:%g %10s %TY-%Tm-%TdT%TH:%TM %p\n'
  du -sh "$build_root" 2>/dev/null || true
else
  echo "(absent — cold build)"
fi
df -h /host

scripts/build-wheel.sh "${PALACE_VERSION:-}"

echo "==> /host/build after the build"
find "$build_root" -maxdepth 1 -printf '%M %u:%g %10s %p\n'
du -sh "$build_root"
# Any file the runner's non-root tar cannot read would break actions/cache/save.
echo "==> entries the runner user could not read (empty is the good answer)"
find "$build_root" \( -type f ! -perm -o+r \) -o \( -type d ! -perm -o+rx \) | head -40
df -h /host

echo "==> before-all finished at $(date -Is), $(( ($(date +%s) - started) / 60 )) minutes"
