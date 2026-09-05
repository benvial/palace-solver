from pathlib import Path

import pytest

import palais_solver
from palais_solver import _exec, _launcher

HYDRA_OUTPUT = """HYDRA build details:
    Version:                                 5.0.1
    Release Date:                            Fri Apr 10 09:45:31 AM CDT 2026
    CC:                              cc
"""


def test_pmi_variables_mark_a_process_manager_launch():
    assert _launcher.launched_by_process_manager({"PMI_RANK": "0", "PMI_SIZE": "2"})


def test_a_direct_run_is_not_a_process_manager_launch():
    assert not _launcher.launched_by_process_manager({"PATH": "/usr/bin"})


def test_hydra_version_is_read_from_the_build_details():
    assert _launcher.parse_hydra_version(HYDRA_OUTPUT) == "5.0.1"


def test_unrecognised_launcher_output_yields_no_version():
    assert _launcher.parse_hydra_version("mpirun (Open MPI) 5.0.3") is None


def test_same_major_series_is_compatible():
    assert _launcher.incompatibility("4.3.2", "4.1.0") is None


def test_a_different_major_series_is_reported_with_the_supported_launcher():
    message = _launcher.incompatibility("4.3.2", "5.0.1")

    assert message is not None
    assert "4.3.2" in message
    assert "5.0.1" in message
    assert "palace-mpiexec" in message


def test_the_vendored_process_manager_is_not_a_foreign_launcher(tmp_path):
    vendored_bin = tmp_path / "palais_solver" / "bin"
    vendored_bin.mkdir(parents=True)

    assert (
        _launcher.foreign_launcher_dir(
            vendored_bin=vendored_bin, parent_exe=vendored_bin / "hydra_pmi_proxy"
        )
        is None
    )


def test_a_process_manager_from_elsewhere_is_a_foreign_launcher(tmp_path):
    vendored_bin = tmp_path / "palais_solver" / "bin"
    foreign_bin = tmp_path / "venv" / "bin"

    assert (
        _launcher.foreign_launcher_dir(
            vendored_bin=vendored_bin, parent_exe=foreign_bin / "hydra_pmi_proxy"
        )
        == foreign_bin
    )


def test_an_unreadable_parent_is_not_a_foreign_launcher(tmp_path):
    assert (
        _launcher.foreign_launcher_dir(vendored_bin=tmp_path, parent_exe=None) is None
    )


def _check(environ, *, parent_exe, probe, vendored_bin=Path("/wheel/bin")):
    return _launcher.refusal_reason(
        environ,
        vendored_version="4.3.2",
        vendored_bin=vendored_bin,
        parent_exe=parent_exe,
        probe=probe,
    )


def test_a_direct_run_is_never_blocked():
    assert _check({}, parent_exe=Path("/bin/bash"), probe=lambda _: "5.0.1") is None


def test_the_vendored_launcher_is_never_probed():
    probed = []

    assert (
        _check(
            {"PMI_RANK": "0"},
            parent_exe=Path("/wheel/bin/hydra_pmi_proxy"),
            probe=probed.append,
        )
        is None
    )
    assert probed == []


def test_a_foreign_launcher_of_another_major_series_is_refused():
    message = _check(
        {"PMI_RANK": "0"},
        parent_exe=Path("/venv/bin/hydra_pmi_proxy"),
        probe=lambda _: "5.0.1",
    )

    assert message is not None
    assert "5.0.1" in message


def test_the_override_lets_a_mismatching_launcher_through():
    assert (
        _check(
            {"PMI_RANK": "0", _launcher.OVERRIDE_ENV: "1"},
            parent_exe=Path("/venv/bin/hydra_pmi_proxy"),
            probe=lambda _: "5.0.1",
        )
        is None
    )


def test_an_undeterminable_launcher_version_is_allowed_through():
    assert (
        _check(
            {"PMI_RANK": "0"},
            parent_exe=Path("/opt/slurm/bin/slurmstepd"),
            probe=lambda _: None,
        )
        is None
    )


def _fake_binary(tmp_path):
    binary = tmp_path / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_palace_refuses_to_start_under_a_mismatching_launcher(
    tmp_path, monkeypatch, capsys
):
    _fake_binary(tmp_path)
    monkeypatch.setattr(palais_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec.os, "execv", lambda *_: pytest.fail("exec'd"))
    monkeypatch.setattr(
        _launcher, "refusal_reason", lambda *_, **__: "launcher mismatch"
    )

    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["config.json"])

    assert excinfo.value.code == 1
    assert "launcher mismatch" in capsys.readouterr().err


def test_the_vendored_launcher_itself_is_not_guarded(tmp_path, monkeypatch):
    launcher = tmp_path / "bin" / "mpiexec"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(palais_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(
        _launcher, "refusal_reason", lambda *_, **__: pytest.fail("guarded")
    )
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.mpiexec(["-n", "2"])

    assert calls
