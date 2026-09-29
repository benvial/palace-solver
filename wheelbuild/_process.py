"""Small shared helpers for the build steps."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


def check_call(command: Sequence[str], *, cwd: Path | None = None) -> None:
    """Echo a command and run it, raising if it fails."""
    print("+ " + " ".join(command), flush=True)
    subprocess.check_call(list(command), cwd=None if cwd is None else str(cwd))
