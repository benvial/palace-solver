# Building the wheel

Every wheel is built natively: inside a `manylinux_2_28` container on Linux, on
an Apple Silicon runner on macOS, under MSYS2 on a Windows runner on Windows. CI
builds the four platforms as four rows of one matrix, each on a native runner —
`ubuntu-24.04`, `ubuntu-24.04-arm`, `macos-15` and `windows-2025`.

## Linux

```bash
scripts/build-in-container.sh 0.17.0      # docker, caches in ./.build-cache
scripts/smoke-test.sh wheelhouse/*.whl CONFIG   # clean venv, 1 rank and -n 2
scripts/interop-test.sh wheelhouse/*.whl CONFIG # real solve under both launchers
```

`scripts/build-wheel.sh` is the in-container pipeline: MPICH → OpenBLAS →
superbuild → notice harvest → wheel assembly → `auditwheel repair` → retag to
`py3-none-manylinux_2_28_<arch>`. The steps are Python modules under
`wheelbuild/` and are unit-tested with `pytest`.

## macOS

There is no container, so `scripts/build-macos.sh` is both the driver and the
recipe:

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

## Windows

Like the macOS script, `scripts/build-windows.sh` is both the driver and the
recipe. It runs under MSYS2's UCRT64 bash, with a native Windows CPython in
`PYTHON` for every `wheelbuild` step:

```bash
PYTHON=/c/Python312/python.exe scripts/build-windows.sh 0.18.1   # BUILD_ROOT defaults to D:\b
```

MSYS2 supplies gcc, g++ and gfortran, GNU make, the POSIX tools Palace's
superbuild drives, and MS-MPI's import library and gfortran bridge
(`mingw-w64-msmpi`); the packages are the ones the `wheels` workflow installs.
MSYS2's own python cannot run the steps, because it reports a platform that
would mis-tag the wheel. The driver fetches and hash-checks the MS-MPI
redistributable, builds OpenBLAS, then runs the superbuild with the carried
patches under `wheelbuild/data/patches/windows/` applied before its first
compile. There is no repair tool: an `.exe` has no RPATH, so
`wheelbuild.pe_repair` copies the payload's PE import closure flat into a
directory of its own, the notices are harvested, and assembly stages that
directory into `bin` beside the executables and link-checks it.

The same caveat as macOS applies, more strongly. No Windows machine is
available to this project, so the only Windows `scripts/build-windows.sh` has
ever run on is a `windows-2025` GitHub runner, where a cold build takes about
two hours. It is written so a Windows owner with MSYS2 can run it, but nobody
here has.

The wheel tests on Windows are Python scripts rather than the bash ones, run
from a native CPython — not Git bash or MSYS2 — with nothing but the virtual
environment and Windows on `PATH`:

```bash
python scripts/smoke-test-windows.py WHEEL CONFIG
python scripts/interop-test-windows.py [--install-msmpi] WHEEL CONFIG
```

The smoke test refuses a machine with MS-MPI installed (a
`System32\msmpi.dll`), since what it proves is that the wheel needs nothing
else. `--install-msmpi` installs the pinned, hash-checked MS-MPI 10.1.3
system-wide for the interop test to launch with; it needs an administrator and
is meant for a disposable CI runner.

## The platform tag

The repair tool is where the platform tag comes from, and the two tools differ:
`auditwheel` is given the tag, while `delocate` derives it from the largest
`minos` in the payload and renames the wheel. So the macOS tag is a measurement
of what was built rather than a value chosen at assembly time, and the pipeline
ends by checking the filename against `wheelbuild.platforms.platform_tag()` — a
wheel tagged for another macOS is a build failure, not a new floor. On Windows
there is no repair tool to ask, and `win_amd64` names no Windows version: the
floor is a constant in `palace_solver/_exec.py`, which refuses an older Windows
at start-up.

## The CPU baseline

OpenBLAS is built with `DYNAMIC_ARCH`, so it picks its kernels at run time, and
the code around those kernels is compiled for a CPU baseline rather than for the
machine that built it: ARMv8-A on both arm platforms, and on `x86_64` the plain
x86-64 architectural level, which the build already leaves in place. That
baseline is the hardware floor the README states, so moving it is a change to
what the published wheel claims.

## Testing a build

- `scripts/smoke-test.sh` installs the wheel into a clean virtual environment
  and solves one problem on one rank and on two.
- `scripts/interop-test.sh` solves a real example on two ranks under both
  `palace-mpiexec` and the `mpiexec` from the PyPI `mpich` wheel, requires the
  results to agree, checks that a rank launched without a rendezvous is
  refused, and checks that a launcher from the next MPICH major series is not.
- `scripts/smoke-test-windows.py` and `scripts/interop-test-windows.py` are
  their Windows twins; see [Windows](#windows).
- `python -m wheelbuild.pin_check` checks that the vendored MPICH stays inside
  the major series the interoperability test proves the wheel against, and the
  vendored MS-MPI inside 10.1; it runs in CI on every push.
- `scripts/verify-install.sh` checks an install prefix the way the smoke test
  checks a wheel — the version stamp, and two ranks solving one problem
  together — which is useful when a build has produced a prefix but not yet a
  wheel.
