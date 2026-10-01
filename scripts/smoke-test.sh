#!/usr/bin/env bash
# Install the built wheel into a clean venv and prove the binary runs.
#
#   scripts/smoke-test.sh WHEEL PALACE_CONFIG
#
# Checks: the wheel is self-contained (it vendors MPICH, so nothing else needs
# installing), every shared library resolves, palace_solver.binary_path()
# finds the payload, and Palace runs PALACE_CONFIG (--dry-run: config parsing,
# mesh partitioning and FE space setup, no solve) on one rank and on two ranks
# under the vendored launcher.
#
# scripts/interop-test.sh carries this further, into a real solve under a
# launcher that did not ship with the wheel.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_wheel_venv.sh"

# Resolved before the working directory moves: the checks below run from a
# temporary directory, and wheelbuild is imported from the checkout rather than
# from the wheel under test.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wheel="$(realpath "${1:?usage: smoke-test.sh WHEEL PALACE_CONFIG}")"
config="$(realpath "${2:?usage: smoke-test.sh WHEEL PALACE_CONFIG}")"
workdir="$(mktemp -d)"
venv="$workdir/venv"
cd "$workdir"

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

# Nothing but the wheel: it must bring its own MPI.
make_wheel_venv "$venv" "$wheel"

binary="$("$venv/bin/python" -c 'import palace_solver; print(palace_solver.binary_path())')"
echo "==> packaged binary: $binary"

echo "==> shared library resolution"
# `ldd` is GNU and macOS ships none, so the question is asked through
# wheelbuild.link_check, which knows what each platform's tool answers.
#
# Three arguments, because three things can be wrong. The solver, where
# --require-mpi also insists it linked MPI at all; every other executable in the
# payload, because the repair tool decides what counts as a library and a process
# manager it treated as data keeps the install names it was linked with; and the
# vendored libraries themselves, which have dependencies of their own and are the
# half of the payload the repair tool actually rewrote. Where they live differs
# per platform, so the installed package is asked rather than told -- which is
# what palace_solver.lib_dir() is for, and a directory that is missing there is a
# repair that put them somewhere else.
libraries="$("$venv/bin/python" -c 'import palace_solver; print(palace_solver.lib_dir())')"
PYTHONPATH="$repo_root" python3 -m wheelbuild.link_check --require-mpi "$binary"
PYTHONPATH="$repo_root" python3 -m wheelbuild.link_check \
  "$(dirname "$binary")" "$libraries"

example_dir="$workdir/example"
copy_example "$config" "$example_dir"
cd "$example_dir"

echo "==> single rank"
"$venv/bin/palace" --dry-run "$(basename "$config")"

echo "==> two ranks, under the vendored process manager"
"$venv/bin/palace-mpiexec" -n 2 "$venv/bin/palace" --dry-run "$(basename "$config")"

echo "==> two ranks through the wrapper's own --np"
# palais drives the solver this way rather than by calling a launcher itself,
# so the console script has to spawn the vendored process manager on its own.
# Rank 0 alone prints the dry-run line; twice would mean the ranks never found
# each other.
np_log="$workdir/np.log"
"$venv/bin/palace" --np 2 --dry-run "$(basename "$config")" >"$np_log" 2>&1
if [[ "$(grep -c "^Dry-run:" "$np_log")" != "1" ]]; then
  tail -20 "$np_log" >&2
  echo "ERROR: --np 2 did not form one MPI_COMM_WORLD of 2 ranks" >&2
  exit 1
fi

echo "==> smoke test passed"
