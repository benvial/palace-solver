"""Console-script wrapper that hands control to the packaged Palace binary."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pypalace_solver import binary_path, mpiexec_path


def mpiexec(argv: Sequence[str] | None = None) -> None:
    """Replace this process with the MPICH launcher vendored in the wheel.

    Args:
        argv: Arguments to forward to ``mpiexec``. Defaults to the arguments
            this console script was invoked with.

    Raises:
        SystemExit: If the wheel carries no process manager.
    """
    _exec_payload(mpiexec_path, argv, name="palace-mpiexec")


def main(argv: Sequence[str] | None = None) -> None:
    """Replace this process with the packaged Palace binary.

    Args:
        argv: Arguments to forward to Palace. Defaults to the arguments this
            console script was invoked with.

    Raises:
        SystemExit: If the wheel carries no Palace binary.
    """
    _exec_payload(binary_path, argv, name="palace")


def _exec_payload(
    resolve: Callable[[], Path], argv: Sequence[str] | None, *, name: str
) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        executable = resolve()
    except FileNotFoundError as error:
        print(f"{name}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    os.execv(str(executable), [str(executable), *arguments])
