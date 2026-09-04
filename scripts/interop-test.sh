#!/usr/bin/env bash
# Prove the packaged solver interoperates with pypalace's MPI process manager.
#
#   scripts/interop-test.sh WHEEL PALACE_CONFIG
#
# The wheel vendors its own MPICH, but pypalace installs the PyPI `mpich` wheel
# for mpi4py, so a user can start the solver with either launcher. This runs a
# real solve — not `--dry-run`, so collective communication is exercised — on
# two ranks under each launcher and requires the two to agree. Stage 3 checks
# the other direction: that a launcher from the next MPICH major series is
# refused. See docs/adr/0004-the-vendored-launcher-is-the-supported-one.md.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_wheel_venv.sh"

wheel="$(realpath "${1:?usage: interop-test.sh WHEEL PALACE_CONFIG}")"
config="$(realpath "${2:?usage: interop-test.sh WHEEL PALACE_CONFIG}")"
workdir="$(mktemp -d)"
venv="$workdir/venv"
cd "$workdir"

# "mpich<5" is the pin pypalace declares, so this is the pairing a user of
# `pip install pypalace[solver]` actually gets.
make_wheel_venv "$venv" "$wheel" "mpich<5"

vendored_launcher="$venv/bin/palace-mpiexec"
foreign_launcher="$venv/bin/mpiexec"
launcher_version() { "$1" --version | awk '/Version:/{print $2}'; }
echo "==> vendored launcher: $(launcher_version "$vendored_launcher")"
echo "==> mpich wheel launcher: $(launcher_version "$foreign_launcher")"

# A failed PMI handshake is silent: each rank would initialise as its own
# MPI_COMM_WORLD, solve the whole problem alone and still exit 0. Palace's
# rank-0 banner names the size of that communicator, so requiring it to say two
# processes exactly once catches both halves of that failure.
run_solve() {
  local name="$1"
  local launcher="$2"
  local directory="$workdir/$name"
  copy_example "$config" "$directory"
  (
    cd "$directory"
    "$launcher" -n 2 "$venv/bin/palace" "$(basename "$config")" \
      >"$workdir/$name.log" 2>&1
  )
  local banner
  banner=$(grep -c "Running with 2 MPI processes" "$workdir/$name.log" || true)
  if [[ "$banner" != "1" ]]; then
    tail -20 "$workdir/$name.log" >&2
    echo "ERROR: $name did not form one MPI_COMM_WORLD of 2 ranks" >&2
    echo "       (rank-0 banner seen $banner times, expected once)" >&2
    exit 1
  fi
}

echo "==> stage 1: two-rank solve under the vendored launcher"
run_solve vendored "$vendored_launcher"

echo "==> stage 2: two-rank solve under the mpich wheel's launcher"
run_solve foreign "$foreign_launcher"

echo "==> the two launchers produce the same results"
"$venv/bin/python" - "$workdir/vendored" "$workdir/foreign" <<'PYTHON'
import pathlib
import sys

TOLERANCE = 1e-9

vendored, foreign = (pathlib.Path(argument) for argument in sys.argv[1:3])
reports = sorted(vendored.glob("postpro/*.csv"))
if not reports:
    sys.exit(f"ERROR: no postprocessing output under {vendored}")


def numbers(path):
    """Every float in a Palace postprocessing CSV, header row skipped."""
    rows = path.read_text().splitlines()[1:]
    return [float(field) for row in rows for field in row.split(",") if field.strip()]


for report in reports:
    other = foreign / "postpro" / report.name
    if not other.is_file():
        sys.exit(f"ERROR: {report.name} is missing from the foreign-launcher run")
    left, right = numbers(report), numbers(other)
    if len(left) != len(right):
        sys.exit(f"ERROR: {report.name} has a different shape under each launcher")
    for column, (a, b) in enumerate(zip(left, right)):
        if abs(a - b) > TOLERANCE * max(abs(a), abs(b), 1.0):
            sys.exit(
                f"ERROR: {report.name} value {column} differs between launchers: "
                f"{a!r} under the vendored one, {b!r} under the mpich wheel's"
            )
    print(f"    {report.name}: {len(left)} values agree")
PYTHON

echo "==> stage 3: a launcher from the next MPICH major series is refused"
guard_venv="$workdir/guard-venv"
install_log="$workdir/guard-install.log"
if make_wheel_venv "$guard_venv" "$wheel" "mpich>=5" >"$install_log" 2>&1; then
  echo "    launcher: $(launcher_version "$guard_venv/bin/mpiexec")"
  guard_log="$workdir/guard.log"
  if "$guard_venv/bin/mpiexec" -n 2 "$guard_venv/bin/palace" --dry-run "$config" \
      >"$guard_log" 2>&1; then
    tail -20 "$guard_log" >&2
    echo "ERROR: the solver ran under a mismatching launcher instead of refusing" >&2
    exit 1
  fi
  grep -q "palace-mpiexec" "$guard_log" || {
    tail -20 "$guard_log" >&2
    echo "ERROR: the launcher guard did not name the supported launcher" >&2
    exit 1
  }
  echo "    refused, and pointed at palace-mpiexec"
elif grep -q "No matching distribution found" "$install_log"; then
  # No mpich>=5 wheel published yet: there is nothing to test the guard with,
  # and that is the only install failure this stage may pass over.
  echo "    skipped: no mpich>=5 wheel available to test the guard against"
else
  tail -20 "$install_log" >&2
  echo "ERROR: could not build the mismatching-launcher environment" >&2
  exit 1
fi

echo "==> interop test passed"
