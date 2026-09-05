import pytest

import palace_solver
from palace_solver import _exec


def _fake_binary(tmp_path):
    binary = tmp_path / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_main_execs_real_binary_with_forwarded_arguments(tmp_path, monkeypatch):
    binary = _fake_binary(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.main(["config.json", "--verbose", "2"])

    assert calls == [(str(binary), [str(binary), "config.json", "--verbose", "2"])]


def test_main_reports_missing_binary_as_exit_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        _exec.main([])

    assert excinfo.value.code == 1
    assert "palace-real" in capsys.readouterr().err


def test_mpiexec_execs_the_vendored_launcher_with_forwarded_arguments(
    tmp_path, monkeypatch
):
    launcher = tmp_path / "bin" / "mpiexec"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.mpiexec(["-n", "2", "palace", "config.json"])

    assert calls == [
        (str(launcher), [str(launcher), "-n", "2", "palace", "config.json"])
    ]
