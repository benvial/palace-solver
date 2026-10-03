"""Drive Palace's CMake superbuild with the feature set the spec pins.

The flag set is deliberately maximal: packaging concerns never trim a solver
feature. Every ``PALACE_WITH_*`` cache option the release defines is named
here, including the ones set to their upstream default, so a default that
changes upstream cannot quietly change what the wheel ships.

MPI comes from the MPICH built by :mod:`wheelbuild.mpich`, which is the same
MPICH the wheel vendors, so the solver runs against exactly what it was
compiled against.

Windows is a separate arm rather than a variation of the other two: MS-MPI
comes from MSYS2's ``mingw-w64-msmpi`` instead of a prefix, the stack links
statically, the generator is named, and the carried patches of
:mod:`wheelbuild.patches` go in around the configure. Its argument vector and
its run are built by functions of their own, so nothing on the Linux and macOS
path changes by a byte.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from wheelbuild import mpich, patches
from wheelbuild._process import check_call

#: Palace feature flags, in spec order. Never trimmed for packaging reasons.
FEATURE_FLAGS = (
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_SHARED_LIBS=ON",
    "-DPALACE_WITH_CUDA=OFF",
    "-DPALACE_WITH_HIP=OFF",
    # MAGMA and GPU-aware MPI are GPU-only; upstream forces MAGMA off
    # without CUDA or HIP, and this says so rather than relying on it.
    "-DPALACE_WITH_MAGMA=OFF",
    "-DPALACE_WITH_GPU_AWARE_MPI=OFF",
    "-DPALACE_WITH_64BIT_INT=OFF",
    # ILP64 BLAS/LAPACK, which upstream marks experimental.
    "-DPALACE_WITH_64BIT_BLAS_INT=OFF",
    "-DPALACE_WITH_OPENMP=ON",
    "-DPALACE_WITH_SUPERLU=ON",
    "-DPALACE_WITH_STRUMPACK=ON",
    "-DPALACE_WITH_STRUMPACK_BUTTERFLYPACK=OFF",
    "-DPALACE_WITH_STRUMPACK_ZFP=ON",
    "-DPALACE_WITH_MUMPS=ON",
    "-DPALACE_WITH_SLEPC=ON",
    "-DPALACE_WITH_ARPACK=ON",
    "-DPALACE_WITH_LIBXSMM=ON",
    "-DPALACE_WITH_GSLIB=ON",
    # Drives the transient solver. On by default upstream since 0.17.0,
    # and named here because the wheel ships the libraries either way.
    "-DPALACE_WITH_SUNDIALS=ON",
)


#: Palace's own CMake build tree, inside the superbuild's.
PALACE_BUILD_DIR = "palace-build"


#: The generator on Windows. Palace drives libCEED, GSLIB and LIBXSMM with
#: ``${CMAKE_MAKE_PROGRAM} VAR=value install``, which needs GNU make and a
#: POSIX shell, so it is MSYS2's make rather than anything native. The driver
#: also exports it as ``CMAKE_GENERATOR``, because several sub-projects are
#: configured by a bare ``${CMAKE_COMMAND} <SOURCE_DIR>`` that would otherwise
#: take CMake's Windows default, NMake.
WINDOWS_GENERATOR = "MSYS Makefiles"

#: Written into the build directory on Windows and passed as
#: ``CMAKE_PROJECT_INCLUDE``; see :data:`wheelbuild.patches.PROJECT_INCLUDE`.
WINDOWS_PROJECT_INCLUDE = "carried-patch-steps.cmake"


def windows_feature_flags() -> tuple[str, ...]:
    """:data:`FEATURE_FLAGS` with the one change Windows makes: a static stack.

    Windows has no RPATH, and several dependencies install their DLLs in the
    wrong place or produce no import library, so everything links into
    ``palace.exe`` and only the GCC runtimes, OpenBLAS, MS-MPI and the two
    libraries Palace forces shared (libCEED, LIBXSMM) ship as DLLs. No feature
    is trimmed.
    """
    return tuple(
        "-DBUILD_SHARED_LIBS=OFF" if flag == "-DBUILD_SHARED_LIBS=ON" else flag
        for flag in FEATURE_FLAGS
    )


def mpi_home(prefix: Path) -> Path:
    """Validate the MPICH install Palace is compiled against.

    Palace needs Fortran MPI (MUMPS, ARPACK and STRUMPACK are Fortran), which
    the PyPI ``mpich`` wheel does not provide, so this is the MPICH built by
    :mod:`wheelbuild.mpich` and later vendored into the wheel.

    Args:
        prefix: MPICH install prefix.

    Returns:
        The validated prefix.

    Raises:
        FileNotFoundError: If the install lacks anything the build needs.
    """
    return mpich.validate(prefix)


def cmake_arguments(
    *,
    source_dir: Path,
    install_prefix: Path,
    mpi_home: Path,
    ccache: bool = True,
    system: str | None = None,
) -> list[str]:
    """Build the CMake configure command for the superbuild.

    Args:
        source_dir: Palace source tree (the superbuild's top-level CMake dir).
        install_prefix: Where the built Palace tree is installed.
        mpi_home: MPICH install prefix to compile against.
        ccache: Route the compilers through ccache.
        system: ``platform.system()`` value; defaults to the running platform.

    Returns:
        The full ``cmake`` argument vector, source directory last.
    """
    arguments = [
        "cmake",
        f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
        f"-DCMAKE_PREFIX_PATH={mpi_home}",
        f"-DMPI_HOME={mpi_home}",
        *FEATURE_FLAGS,
    ]
    if (system or platform.system()) == "Darwin":
        # Palace links some STRUMPACK companions by bare package name --
        # palace/CMakeLists.txt loops over zfp, slate, lapackpp, blaspp and
        # ptscotch and emits a plain `-lzfp` — and Apple's linker searches no
        # path that reaches the shared install prefix, so the build dies at 97%
        # of libpalace.dylib with `ld: library 'zfp' not found` even though
        # libzfp.dylib is installed in that very prefix. Same family as the
        # lib64/lib mismatch wheelbuild.prefix papers over, and upstream
        # fragility rather than anything macOS did wrong.
        library_dir = f"-L{install_prefix / 'lib'}"
        arguments += [
            f"-DCMAKE_EXE_LINKER_FLAGS={library_dir}",
            f"-DCMAKE_SHARED_LINKER_FLAGS={library_dir}",
        ]
    if ccache:
        arguments += [
            "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_Fortran_COMPILER_LAUNCHER=ccache",
        ]
    arguments.append(str(source_dir))
    return arguments


def windows_cmake_arguments(
    *,
    source_dir: Path,
    install_prefix: Path,
    project_include: Path,
    ccache: bool = True,
) -> list[str]:
    """Build the CMake configure command for the superbuild on Windows.

    Paths are written with forward slashes (``D:/b/install``): CMake and the
    MSYS2 tools both read that form, where a backslash is an escape to one and
    a separator to the other.

    No ``MPI_HOME``: MS-MPI's headers, import library and the ``libmsmpifec``
    gfortran bridge come from MSYS2's ``mingw-w64-msmpi``, and FindMPI finds
    them on its own. The prefix is still searched first, for OpenBLAS.

    Args:
        source_dir: Palace source tree (the superbuild's top-level CMake dir).
        install_prefix: Where the built Palace tree is installed.
        project_include: The file :func:`run_windows` writes, giving every
            ExternalProject a ``<name>-patch`` step target.
        ccache: Route the compilers through ccache.

    Returns:
        The full ``cmake`` argument vector, source directory last.
    """
    prefix = install_prefix.as_posix()
    # The Darwin flags, for the Darwin reason: Palace links STRUMPACK's
    # companions by bare name (`-lzfp`) and MinGW's ld searches no path that
    # reaches the prefix either.
    library_dir = f"-L{prefix}/lib"
    arguments = [
        "cmake",
        "-G",
        WINDOWS_GENERATOR,
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
        f"-DCMAKE_PREFIX_PATH={prefix}",
        *windows_feature_flags(),
        f"-DCMAKE_PROJECT_INCLUDE={project_include.as_posix()}",
        f"-DCMAKE_EXE_LINKER_FLAGS={library_dir}",
        f"-DCMAKE_SHARED_LINKER_FLAGS={library_dir}",
    ]
    if ccache:
        arguments += [
            "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_Fortran_COMPILER_LAUNCHER=ccache",
        ]
    arguments.append(source_dir.as_posix())
    return arguments


def build_rpath_command(
    *,
    build_dir: Path,
    install_prefix: Path,
    system: str | None = None,
) -> list[str] | None:
    """Return the command that gives Palace's build tree an rpath, if it lacks one.

    Darwin only. Catch2 lists the tests by running the freshly linked
    ``palace-unit-tests``, and without a build rpath to the prefix that binary
    cannot load ``@rpath/libparpack.2.dylib``, so the listing fails and make
    deletes the binary. Linux needs nothing, because the driver exports
    ``LD_LIBRARY_PATH``.

    The superbuild does not forward ``CMAKE_BUILD_RPATH`` to Palace, so the
    command reconfigures Palace's build tree directly. That relinks
    ``libpalace``, and :func:`run` then builds the superbuild again so that the
    relinked Palace is installed in the same run. Otherwise the next run would
    install different bytes and change the upstream test gate's fingerprint.

    Args:
        build_dir: The superbuild's build directory.
        install_prefix: The install prefix Palace is installed into.
        system: ``platform.system()`` value; defaults to the running platform.

    Returns:
        The command, or ``None`` off Darwin or when the build tree already has
        the rpath.
    """
    if (system or platform.system()) != "Darwin":
        return None
    palace_build = build_dir / PALACE_BUILD_DIR
    rpath = install_prefix / "lib"
    cache = palace_build / "CMakeCache.txt"
    if cache.is_file() and any(
        line.startswith("CMAKE_BUILD_RPATH:") and line.split("=", 1)[1] == str(rpath)
        for line in cache.read_text().splitlines()
    ):
        return None
    return ["cmake", f"-DCMAKE_BUILD_RPATH={rpath}", str(palace_build)]


def unit_test_commands(
    *,
    build_dir: Path,
    install_prefix: Path,
    jobs: int,
) -> list[list[str]]:
    """Build the commands that compile Palace's tests and install them.

    These are what the upstream test gate runs. Upstream's ``palace-tests``
    step does the same two things, but its inner ``make`` gets no jobserver and
    builds serially, so its two commands are run here directly, with ``-j``.
    The install puts ``palace-unit-tests``, Catch2 and the test data into the
    prefix. The data has to be there, because the tests have its path compiled
    in. Nothing of it reaches the wheel: :func:`wheelbuild.assemble.find_palace_binary`
    matches the solver by name.

    Args:
        build_dir: The superbuild's build directory.
        install_prefix: The install prefix Palace is installed into.
        jobs: Parallel build jobs.

    Returns:
        The commands, in the order they run.
    """
    palace_build = build_dir / PALACE_BUILD_DIR
    return [
        ["cmake", "--build", str(palace_build), "--target", "unit-tests", f"-j{jobs}"],
        [
            "cmake",
            "--install",
            str(palace_build / "test" / "unit"),
            "--prefix",
            str(install_prefix),
        ],
    ]


def run_windows(
    *,
    source_dir: Path,
    build_dir: Path,
    install_prefix: Path,
    jobs: int,
    ccache: bool = True,
) -> None:
    """Patch, configure and build Palace on Windows.

    The order is the carried-patch recipe of :mod:`wheelbuild.patches`: Palace's
    patches committed and stale dependency trees discarded before the configure;
    a pre-pass that only fetches; the dependency patches; then the build; then
    proof that every patch survived it.

    Args:
        source_dir: Palace's checkout, tagged ``upstream-v<version>`` on its
            tarball commit.
        build_dir: Scratch directory for the superbuild.
        install_prefix: Install destination for the Palace tree.
        jobs: Parallel build jobs.
        ccache: Route the compilers through ccache.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    patches.prepare_palace(source_dir)
    patches.discard_stale_dependencies(build_dir)
    include = build_dir / WINDOWS_PROJECT_INCLUDE
    # Rewritten only when it differs: the configure depends on it, so touching
    # it on every run would regenerate the superbuild for nothing.
    if not include.is_file() or include.read_text() != patches.PROJECT_INCLUDE:
        include.write_text(patches.PROJECT_INCLUDE)
    configure = windows_cmake_arguments(
        source_dir=source_dir,
        install_prefix=install_prefix,
        project_include=include,
        ccache=ccache,
    )
    check_call(configure, cwd=build_dir)
    check_call(
        ["cmake", "--build", ".", f"-j{jobs}", "--target", *patches.patch_targets()],
        cwd=build_dir,
    )
    patches.apply_dependencies(build_dir)
    check_call(["cmake", "--build", ".", f"-j{jobs}"], cwd=build_dir)
    patches.verify(source_dir, build_dir)


def run(
    *,
    source_dir: Path,
    build_dir: Path,
    install_prefix: Path,
    prefix: Path,
    jobs: int,
    ccache: bool = True,
    system: str | None = None,
) -> None:
    """Configure and build Palace, installing into ``install_prefix``.

    Off Windows, Palace's tests are built and installed too, for the upstream
    test gate (:func:`unit_test_commands`). Windows does not build them yet,
    because its test sources do not compile without carried patches.

    Args:
        source_dir: Palace source tree.
        build_dir: Scratch directory for the superbuild (reuse it to benefit
            from the cached dependency tree).
        install_prefix: Install destination for the Palace tree.
        prefix: MPICH install prefix. Unused on Windows, which has no MPICH.
        jobs: Parallel build jobs.
        ccache: Route the compilers through ccache.
        system: ``platform.system()`` value; defaults to the running platform.
    """
    if (system or platform.system()) == "Windows":
        run_windows(
            source_dir=source_dir,
            build_dir=build_dir,
            install_prefix=install_prefix,
            jobs=jobs,
            ccache=ccache,
        )
        return
    build_dir.mkdir(parents=True, exist_ok=True)
    configure = cmake_arguments(
        source_dir=source_dir,
        install_prefix=install_prefix,
        mpi_home=mpi_home(prefix),
        ccache=ccache,
    )
    check_call(configure, cwd=build_dir)
    check_call(["cmake", "--build", ".", f"-j{jobs}"], cwd=build_dir)
    rpath = build_rpath_command(
        build_dir=build_dir, install_prefix=install_prefix, system=system
    )
    if rpath is not None:
        check_call(rpath, cwd=build_dir)
        check_call(["cmake", "--build", ".", f"-j{jobs}"], cwd=build_dir)
    for command in unit_test_commands(
        build_dir=build_dir, install_prefix=install_prefix, jobs=jobs
    ):
        check_call(command, cwd=build_dir)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the superbuild step."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--install-prefix", type=Path, required=True)
    parser.add_argument(
        "--prefix",
        type=Path,
        default=Path(sys.prefix),
        help="MPICH install prefix (default: sys.prefix); unused on Windows",
    )
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--no-ccache", action="store_true")
    args = parser.parse_args(argv)
    run(
        source_dir=args.source_dir,
        build_dir=args.build_dir,
        install_prefix=args.install_prefix,
        prefix=args.prefix,
        jobs=args.jobs,
        ccache=not args.no_ccache,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
