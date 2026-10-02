"""What the package tells a user about the machines it runs on.

Four wheels ship — ``manylinux_2_28_x86_64``, ``manylinux_2_28_aarch64``,
``macosx_15_0_arm64`` and ``win_amd64`` — and three of them carry a floor that a
pip error alone will not explain: the oldest macOS the payload was compiled
for, the oldest arm core the vendored OpenBLAS was, and the oldest Windows,
which ``win_amd64`` does not encode at all. The classifiers are where PyPI
describes the supported platforms and the README is where those floors are
written down in prose, so both go stale the moment a floor moves. These tests
tie them to the constants the build actually uses.
"""

import tomllib
from pathlib import Path

import pytest

from palace_solver import _exec
from wheelbuild import openblas, platforms

ROOT = Path(__file__).resolve().parent.parent

#: One trove classifier per operating system a wheel is built for. Deliberately
#: the specific Linux and macOS pair rather than a broader ``Operating System ::
#: POSIX``, which would claim the BSDs and AIX as well: the supported platform
#: set is closed, and a classifier is the only part of it PyPI shows. Windows
#: is the bare classifier: the per-version ones name desktop releases only, so
#: ``Windows 10`` and ``Windows 11`` would leave out Windows Server, which is
#: inside the floor and is the only Windows the wheel has been tested on.
SUPPORTED_OS_CLASSIFIERS = frozenset(
    {
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows",
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
    assert "Windows" not in metadata["description"]


def test_the_readme_does_not_call_the_package_a_linux_wheel(readme):
    assert "Linux binary wheel" not in readme


def test_the_readme_states_the_macos_floor(readme):
    """The floor is a build constant; the README is where a user reads it."""
    assert f"macOS {platforms.MACOS_DEPLOYMENT_TARGET}" in readme


def test_the_readme_states_the_arm_cpu_baseline(readme):
    """OpenBLAS spells the target ``ARMV8``; the README spells it ARMv8-A."""
    assert openblas.BASELINE_TARGETS["aarch64"].lower() in readme.lower()


def test_the_readme_states_the_windows_floor(readme):
    """``win_amd64`` is versionless, so the README is the only place a user
    learns the floor before ``_exec.py`` refuses an older Windows."""
    assert f"{_exec.WINDOWS_FLOOR_NAME} or later" in " ".join(readme.split())


def test_the_readme_says_the_ms_mpi_files_are_under_microsofts_terms(readme):
    """MS-MPI's licence asks that end users be bound by terms at least as
    protective as Microsoft's, so the README, which every wheel's metadata
    carries, says whose terms the MS-MPI files are under."""
    prose = " ".join(readme.split())
    assert "Microsoft MPI Redistributable license terms" in prose
    assert "not under this package's license" in prose
