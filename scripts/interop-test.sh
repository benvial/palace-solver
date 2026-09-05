#!/usr/bin/env bash
# Prove the packaged solver interoperates with palais's MPI process manager.
#
#   scripts/interop-test.sh WHEEL PALACE_CONFIG
#
# The wheel vendors its own MPICH, but palais installs the PyPI `mpich` wheel
# for mpi4py, so a user can start the solver with either launcher. This runs a
# real solve — not `--dry-run`, so collective communication is exercised — on
# two ranks under each launcher and requires the two to agree. Stage 3 checks
# that a rank launched with no MPI rendezvous is refused rather than left to
# solve the problem alone; stage 4 checks that a launcher from the next MPICH
# major series is *not* refused, since that pairing works. See
# docs/adr/0004-the-vendored-launcher-is-the-supported-one.md.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_wheel_venv.sh"

wheel="$(realpath "${1:?usage: interop-test.sh WHEEL PALACE_CONFIG}")"
config="$(realpath "${2:?usage: interop-test.sh WHEEL PALACE_CONFIG}")"
workdir="$(mktemp -d)"
venv="$workdir/venv"
cd "$workdir"

# "mpich<5" is the pin palais declares, so this is the pairing a user of
# `pip install palais[solver]` actually gets.
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

echo "==> stage 3: a rank launched without an MPI rendezvous is refused"
# Reproduce the silent failure: strip the PMI variables from an otherwise
# normal two-rank launch. Without the guard each rank initialises as its own
# MPI_COMM_WORLD, solves the whole problem alone and exits 0.
guard_log="$workdir/guard.log"
guard_dir="$workdir/guard"
copy_example "$config" "$guard_dir"
if (
  cd "$guard_dir"
  "$vendored_launcher" -n 2 env -u PMI_RANK -u PMI_SIZE -u PMI_FD \
    "$venv/bin/palace" --dry-run "$(basename "$config")"
) >"$guard_log" 2>&1; then
  tail -20 "$guard_log" >&2
  echo "ERROR: ranks ran without a rendezvous instead of being refused" >&2
  exit 1
fi
grep -q "palace-mpiexec" "$guard_log" || {
  tail -20 "$guard_log" >&2
  echo "ERROR: the launcher guard did not name the supported launcher" >&2
  exit 1
}
echo "    refused, and pointed at palace-mpiexec"

echo "==> stage 4: a launcher from the next MPICH major series still runs"
# The guard is on the rendezvous, not on the version: this pairing is
# unsupported but works, and must not be blocked.
major_venv="$workdir/major-venv"
install_log="$workdir/major-install.log"
if make_wheel_venv "$major_venv" "$wheel" "mpich>=5" >"$install_log" 2>&1; then
  echo "    launcher: $(launcher_version "$major_venv/bin/mpiexec")"
  major_log="$workdir/major.log"
  major_dir="$workdir/major"
  copy_example "$config" "$major_dir"
  (
    cd "$major_dir"
    "$major_venv/bin/mpiexec" -n 2 "$major_venv/bin/palace" --dry-run \
      "$(basename "$config")"
  ) >"$major_log" 2>&1 || {
    tail -20 "$major_log" >&2
    echo "ERROR: a working cross-major launch was refused or failed" >&2
    exit 1
  }
  # Rank 0 alone prints it; twice would mean the ranks did not find each other.
  if [[ "$(grep -c "^Dry-run:" "$major_log")" != "1" ]]; then
    tail -20 "$major_log" >&2
    echo "ERROR: the ranks did not form one MPI_COMM_WORLD" >&2
    exit 1
  fi
  grep -q "note:" "$major_log" || {
    echo "ERROR: no version remark was printed for a cross-major launcher" >&2
    exit 1
  }
  echo "    ran, one MPI_COMM_WORLD, with a version remark on stderr"
elif grep -q "No matching distribution found" "$install_log"; then
  # No mpich>=5 wheel published yet: nothing to check this against, and that is
  # the only install failure this stage may pass over.
  echo "    skipped: no mpich>=5 wheel available"
else
  tail -20 "$install_log" >&2
  echo "ERROR: could not build the cross-major launcher environment" >&2
  exit 1
fi

echo "==> interop test passed"
