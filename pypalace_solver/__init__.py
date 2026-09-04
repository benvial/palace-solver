"""Palace solver binary, packaged as a Python wheel.

The wheel ships the ``palace`` executable and its vendored shared libraries
inside this package. ``binary_path()`` is the resolution hook that pypalace's
runner uses to find the packaged solver.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "binary_path", "lib_dir"]

#: Single source of the version, mirroring the Palace release this wheel ships
#: (with a ``.postN`` segment for packaging-only fixes). ``pyproject.toml``,
#: the build scripts and CI all read it from here.
__version__ = "0.17.0"

#: Palace release shipped by this wheel.
PALACE_VERSION = __version__

#: Name of the real solver executable inside :data:`_PACKAGE_DIR` / ``bin``.
BINARY_NAME = "palace-real"

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


def lib_dir() -> Path:
    """Return the directory holding the vendored shared libraries."""
    return _PACKAGE_DIR / "lib"
