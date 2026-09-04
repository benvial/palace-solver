"""Console-script wrapper that hands control to the packaged Palace binary."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from pypalace_solver import binary_path


def main(argv: Sequence[str] | None = None) -> None:
    """Replace this process with the packaged Palace binary.

    Args:
        argv: Arguments to forward to Palace. Defaults to the arguments this
            console script was invoked with.

    Raises:
        SystemExit: If the wheel carries no Palace binary.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        binary = binary_path()
    except FileNotFoundError as error:
        print(f"palace: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    os.execv(str(binary), [str(binary), *arguments])
