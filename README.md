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
  macOS wheel: upstream Palace stopped building that target in November 2024,
  and the reasoning is in
  `docs/adr/0006-parameterised-raw-jobs-not-cibuildwheel.md`.

Anything outside that set gets `No matching distribution found` from pip, which
is the intended answer — the alternative is a wheel that installs and then
faults. Windows, musl-based Linux and GPU builds are not planned; a cluster
builds Palace itself and points palais at that build.

Installing this package is what `pip install palais[solver]` does for you; it
is the workstation path to a working solver — no conda, no docker, no compiler.
Clusters keep building Palace themselves and point palais at that build with
an explicit executable argument or `PALAIS_PALACE_EXE`.

## What the wheel contains

- `palace-real`, the Palace binary, built with the full feature set: OpenMP,
  SuperLU_DIST, STRUMPACK (with ZFP), MUMPS, SLEPc, ARPACK, LIBXSMM and GSLIB.
  No GPU support, 32-bit integers.
- Every shared library that build needs, vendored by the platform's repair tool
  (`auditwheel` on Linux, `delocate` on macOS) — including
  MPICH (with Hydra) and OpenBLAS, neither of which any of the three build
  platforms provides in a form the wheel could rely on.
- `THIRD-PARTY-NOTICES`, harvested from the superbuild's own source checkouts.

The CPU requirement is the one stated above and no more. OpenBLAS is built
with `DYNAMIC_ARCH`, so it picks its kernels at run time, and the code around
those kernels is compiled for a CPU baseline rather than for the machine that
built it: ARMv8-A on both arm platforms, and on `x86_64` the plain x86-64
architectural level, which the build already leaves in place. A newer CPU is
used through the run-time dispatch, not by installing a different wheel.

The package version mirrors the Palace release it ships (`.postN` for
packaging-only fixes). Palace is Apache-2.0; see `LICENSE` and
`THIRD-PARTY-NOTICES`.

## Command line

`palace` takes the same options as the wrapper script upstream Palace ships,
so anything written against upstream — palais's runner included — works
unchanged:

```bash
palace --np 4 config.json              # four ranks under the vendored launcher
palace --nt 2 --np 4 config.json       # ... two OpenMP threads each
palace --serial config.json            # no MPI launcher at all
palace --launcher mpirun --np 4 config.json
palace --help
```

Two differences from upstream's wrapper. The default launcher is the
`mpiexec` vendored here rather than a `mpirun` found on `PATH`, which would
belong to some other MPI install. And the ranks are started as the `palace`
console script rather than as the raw binary, so each of them runs the
rendezvous check described below.

`--nt` sets `OMP_NUM_THREADS`, and leaving it unset means one thread, not one
per core — as upstream, since anything else oversubscribes a multi-rank run.

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
and exit 0. `PALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER=1` runs anyway. The reasoning
is in `docs/adr/0004-the-vendored-launcher-is-the-supported-one.md`.

The check belongs to the `palace` console script, so that is what a caller
should launch: `executable_path()` returns it, and falls back to the binary
only when it cannot be found. `binary_path()` returns the raw binary and is
unguarded.

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

## Building the wheel

The Linux wheels are built inside a `manylinux_2_28` container. Locally:

```bash
scripts/build-in-container.sh 0.17.0      # docker, caches in ./.build-cache
scripts/smoke-test.sh wheelhouse/*.whl CONFIG   # clean venv, 1 rank and -n 2
scripts/interop-test.sh wheelhouse/*.whl CONFIG # real solve under both launchers
scripts/e2e-test.sh wheelhouse/*.whl [PALAIS]   # palais drives the wheel
```

`scripts/interop-test.sh` solves a real example on two ranks under both
`palace-mpiexec` and the `mpiexec` from the PyPI `mpich` wheel, requires the
results to agree, checks that a rank launched without a rendezvous is refused,
and checks that a launcher from the next MPICH major series is not.

`scripts/e2e-test.sh` installs `palais[solver]` into an empty virtual
environment, resolving the extra from the wheel just built, and runs one palais
example on two ranks through the high-level API — the whole path a user of
`pip install palais[solver]` takes. It needs a palais checkout, so it is a
Linux release-time step: CI cannot reach palais, and on macOS nobody here can
run it at all.

`python -m wheelbuild.pin_check` checks that the vendored MPICH stays inside
the major series the interop test proves the wheel against; it runs in CI on
every push. The end-to-end test needs a palais checkout, which CI has no
access to, so it is a release-time step run locally.

`scripts/build-wheel.sh` is the in-container pipeline: MPICH → OpenBLAS → superbuild →
notice harvest → wheel assembly → `auditwheel repair` → retag to
`py3-none-manylinux_2_28_<arch>`. The steps are Python modules under
`wheelbuild/` and are unit-tested with `pytest`.

On macOS there is no container, so `scripts/build-macos.sh` is both the driver
and the recipe:

```bash
scripts/build-macos.sh 0.17.0             # caches in ~/palace-build
scripts/smoke-test.sh wheelhouse/*.whl CONFIG
scripts/interop-test.sh wheelhouse/*.whl CONFIG
```

It drives the same `wheelbuild/` modules in the same order, writes its wheel to
the same `wheelhouse/`, and differs only where macOS does: one preinstalled
Homebrew GCC for C, C++ and Fortran, an explicit `MACOSX_DEPLOYMENT_TARGET`, a
CMake pinned inside 3.31, and `delocate-wheel` in place of `auditwheel repair`.

Read those macOS instructions as CI's recipe rather than as a tested local
workflow. No Apple Silicon machine is available to this project, so the only
macOS `scripts/build-macos.sh` has ever run on is a `macos-15` GitHub runner. It
is written so a Mac owner can run it — it takes the same arguments as the Linux
driver and caches under `~/palace-build` — but nobody here has, and it expects
the preinstalled Homebrew toolchain a runner image carries rather than
installing one.

The repair tool is also where the platform tag comes from, and the two tools
differ: `auditwheel` is given the tag, while `delocate` derives it from the
largest `minos` in the payload and renames the wheel. So the macOS tag is a
measurement of what was built rather than a value chosen at assembly time, and
the pipeline ends by checking the filename against
`wheelbuild.platforms.platform_tag()` — a wheel tagged for another macOS is a
build failure, not a new floor.

`scripts/verify-install.sh` checks an install prefix the way the smoke test
checks a wheel — the version stamp, and two ranks solving one problem together
— which is useful when a build has produced a prefix but not yet a wheel.

CI builds the three platforms as three rows of one matrix, each on a native
runner: `ubuntu-24.04`, `ubuntu-24.04-arm` and `macos-15`.

## Releasing

`palace_solver.__version__` is the only place the version is written down;
`pyproject.toml`, the build scripts and CI all read it from there. It mirrors
the Palace release the wheel ships, with a `.postN` segment for a
packaging-only fix that ships the same Palace — `0.17.0`, then `0.17.0.post1`.
Palace's own release is what decides the first three numbers; nothing here
gets to choose them. The build scripts and `PALACE_VERSION` drop the `.postN`
segment, since upstream has no tag for it — a `.post` release rebuilds the
same Palace.

The release tag is `v` plus that version, spelled identically: `v0.17.0`,
`v0.17.0.post1`. `python -m wheelbuild.tag_check <tag>` enforces the match, and
CI runs it on tag pushes before the build, because a tag that disagrees with
the packaged version publishes a release under a number nobody chose and PyPI
never lets a filename be reused.

Per release:

1. Bump `palace_solver.__version__`, and `MPICH_VERSION` if the vendored MPICH
   moved. Commit.
2. Run the check CI cannot: `scripts/e2e-test.sh wheelhouse/*.whl
   <checkout>` against a current palais checkout, on Linux.
3. Tag and push the tag. The tag runs the checks, builds every platform's wheel
   and, on its own runner, smoke-tests it and solves a real example under both
   launchers, then publishes to PyPI.

A release is all or nothing across the platforms. The `publish` job waits for
every matrix row, collects each row's artifact by pattern into one directory,
and `python -m wheelbuild.release_check dist` refuses the upload unless that
directory holds exactly one wheel per supported platform and nothing else. A
row that failed skips the job rather than publishing a subset, because a
release missing a platform cannot be repaired — PyPI never lets a filename be
reused, so the only fix is another version number.

Publishing is entirely CI's: the `publish` job runs only for `refs/tags/v*`,
in the `release` environment, and uploads through PyPI's trusted publishing —
no API token lives in this repository, and nothing is uploaded from a
workstation.

Two things must exist outside this repository for that job to work, and are
the first place to look if a release fails at the upload step:

- A trusted publisher registered on PyPI for `palace-solver`, naming this
  repository, the `wheels.yml` workflow and the `release` environment.
- The `release` environment on GitHub. Adding required reviewers to it is how a
  release is made to wait for a human before uploading.

The repaired x86_64 Linux wheel is 67.7 MB, under PyPI's 100 MB per-file
limit. The build
prints the size and CI puts it in the job summary; if a later Palace release
pushes it over, request a limit increase with that concrete wheel before the
upload — the request needs a built file, not an estimate.
