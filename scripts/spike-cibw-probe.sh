#!/usr/bin/env bash
# Spike probe (wayfinder ticket 06), run as cibuildwheel's `before-all`.
#
# Answers the cheap half of the question in minutes rather than in a
# superbuild: does a write the container makes into the /host mount land on
# the runner's filesystem, and can actions/cache save and restore the result
# when the container wrote it as root?
#
# Writes two trees, kept in separate cache entries so one failing does not
# hide the other:
#
#   /host/hostprobe         world-readable, the shape a normal build tree has
#   /host/hostprobe-strict   root-owned 0600 file inside a 0700 directory,
#                            the shape that would defeat a non-root tar
set -euo pipefail

friendly=/host/hostprobe
strict=/host/hostprobe-strict
run="${GITHUB_RUN_ID:-local}"

echo "==> container identity"
id
echo "umask: $(umask)"
cat /etc/os-release | head -2

echo "==> what the /host mount already holds (restored from cache, if anything)"
for tree in "$friendly" "$strict"; do
  echo "--- $tree"
  if [[ -d "$tree" ]]; then
    ls -la "$tree" || echo "(cannot list: $?)"
    find "$tree" -maxdepth 2 -printf '%M %u:%g %10s %TY-%Tm-%TdT%TH:%TM %p\n' 2>&1 | head -20
    if [[ -f "$tree/marker.txt" ]]; then
      echo "marker from the previous run:"
      cat "$tree/marker.txt"
    fi
    if [[ -f "$tree/payload.bin" ]]; then
      echo "payload checksum: $(md5sum "$tree/payload.bin")"
    fi
  else
    echo "(absent)"
  fi
done

echo "==> writing the friendly tree"
mkdir -p "$friendly/nested"
printf 'written by run %s at %s\n' "$run" "$(date -Is)" >"$friendly/marker.txt"
# 256 MB of incompressible bytes: enough for the cache to be a real upload and
# for a truncated restore to show up in the checksum.
dd if=/dev/urandom of="$friendly/payload.bin" bs=1M count=256 status=none
md5sum "$friendly/payload.bin" >"$friendly/payload.md5"
# A symlink and an executable, because a build tree has both.
ln -sfn payload.bin "$friendly/payload.link"
printf '#!/bin/sh\necho hello\n' >"$friendly/nested/run.sh"
chmod 0755 "$friendly/nested/run.sh"

echo "==> writing the strict tree (root-owned, no group or other access)"
mkdir -p "$strict"
chmod 0700 "$strict"
printf 'run %s\n' "$run" >"$strict/secret.txt"
chmod 0600 "$strict/secret.txt"

echo "==> final state as the container sees it"
find "$friendly" "$strict" -printf '%M %u:%g %10s %p\n'
df -h /host
