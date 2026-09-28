"""Darwin shims for the throwaway macOS build spike (wayfinder ticket 04).

This is spike code, not production code. It exists so the spike can drive the
real :mod:`wheelbuild` modules on macOS instead of a fresh script, and so that
every place the production recipe is Linux-shaped shows up here as an explicit,
countable deviation rather than as a silent rewrite.

Three deviations live here:

* ``wheelbuild.mpich.REQUIRED_ARTEFACTS`` and
  ``wheelbuild.openblas.REQUIRED_ARTEFACTS`` name ELF sonames
  (``lib/libmpi.so.12``, ``lib/libopenblas.so``). On Darwin the same installs
  produce ``lib/libmpi.12.dylib`` and ``lib/libopenblas.dylib``, so the
  validators reject a perfectly good tree.
* ``wheelbuild.assemble.find_palace_binary`` filters on ``is_elf``, which is
  false for every Mach-O binary.
* ``wheelbuild.mpich.configure_arguments`` and
  ``wheelbuild.superbuild.cmake_arguments`` take no extra arguments, and the
  spike needs to vary the MPICH device and pass OpenMP hints.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from wheelbuild import mpich, openblas, superbuild
from wheelbuild._process import check_call

#: What an MPICH install actually looks like on Darwin.
DARWIN_MPICH_ARTEFACTS = (
    Path("bin/mpiexec"),
    Path("include/mpi.h"),
    Path("include/mpif.h"),
    Path("lib/libmpi.12.dylib"),
    Path("lib/libmpifort.12.dylib"),
)

#: What an OpenBLAS install actually looks like on Darwin.
DARWIN_OPENBLAS_ARTEFACTS = (
    Path("include/cblas.h"),
    Path("lib/libopenblas.dylib"),
)

#: Mach-O magic numbers: 64-bit little/big endian and the universal archive.
MACHO_MAGIC = (
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
)


def patch_validators() -> None:
    """Point the wheelbuild validators at Darwin artefact names."""
    mpich.REQUIRED_ARTEFACTS = DARWIN_MPICH_ARTEFACTS
    openblas.REQUIRED_ARTEFACTS = DARWIN_OPENBLAS_ARTEFACTS


def is_macho(path: Path) -> bool:
    """Whether ``path`` is a Mach-O binary."""
    with path.open("rb") as handle:
        return handle.read(4) in MACHO_MAGIC


def find_palace_binary(install_prefix: Path) -> Path:
    """Darwin twin of :func:`wheelbuild.assemble.find_palace_binary`."""
    candidates = sorted(
        path
        for path in (install_prefix / "bin").glob("palace*")
        if path.is_file() and not path.is_symlink() and is_macho(path)
    )
    if not candidates:
        raise FileNotFoundError(f"no Mach-O Palace binary under {install_prefix / 'bin'}")
    return candidates[0]


def build_mpich(
    *, source_dir: Path, build_dir: Path, prefix: Path, jobs: int, extra: Sequence[str]
) -> Path:
    """Run the production MPICH recipe plus ``extra`` configure arguments."""
    build_dir.mkdir(parents=True, exist_ok=True)
    configure = mpich.configure_arguments(source_dir=source_dir, prefix=prefix)
    check_call([*configure, *extra], cwd=build_dir)
    check_call(["make", f"-j{jobs}"], cwd=build_dir)
    check_call(["make", "install"], cwd=build_dir)
    return mpich.validate(prefix)


def build_superbuild(
    *,
    source_dir: Path,
    build_dir: Path,
    install_prefix: Path,
    prefix: Path,
    jobs: int,
    extra: Sequence[str],
) -> None:
    """Run the production superbuild recipe plus ``extra`` CMake arguments."""
    build_dir.mkdir(parents=True, exist_ok=True)
    arguments = superbuild.cmake_arguments(
        source_dir=source_dir,
        install_prefix=install_prefix,
        mpi_home=superbuild.mpi_home(prefix),
        ccache=True,
    )
    # cmake_arguments puts the source directory last, and it must stay last.
    arguments = [*arguments[:-1], *extra, arguments[-1]]
    check_call(arguments, cwd=build_dir)
    check_call(["cmake", "--build", ".", f"-j{jobs}"], cwd=build_dir)


def main(argv: Sequence[str] | None = None) -> int:
    """Spike entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    jobs_default = os.cpu_count() or 1

    mpich_parser = sub.add_parser("mpich")
    mpich_parser.add_argument("--source-dir", type=Path, required=True)
    mpich_parser.add_argument("--build-dir", type=Path, required=True)
    mpich_parser.add_argument("--prefix", type=Path, required=True)
    mpich_parser.add_argument("--jobs", type=int, default=jobs_default)
    # Repeatable, and spelled with "=", because a bare positional starting
    # with "--" is an unrecognised option to argparse.
    mpich_parser.add_argument("--extra-arg", action="append", default=[])

    openblas_parser = sub.add_parser("openblas")
    openblas_parser.add_argument("--source-dir", type=Path, required=True)
    openblas_parser.add_argument("--prefix", type=Path, required=True)
    openblas_parser.add_argument("--jobs", type=int, default=jobs_default)

    superbuild_parser = sub.add_parser("superbuild")
    superbuild_parser.add_argument("--source-dir", type=Path, required=True)
    superbuild_parser.add_argument("--build-dir", type=Path, required=True)
    superbuild_parser.add_argument("--install-prefix", type=Path, required=True)
    superbuild_parser.add_argument("--prefix", type=Path, required=True)
    superbuild_parser.add_argument("--jobs", type=int, default=jobs_default)
    superbuild_parser.add_argument("--extra-arg", action="append", default=[])

    binary_parser = sub.add_parser("palace-binary")
    binary_parser.add_argument("--install-prefix", type=Path, required=True)

    args = parser.parse_args(argv)
    patch_validators()

    if args.command == "mpich":
        prefix = build_mpich(
            source_dir=args.source_dir,
            build_dir=args.build_dir,
            prefix=args.prefix,
            jobs=args.jobs,
            extra=args.extra_arg,
        )
        print(f"MPICH installed into {prefix}")
    elif args.command == "openblas":
        prefix = openblas.run(
            source_dir=args.source_dir, prefix=args.prefix, jobs=args.jobs
        )
        print(f"OpenBLAS installed into {prefix}")
    elif args.command == "superbuild":
        build_superbuild(
            source_dir=args.source_dir,
            build_dir=args.build_dir,
            install_prefix=args.install_prefix,
            prefix=args.prefix,
            jobs=args.jobs,
            extra=args.extra_arg,
        )
    elif args.command == "palace-binary":
        print(find_palace_binary(args.install_prefix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
