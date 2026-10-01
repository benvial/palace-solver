"""Refuse to save a build tree the cache would fail to read.

A cache that fails open is worse than no cache: one file the saving user
cannot read makes ``actions/cache/save``'s tar exit 2, which the action
downgrades to a warning, so the next run pays a full cold build with nothing
anywhere to explain why.

Asking the question in Python rather than with ``find`` is what makes it the
same question on every platform. GNU find's ``-readable`` does not exist in BSD
find, and its nearest portable spelling — ``! -perm -u+r`` — answers a
different question: a root-owned file at mode 0600 has the owner read bit set
while the runner user still cannot read it, which is precisely the case this
guard was written for.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from pathlib import Path

#: How many paths to report. The list is evidence, not an inventory: one
#: unreadable file is already a failed save.
REPORT_LIMIT = 20


def unreadable_paths(root: Path, *, limit: int = REPORT_LIMIT) -> list[str]:
    """Return paths under ``root`` this process cannot read.

    Symbolic links are skipped: tar stores the link rather than following it,
    so a dangling one is not a save failure, while ``os.access`` on it reports
    the target it cannot reach.

    Args:
        root: Directory to walk.
        limit: Stop after this many findings.

    Returns:
        Unreadable directories and files, at most ``limit`` of them, in walk
        order.
    """
    found: list[str] = []

    def note(path: str | Path | None) -> None:
        if path is not None and len(found) < limit:
            found.append(str(path))

    if not os.access(root, os.R_OK | os.X_OK):
        note(root)
        return found

    def on_error(error: OSError) -> None:
        note(error.filename)

    for directory, _subdirectories, filenames in os.walk(root, onerror=on_error):
        if len(found) >= limit:
            break
        _note_unreadable(Path(directory), filenames, note)
    return found


def _note_unreadable(
    directory: Path,
    filenames: list[str],
    note: Callable[[str | Path | None], None],
) -> None:
    """Report one directory, or the files in it if it can be entered at all."""
    if not os.access(directory, os.R_OK | os.X_OK):
        note(directory)
        return
    for name in filenames:
        path = directory / name
        if not path.is_symlink() and not os.access(path, os.R_OK):
            note(path)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the pre-save readability check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)

    if not args.root.exists():
        # A build that failed before the tree existed has nothing to save and
        # nothing to complain about.
        print(f"{args.root} does not exist; nothing to save")
        return 0

    found = unreadable_paths(args.root)
    if found:
        print(
            f"::error::unreadable entries under {args.root} would make the "
            "cache save fail silently"
        )
        for path in found:
            print(path)
        return 1
    print(f"{args.root} is readable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
