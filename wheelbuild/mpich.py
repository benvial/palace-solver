"""Build the MPICH that the wheel vendors.

The wheel carries its own MPI. The PyPI ``mpich`` wheel is C-only — no
``libmpifort``, no ``mpif.h``, no ``mpi.mod`` — while Palace needs Fortran MPI
for MUMPS, ARPACK and STRUMPACK, so it cannot serve as the build or runtime
MPI. MPICH is therefore compiled from source here, linked into Palace, and
vendored into the wheel together with the Hydra process manager, so
``mpiexec -n 4 palace config.json`` works with nothing else installed.

The version is pinned to the one palais depends on, so a Palace launched by
either process manager speaks the same PMI wire protocol. The pin itself lives
in ``palais_solver`` because the wheel needs it at run time too, for the
launcher guard; ``wheelbuild.pin_check`` checks it against palais's.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from palais_solver import MPICH_VERSION
from wheelbuild._process import check_call

#: Files an MPICH install must have for Palace to configure and run against it.
REQUIRED_ARTEFACTS = (
    Path("bin/mpiexec"),
    Path("include/mpi.h"),
    Path("include/mpif.h"),
    Path("lib/libmpi.so.12"),
    Path("lib/libmpifort.so.12"),
)


def source_url(version: str = MPICH_VERSION) -> str:
    """Return the download URL of the pinned MPICH source tarball."""
    return f"https://www.mpich.org/static/downloads/{version}/mpich-{version}.tar.gz"


def configure_arguments(*, source_dir: Path, prefix: Path) -> list[str]:
    """Return the MPICH ``configure`` command.

    Args:
        source_dir: Unpacked MPICH source tree.
        prefix: Install prefix for the build.

    Returns:
        The ``configure`` argument vector.
    """
    return [
        str(source_dir / "configure"),
        f"--prefix={prefix}",
        "--enable-shared",
        "--disable-static",
        # Palace's Fortran dependencies (MUMPS, ARPACK, STRUMPACK) need the
        # Fortran bindings the PyPI mpich wheel does not ship.
        "--enable-fortran=all",
        "--with-pm=hydra",
        "--enable-fast=O2",
        # gfortran 10+ rejects MPICH's legacy argument-mismatch idioms.
        "FFLAGS=-fallow-argument-mismatch",
        "FCFLAGS=-fallow-argument-mismatch",
    ]


def validate(prefix: Path) -> Path:
    """Check that an MPICH install carries the artefacts the build needs.

    Args:
        prefix: MPICH install prefix.

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
            f"incomplete MPICH install at {prefix}: missing "
            + ", ".join(str(relative) for relative in missing)
        )
    return prefix


def run(*, source_dir: Path, build_dir: Path, prefix: Path, jobs: int) -> Path:
    """Configure, build and install MPICH.

    Args:
        source_dir: Unpacked MPICH source tree.
        build_dir: Out-of-tree build directory (cache it to skip rebuilds).
        prefix: Install prefix.
        jobs: Parallel build jobs.

    Returns:
        The validated install prefix.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    check_call(configure_arguments(source_dir=source_dir, prefix=prefix), cwd=build_dir)
    check_call(["make", f"-j{jobs}"], cwd=build_dir)
    check_call(["make", "install"], cwd=build_dir)
    return validate(prefix)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the MPICH build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    prefix = run(
        source_dir=args.source_dir,
        build_dir=args.build_dir,
        prefix=args.prefix,
        jobs=args.jobs,
    )
    print(f"MPICH {MPICH_VERSION} installed into {prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
