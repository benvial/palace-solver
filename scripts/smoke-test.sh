#!/usr/bin/env bash
# Install the built wheel into a clean venv and prove the binary runs.
#
#   scripts/smoke-test.sh WHEEL PALACE_CONFIG
#
# Checks: the wheel vendors no MPI, libmpi resolves into the mpich wheel,
# pypalace_solver.binary_path() finds the payload, and Palace runs PALACE_CONFIG
# (--dry-run: config parsing, mesh partitioning and FE space setup, no solve)
# on one rank and under `mpiexec -n 2`.
set -euo pipefail

wheel="${1:?usage: smoke-test.sh WHEEL PALACE_CONFIG}"
config="${2:?usage: smoke-test.sh WHEEL PALACE_CONFIG}"
venv="$(mktemp -d)/venv"

echo "==> no MPI vendored into the wheel"
if unzip -l "$wheel" | grep -E "libmpi"; then
  echo "ERROR: the wheel vendors an MPI implementation" >&2
  exit 1
fi

python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
"$venv/bin/pip" install --quiet "mpich<5" "$wheel"

binary="$("$venv/bin/python" -c 'import pypalace_solver; print(pypalace_solver.binary_path())')"
echo "==> packaged binary: $binary"

echo "==> shared library resolution"
if ldd "$binary" | grep -q "not found"; then
  ldd "$binary" | grep "not found"
  echo "ERROR: unresolved shared libraries" >&2
  exit 1
fi
ldd "$binary" | grep -E "libmpi" || {
  echo "ERROR: binary does not link libmpi" >&2
  exit 1
}

echo "==> single rank"
"$venv/bin/palace" --dry-run "$config"

echo "==> two ranks"
"$venv/bin/mpiexec" -n 2 "$venv/bin/palace" --dry-run "$config"

echo "==> smoke test passed"
