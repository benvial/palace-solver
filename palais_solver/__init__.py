"""Palace solver binary, packaged as a Python wheel.

The wheel ships the ``palace`` executable and its vendored shared libraries
inside this package. ``binary_path()`` is the resolution hook that palais's
runner uses to find the packaged solver.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "MPICH_VERSION",
    "__version__",
    "binary_path",
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

#: MPICH release vendored in this wheel, matching the ``mpich`` pin palais
#: declares for mpi4py. The build step and the runtime launcher guard both read
#: it from here; ``wheelbuild.interop`` checks it against palais's pin.
MPICH_VERSION = "4.3.2"

_PACKAGE_DIR = Path(__file__).resolve().parent


def binary_path() -> Path:
    """Return the path of the packaged Palace executable.

    Callers that launch this path themselves bypass the ``palace`` console
    script, and with it the launcher guard that refuses a process manager from
    another MPICH major series. Call :func:`launcher_conflict` from the
    launching process to make the same check, or launch the ``palace`` console
    script instead.

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
            "palais-solver does not contain a Palace binary"
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
            "of palais-solver does not contain the MPICH process manager"
        )
    return candidate


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

    from palais_solver import _launcher  # noqa: PLC0415

    return _launcher.refusal_reason(os.environ, _launcher.parent_executable())


def lib_dir() -> Path:
    """Return the directory holding the vendored shared libraries.

    ``auditwheel`` puts them in a ``palais_solver.libs`` directory beside the
    package, and the binary finds them through its RPATH; the path is exposed
    for callers that want to set ``LD_LIBRARY_PATH`` themselves.
    """
    return _PACKAGE_DIR.parent / "palais_solver.libs"
