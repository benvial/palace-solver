"""Palace solver binary, packaged as a Python wheel.

The wheel ships the ``palace`` executable and its vendored shared libraries
inside this package. ``binary_path()`` is the resolution hook that pypalace's
runner uses to find the packaged solver.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "binary_path", "lib_dir", "mpiexec_path"]

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

_PACKAGE_DIR = Path(__file__).resolve().parent


def binary_path() -> Path:
    """Return the path of the packaged Palace executable.

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
            "pypalace-solver does not contain a Palace binary"
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
            "of pypalace-solver does not contain the MPICH process manager"
        )
    return candidate


def lib_dir() -> Path:
    """Return the directory holding the vendored shared libraries.

    ``auditwheel`` puts them in a ``pypalace_solver.libs`` directory beside the
    package, and the binary finds them through its RPATH; the path is exposed
    for callers that want to set ``LD_LIBRARY_PATH`` themselves.
    """
    return _PACKAGE_DIR.parent / "pypalace_solver.libs"
