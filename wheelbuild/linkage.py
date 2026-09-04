"""Set the RPATHs of the payload before ``auditwheel repair`` vendors its deps.

The wheel carries its own MPI, so every shared library the binary needs ends up
inside the wheel and ``auditwheel`` handles the vendored ones. These entries
only let the binary find the libraries staged next to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wheelbuild._process import check_call, is_elf


@dataclass(frozen=True)
class _Component:
    """A payload directory of the wheel and the RPATH its ELF files need."""

    directory: Path
    rpath: tuple[str, ...]


#: The payload directories of the wheel and the RPATH each one needs.
_COMPONENTS = {
    "bin": _Component(
        directory=Path("pypalace_solver/bin"),
        rpath=("$ORIGIN/../lib",),
    ),
    "lib": _Component(
        directory=Path("pypalace_solver/lib"),
        rpath=("$ORIGIN",),
    ),
}


def rpath_entries(component: str) -> list[str]:
    """Return the RPATH entries for files in ``pypalace_solver/<component>``.

    Args:
        component: Either ``"bin"`` or ``"lib"``.

    Returns:
        RPATH entries, vendored libraries first, mpich wheel second.

    Raises:
        ValueError: If ``component`` is not a payload directory of the wheel.
    """
    try:
        return list(_COMPONENTS[component].rpath)
    except KeyError:
        raise ValueError(
            f"no RPATH defined for wheel component {component!r}; "
            f"expected one of {sorted(_COMPONENTS)}"
        ) from None


def patchelf_command(target: Path, component: str) -> list[str]:
    """Return the ``patchelf`` invocation that sets ``target``'s RPATH."""
    return _set_rpath_command(target, rpath_entries(component))


def _set_rpath_command(target: Path, entries: list[str]) -> list[str]:
    return [
        "patchelf",
        "--force-rpath",
        "--set-rpath",
        ":".join(entries),
        str(target),
    ]


def set_rpaths(package_dir: Path) -> None:
    """Rewrite the RPATH of every ELF file in the staged package tree.

    Args:
        package_dir: The staged ``pypalace_solver`` directory.
    """
    for component in ("bin", "lib"):
        directory = package_dir / component
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.is_symlink() and is_elf(path):
                check_call(patchelf_command(path, component))
