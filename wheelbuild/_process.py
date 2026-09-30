"""Small shared helpers for the build steps."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


def check_call(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Echo a command and run it, raising if it fails.

    Args:
        command: Argument list to run.
        cwd: Working directory, or the current one.
        env: Extra environment variables, layered over the inherited
            environment rather than replacing it -- the build steps need the
            whole toolchain environment they were started with, and add to it.
            Echoed with the command, since a variable that changes what a tool
            produces is part of the invocation.
    """
    assignments = [f"{name}={value} " for name, value in sorted((env or {}).items())]
    print("+ " + "".join(assignments) + " ".join(command), flush=True)
    subprocess.check_call(
        list(command),
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else {**os.environ, **env},
    )
