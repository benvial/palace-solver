# palace-solver


[![Build](https://img.shields.io/github/actions/workflow/status/benvial/palace-solver/wheels.yml?style=for-the-badge&logo=github&label=build)](https://github.com/benvial/palace-solver/actions/workflows/wheels.yml)
[![PYPI](https://img.shields.io/pypi/v/palace-solver?style=for-the-badge&logo=pypi&logoColor=white)](https://pypi.org/project/palace-solver)
[![LICENSE](https://img.shields.io/pypi/l/palace-solver?style=for-the-badge&logo=readthedocs&logoColor=white&color=blue)](https://github.com/benvial/palace-solver/blob/main/LICENSE)


The [Palace](https://github.com/awslabs/palace) 3D finite-element
electromagnetics solver, packaged as a binary wheel.

```bash
pip install palace-solver
palace config.json
palace-mpiexec -n 4 palace config.json
```

That is the whole installation: no conda, no docker, no compiler, and no MPI to
install first. The wheel carries the solver and every shared library it needs.

## Supported platforms

A release carries one wheel per platform and `pip` installs the one that
matches:

- **Linux x86-64** — `manylinux_2_28_x86_64`; glibc 2.28 or newer, which is
  RHEL 8, Debian 10, Ubuntu 20.04 and anything later. Any x86-64 CPU.
- **Linux 64-bit arm** — `manylinux_2_28_aarch64`; the same glibc floor, and
  any ARMv8-A core or later. That includes Graviton2 and every later arm
  server core, and needs neither SVE nor an ARMv8.1+ extension.
- **macOS 15.0 or later, Apple Silicon** — `macosx_15_0_arm64`. The floor is
  the oldest macOS the payload is compiled for, and it comes from the runtime
  libraries the wheel vendors rather than from a preference. There is no Intel
  macOS wheel: upstream Palace stopped building that target in November 2024.

Anything outside that set gets `No matching distribution found` from pip, which
is the intended answer — the alternative is a wheel that installs and then
faults. Windows, musl-based Linux and GPU builds are not planned, and a cluster
is better served by building Palace itself.

## What the wheel contains

- `palace-real`, the Palace binary, built with the full feature set: OpenMP,
  SuperLU_DIST, STRUMPACK (with ZFP), MUMPS, SLEPc, ARPACK, LIBXSMM and GSLIB.
  No GPU support, 32-bit integers.
- Every shared library that build needs, MPICH (with Hydra) and OpenBLAS
  included, vendored by the platform's repair tool.
- `THIRD-PARTY-NOTICES`, harvested from the superbuild's own source checkouts.

The CPU requirement is the one stated above and no more: OpenBLAS is built with
`DYNAMIC_ARCH` and picks its kernels at run time, so a newer CPU is used
through that dispatch rather than by installing a different wheel.

The package version mirrors the Palace release it ships, with a `.postN`
segment for packaging-only fixes. Palace is Apache-2.0; see `LICENSE` and
`THIRD-PARTY-NOTICES`.

## Command line

`palace` takes the same options as the wrapper script upstream Palace ships, so
anything written against upstream works unchanged:

```bash
palace --np 4 config.json              # four ranks under the vendored launcher
palace --nt 2 --np 4 config.json       # ... two OpenMP threads each
palace --serial config.json            # no MPI launcher at all
palace --launcher mpirun --np 4 config.json
palace --help
```

Two differences from upstream's wrapper: the default launcher is the `mpiexec`
vendored here rather than a `mpirun` found on `PATH`, and the ranks are started
as the `palace` console script rather than as the raw binary, so each of them
runs the launcher check. `--nt` sets `OMP_NUM_THREADS`, and leaving it unset
means one thread rather than one per core, as upstream.

## MPI

The wheel vendors its own MPICH, built with the Fortran bindings Palace needs,
and `palace-mpiexec` is its Hydra launcher. That launcher, on a single node, is
the supported shape. Another process manager works while it belongs to the same
MPICH major series; what the solver refuses is a rank started **without an MPI
rendezvous**, because that case does not fail — every rank would solve the
whole problem alone, overwrite the others' output and exit 0.
`PALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER=1` runs anyway.

## Python API

```python
import palace_solver

palace_solver.executable_path()  # -> what to launch: the guarded console script
palace_solver.binary_path()  # -> .../site-packages/palace_solver/bin/palace-real
# Where the vendored libraries are, which differs by platform:
# palace_solver.libs beside the package on Linux, palace_solver/.dylibs inside
# it on macOS.
palace_solver.lib_dir()
palace_solver.launcher_conflict()  # -> None, or why this launcher is refused
```

`executable_path()` is what a caller should launch: it returns the guarded
console script, and falls back to the raw binary only when the script cannot be
found. `binary_path()` returns that binary and is unguarded.

## More

- [`docs/mpi.md`](https://github.com/benvial/palace-solver/blob/main/docs/mpi.md) — the vendored MPICH, the launchers it works
  with, and what the rendezvous check reads.
- [`docs/building.md`](https://github.com/benvial/palace-solver/blob/main/docs/building.md) — building the wheel on either
  platform, and the scripts that test one.
- [`docs/releasing.md`](https://github.com/benvial/palace-solver/blob/main/docs/releasing.md) — the release procedure, its dry
  run, and the PyPI limits a release spends.
