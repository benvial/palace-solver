#!/usr/bin/env bash
# Prove palais drives the packaged solver end to end.
#
#   scripts/e2e-test.sh WHEEL [PALAIS_CHECKOUT]
#
# Installs `palais[solver]` into a clean virtual environment, resolving the
# extra's `palace-solver` requirement from the directory holding WHEEL, then
# runs one palais example through the high-level API on two ranks. What this
# adds over smoke-test.sh and interop-test.sh is the caller: palais resolves
# the executable itself and drives it with upstream Palace's wrapper options
# (`--np`), so the whole path a user of `pip install palais[solver]` takes is
# exercised.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_wheel_venv.sh"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wheel="$(realpath "${1:?usage: e2e-test.sh WHEEL [PALAIS_CHECKOUT]}")"
palais_dir="$(realpath "${2:-$repo_root/../palais}")"
[[ -f "$palais_dir/pyproject.toml" ]] || {
  echo "ERROR: $palais_dir is not a palais checkout" >&2
  exit 1
}
workdir="$(mktemp -d)"
venv="$workdir/venv"
cd "$workdir"

echo "==> install palais[solver], resolving the extra from the built wheel"
# The extra pins `palace-solver`, so this also proves the wheel's name and
# version satisfy what palais asks for.
make_wheel_venv "$venv" "--find-links=$(dirname "$wheel")" "$palais_dir[solver]"

echo "==> palais resolves the packaged solver"
"$venv/bin/python" - <<'PYTHON'
import palace_solver
from palais.simulation.solve import PalaceSolver

packaged = palace_solver.executable_path()
solver = PalaceSolver()
if solver.executable_path != str(packaged):
    raise SystemExit(
        f"ERROR: palais resolved {solver.executable_path!r}, "
        f"not the packaged solver at {str(packaged)!r}"
    )
if solver.executable_type != "local":
    raise SystemExit(
        f"ERROR: palais chose the {solver.executable_type} runtime, not the "
        "locally installed solver"
    )
print(f"    {solver.executable_path}")
PYTHON

echo "==> two-rank solve through the high-level API"
cp -r "$palais_dir/data/spheres" "$workdir/spheres"
chmod -R u+w "$workdir/spheres"
cd "$workdir/spheres"
"$venv/bin/python" - <<'PYTHON'
from palais import Electrostatic, Material

sim = Electrostatic("mesh/spheres.msh", l0=0.01)
sim.materials = {"domain": Material(permittivity=1.0)}
sim.terminals = ["sphere_a", "sphere_b"]
sim.ground = "farfield"

result = sim.run(num_procs=2)
# The high-level result wraps the raw one; exit status and stdout live there.
palace = result.palace
if not palace.success or palace.return_code != 0:
    raise SystemExit(f"ERROR: the solve failed\n{palace.report()}")

# A failed PMI handshake is silent: each rank would become its own
# MPI_COMM_WORLD, solve the whole problem alone and still exit 0. Rank 0 alone
# prints the banner, so seeing it exactly once is what rules that out.
banner = palace.stdout.count("Running with 2 MPI processes")
if banner != 1:
    raise SystemExit(
        f"ERROR: the ranks did not form one MPI_COMM_WORLD of 2 "
        f"(rank-0 banner seen {banner} times, expected once)"
    )

capacitance = result.capacitance
if capacitance.shape != (2, 2):
    raise SystemExit(f"ERROR: capacitance matrix has shape {capacitance.shape}")
if not capacitance.to_numpy().all():
    raise SystemExit(f"ERROR: capacitance matrix holds a zero\n{capacitance}")
print(f"    capacitance (F):\n{capacitance}")
PYTHON

echo "==> end-to-end test passed"
