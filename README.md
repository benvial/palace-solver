# palais-solver

The [Palace](https://github.com/awslabs/palace) 3D finite-element
electromagnetics solver, packaged as a Linux binary wheel.

```bash
pip install palais-solver
palace config.json
palace-mpiexec -n 4 palace config.json
```

Installing this package is what `pip install palais[solver]` does for you; it
is the workstation path to a working solver — no conda, no docker, no compiler.
Clusters keep building Palace themselves and point palais at that build with
an explicit executable argument or `PALAIS_PALACE_EXE`.

## What the wheel contains

- `palace-real`, the Palace binary, built with the full feature set: OpenMP,
  SuperLU_DIST, STRUMPACK (with ZFP), MUMPS, SLEPc, ARPACK, LIBXSMM and GSLIB.
  No GPU support, 32-bit integers.
- Every shared library that build needs, vendored by `auditwheel` — including
  MPICH (with Hydra) and OpenBLAS, neither of which the manylinux image
  provides.
- `THIRD-PARTY-NOTICES`, harvested from the superbuild's own source checkouts.

The package version mirrors the Palace release it ships (`.postN` for
packaging-only fixes). Palace is Apache-2.0; see `LICENSE` and
`THIRD-PARTY-NOTICES`.

## MPI

The wheel carries its own MPICH, built from source with the Fortran bindings
Palace needs for MUMPS, ARPACK and STRUMPACK — the PyPI
[`mpich`](https://pypi.org/project/mpich/) wheel is C-only and cannot build
them (see `docs/adr/0002-vendor-mpich-in-the-solver-wheel.md`). Nothing else
needs installing, and `palace-mpiexec` is the vendored Hydra launcher.

MPICH is pinned to the version palais depends on (`mpich<5`), so Palace also
runs correctly when started by an `mpiexec` from that wheel. Palace is a
separate process, so its MPI never shares an address space with the one
`mpi4py` uses.

`palace-mpiexec` is the supported launcher, and single-node runs are the
supported shape — clusters keep building Palace themselves. Another process
manager works fine, whatever MPICH it is; what the solver refuses is a rank
that a process manager started **without an MPI rendezvous** — none of
`PMI_FD`, `PMI_PORT`, `PMI_RANK`, `PMIX_RANK`, `PMIX_NAMESPACE`,
`PMIX_SERVER_URI` or `OMPI_COMM_WORLD_RANK` passed down. That case is
worth stopping because it does not fail: every rank would initialise as its own
`MPI_COMM_WORLD`, solve the whole problem alone, overwrite the others' output
and exit 0. `PALAIS_SOLVER_ALLOW_FOREIGN_LAUNCHER=1` runs anyway. The reasoning
is in `docs/adr/0004-the-vendored-launcher-is-the-supported-one.md`.

The check belongs to the `palace` console script. Code that launches
`binary_path()` itself should call `palais_solver.launcher_conflict()` before
spawning ranks to make the same check.

## Python API

```python
import palais_solver

palais_solver.binary_path()  # -> .../site-packages/palais_solver/bin/palace-real
palais_solver.lib_dir()  # -> .../site-packages/palais_solver/lib
palais_solver.launcher_conflict()  # -> None, or why this launcher is refused
```

## Building the wheel

The wheel is built inside a `manylinux_2_28` container. Locally:

```bash
scripts/build-in-container.sh 0.17.0      # docker, caches in ./.build-cache
scripts/smoke-test.sh wheelhouse/*.whl CONFIG   # clean venv, 1 rank and -n 2
scripts/interop-test.sh wheelhouse/*.whl CONFIG # real solve under both launchers
```

`scripts/interop-test.sh` solves a real example on two ranks under both
`palace-mpiexec` and the `mpiexec` from the PyPI `mpich` wheel, requires the
results to agree, checks that a rank launched without a rendezvous is refused,
and checks that a launcher from the next MPICH major series is not. `python -m wheelbuild.pin_check` checks the vendored MPICH against
the `mpich` pin recorded for palais; pass `--palais <checkout>` to also
check that palais still declares it — CI cannot, so that half is a release-
time step.

`scripts/build-wheel.sh` is the in-container pipeline: MPICH → OpenBLAS → superbuild →
notice harvest → wheel assembly → `auditwheel repair` → retag to
`py3-none-manylinux_2_28_x86_64`. The steps are Python modules under
`wheelbuild/` and are unit-tested with `pytest`.

Linux aarch64 and macOS are later milestones.

## Releasing

Tag the commit with the Palace release the wheel ships (`v0.17.0`, or
`v0.17.0.post1` for a packaging-only fix). The tag runs the wheel job and
publishes to PyPI through trusted publishing — no API token lives in the
repository. If the repaired wheel exceeds PyPI's 100 MB per-file limit (the
build prints its size), request a limit increase with that concrete wheel
before the first upload.
