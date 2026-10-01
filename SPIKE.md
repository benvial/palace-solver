# Throwaway spike: Palace on a Windows amd64 runner

This branch is disposable. **Do not merge it.** It answers one question for
the windows-support effort (ticket 07): does Palace's full-feature superbuild
build on a GitHub `windows-2025` runner with MSYS2 UCRT64 gcc/gfortran and
MS-MPI, and does the result run from a wheel?

Spike-only files:

- `.github/workflows/spike-windows.yml` has three jobs. `targets` builds only
  libCEED, GSLIB and STRUMPACK-with-MPI, the three dependencies with no Windows
  evidence. `superbuild` builds everything else cold, then works out the PE
  import closure, builds the wheels and runs delvewheel. `verify` runs on a
  clean runner with no MSYS2 on `PATH` and no MS-MPI installed, then installs a
  system MS-MPI for the interop case.
- `scripts/spike-windows-build.sh` is the compile half, run under MSYS2 bash.
  Each place it departs from the Linux recipe is marked `DEVIATION:`.
- `scripts/spike_windows.py` is the Python half, run under the runner's CPython.
  It prints `RESULT` lines, and the workflow copies them into the job summary.
- `scripts/spike-windows-patches/` holds patches applied to Palace's sources.
  Each one is a *patchable* finding.

The recipe is the one ticket 06 fixed: `windows-2025`, build root `D:\b`,
`BUILD_SHARED_LIBS=OFF`, the GCC runtimes as DLLs, every solver dependency at
Palace's pins, and OpenBLAS built with the Linux x86_64 arguments verbatim.
CMake is Kitware's 3.31.8 Windows build rather than MSYS2's rolling 4.x.

Failure classes, per ticket 06: **patch** (logged and counted), **pin bump**
(flagged as Windows needing a different version than Linux), **structural**
(no-go).

## Runs

<!-- one entry per run: id, what changed, what it settled -->
