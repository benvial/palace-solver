#!/usr/bin/env bash
# Prove a superbuild install prefix holds a Palace that runs.
#
#   scripts/verify-install.sh INSTALL_PREFIX PALACE_VERSION PALACE_CONFIG
#
# The install prefix is what the build produces and the wheel is later cut
# from, so this is the earliest point at which "it built" and "it works" can be
# told apart. It asks the two questions a wheel would: does the binary report
# the Palace it was supposed to be stamped with, and do two ranks under the
# vendored process manager solve one problem together rather than the same
# problem twice.
#
# scripts/smoke-test.sh and scripts/interop-test.sh ask more of the wheel; this
# asks the minimum of a platform that has no wheel yet.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_wheel_venv.sh"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="$(cd "${1:?usage: verify-install.sh INSTALL_PREFIX PALACE_VERSION PALACE_CONFIG}" && pwd)"
palace_version="${2:?usage: verify-install.sh INSTALL_PREFIX PALACE_VERSION PALACE_CONFIG}"
config_argument="${3:?usage: verify-install.sh INSTALL_PREFIX PALACE_VERSION PALACE_CONFIG}"
# Spelled out rather than with realpath, which is GNU coreutils: the callers of
# this one are a macOS runner and a Mac owner.
config="$(cd "$(dirname "$config_argument")" && pwd)/$(basename "$config_argument")"
workdir="$(mktemp -d)"

binary="$(PYTHONPATH="$repo_root" python3 -c '
import pathlib, sys
from wheelbuild.assemble import find_palace_binary

print(find_palace_binary(pathlib.Path(sys.argv[1])))
' "$prefix")"
echo "==> installed binary: $binary"

echo "==> version stamp"
# UNKNOWN here means the release tag never reached a reconfigure, which is
# silent everywhere else and ships a wheel that cannot say what it contains.
if ! "$binary" --version | grep -q "Palace version: v$palace_version"; then
  "$binary" --version >&2
  echo "ERROR: the installed Palace is not stamped v$palace_version" >&2
  exit 1
fi

echo "==> two-rank solve under the vendored process manager"
# A real solve, not --dry-run, so collective communication is exercised. A
# failed PMI rendezvous is silent: each rank would initialise as its own
# MPI_COMM_WORLD, solve the whole problem alone and still exit 0, so the
# rank-0 banner naming two processes exactly once is what rules that out.
example_dir="$workdir/example"
copy_example "$config" "$example_dir"
log="$workdir/solve.log"
# The status is captured rather than left to `set -e`: a solve that exits
# non-zero is the case whose output matters most, and dying here would take the
# log with it.
status=0
(
  cd "$example_dir"
  "$prefix/bin/mpiexec" -n 2 "$binary" "$(basename "$config")" >"$log" 2>&1
) || status=$?
if (( status != 0 )); then
  tail -20 "$log" >&2
  echo "ERROR: the two-rank solve exited $status" >&2
  exit 1
fi
banner=$(grep -c "Running with 2 MPI processes" "$log" || true)
if [[ "$banner" != "1" ]]; then
  tail -20 "$log" >&2
  echo "ERROR: the solve did not form one MPI_COMM_WORLD of 2 ranks" >&2
  echo "       (rank-0 banner seen $banner times, expected once)" >&2
  exit 1
fi

echo "==> install verified"
