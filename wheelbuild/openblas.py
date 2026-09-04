"""Build the OpenBLAS that Palace links and the wheel vendors.

Palace does not build BLAS/LAPACK itself: ``cmake/ExternalBLASLAPACK.cmake``
requires one already on the system (OpenBLAS, MKL, AOCL or ARMPL), and the
manylinux image ships none. Building it here rather than installing the distro
package buys two things the wheel needs: ``DYNAMIC_ARCH``, so one binary picks
its kernels at run time on whatever CPU a user has, and an OpenMP-threaded
build that matches Palace's own OpenMP.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from wheelbuild._process import check_call

#: OpenBLAS release vendored into the wheel.
OPENBLAS_VERSION = "0.3.34"

#: Files an OpenBLAS install must have for Palace to configure against it.
REQUIRED_ARTEFACTS = (
    Path("include/cblas.h"),
    Path("lib/libopenblas.so"),
)


def source_url(version: str = OPENBLAS_VERSION) -> str:
    """Return the download URL of the pinned OpenBLAS source tarball."""
    return (
        "https://github.com/OpenMathLib/OpenBLAS/releases/download/"
        f"v{version}/OpenBLAS-{version}.tar.gz"
    )


def build_arguments(*, jobs: int) -> list[str]:
    """Return the ``make`` command that builds OpenBLAS.

    Args:
        jobs: Parallel build jobs.

    Returns:
        The ``make`` argument vector.
    """
    return [
        "make",
        f"-j{jobs}",
        # One wheel runs on many CPUs, so build every kernel and dispatch at
        # run time.
        "DYNAMIC_ARCH=1",
        # Match Palace's threading model rather than mixing pthreads with it.
        "USE_OPENMP=1",
        # Palace is built with PALACE_WITH_64BIT_INT=OFF, so LP64 it is.
        "INTERFACE64=0",
        "NO_STATIC=1",
    ]


def install_arguments(*, prefix: Path) -> list[str]:
    """Return the ``make install`` command for an OpenBLAS build."""
    return ["make", "install", f"PREFIX={prefix}", "NO_STATIC=1"]


def validate(prefix: Path) -> Path:
    """Check that an OpenBLAS install carries what Palace's CMake looks for.

    Args:
        prefix: OpenBLAS install prefix.

    Returns:
        The validated prefix.

    Raises:
        FileNotFoundError: If any required artefact is missing.
    """
    missing = [
        relative for relative in REQUIRED_ARTEFACTS if not (prefix / relative).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"incomplete OpenBLAS install at {prefix}: missing "
            + ", ".join(str(relative) for relative in missing)
        )
    return prefix


def run(*, source_dir: Path, prefix: Path, jobs: int) -> Path:
    """Build and install OpenBLAS.

    OpenBLAS builds in its source tree, so ``source_dir`` doubles as the build
    directory; cache it to skip a rebuild.

    Args:
        source_dir: Unpacked OpenBLAS source tree.
        prefix: Install prefix.
        jobs: Parallel build jobs.

    Returns:
        The validated install prefix.
    """
    check_call(build_arguments(jobs=jobs), cwd=source_dir)
    check_call(install_arguments(prefix=prefix), cwd=source_dir)
    return validate(prefix)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the OpenBLAS build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    prefix = run(source_dir=args.source_dir, prefix=args.prefix, jobs=args.jobs)
    print(f"OpenBLAS {OPENBLAS_VERSION} installed into {prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
