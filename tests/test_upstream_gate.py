"""The upstream test gate's judge, its exclusion file and its fingerprint."""

import json
import os

import pytest

from wheelbuild import superbuild, upstream_gate
from wheelbuild.platforms import supported_platform_tags
from wheelbuild.upstream_gate import Exclusion

LINUX = "manylinux_2_28_x86_64"


def test_the_exclusion_file_is_well_formed():
    exclusions = upstream_gate.load_exclusions()

    assert exclusions
    for exclusion in exclusions:
        assert set(exclusion.platforms) <= set(supported_platform_tags())


def test_the_mfem_exclusions_cover_every_platform_and_the_symlink_one_only_windows():
    """Ticket 04 of the palace-tests effort: 18 cases everywhere, one on Windows."""
    exclusions = upstream_gate.load_exclusions()
    everywhere = [
        e for e in exclusions if set(e.platforms) == set(supported_platform_tags())
    ]
    windows = [e for e in exclusions if e.platforms == ("win_amd64",)]

    assert len(everywhere) == 18
    assert all("MFEM_USE_EXCEPTIONS" in e.reason for e in everywhere)
    assert [e.test for e in windows] == [
        "RemovePreviousOutput removes a symlink without following it"
    ]
    assert len(exclusions) == 19


def _write(tmp_path, text):
    path = tmp_path / "exclusions.toml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ('test = "A"\nplatforms = ["win_amd64"]\n', "exactly"),
        ('test = "A"\nplatforms = ["win_amd64"]\nreason = ""\n', "reason"),
        ('test = "A"\nplatforms = []\nreason = "r"\n', "platforms"),
        ('test = "A"\nplatforms = ["win32"]\nreason = "r"\n', "no wheel"),
        ('test = "A"\nplatforms = ["win_amd64", "win_amd64"]\nreason = "r"\n', "twice"),
        (
            'test = "A"\nplatforms = ["win_amd64"]\nreason = "r"\nnote = "x"\n',
            "exactly",
        ),
    ],
)
def test_a_malformed_exclusion_is_refused(tmp_path, entries, message):
    with pytest.raises(ValueError, match=message):
        upstream_gate.load_exclusions(_write(tmp_path, "[[exclusion]]\n" + entries))


def test_a_test_case_excluded_twice_is_refused(tmp_path):
    entry = '[[exclusion]]\ntest = "A"\nplatforms = ["win_amd64"]\nreason = "r"\n'

    with pytest.raises(ValueError, match="excluded twice"):
        upstream_gate.load_exclusions(_write(tmp_path, entry + entry))


EXCLUDED = (Exclusion("Aborts", (LINUX,), "needs exceptions"),)
REGISTERED = ["serial-Aborts", "mpi-Aborts", "serial-Passes", "mpi-Passes"]


def _judge(results, exclusions=EXCLUDED, registered=REGISTERED, platform=LINUX):
    return upstream_gate.judge(
        platform=platform, exclusions=exclusions, registered=registered, results=results
    )


def test_a_sweep_whose_only_failures_are_excluded_passes():
    results = [
        ("serial-Aborts", "fail"),
        ("mpi-Aborts", "fail"),
        ("serial-Passes", "run"),
        ("mpi-Passes", "run"),
    ]

    assert _judge(results) == []


def test_a_failure_that_is_not_excluded_fails_the_row():
    problems = _judge([("serial-Aborts", "fail"), ("serial-Passes", "fail")])

    assert problems == ["'serial-Passes' failed"]


def test_an_excluded_test_that_passes_fails_the_row():
    """On any rank count: the exclusion names the case, not one entry."""
    problems = _judge([("serial-Aborts", "fail"), ("mpi-Aborts", "run")])

    assert len(problems) == 1
    assert "'mpi-Aborts' passed but is excluded" in problems[0]


def test_an_excluded_test_that_times_out_or_skips_is_still_not_passing():
    assert _judge([("serial-Aborts", "notrun"), ("mpi-Aborts", "fail")]) == []


def test_a_skip_upstream_chose_is_not_a_failure():
    assert _judge([("serial-Passes", "notrun")]) == []


def test_an_exclusion_the_suite_does_not_register_fails_the_row():
    exclusions = (*EXCLUDED, Exclusion("Renamed upstream", (LINUX,), "r"))

    problems = _judge([("serial-Passes", "run")], exclusions=exclusions)

    assert problems == [
        "excluded test case 'Renamed upstream' is not registered by the suite"
    ]


def test_an_exclusion_for_another_platform_is_ignored_here():
    exclusions = (Exclusion("Passes", ("win_amd64",), "r"),)

    assert _judge([("serial-Passes", "run")], exclusions=exclusions) == []
    assert _judge([("serial-Aborts", "fail")], exclusions=exclusions) == [
        "'serial-Aborts' failed"
    ]


def test_a_sweep_that_ran_nothing_fails_the_row():
    assert _judge([]) == ["the sweep ran no tests"]


def test_the_judge_reads_ctests_reports(tmp_path):
    """The shapes ctest writes: two entries can share a name upstream."""
    report = tmp_path / "units.xml"
    report.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="Linux" tests="4" failures="1" skipped="1">\n'
        '  <testcase name="serial-PostOperator" classname="x" status="run"/>\n'
        '  <testcase name="serial-PostOperator" classname="x" status="run"/>\n'
        '  <testcase name="serial-Aborts" classname="x" time="0" status="fail">\n'
        '    <failure message="Failed"/>\n'
        "  </testcase>\n"
        '  <testcase name="serial-Skips" classname="x" time="0" status="notrun">\n'
        '    <skipped message="Skipped"/>\n'
        "  </testcase>\n"
        "</testsuite>\n"
    )
    tests = tmp_path / "tests.json"
    tests.write_text(json.dumps({"tests": [{"name": "serial-Aborts"}]}))

    assert upstream_gate.junit_results(report) == [
        ("serial-PostOperator", "run"),
        ("serial-PostOperator", "run"),
        ("serial-Aborts", "fail"),
        ("serial-Skips", "notrun"),
    ]
    assert upstream_gate.registered_entries(tests) == ["serial-Aborts"]


@pytest.fixture
def prefix(tmp_path):
    root = tmp_path / "install"
    (root / "bin").mkdir(parents=True)
    (root / "lib" / "cmake").mkdir(parents=True)
    (root / "share" / "palace" / "test").mkdir(parents=True)
    (root / "bin" / "palace-x86_64.bin").write_bytes(b"\x7fELF solver")
    (root / "lib" / "libpalace.so.0").write_bytes(b"\x7fELF library")
    (root / "lib" / "libpalace.so").symlink_to("libpalace.so.0")
    (root / "share" / "palace" / "test" / "ref.csv").write_text("1,2\n")
    (root / "include").mkdir()
    (root / "include" / "palace.hpp").write_text("// not tested\n")
    exclusions = tmp_path / "exclusions.toml"
    exclusions.write_text("")
    return root, exclusions


def test_the_fingerprint_follows_content_not_time(prefix):
    root, exclusions = prefix
    before = upstream_gate.fingerprint(root, exclusions)

    os.utime(root / "lib" / "libpalace.so.0", (0, 0))
    (root / "include" / "palace.hpp").write_text("// changed, not tested\n")

    assert upstream_gate.fingerprint(root, exclusions) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda root, _x: (root / "bin" / "palace-x86_64.bin").write_bytes(
            b"\x7fELF other"
        ),
        lambda root, _x: (root / "lib" / "libnew.so").write_bytes(b""),
        lambda root, _x: (root / "share" / "palace" / "test" / "ref.csv").write_text(
            "1,3\n"
        ),
        lambda root, _x: (
            (root / "lib" / "libpalace.so").unlink()
            or (root / "lib" / "libpalace.so").symlink_to("elsewhere")
        ),
        lambda _root, exclusions: exclusions.write_text("# edited\n"),
    ],
    ids=["solver", "new library", "test data", "symlink target", "exclusions"],
)
def test_the_fingerprint_changes_with_what_is_tested(prefix, change):
    root, exclusions = prefix
    before = upstream_gate.fingerprint(root, exclusions)

    change(root, exclusions)

    assert upstream_gate.fingerprint(root, exclusions) != before


def test_the_fingerprint_refuses_a_prefix_without_the_tests(prefix):
    root, exclusions = prefix
    for path in sorted((root / "share").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()

    with pytest.raises(FileNotFoundError):
        upstream_gate.fingerprint(root, exclusions)


def test_the_tests_are_built_with_jobs_and_installed_into_the_prefix(tmp_path):
    commands = superbuild.unit_test_commands(
        build_dir=tmp_path / "superbuild", install_prefix=tmp_path / "install", jobs=4
    )
    palace_build = tmp_path / "superbuild" / "palace-build"

    assert commands == [
        ["cmake", "--build", str(palace_build), "--target", "unit-tests", "-j4"],
        [
            "cmake",
            "--install",
            str(palace_build / "test" / "unit"),
            "--prefix",
            str(tmp_path / "install"),
        ],
    ]


def _run(tmp_path, monkeypatch, system):
    calls = []
    monkeypatch.setattr(superbuild, "mpi_home", lambda prefix: prefix)
    monkeypatch.setattr(
        superbuild, "check_call", lambda command, **_kw: calls.append(command)
    )
    superbuild.run(
        source_dir=tmp_path / "palace",
        build_dir=tmp_path / "superbuild",
        install_prefix=tmp_path / "install",
        prefix=tmp_path / "install",
        jobs=2,
        system=system,
    )
    return calls


def _tests(tmp_path):
    return superbuild.unit_test_commands(
        build_dir=tmp_path / "superbuild", install_prefix=tmp_path / "install", jobs=2
    )


def test_the_linux_run_builds_the_tests_after_the_superbuild(tmp_path, monkeypatch):
    calls = _run(tmp_path, monkeypatch, "Linux")

    assert calls[1] == ["cmake", "--build", ".", "-j2"]
    assert calls[2:] == _tests(tmp_path)


def test_the_windows_run_builds_the_tests_once_the_patches_are_proved(
    tmp_path, monkeypatch
):
    for name in ("prepare_palace", "discard_stale_dependencies", "apply_dependencies"):
        monkeypatch.setattr(superbuild.patches, name, lambda *_args: None)
    calls = []
    monkeypatch.setattr(
        superbuild.patches, "verify", lambda *_args: calls.append("verify")
    )
    monkeypatch.setattr(
        superbuild, "check_call", lambda command, **_kw: calls.append(command)
    )
    superbuild.run(
        source_dir=tmp_path / "palace",
        build_dir=tmp_path / "superbuild",
        install_prefix=tmp_path / "install",
        prefix=tmp_path / "install",
        jobs=2,
        system="Windows",
    )

    verified = calls.index("verify")
    assert calls[verified - 1] == ["cmake", "--build", ".", "-j2"]
    assert calls[verified + 1 :] == _tests(tmp_path)


def test_darwin_gives_palace_an_rpath_and_installs_the_relinked_palace_in_the_same_run(
    tmp_path, monkeypatch
):
    """The reconfigure relinks libpalace. Building the superbuild again
    reinstalls it now, so the next run installs nothing new and the gate
    fingerprint holds."""
    calls = _run(tmp_path, monkeypatch, "Darwin")
    build = ["cmake", "--build", ".", "-j2"]

    assert calls[1:4] == [
        build,
        [
            "cmake",
            f"-DCMAKE_BUILD_RPATH={tmp_path / 'install' / 'lib'}",
            str(tmp_path / "superbuild" / "palace-build"),
        ],
        build,
    ]
    assert calls[4:] == _tests(tmp_path)


def test_darwin_leaves_a_build_tree_that_has_the_rpath_alone(tmp_path, monkeypatch):
    cache = tmp_path / "superbuild" / "palace-build" / "CMakeCache.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        f"CMAKE_BUILD_RPATH:UNINITIALIZED={tmp_path / 'install' / 'lib'}\n"
    )

    calls = _run(tmp_path, monkeypatch, "Darwin")

    assert calls[1] == ["cmake", "--build", ".", "-j2"]
    assert calls[2:] == _tests(tmp_path)


def test_darwin_corrects_a_build_rpath_to_another_prefix(tmp_path):
    cache = tmp_path / "superbuild" / "palace-build" / "CMakeCache.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text("CMAKE_BUILD_RPATH:UNINITIALIZED=/elsewhere/lib\n")

    assert superbuild.build_rpath_command(
        build_dir=tmp_path / "superbuild",
        install_prefix=tmp_path / "install",
        system="Darwin",
    )


def test_linux_needs_no_build_rpath(tmp_path):
    assert (
        superbuild.build_rpath_command(
            build_dir=tmp_path / "superbuild",
            install_prefix=tmp_path / "install",
            system="Linux",
        )
        is None
    )
