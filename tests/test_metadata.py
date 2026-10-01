"""What the package tells a user about the machines it runs on.

Three wheels ship — ``manylinux_2_28_x86_64``, ``manylinux_2_28_aarch64`` and
``macosx_15_0_arm64`` — and two of them carry a hardware floor that a pip error
alone will not explain: the oldest macOS the payload was compiled for, and the
oldest arm core the vendored OpenBLAS was. The classifiers are where PyPI
describes the supported platforms and the README is where those floors are
written down in prose, so both go stale the moment a floor moves. These tests
tie them to the constants the build actually uses.
"""

import tomllib
from pathlib import Path

import pytest

from wheelbuild import openblas, platforms

ROOT = Path(__file__).resolve().parent.parent

#: One trove classifier per operating system a wheel is built for. Deliberately
#: the specific pair rather than a broader ``Operating System :: POSIX``, which
#: would claim the BSDs and AIX as well: the supported platform set is closed,
#: and a classifier is the only part of it PyPI shows.
SUPPORTED_OS_CLASSIFIERS = frozenset(
    {
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS :: MacOS X",
    }
)


@pytest.fixture(scope="module")
def metadata():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


@pytest.fixture(scope="module")
def readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_a_classifier_names_every_operating_system_a_wheel_is_built_for(metadata):
    assert set(metadata["classifiers"]) >= SUPPORTED_OS_CLASSIFIERS


def test_no_classifier_claims_a_platform_the_build_refuses(metadata):
    """Catches both a stray ``OS Independent`` and a broadening to bare POSIX."""
    claimed = {
        classifier
        for classifier in metadata["classifiers"]
        if classifier.startswith("Operating System ::")
    }
    assert claimed == set(SUPPORTED_OS_CLASSIFIERS)


def test_the_summary_names_no_operating_system(metadata):
    """PyPI's one-line summary outlives any single platform's arrival."""
    assert "Linux" not in metadata["description"]
    assert "macOS" not in metadata["description"]


def test_the_readme_does_not_call_the_package_a_linux_wheel(readme):
    assert "Linux binary wheel" not in readme


def test_the_readme_states_the_macos_floor(readme):
    """The floor is a build constant; the README is where a user reads it."""
    assert f"macOS {platforms.MACOS_DEPLOYMENT_TARGET}" in readme


def test_the_readme_states_the_arm_cpu_baseline(readme):
    """OpenBLAS spells the target ``ARMV8``; the README spells it ARMv8-A."""
    assert openblas.BASELINE_TARGETS["aarch64"].lower() in readme.lower()
