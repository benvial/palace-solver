"""Palace solver binary, packaged as a Python wheel.

The wheel ships the ``palace`` executable and its vendored shared libraries
inside this package. ``binary_path()`` is the resolution hook that palais's
runner uses to find the packaged solver.
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
__version__ = "0.17.0"

#: Palace release shipped by this wheel.
PALACE_VERSION = __version__

#: Name of the real solver executable inside :data:`_PACKAGE_DIR` / ``bin``.
BINARY_NAME = "palace-real"

#: Name of the vendored MPICH process manager inside the same directory.
LAUNCHER_NAME = "mpiexec"

#: Name of the console script this package installs, which wraps the binary
#: with the launcher guard in :mod:`._launcher`.
CONSOLE_SCRIPT_NAME = "palace"

#: MPICH release vendored in this wheel, matching the ``mpich`` pin palais
#: declares for mpi4py. The build step and the runtime launcher guard both read
#: it from here; ``wheelbuild.interop`` checks it against palais's pin.
MPICH_VERSION = "4.3.2"

_PACKAGE_DIR = Path(__file__).resolve().parent


def binary_path() -> Path:
    """Return the path of the packaged Palace executable.

    This is the raw binary, and launching it directly bypasses the ``palace``
    console script and with it the launcher guard, which is the only thing
    standing between a badly launched run and silently wrong results. Use
    :func:`executable_path` to launch the solver; use this when the ELF binary
    itself is what is wanted.

    Returns:
        Absolute path to the ``palace-real`` binary shipped in this wheel.

    Raises:
        FileNotFoundError: If the wheel was installed without its binary
            payload (for example an editable install of the source tree).
    """
    candidate = _PACKAGE_DIR / "bin" / BINARY_NAME
    if not candidate.is_file():
        raise FileNotFoundError(
            f"{BINARY_NAME} is missing from {candidate.parent}; this install of "
            "palace-solver does not contain a Palace binary"
        )
    return candidate


def mpiexec_path() -> Path:
    """Return the path of the MPICH process manager vendored in this wheel.

    The wheel carries its own MPI, so multi-rank runs do not depend on an
    ``mpiexec`` being installed elsewhere in the environment.

    Returns:
        Absolute path to the vendored ``mpiexec``.

    Raises:
        FileNotFoundError: If the wheel was installed without its binary
            payload.
    """
    candidate = _PACKAGE_DIR / "bin" / LAUNCHER_NAME
    if not candidate.is_file():
        raise FileNotFoundError(
            f"{LAUNCHER_NAME} is missing from {candidate.parent}; this install "
            "of palace-solver does not contain the MPICH process manager"
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

    Returns:
        Path of the console script, or ``None`` if it cannot be located.
    """
    candidates = (
        Path(sysconfig.get_path("scripts")) / CONSOLE_SCRIPT_NAME,
        Path(sys.executable).parent / CONSOLE_SCRIPT_NAME,
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(errors="ignore")
        except OSError:
            continue
        if __name__ in content:
            return candidate
    return None


def executable_path() -> Path:
    """Return what a caller should launch to run the packaged solver.

    This is the resolution hook for palais's runner. It prefers the ``palace``
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
    check for itself; a caller that launches :func:`binary_path` directly —
    which is how palais's runner resolves the solver — has to make it here.

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

    ``auditwheel`` puts them in a ``palace_solver.libs`` directory beside the
    package, and the binary finds them through its RPATH; the path is exposed
    for callers that want to set ``LD_LIBRARY_PATH`` themselves.
    """
    return _PACKAGE_DIR.parent / "palace_solver.libs"
