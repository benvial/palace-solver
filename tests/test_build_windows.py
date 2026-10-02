"""Facts about the Windows driver that are cheaper to assert than to run.

scripts/build-windows.sh runs only on a Windows runner under MSYS2, about two
hours twenty cold, so these hold the choices it must not lose.
"""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-windows.sh"


def _script():
    return SCRIPT.read_text()


def test_it_refuses_to_run_outside_msys2_ucrt64():
    assert '"${MSYSTEM:-}" != "UCRT64"' in _script()


def test_every_wheelbuild_step_runs_on_a_native_cpython():
    """MSYS2's pythons report mingw_x86_64_ucrt or msys and would mis-tag the
    wheel; sysconfig is what tells them from the runner's."""
    script = _script()

    assert "sysconfig.get_platform()" in script
    assert '"win-amd64"' in script
    assert "pythonLocation" in script


def test_cmake_is_kitwares_3_31_checked_by_hash():
    script = _script()

    assert "cmake_version=3.31.8" in script
    assert "sha256sum --check" in script
    assert "github.com/Kitware/CMake/releases/download" in script


def test_sub_projects_without_a_generator_get_msys_makefiles():
    assert 'export CMAKE_GENERATOR="MSYS Makefiles"' in _script()


def test_the_tarball_commit_is_not_the_release_tag():
    """wheelbuild.patches commits the carried patches on top and moves the
    release tag there, so `palace --version` prints the release, not -dirty."""
    script = _script()

    assert 'tag "upstream-v$palace_version"' in script
    assert 'tag "v$palace_version"' not in script


def test_the_installed_binary_must_report_the_release_exactly():
    assert 'grep -x "Palace version: v$palace_version"' in _script()


def test_msmpi_is_fetched_outside_the_build_root():
    """It is fetched and hash-checked on every run, and never cached."""
    script = _script()

    assert "wheelbuild.msmpi" in script
    assert "-msmpi" in script


def test_the_script_is_checked_out_with_lf_line_endings():
    """MSYS2's bash would read every CRLF line as ending in a stray \\r."""
    attributes = (SCRIPT.parents[1] / ".gitattributes").read_text()

    assert "*.sh text eol=lf" in attributes
    assert b"\r" not in SCRIPT.read_bytes()
