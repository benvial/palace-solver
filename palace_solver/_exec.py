"""Console-script wrapper that hands control to the packaged Palace binary."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from palace_solver import _launcher, binary_path, mpiexec_path


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

    A rank a process manager started without an MPI rendezvous is refused here
    rather than left to solve the whole problem alone and exit 0; see
    :mod:`._launcher`.

    Args:
        argv: Arguments to forward to Palace. Defaults to the arguments this
            console script was invoked with.

    Raises:
        SystemExit: If the wheel carries no Palace binary, or this rank was
            launched without a rendezvous.
    """
    parent_exe = _launcher.parent_executable()
    note = _launcher.version_note(parent_exe)
    if note is not None:
        print(f"palace: {note}", file=sys.stderr)
    reason = _launcher.refusal_reason(os.environ, parent_exe)
    if reason is not None:
        print(f"palace: {reason}", file=sys.stderr)
        raise SystemExit(1)
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
