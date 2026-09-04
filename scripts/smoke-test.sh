#!/usr/bin/env bash
# Install the built wheel into a clean venv and prove the binary runs.
#
#   scripts/smoke-test.sh WHEEL PALACE_CONFIG
#
# Checks: the wheel is self-contained (it vendors MPICH, so nothing else needs
# installing), every shared library resolves, pypalace_solver.binary_path()
# finds the payload, and Palace runs PALACE_CONFIG (--dry-run: config parsing,
# mesh partitioning and FE space setup, no solve) on one rank and on two ranks
# under the vendored launcher.
set -euo pipefail

wheel="${1:?usage: smoke-test.sh WHEEL PALACE_CONFIG}"
config="${2:?usage: smoke-test.sh WHEEL PALACE_CONFIG}"
venv="$(mktemp -d)/venv"

echo "==> the wheel vendors MPICH"
# Read the archive with Python: `unzip -l | grep -q` exits early, SIGPIPEs
# unzip, and trips `set -o pipefail`.
python3 - "$wheel" <<'PYTHON'
import sys, zipfile

names = zipfile.ZipFile(sys.argv[1]).namelist()
for library in ("libmpi", "libmpifort", "libopenblas"):
    if not any(library in name for name in names):
        sys.exit(f"ERROR: {library} is missing from the wheel")
for executable in ("bin/palace-real", "bin/mpiexec"):
    if not any(name.endswith(executable) for name in names):
        sys.exit(f"ERROR: {executable} is missing from the wheel")
PYTHON

python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
# Nothing but the wheel: it must bring its own MPI.
"$venv/bin/pip" install --quiet "$wheel"

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

echo "==> two ranks, under the vendored process manager"
"$venv/bin/palace-mpiexec" -n 2 "$venv/bin/palace" --dry-run "$config"

echo "==> smoke test passed"
