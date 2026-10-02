"""Repair the Windows payload: copy its DLL closure flat, beside the executables.

The Windows wheel ships executables, not an extension module, and an ``.exe``
has no RPATH. The loader looks for a DLL in the executable's own directory
first, so the repair copies every non-system DLL the solver and the MS-MPI
launchers load, directly or through another DLL, into one directory, which
staging then puts beside them in ``palace_solver/bin`` (ADR-0007).
``delvewheel`` is not used: it mangles names into a ``.libs`` directory that an
executable has no way to find.

The repair runs before the wheel exists, unlike ``auditwheel`` and
``delocate``, which take a wheel and return one. The Windows notices are
rendered from the DLLs actually copied (``wheelbuild.notices --payload-dir``),
and the notices file is packaged into the wheel, so the copy has to come first.
The order is: this module, the notices, then ``wheelbuild.assemble
--payload-dir``.

The closure is :func:`wheelbuild.link_check.pe_import_closure`, the same walk
the link check repeats on the staged payload, so the repair and its check
cannot disagree about what a DLL needs.
"""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

from wheelbuild import assemble, link_check, msmpi

#: Where MSYS2's UCRT64 environment keeps its DLLs on a GitHub runner and in a
#: default MSYS2 install: the GCC runtimes, zlib and ``libmsmpifec.dll``.
DEFAULT_TOOLCHAIN_DIR = Path("C:/msys64/ucrt64/bin")


def search_path(
    *, install_prefix: Path, msmpi_dir: Path, toolchain_dir: Path
) -> tuple[Path, ...]:
    """Return the directories the closure is looked up in, first match winning.

    The order matters in one place. The fetched ``msmpi.dll`` is the only copy
    whose bytes are checked against Microsoft's hashes, so its directory comes
    before the toolchain's. MSYS2's ``mingw-w64-msmpi`` ships no ``msmpi.dll``
    today, and :func:`wheelbuild.msmpi.verify_wheel` checks the wheel's copy
    again in case that changes. The install prefix comes first because a DLL
    the superbuild built (``libopenblas``, ``libceed``, ``libxsmm``) has to win
    over any same-named one MSYS2 might carry.

    Args:
        install_prefix: The superbuild install prefix. MinGW installs DLLs into
            its ``bin``, and Palace installs ``libceed`` and ``libxsmm`` into
            its ``lib``.
        msmpi_dir: Where ``wheelbuild.msmpi`` wrote the verified MS-MPI files.
        toolchain_dir: MSYS2's UCRT64 ``bin``.

    Returns:
        The directories, highest priority first.
    """
    return (install_prefix / "bin", install_prefix / "lib", msmpi_dir, toolchain_dir)


def roots(*, install_prefix: Path, msmpi_dir: Path) -> tuple[Path, ...]:
    """Return the executables whose closure the wheel carries.

    These are the solver and MS-MPI's ``mpiexec.exe`` and ``smpd.exe``. The
    launchers import only system DLLs today, so they add nothing, but the wheel
    ships them, so their imports belong in the walk.

    Args:
        install_prefix: The superbuild install prefix.
        msmpi_dir: Where ``wheelbuild.msmpi`` wrote the verified MS-MPI files.

    Returns:
        The solver first, then the launchers.
    """
    launchers = tuple(
        msmpi_dir / entry.name
        for entry in msmpi.REDISTRIBUTABLE_FILES
        if entry.name.endswith(".exe")
    )
    return (assemble.find_palace_binary(install_prefix), *launchers)


def repair(
    *, binaries: Iterable[Path], search: Sequence[Path], output_dir: Path
) -> tuple[Path, ...]:
    """Copy the DLL closure of ``binaries`` into ``output_dir``, flat.

    The output directory is emptied first, so a DLL that a previous run copied
    and this one no longer needs cannot reach the wheel.

    Args:
        binaries: The executables to walk from.
        search: Where to find DLLs, highest priority first; see
            :func:`search_path`.
        output_dir: The payload directory to fill.

    Returns:
        The DLLs copied, each as its source path, in the order the walk reached
        them.

    Raises:
        wheelbuild.link_check.UnresolvedImportError: If an import is neither a
            system DLL nor in ``search``. Nothing is copied in that case.
    """
    closure = link_check.pe_import_closure(binaries, search)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    for source in closure:
        shutil.copy2(source, output_dir / source.name)
    return closure


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the Windows repair."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-prefix", type=Path, required=True)
    parser.add_argument(
        "--msmpi-dir",
        type=Path,
        required=True,
        help="where wheelbuild.msmpi wrote the MS-MPI redistributable",
    )
    parser.add_argument(
        "--toolchain-dir",
        type=Path,
        default=DEFAULT_TOOLCHAIN_DIR,
        help=f"MSYS2's UCRT64 bin directory (default: {DEFAULT_TOOLCHAIN_DIR})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="payload directory to fill; emptied first",
    )
    args = parser.parse_args(argv)
    search = search_path(
        install_prefix=args.install_prefix,
        msmpi_dir=args.msmpi_dir,
        toolchain_dir=args.toolchain_dir,
    )
    executables = roots(install_prefix=args.install_prefix, msmpi_dir=args.msmpi_dir)
    copied = repair(binaries=executables, search=search, output_dir=args.output_dir)
    # Logged rather than checked against a list: the toolchain rolls
    # (ADR-0007 §2), and the link check and the notice audit already fail on a
    # DLL that is missing or that no notice covers.
    print(f"walked {', '.join(path.name for path in executables)}")
    print(f"copied {len(copied)} DLLs into {args.output_dir}:")
    for source in sorted(copied, key=lambda path: path.name.lower()):
        print(
            f"  {source.name:<24} {source.stat().st_size:>12,} B  from {source.parent}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
