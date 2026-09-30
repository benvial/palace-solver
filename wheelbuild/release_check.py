"""Refuse to publish a release that is missing a platform's wheel.

A release is a single unrecoverable event across three filenames. PyPI never
lets a filename be reused, so a tag that uploaded two of the three wheels
cannot be repaired by re-running the job: the fix is another version number,
and until it exists every user on the missing platform installs nothing. That
makes the arithmetic between the matrix and ``dist/`` worth checking on its
own, rather than trusting that the download step collected what the build
produced.

Two failure shapes this catches, neither of which is an error anywhere else.
An artifact that was never uploaded, or a download pattern that stopped
matching one, leaves ``dist/`` short a wheel — and ``actions/download-artifact``
treats matching nothing as a warning, while ``gh-action-pypi-publish``
uploads whatever the directory happens to hold. Conversely, anything extra in
the directory *is* uploaded, so a stray file is not inert either.

The check is filename equality against the set the release should carry,
reconstructed from :func:`wheelbuild.platforms.supported_platform_tags` and the
tags :mod:`wheelbuild.assemble` retags every wheel with. That covers the
platform set, the version and the ``py3-none`` retag in one comparison,
because each of those is a component of the name.

What it deliberately does not do is open the wheels. Every payload question --
the vendored libraries, the platform tag against the Mach-O headers, the size
against PyPI's limit -- was answered on the runner that built it, where the
install prefix it was built from was still there to compare against. This runs
on a runner that has none of that and only the finished files.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from palace_solver import __version__
from wheelbuild.assemble import ABI_TAG, PYTHON_TAG
from wheelbuild.platforms import supported_platform_tags

#: The distribution component of a wheel filename: the project name from
#: ``pyproject.toml`` with its separators escaped to underscores, per the binary
#: distribution format. Written down rather than derived because this runs
#: without the build's dependencies, and tied to ``pyproject.toml`` by
#: ``tests/test_release_check.py``.
DISTRIBUTION = "palace_solver"


def expected_wheel_names(*, version: str | None = None) -> tuple[str, ...]:
    """Return the filename of every wheel a release is supposed to carry.

    Args:
        version: Version the release publishes. Defaults to
            ``palace_solver.__version__``, which is what the wheels were built
            from and what the tag check already compared the tag against.

    Returns:
        One filename per supported platform.
    """
    resolved = __version__ if version is None else version
    return tuple(
        f"{DISTRIBUTION}-{resolved}-{PYTHON_TAG}-{ABI_TAG}-{tag}.whl"
        for tag in supported_platform_tags()
    )


def problems(directory: Path, *, version: str | None = None) -> list[str]:
    """Report every way ``directory`` is not the set of wheels to publish.

    Args:
        directory: The directory the upload step will publish, contents and
            all.
        version: Version the release publishes; defaults as
            :func:`expected_wheel_names` does.

    Returns:
        A message per finding, empty when the directory holds exactly the
        expected wheels and nothing else.
    """
    expected = set(expected_wheel_names(version=version))
    if not directory.is_dir():
        return [
            f"{directory} is not a directory, so no wheel was collected; "
            f"this release needs all {len(expected)} of "
            f"{', '.join(sorted(expected))}"
        ]

    present = {entry.name: entry for entry in directory.iterdir()}
    found: list[str] = []

    missing = sorted(expected - set(present))
    if missing:
        found.append(
            f"{directory} is missing {len(missing)} of the {len(expected)} "
            f"wheels this release carries: {', '.join(missing)}. Publishing "
            "now would ship a release no later run can complete, because PyPI "
            "never reuses a filename."
        )

    unexpected = sorted(set(present) - expected)
    if unexpected:
        # The upload step publishes the whole directory, so this is not a
        # tidiness complaint. A subdirectory in particular is the signature of
        # a download that did not merge the artifacts into one directory.
        listing = ", ".join(
            f"{name}/" if present[name].is_dir() else name for name in unexpected
        )
        found.append(
            f"{directory} holds {len(unexpected)} entry/entries this release "
            f"does not publish, and the upload step would publish them: "
            f"{listing}. Expected exactly {', '.join(sorted(expected))}."
        )
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the pre-publish wheel-set check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, help="the directory the upload step publishes"
    )
    parser.add_argument(
        "--version",
        default=None,
        help="version the release publishes; defaults to palace_solver.__version__",
    )
    args = parser.parse_args(argv)

    found = problems(args.directory, version=args.version)
    if found:
        for problem in found:
            print(f"::error::{problem}")
        return 1
    names = expected_wheel_names(version=args.version)
    print(f"{args.directory} holds all {len(names)} wheels this release carries:")
    for name in sorted(names):
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
