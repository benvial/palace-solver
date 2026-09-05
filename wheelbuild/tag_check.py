"""Keep a release tag and the version inside the wheel from disagreeing.

Two systems read a release from two different places: PyPI reads the version
recorded in the wheel, which comes from ``palace_solver.__version__``, and a
reader of the repository reads the git tag. Nothing ties them together, so a
tag pushed without the version bump — or bumped without moving the tag —
publishes a release under a number nobody chose, and the mistake is not
correctable afterwards because PyPI never reuses a filename.

This is that tie. CI runs it on tag pushes, before the hour-long build, so a
mismatched tag fails in seconds rather than at the upload.

Tags are ``v`` plus the version exactly as the module spells it: ``v0.17.0``
for the Palace release, ``v0.17.0.post1`` for a packaging-only fix on top of
it. The comparison is deliberately literal rather than a parsed-version
equality: ``v0.17.0.post01`` and ``v0.17.0.post1`` mean the same release to
Python's version rules but are two different tags to git, and a release should
not be reachable under two spellings.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from palace_solver import __version__

#: What a release tag starts with, before the version.
TAG_PREFIX = "v"


def tag_problem(tag: str, version: str | None = None) -> str | None:
    """Report a tag that does not name the version the wheel will carry.

    Args:
        tag: The git tag being released, ``v`` prefix included.
        version: Version the package records. Defaults to
            ``palace_solver.__version__``, read when the check runs rather
            than when this module was imported.

    Returns:
        A message, or ``None`` when the tag and the version agree.
    """
    version = __version__ if version is None else version
    if not tag.startswith(TAG_PREFIX):
        return (
            f"the release tag {tag!r} does not start with {TAG_PREFIX!r}; "
            f"tag this release {TAG_PREFIX}{version}"
        )
    tagged = tag[len(TAG_PREFIX) :]
    if tagged != version:
        return (
            f"the release tag {tag!r} names version {tagged!r}, but this "
            f"package records {version!r}. PyPI would publish {version!r} "
            "under a tag that says otherwise, and the filename cannot be "
            "reused once uploaded. Move palace_solver.__version__ or the tag, "
            "whichever is wrong."
        )
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the tag check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="the release tag, such as v0.17.0")
    args = parser.parse_args(argv)

    problem = tag_problem(args.tag)
    if problem is not None:
        print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print(f"tag {args.tag} matches the packaged version {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
