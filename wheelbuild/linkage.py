"""Make the packaged binary find ``libmpi`` inside the installed mpich wheel.

The wheel never vendors an MPI implementation. ``auditwheel repair`` is run
with ``--exclude 'libmpi*'``, which leaves ``libmpi.so.12`` as an unresolved
external reference; these RPATH entries are what resolve it at runtime.

Layout the entries encode, as pip installs both wheels into one environment::

    <prefix>/lib/libmpi.so.12                                     (mpich wheel)
    <prefix>/lib/pythonX.Y/site-packages/pypalace_solver/bin/     (this wheel)
    <prefix>/lib/pythonX.Y/site-packages/pypalace_solver/lib/     (this wheel)

So from either package directory, ``$ORIGIN/../../../..`` is ``<prefix>/lib``.
The pypalace runner injects ``LD_LIBRARY_PATH`` as a fallback for layouts that
do not nest site-packages two levels under ``<prefix>/lib`` (Debian's
``dist-packages``, ``--target`` installs).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from wheelbuild._process import check_call, is_elf

#: From ``pypalace_solver/{bin,lib}`` up to the environment's ``lib`` directory,
#: where the mpich wheel drops ``libmpi.so.12``.
MPI_LIB_RPATH = "$ORIGIN/../../../.."


@dataclass(frozen=True)
class _Component:
    """A payload directory of the wheel and the RPATH its ELF files need."""

    directory: Path
    rpath: tuple[str, ...]


#: The payload directories. ``libs`` is where ``auditwheel repair`` drops the
#: vendored libraries: it sits at the site-packages level, one directory closer
#: to the prefix than the package's own ``bin`` and ``lib``.
_COMPONENTS = {
    "bin": _Component(
        directory=Path("pypalace_solver/bin"),
        rpath=("$ORIGIN/../lib", MPI_LIB_RPATH),
    ),
    "lib": _Component(
        directory=Path("pypalace_solver/lib"),
        rpath=("$ORIGIN", MPI_LIB_RPATH),
    ),
    "libs": _Component(
        directory=Path("pypalace_solver.libs"),
        rpath=("$ORIGIN", "$ORIGIN/../../.."),
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


def append_rpath_command(target: Path, *, existing: str, component: str) -> list[str]:
    """Return the ``patchelf`` call that re-adds the mpich entry to an RPATH.

    ``auditwheel repair`` rewrites RPATHs to point at its vendor directory and
    drops ours, so the mpich entry is appended again afterwards. Existing
    entries are preserved and the mpich entry is never duplicated.

    Args:
        target: ELF file inside the unpacked wheel.
        existing: RPATH ``auditwheel`` left on the file, ``:``-separated.
        component: Payload component the file belongs to.

    Returns:
        The ``patchelf`` argument vector.
    """
    mpi_entry = rpath_entries(component)[-1]
    entries = [entry for entry in existing.split(":") if entry]
    if mpi_entry not in entries:
        entries.append(mpi_entry)
    return _set_rpath_command(target, entries)


def elf_targets(root: Path) -> list[tuple[Path, str]]:
    """List the ELF files of an unpacked wheel with their component.

    Args:
        root: Directory an unpacked wheel was extracted into.

    Returns:
        ``(path, component)`` pairs, in component then path order.
    """
    targets = []
    for component, spec in _COMPONENTS.items():
        directory = root / spec.directory
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and not path.is_symlink() and is_elf(path):
                targets.append((path, component))
    return targets


def restore_mpi_rpaths(root: Path) -> None:
    """Re-add the mpich RPATH entry to every ELF file of an unpacked wheel."""
    for path, component in elf_targets(root):
        existing = subprocess.check_output(
            ["patchelf", "--print-rpath", str(path)], text=True
        ).strip()
        check_call(append_rpath_command(path, existing=existing, component=component))


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
