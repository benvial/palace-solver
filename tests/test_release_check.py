"""The last gate before an upload that cannot be taken back.

PyPI never reuses a filename, so a release that went out missing a platform is
not correctable — the next release is the fix. These tests are about the one
question the publish job can still ask: does `dist/` hold exactly the set of
wheels a release is supposed to carry?
"""

from pathlib import Path

import pytest

from wheelbuild import release_check
from wheelbuild.platforms import supported_platform_tags

VERSION = "0.18.1.post1"


def _write(dist: Path, *names: str) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    for name in names:
        (dist / name).write_bytes(b"PK\x03\x04")
    return dist


def _name(tag: str, version: str = VERSION) -> str:
    return f"palace_solver-{version}-py3-none-{tag}.whl"


def test_a_complete_set_of_wheels_has_no_problems(tmp_path):
    dist = _write(tmp_path / "dist", *(_name(tag) for tag in supported_platform_tags()))

    assert release_check.problems(dist, version=VERSION) == []


def test_a_missing_platform_is_a_problem(tmp_path):
    """The case the ticket exists for: a tag that ships three of four wheels."""
    tags = supported_platform_tags()
    dist = _write(tmp_path / "dist", *(_name(tag) for tag in tags[:-1]))

    found = release_check.problems(dist, version=VERSION)

    assert len(found) == 1
    assert tags[-1] in found[0]


def test_an_empty_directory_is_a_problem(tmp_path):
    """A pattern that matched no artifact downloads nothing and warns; the
    upload would then succeed at publishing no wheel at all.
    """
    dist = _write(tmp_path / "dist")

    assert release_check.problems(dist, version=VERSION)


def test_a_missing_directory_is_a_problem(tmp_path):
    assert release_check.problems(tmp_path / "absent", version=VERSION)


def test_an_unexpected_file_is_a_problem(tmp_path):
    """gh-action-pypi-publish uploads everything in the directory, so a stray
    file is not inert.
    """
    dist = _write(
        tmp_path / "dist",
        *(_name(tag) for tag in supported_platform_tags()),
        "palace_solver-0.18.1.post1.tar.gz",
    )

    found = release_check.problems(dist, version=VERSION)

    assert len(found) == 1
    assert "tar.gz" in found[0]


def test_a_wheel_naming_another_version_is_a_problem(tmp_path):
    """Every wheel in a release comes from one commit, so a second version in
    the directory means something was collected that this run did not build.
    """
    tags = supported_platform_tags()
    dist = _write(
        tmp_path / "dist",
        *(_name(tag) for tag in tags[:-1]),
        _name(tags[-1], version="0.18.0"),
    )

    found = release_check.problems(dist, version=VERSION)

    assert any("0.18.0" in problem for problem in found)


def test_an_unrepaired_wheel_is_a_problem(tmp_path):
    """`linux_x86_64` is what an unrepaired wheel carries, and pip will not
    install it from PyPI at all.
    """
    tags = supported_platform_tags()
    dist = _write(
        tmp_path / "dist", *(_name(tag) for tag in tags[:-1]), _name("linux_x86_64")
    )

    assert release_check.problems(dist, version=VERSION)


def test_a_wheel_still_carrying_a_cpython_abi_tag_is_a_problem(tmp_path):
    """The payload is a binary with no Python ABI; a cp313 tag would publish a
    wheel every other interpreter refuses.
    """
    tags = supported_platform_tags()
    dist = tmp_path / "dist"
    dist.mkdir()
    for tag in tags[:-1]:
        (dist / _name(tag)).write_bytes(b"PK\x03\x04")
    (dist / f"palace_solver-{VERSION}-cp313-cp313-{tags[-1]}.whl").write_bytes(b"PK")

    assert release_check.problems(dist, version=VERSION)


def test_the_expected_names_are_one_per_supported_platform():
    names = release_check.expected_wheel_names(version=VERSION)

    assert len(names) == len(supported_platform_tags())
    assert set(names) == {_name(tag) for tag in supported_platform_tags()}


def test_the_distribution_component_is_the_project_name_escaped():
    """A wheel filename escapes the distribution name, so `palace-solver`
    becomes `palace_solver`; getting it wrong here would reject every real
    wheel.
    """
    import re
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    name = tomllib.loads(pyproject.read_text())["project"]["name"]

    assert re.sub(r"[-_.]+", "_", name) == release_check.DISTRIBUTION


def test_main_reports_and_fails_on_an_incomplete_set(tmp_path, capsys):
    tags = supported_platform_tags()
    dist = _write(tmp_path / "dist", *(_name(tag) for tag in tags[:-1]))

    code = release_check.main([str(dist), "--version", VERSION])

    assert code == 1
    assert tags[-1] in capsys.readouterr().out


def test_main_accepts_a_complete_set(tmp_path):
    dist = _write(tmp_path / "dist", *(_name(tag) for tag in supported_platform_tags()))

    assert release_check.main([str(dist), "--version", VERSION]) == 0


def test_main_defaults_to_the_packaged_version(tmp_path):
    """The version is not an argument the release operator has to keep in step
    with the tag: the same constant the tag check reads is the default.
    """
    from palace_solver import __version__

    dist = _write(
        tmp_path / "dist",
        *(_name(tag, version=__version__) for tag in supported_platform_tags()),
    )

    assert release_check.main([str(dist)]) == 0


@pytest.mark.parametrize(
    "tag",
    [
        "manylinux_2_28_x86_64",
        "manylinux_2_28_aarch64",
        "macosx_15_0_arm64",
        "win_amd64",
    ],
)
def test_every_supported_platform_is_one_the_tag_derivation_produces(tag):
    assert tag in supported_platform_tags()


def test_a_release_carries_four_wheels():
    """Three platforms from ADR-0006 and Windows from ADR-0007."""
    names = release_check.expected_wheel_names(version=VERSION)

    assert len(names) == 4
    assert _name("win_amd64") in names


def test_a_release_missing_only_the_windows_wheel_is_a_problem(tmp_path):
    tags = [tag for tag in supported_platform_tags() if tag != "win_amd64"]
    dist = _write(tmp_path / "dist", *(_name(tag) for tag in tags))

    found = release_check.problems(dist, version=VERSION)

    assert len(found) == 1
    assert "win_amd64" in found[0]
