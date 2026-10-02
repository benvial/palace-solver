"""Palace solver binary, packaged as a Python wheel.

The wheel ships the ``palace`` executable and its vendored shared libraries
inside this package. ``executable_path()`` and ``binary_path()`` are the
resolution hooks a caller uses to find the packaged solver.
"""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

__all__ = [
    "MPICH_VERSION",
    "__version__",
    "binary_path",
    "console_script_path",
    "executable_path",
    "launcher_conflict",
    "lib_dir",
    "mpiexec_path",
]

#: Single source of the version, mirroring the Palace release this wheel ships
#: (with a ``.postN`` segment for packaging-only fixes). ``pyproject.toml``,
#: the build scripts and CI all read it from here.
__version__ = "0.18.1.post2"

#: Palace release shipped by this wheel: the package version without any
#: ``.postN`` packaging segment, since ``0.17.0.post1`` still ships Palace
#: ``0.17.0``. The build scripts fetch this release's tag from upstream, which
#: is why the segment must not reach them: ``v0.17.0.post1`` is a tag that
#: exists only here.
PALACE_VERSION = __version__.split(".post", maxsplit=1)[0]

#: Name of the real solver executable inside :data:`_PACKAGE_DIR` / ``bin``,
#: less the ``.exe`` it carries on Windows; see :func:`_executable_name`.
BINARY_NAME = "palace-real"

#: Name of the vendored MPI process manager inside the same directory, less
#: its ``.exe`` on Windows.
LAUNCHER_NAME = "mpiexec"

#: Name of the console script this package installs, which wraps the binary
#: with the launcher guard in :mod:`._launcher`, less its ``.exe`` on Windows.
CONSOLE_SCRIPT_NAME = "palace"

#: MPICH release vendored in this wheel. The build step and the runtime
#: launcher guard both read it from here; ``wheelbuild.pin_check`` fails CI if
#: it leaves the major series the interop test proves the wheel against.
MPICH_VERSION = "4.3.2"

_PACKAGE_DIR = Path(__file__).resolve().parent


def _executable_name(name: str) -> str:
    """Return the file name ``name`` is installed under on this platform.

    Windows runs only files named ``.exe``, so there the solver, the process
    manager and the console script all carry it; elsewhere none does.
    """
    return f"{name}.exe" if sys.platform == "win32" else name


def binary_path() -> Path:
    """Return the path of the packaged Palace executable.

    This is the raw binary, and launching it directly bypasses the ``palace``
    console script and with it the launcher guard, which is the only thing
    standing between a badly launched run and silently wrong results. Use
    :func:`executable_path` to launch the solver; use this when the binary
    itself is what is wanted.

    Returns:
        Absolute path to the ``palace-real`` binary shipped in this wheel
        (``palace-real.exe`` on Windows).

    Raises:
        FileNotFoundError: If the wheel was installed without its binary
            payload (for example an editable install of the source tree).
    """
    candidate = _PACKAGE_DIR / "bin" / _executable_name(BINARY_NAME)
    if not candidate.is_file():
        raise FileNotFoundError(
            f"{candidate.name} is missing from {candidate.parent}; this install of "
            "palace-solver does not contain a Palace binary"
        )
    return candidate


def mpiexec_path() -> Path:
    """Return the path of the MPI process manager vendored in this wheel.

    The wheel carries its own MPI, so multi-rank runs do not depend on an
    ``mpiexec`` being installed elsewhere in the environment. It is MPICH's
    Hydra, or MS-MPI's ``mpiexec.exe`` on Windows.

    Returns:
        Absolute path to the vendored ``mpiexec``.

    Raises:
        FileNotFoundError: If the wheel was installed without its binary
            payload.
    """
    candidate = _PACKAGE_DIR / "bin" / _executable_name(LAUNCHER_NAME)
    if not candidate.is_file():
        raise FileNotFoundError(
            f"{candidate.name} is missing from {candidate.parent}; this install "
            "of palace-solver does not contain the MPI process manager"
        )
    return candidate


def console_script_path() -> Path | None:
    """Return the ``palace`` console script this package installed, if found.

    The console script is what carries the launcher guard: it checks that the
    rank it is starting was handed an MPI rendezvous before handing control to
    the binary. The binary itself cannot check anything.

    The script is looked for beside the running interpreter — which is where a
    virtual environment puts it — and never on ``PATH``, because a ``palace``
    on ``PATH`` may well be a Palace built from source, and returning that
    would silently run a different solver than the one this wheel ships. For
    the same reason a script that does not reference this package is rejected.

    On Windows the script is ``palace.exe``: a launcher executable carrying
    the Python script as a zip archive, which both pip and uv store
    uncompressed, so the package name is searched for in the file's bytes
    rather than its text.

    Returns:
        Path of the console script, or ``None`` if it cannot be located.
    """
    name = _executable_name(CONSOLE_SCRIPT_NAME)
    candidates = (
        Path(sysconfig.get_path("scripts")) / name,
        Path(sys.executable).parent / name,
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_bytes()
        except OSError:
            continue
        if __name__.encode() in content:
            return candidate
    return None


def executable_path() -> Path:
    """Return what a caller should launch to run the packaged solver.

    This is the resolution hook a caller should use. It prefers the ``palace``
    console script, so that multi-rank launches go through the launcher guard,
    and falls back to the raw binary when the script cannot be located.

    Prefer this over :func:`binary_path`, which returns the unguarded binary.

    Returns:
        Path of the console script, or of the packaged binary.

    Raises:
        FileNotFoundError: If the wheel was installed without its binary
            payload.
    """
    script = console_script_path()
    return binary_path() if script is None else script


def launcher_conflict() -> str | None:
    """Return why this process must not launch the solver, if there is one.

    A process manager that starts ranks without handing them an MPI rendezvous
    does not fail: each rank becomes its own ``MPI_COMM_WORLD``, solves the
    whole problem alone and exits 0. The ``palace`` console script makes this
    check for itself; a caller that launches :func:`binary_path` directly has
    to make it here.

    Note that this reports on the *calling* process's own launch environment,
    so it is worth calling from inside each rank rather than from a parent that
    spawns them.

    Returns:
        An operator-facing message naming the conflict, or ``None`` when the
        launch is allowed.
    """
    import os  # noqa: PLC0415

    from palace_solver import _launcher  # noqa: PLC0415

    return _launcher.refusal_reason(os.environ, _launcher.parent_executable())


def lib_dir() -> Path:
    """Return the directory holding the vendored shared libraries.

    The repair tool decides where they live and the three disagree:
    ``auditwheel`` puts them in a ``palace_solver.libs`` directory *beside* the
    package, ``delocate`` puts them in a ``.dylibs`` directory *inside* it, and
    on Windows they sit in ``bin`` with the executables, because an ``.exe``
    has no RPATH and the loader looks in its own directory first. The binary
    finds them in every case — through its RPATH on Linux, through
    ``@loader_path`` install names on macOS, through the loader's search order
    on Windows — so this is for callers that want to set ``LD_LIBRARY_PATH``,
    ``DYLD_LIBRARY_PATH`` or ``PATH`` themselves, and for them the difference
    is the whole answer.
    """
    if sys.platform == "win32":
        return _PACKAGE_DIR / "bin"
    if sys.platform == "darwin":
        return _PACKAGE_DIR / ".dylibs"
    return _PACKAGE_DIR.parent / "palace_solver.libs"
