"""Drive Palace's CMake superbuild with the feature set the spec pins.

The flag set is deliberately maximal and mirrors ``pypalace/_cli/build.py``;
packaging concerns never trim a solver feature. MPI comes from the ``mpich``
wheel installed into the build environment, so the superbuild compiles against
the same MPICH the runtime wheel will link to.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from wheelbuild._process import check_call

#: Palace feature flags, in spec order. Never trimmed for packaging reasons.
FEATURE_FLAGS = (
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_SHARED_LIBS=ON",
    "-DPALACE_WITH_CUDA=OFF",
    "-DPALACE_WITH_HIP=OFF",
    "-DPALACE_WITH_64BIT_INT=OFF",
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
)


def mpi_home(prefix: Path) -> Path:
    """Validate that ``prefix`` holds the payload of the PyPI ``mpich`` wheel.

    The wheel is a data wheel: it installs ``lib/libmpi.so.12``,
    ``include/mpi.h`` and ``bin/mpiexec`` directly under the environment
    prefix, which is exactly the layout CMake's ``MPI_HOME`` expects.

    Args:
        prefix: Environment prefix the ``mpich`` wheel was installed into.

    Returns:
        The validated prefix.

    Raises:
        FileNotFoundError: If the mpich wheel payload is not there.
    """
    library = prefix / "lib" / "libmpi.so.12"
    header = prefix / "include" / "mpi.h"
    missing = [path for path in (library, header) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"no mpich wheel payload under {prefix}: missing "
            + ", ".join(str(path) for path in missing)
        )
    return prefix


def cmake_arguments(
    *,
    source_dir: Path,
    install_prefix: Path,
    mpi_home: Path,
    ccache: bool = True,
) -> list[str]:
    """Build the CMake configure command for the superbuild.

    Args:
        source_dir: Palace source tree (the superbuild's top-level CMake dir).
        install_prefix: Where the built Palace tree is installed.
        mpi_home: Prefix of the mpich wheel to compile against.
        ccache: Route the compilers through ccache.

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
    if ccache:
        arguments += [
            "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
            "-DCMAKE_Fortran_COMPILER_LAUNCHER=ccache",
        ]
    arguments.append(str(source_dir))
    return arguments


def run(
    *,
    source_dir: Path,
    build_dir: Path,
    install_prefix: Path,
    prefix: Path,
    jobs: int,
    ccache: bool = True,
) -> None:
    """Configure and build Palace, installing into ``install_prefix``.

    Args:
        source_dir: Palace source tree.
        build_dir: Scratch directory for the superbuild (reuse it to benefit
            from the cached dependency tree).
        install_prefix: Install destination for the Palace tree.
        prefix: Environment prefix holding the mpich wheel.
        jobs: Parallel build jobs.
        ccache: Route the compilers through ccache.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    configure = cmake_arguments(
        source_dir=source_dir,
        install_prefix=install_prefix,
        mpi_home=mpi_home(prefix),
        ccache=ccache,
    )
    check_call(configure, cwd=build_dir)
    check_call(["cmake", "--build", ".", f"-j{jobs}"], cwd=build_dir)


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
        help="environment prefix holding the mpich wheel (default: sys.prefix)",
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
