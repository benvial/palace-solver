"""Small shared helpers for the build steps."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

_ELF_MAGIC = b"\x7fELF"


def check_call(command: Sequence[str], *, cwd: Path | None = None) -> None:
    """Echo a command and run it, raising if it fails."""
    print("+ " + " ".join(command), flush=True)
    subprocess.check_call(list(command), cwd=None if cwd is None else str(cwd))


def is_elf(path: Path) -> bool:
    """Whether ``path`` is an ELF binary or shared library."""
    with path.open("rb") as handle:
        return handle.read(4) == _ELF_MAGIC
