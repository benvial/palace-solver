"""Read the wheel job's matrix rows, optionally narrowed to one platform.

The rows are ``.github/wheel-matrix.toml``; the ``plan`` job of
``.github/workflows/wheels.yml`` runs this and hands the result to the ``wheel``
job as ``include: ${{ fromJSON(needs.plan.outputs.rows) }}``. A static
``include`` list cannot be narrowed by a dispatch input, which is why the rows
are data that a job computes from rather than YAML the matrix holds.

Stdlib only (``tomllib``), because it runs on the runner's preinstalled Python
before anything is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from collections.abc import Sequence
from pathlib import Path

#: The rows, at the repository root's ``.github``.
MATRIX = Path(__file__).resolve().parents[1] / ".github" / "wheel-matrix.toml"


class MatrixError(ValueError):
    """The rows cannot be read, or the requested narrowing selects nothing."""


def rows(path: Path = MATRIX) -> list[dict[str, str]]:
    """Every row, in file order."""
    with path.open("rb") as handle:
        table = tomllib.load(handle)
    found = table.get("row")
    if not isinstance(found, list) or not found:
        raise MatrixError(f"{path} defines no [[row]] tables")
    return found


def select(
    all_rows: Sequence[dict[str, str]], only: str | None
) -> list[dict[str, str]]:
    """The rows a run builds: all of them, or the one whose tag is ``only``.

    Raises:
        MatrixError: If ``only`` names no row, which would otherwise be a run
            that builds nothing and reports success.
    """
    if only is None:
        return list(all_rows)
    chosen = [row for row in all_rows if row["tag"] == only]
    if not chosen:
        raise MatrixError(f"no matrix row has tag {only!r}")
    return chosen


def main(argv: Sequence[str] | None = None) -> int:
    """Print the rows as JSON, and write them to ``$GITHUB_OUTPUT`` when set."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="build only the row with this platform tag")
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    args = parser.parse_args(argv)
    chosen = json.dumps(select(rows(args.matrix), args.only or None))
    print(chosen)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"rows={chosen}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
