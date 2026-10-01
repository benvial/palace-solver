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

- **36907972682.** OpenBLAS compiled with the Linux recipe and failed to link:
  `ld: cannot find -lgomp`. MSYS2 ships libgomp as a separate package,
  `mingw-w64-ucrt-x86_64-libgomp`, which `gcc` does not depend on. That
  package is now in the install list. This is a provisioning gap, not a patch.
  The compile took about 75 min against about 15 min on Linux at the same
  `-j4`, so each object costs about 5-8x as much.
- **36917961846.** The cache restored the OpenBLAS tree and the build resumed:
  link and install only, 12 min. The probe ruled out process overhead: an
  MSYS2 fork costs 26 ms, a native process start 50 ms, and gcc on an empty
  file 58 ms, all far below the ~1.3 s each OpenBLAS object took.
  Defender's real-time protection is off. Job one's three targets:
  - **GSLIB: built.** It produced a static `libgs.a` under MinGW with
    MS-MPI, with no patch. The only warnings are `-Wstringop-overflow` on
    `MPI_STATUSES_IGNORE`.
  - **libCEED: blocked by LIBXSMM.** LIBXSMM fails to compile under LLP64:
    `generator_rv64_instructions.c` stores a 64-bit mask in an
    `unsigned long`, which is 32 bits on Windows, and `-Werror=overflow`
    turns the warning into an error. This is a real upstream bug, still
    present on LIBXSMM `main`, though the code is RISC-V only and never runs
    on x86. Patch 2 (`libxsmm-llp64.diff`) fixes it, wired in through a
    `PATCH_COMMAND` like upstream's patches for MFEM and MUMPS. It is
    patchable.
  - **STRUMPACK: blocked by its prerequisites.** METIS, ZFP and ScaLAPACK
    never configured: Palace configures them with a bare
    `${CMAKE_COMMAND} <SOURCE_DIR>`, so CMake fell back to NMake. Fixed in
    the driver by exporting `CMAKE_GENERATOR="MSYS Makefiles"`. Not a patch.
- **36920388161.** The probe settled the slow OpenBLAS compile: a
  translation unit holding only `#include <windows.h>` takes 0.78 s on this
  runner, and OpenBLAS's `common.h` includes it in every object. That is
  most of the ~1.3 s per object. The cost comes from OpenBLAS's sources,
  not the toolchain, so the remedy is caching the OpenBLAS install. Neither
  fix from the previous run took effect, both because of bugs in the spike:
  - The patches were never applied, and the driver reported them as
    "already applied (or stale)". A plain `git apply` passes locally, so the
    likely cause is CRLF from the Windows checkout. Fixed with
    `.gitattributes` (`-text` on the patches). The driver now fails loudly
    when a patch neither applies nor is already applied. **Correction:** the
    `memoryreporting` patch was not applied in run 36917961846 either.
  - METIS, ZFP and ScaLAPACK still reported NMake: their build trees had
    cached NMake from the failed configure. The configure stage now discards
    any sub-project cache that recorded NMake.
- **36921314814.** The patches applied, and LIBXSMM, METIS, ZFP and
  ScaLAPACK all built: patch 2 works and the generator fix holds. Two new
  failures, both small source fixes:
  - **libCEED: patch 3 (`libCEED-posix-memalign.diff`).** `ceed.c` calls
    `posix_memalign`, which Windows lacks, and it is the only POSIX-only call
    in libCEED's interface and CPU backends. Still unfixed on libCEED `main`.
    The patch uses `malloc` on `_WIN32`. `_aligned_malloc` is ruled out
    because `CeedReallocArray` and `CeedFree` call `realloc()` and `free()`
    on the result. Giving up 64-byte alignment is safe: `backend.h` already
    says `CeedCalloc` returns only calloc alignment.
  - **STRUMPACK: patch 4 (`STRUMPACK-llp64.diff`).** `MPIWrapper.hpp`
    specializes `mpi_type<>` for `unsigned long` but not
    `unsigned long long`, which is `std::size_t` on Windows. The patch adds
    that specialization (`MPI_UNSIGNED_LONG_LONG`, which MS-MPI's `mpi.h`
    defines). It is harmless on LP64, where the two types are distinct.
  Patch count: 4, all patchable. The spike has found nothing structural so far.
- **36923058833.** The configure stage exited silently: the new NMake
  cleanup's `grep` found nothing, returned 1, and `pipefail` ended the
  script. A spike bug. Fixed.
- **36923522364.** libCEED compiled with patch 3, including the XSMM
  backends. GCC warns that the weak-symbol visibility attribute is ignored on
  PE. libCEED then failed writing `ceed.pc`: its rule is
  `sed -e "s:%prefix%:$(prefix):"`, and the colon in `D:/b/install` ends the
  expression early. The fix changes the delimiter to `|`. It goes into the
  same libCEED diff, now `libCEED-windows.diff`, so it is still patch 3.
  STRUMPACK got past `MPIWrapper.hpp` and failed in `GMResMPI.cpp`:
  `std::max(n, 1ul)` mixes `size_t` (`unsigned long long` on Windows) with
  `unsigned long`. That is the only such literal in STRUMPACK's sources. The
  fix goes into patch 4. Spike changes: build stages use `make -k`, so one
  run reports every failing object, and the source stage resets Palace's tree
  and re-patches each run. A cached dependency checkout is discarded
  whenever its diffs change.
- **36924871897.** **Job one passed: libCEED (with LIBXSMM), GSLIB and
  STRUMPACK with MPI all build under MinGW with MS-MPI.** It took four
  patches, all patchable, all small:
  1. Palace's `memoryreporting.cpp`: `getrusage` replaced with
     `GetProcessMemoryInfo`.
  2. LIBXSMM's RISC-V generator: a 64-bit mask stored in an
     `unsigned long`.
  3. libCEED: `posix_memalign`, and a `sed s:...:` that breaks on `D:`.
  4. STRUMPACK: no `mpi_type<unsigned long long>`, and
     `std::max(size_t, 1ul)`.

  Three of the four are LLP64 (`long` is 32 bits on Windows) and one is the
  drive-letter colon. None is structural. libCEED installs as
  `build/libceed.so`, a PE DLL under a `.so` name, because Palace forces it
  shared. Whether Palace links against it is job two's question.
- **36924871897, job two (full superbuild, 7 min with `-k`).** SUNDIALS,
  SuperLU_DIST, scn, PARPACK and the rest built. Three failures, each with
  an upstream fix that postdates Palace's pin:
  - **hypre: patch 5 (`hypre-mingw-ffs.diff`).** MinGW has no `ffs()`.
    hypre `master` already has the fix (`#elif defined(__MINGW32__)` to
    `__builtin_ffs`); this backports it to `_hypre_utilities.h` and
    `hopscotch_hash.h`. Predicted by ticket 02.
  - **MUMPS: patch 6 (`mumps-mingw-mpi.diff`).** gfortran rejects MS-MPI's
    `mpif.h` in `*ana_aux_par.F` ("'dllimport' implies default visibility,
    but 'mpi_bottom' has already been declared with a different
    visibility"). scivision/mumps fixed this today, 2026-10-01, in
    `09a718d177`, from MSYS2's `0004-mpi-module.patch`: the four files switch
    to `USE MPI`. The backport applies the same `.patch` to the MUMPS 5.7.3
    this pin downloads; it applies with offsets only. Predicted by ticket 02.
  - **PETSc: not a patch.** `./configure` (`#!/usr/bin/env python3`) found
    MinGW's python and refused it ("Windows python detected. Please rerun
    ./configure with cygwin-python"). The driver now installs MSYS2's
    `python` and puts a `python3` shim for `/usr/bin/python3` first on
    `PATH`. This is a provisioning deviation, documented by PETSc for MSYS2.
- **36928018416.** Patches 5 and 6 work: hypre and MUMPS (parallel, with
  ScaLAPACK and MS-MPI) build. Two configure failures remain, neither one
  diagnosable from the job log alone:
  - **PETSc**, now running under the POSIX python, rejects
    `--with-cc=C:/msys64/ucrt64/bin/cc.exe` ("cannot be found or does not
    work"). Suspected cause: the Windows-form path under a Cygwin-style
    python. Not yet confirmed.
  - **MFEM**'s `FindSTRUMPACK` test (`STRUMPACK_VERSION_OK`) fails to
    compile or link against the static STRUMPACK.
  The next run collects `configure.log` and each sub-project's
  `CMakeConfigureLog.yaml`.
- **36929717229.** The new logs explain both failures:
  - **PETSc:** `configure.log` shows the POSIX python searching `PATH` for
    a program literally named `C:/msys64/ucrt64/bin/cc.exe`. To it, a drive
    letter is a relative path. The `python3` shim now rewrites every `X:/`
    path in a `*/configure` script's arguments, and in `PETSC_DIR` and
    `SLEPC_DIR`, to `/x/`. Still a provisioning deviation. Open risk: PETSc
    will then record `/d/b/...` paths in `petscvariables`, which native
    tools (MFEM's and Palace's CMake, gcc) cannot resolve.
  - **MFEM: patch 7 (`palace-mfem-static-scalapack.patch`).** The
    `FindSTRUMPACK` test link fails with 562 undefined references, all from
    `libscalapack.a`: MPI calls (`MPI_Op_create`, `MPI_Type_match_size`,
    ...) and BLAS/LAPACK routines (`lsame_`, `dsymm_`, ...). MFEM places the
    REQUIRED_LIBRARIES (ScaLAPACK) after the REQUIRED_PACKAGES (MPI, BLAS).
    A static archive then finds nothing after it to resolve against; on
    Linux the shared `libscalapack.so` hid the problem. The patch appends
    BLAS/LAPACK and MPI after ScaLAPACK when `NOT BUILD_SHARED_LIBS`, for
    both STRUMPACK and MUMPS. This is a static-linking bug in Palace, not
    specific to Windows, and could go upstream.
- **36931006599.** **PETSc configured, built and installed** (static,
  complex, MS-MPI, OpenBLAS) once its `configure` saw POSIX paths. Two
  failures:
  - **SLEPc:** "SLEPC_DIR is not the current directory". The shim
    lower-cased the drive (`/d/b/...`), while make's working directory is
    `/D/b/...`, and SLEPc compares the two case-sensitively. The shim now
    keeps the letter's case.
  - **MFEM:** patch 7 reached MFEM, and the order was right, but
    `mfem_find_package` runs `list(REMOVE_DUPLICATES)` on the full list. That
    keeps the first, too-early copy of `libmsmpi.dll.a` and
    `libopenblas.dll.a` and drops the copies after ScaLAPACK. Patch 7 now
    spells the trailing ones `-Wl,<path>`: the same file to `ld`, a
    different string to CMake.
