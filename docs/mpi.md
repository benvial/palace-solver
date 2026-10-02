# MPI, launchers and the rendezvous check

On Linux and macOS the wheel carries its own MPICH, built from source with the
Fortran bindings Palace needs for MUMPS, ARPACK and STRUMPACK — the PyPI
[`mpich`](https://pypi.org/project/mpich/) wheel is C-only and cannot build
them (see `docs/adr/0002-vendor-mpich-in-the-solver-wheel.md`). Nothing else
needs installing, and `palace-mpiexec` is the vendored Hydra launcher. The
Windows wheel vendors MS-MPI instead; see [Windows](#windows).

The vendored MPICH stays inside one major series (`mpich<5`), so Palace also
runs correctly when started by an `mpiexec` from that PyPI wheel. Palace is a
separate process, so its MPI never shares an address space with the one
`mpi4py` uses. `python -m wheelbuild.pin_check` fails CI if the vendored
version ever leaves the series the interoperability test proves, since that
pairing is what the series buys.

## What is refused, and why

`palace-mpiexec` is the supported launcher, and single-node runs are the
supported shape — a cluster is better served by building Palace itself. Another
process manager works fine, whatever MPICH it is. What the solver refuses is a
rank that a process manager started **without an MPI rendezvous** — none of
`PMI_FD`, `PMI_PORT`, `PMI_RANK`, `PMIX_RANK`, `PMIX_NAMESPACE`,
`PMIX_SERVER_URI` or `OMPI_COMM_WORLD_RANK` passed down.

That case is worth stopping because it does not fail: every rank would
initialise as its own `MPI_COMM_WORLD`, solve the whole problem alone,
overwrite the others' output and exit 0.
`PALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER=1` runs anyway. The reasoning is in
`docs/adr/0004-the-vendored-launcher-is-the-supported-one.md`.

The check belongs to the `palace` console script, so that is what a caller
should launch: `palace_solver.executable_path()` returns it, and falls back to
the binary only when it cannot be found. `palace_solver.binary_path()` returns
the raw binary and is unguarded.

## Windows

MPICH has had no native Windows port since 2011, so the Windows wheel vendors
Microsoft's MS-MPI (see `docs/adr/0007-ship-a-windows-wheel.md`).
`msmpi.dll`, `mpiexec.exe` and `smpd.exe` are byte for byte the files of
Microsoft's MS-MPI 10.1.3 redistributable, fetched and hash-checked when the
wheel is built, and are under Microsoft's license terms rather than this
package's; `libmsmpifec.dll`, the gfortran bridge to them, comes from the
MSYS2 toolchain. Nothing needs installing here either.

`palace-mpiexec` is MS-MPI's own `mpiexec`. It runs locally unless `-host`,
`-hosts`, `-machinefile` or an HPC Pack `CCP_NODES` names other machines, and
starts the ranks from an `smpd` manager of its own rather than from a Windows
service. Both console scripts set `MSMPI_LOCAL_ONLY=1` unless it is already
set, so a single-node run, a single rank included, stays off the network.

The `mpiexec` of a system-wide MS-MPI 10.1 install works as well: on a
`windows-2025` runner, MS-MPI 10.1.3's (`mpiexec` reports `10.1.12498.52`)
launching the `palace` console script forms one `MPI_COMM_WORLD` and gives the
same results as `palace-mpiexec`. `python -m wheelbuild.pin_check` holds the
vendored MS-MPI inside 10.1, the series that test proves.

The refusal is the same, read differently. MS-MPI's PMI client decides between
rank and singleton from `PMI_KVS` alone, so that is the only rendezvous
variable on Windows: a rank a process manager starts with `PMI_RANK` but no
`PMI_KVS` — what Intel MPI's `mpiexec` sets — would run as rank 0 of 1, and is
refused with exit status 1 and a message naming `PMI_KVS`. The parent that marks a launched rank
is `smpd`, not `mpiexec`, and the read skips the console script's own `.exe`
launcher, which pip puts between `smpd` and Python. The remark about another
MPICH major series is never printed on Windows: neither MS-MPI's `mpiexec`
nor Intel MPI's answers Hydra's `--version`.

Windows has no `exec`, so both console scripts run their child, wait for it
and exit with its status: a failing run's status passes through either of
them (1 for a missing config file). Ctrl-Break ends `palace --np 2` with
`0xC000013A`. Ctrl-C ends it with `mpiexec`'s own status, `0xFFFFFFFF` (-1,
"job aborted by the user"). Neither leaves a rank running.
