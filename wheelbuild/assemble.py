"""Assemble, repair and retag the pypalace-solver platform wheel.

Pipeline: stage the superbuild install tree into the package directory, set
RPATHs, build a wheel, run ``auditwheel repair`` to vendor every shared library
it needs — MPI included, since the wheel carries its own — and retag the result
``py3-none`` because the payload is a binary with no Python ABI.
"""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pypalace_solver import BINARY_NAME, LAUNCHER_NAME
from wheelbuild import linkage
from wheelbuild._process import check_call, is_elf

#: Wheel platform tag, per the spec (manylinux_2_28, x86_64 first).
PLATFORM_TAG = "manylinux_2_28_x86_64"

#: PyPI's default per-file upload limit. Exceeding it needs a limit request.
PYPI_SIZE_LIMIT_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class SizeReport:
    """Size of a built wheel, measured against PyPI's upload limit."""

    path: Path
    size_bytes: int

    @property
    def exceeds_pypi_limit(self) -> bool:
        """Whether the wheel needs a PyPI file-size limit request."""
        return self.size_bytes > PYPI_SIZE_LIMIT_BYTES

    @property
    def text(self) -> str:
        """One-line human-readable report."""
        megabytes = self.size_bytes / (1024 * 1024)
        limit = PYPI_SIZE_LIMIT_BYTES // (1024 * 1024)
        verdict = "OVER" if self.exceeds_pypi_limit else "under"
        return (
            f"{self.path.name}: {megabytes:.1f} MB "
            f"({verdict} the {limit} MB PyPI upload limit)"
        )


def size_report(wheel: Path) -> SizeReport:
    """Measure ``wheel`` against PyPI's upload limit."""
    return SizeReport(path=wheel, size_bytes=wheel.stat().st_size)


def find_palace_binary(install_prefix: Path) -> Path:
    """Locate the real Palace executable in a superbuild install tree.

    Palace installs a small ``bin/palace`` launcher script alongside the actual
    ELF binary (``palace-<arch>.bin``); the wheel ships the ELF one and
    provides its own console script.

    Args:
        install_prefix: Superbuild install prefix.

    Returns:
        Path of the ELF Palace binary.

    Raises:
        FileNotFoundError: If no ELF ``palace*`` binary is installed.
    """
    candidates = sorted(
        path
        for path in (install_prefix / "bin").glob("palace*")
        if path.is_file() and not path.is_symlink() and is_elf(path)
    )
    if not candidates:
        raise FileNotFoundError(f"no Palace ELF binary under {install_prefix / 'bin'}")
    return candidates[0]


def stage(
    *, install_prefix: Path, package_dir: Path, notices: Path | None = None
) -> Path:
    """Copy the superbuild payload into the package directory.

    Args:
        install_prefix: Superbuild install prefix.
        package_dir: The ``pypalace_solver`` package directory to fill.
        notices: Optional THIRD-PARTY-NOTICES file to ship alongside.

    Returns:
        Path of the staged binary.
    """
    source_binary = find_palace_binary(install_prefix)
    binary_dir = package_dir / "bin"
    library_dir = package_dir / "lib"
    for directory in (binary_dir, library_dir):
        _clear_payload(directory)

    staged_binary = binary_dir / BINARY_NAME
    shutil.copy2(source_binary, staged_binary)
    staged_binary.chmod(staged_binary.stat().st_mode | 0o111)

    for launcher in _process_manager_binaries(install_prefix):
        destination = binary_dir / launcher.name
        shutil.copy2(launcher, destination)
        destination.chmod(destination.stat().st_mode | 0o111)

    for path in sorted((install_prefix / "lib").iterdir()):
        if path.is_dir() or path.is_symlink() or not _is_shared_library(path):
            continue
        shutil.copy2(path, library_dir / path.name)

    if notices is not None:
        shutil.copy2(notices, package_dir / "THIRD-PARTY-NOTICES")
    return staged_binary


def _clear_payload(directory: Path) -> None:
    """Empty a payload directory, keeping the tracked ``.gitkeep`` placeholder."""
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.name.startswith("."):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _process_manager_binaries(install_prefix: Path) -> list[Path]:
    """Return the Hydra process manager executables to ship.

    ``mpiexec`` and its ``hydra_*`` helpers are what run the solver on several
    ranks; the ``mpicc``-style compiler wrappers are build-time only shell
    scripts and stay out of the wheel.
    """
    return sorted(
        path
        for path in (install_prefix / "bin").iterdir()
        if path.is_file()
        and not path.is_symlink()
        and is_elf(path)
        and (path.name == LAUNCHER_NAME or path.name.startswith(("mpiexec", "hydra_")))
    )


def repair_command(*, wheel: Path, output_dir: Path) -> list[str]:
    """Return the ``auditwheel repair`` invocation for a built wheel.

    Nothing is excluded: the vendored MPICH is part of the payload, so the
    wheel is self-contained and needs no MPI installed beside it.
    """
    return [
        "auditwheel",
        "repair",
        "--plat",
        PLATFORM_TAG,
        "--wheel-dir",
        str(output_dir),
        str(wheel),
    ]


def retag_command(wheel: Path) -> list[str]:
    """Return the ``wheel tags`` invocation that forces the ``py3-none`` tag."""
    return [
        "wheel",
        "tags",
        "--python-tag",
        "py3",
        "--abi-tag",
        "none",
        "--platform-tag",
        PLATFORM_TAG,
        "--remove",
        str(wheel),
    ]


def build(
    *,
    project_dir: Path,
    install_prefix: Path,
    output_dir: Path,
    notices: Path | None = None,
) -> Path:
    """Run the full assemble → repair → retag pipeline.

    Args:
        project_dir: Repository root holding ``pyproject.toml``.
        install_prefix: Superbuild install prefix.
        output_dir: Where the repaired wheel is written.
        notices: Optional THIRD-PARTY-NOTICES file to ship.

    Returns:
        Path of the final wheel.
    """
    package_dir = project_dir / "pypalace_solver"
    stage(install_prefix=install_prefix, package_dir=package_dir, notices=notices)
    linkage.set_rpaths(package_dir)

    raw_dir = output_dir / "raw"
    shutil.rmtree(raw_dir, ignore_errors=True)
    check_call(
        ["python", "-m", "build", "--wheel", "--outdir", str(raw_dir), str(project_dir)]
    )
    raw_wheel = _single_wheel(raw_dir)

    check_call(repair_command(wheel=raw_wheel, output_dir=output_dir))
    repaired = _single_wheel(output_dir)
    check_call(retag_command(repaired))

    final = _single_wheel(output_dir)
    print(size_report(final).text, flush=True)
    return final


def _single_wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel in {directory}, found {wheels}")
    return wheels[0]


def _is_shared_library(path: Path) -> bool:
    return ".so" in path.name and path.is_file() and is_elf(path)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for wheel assembly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--install-prefix", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--notices", type=Path, default=None)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    build(
        project_dir=args.project_dir,
        install_prefix=args.install_prefix,
        output_dir=args.output_dir,
        notices=args.notices,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
