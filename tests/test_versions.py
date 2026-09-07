"""The package version and the Palace release it ships, kept in step.

``__version__`` is the wheel's number and mirrors the Palace release, with a
``.postN`` segment allowed for a packaging-only fix. ``PALACE_VERSION`` is
what the build scripts fetch from upstream, so it must be the release without
that segment: ``v0.17.0.post1`` is a tag only this repository has.
"""

from palace_solver import PALACE_VERSION, __version__


def test_the_palace_version_prefixes_the_package_version():
    assert __version__.startswith(PALACE_VERSION)


def test_the_palace_version_carries_no_packaging_segment():
    """A .postN reaching the build would 404 against upstream's tags."""
    assert ".post" not in PALACE_VERSION
    remainder = __version__.removeprefix(PALACE_VERSION)
    assert remainder == "" or remainder.startswith(".post")
