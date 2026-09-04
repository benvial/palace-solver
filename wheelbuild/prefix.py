"""Keep one library directory in the shared install prefix.

Projects that use CMake's ``GNUInstallDirs`` install into ``lib64`` on
RHEL-family systems, while others (and MPICH's and OpenBLAS's own build
systems) use ``lib``. In the Palace superbuild the two disagree: ZFP installs
``libzfp.so`` into ``lib64`` and Palace links ``<prefix>/lib/libzfp.so``, so
the build fails with ``cannot find -lzfp``.

Making ``lib64`` a symlink to ``lib`` before anything is built means both
spellings resolve to the same files whichever convention a sub-project picks.
"""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Sequence
from pathlib import Path


def unify_lib_directories(prefix: Path) -> Path:
    """Make ``<prefix>/lib64`` an alias of ``<prefix>/lib``.

    Anything already installed into a real ``lib64`` directory is moved into
    ``lib`` first, so the function is safe to run against a warm build cache.

    Args:
        prefix: The shared install prefix.

    Returns:
        The ``lib`` directory every library now lives in.
    """
    library_dir = prefix / "lib"
    library_dir.mkdir(parents=True, exist_ok=True)
    alias = prefix / "lib64"

    if alias.is_symlink():
        alias.unlink()
    elif alias.is_dir():
        for path in alias.iterdir():
            destination = library_dir / path.name
            if destination.exists() or destination.is_symlink():
                continue
            shutil.move(str(path), str(destination))
        shutil.rmtree(alias)

    alias.symlink_to("lib", target_is_directory=True)
    return library_dir


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the prefix layout fix-up."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args(argv)
    library_dir = unify_lib_directories(args.prefix)
    print(f"lib64 aliases {library_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
