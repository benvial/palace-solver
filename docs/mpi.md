# MPI, launchers and the rendezvous check

The wheel carries its own MPICH, built from source with the Fortran bindings
Palace needs for MUMPS, ARPACK and STRUMPACK — the PyPI
[`mpich`](https://pypi.org/project/mpich/) wheel is C-only and cannot build
them (see `docs/adr/0002-vendor-mpich-in-the-solver-wheel.md`). Nothing else
needs installing, and `palace-mpiexec` is the vendored Hydra launcher.

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
