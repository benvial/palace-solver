import os

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


def _fake_launcher(tmp_path):
    launcher = tmp_path / "bin" / "mpiexec"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    return launcher


def test_parse_arguments_keeps_palace_arguments_and_reads_wrapper_options():
    invocation = _exec.parse_arguments(
        [
            "--np",
            "4",
            "-nt",
            "2",
            "--launcher-args",
            "--bind-to core",
            "--dry-run",
            "config.json",
        ]
    )

    assert invocation.num_procs == 4
    assert invocation.num_threads == 2
    assert invocation.launcher_args == ["--bind-to", "core"]
    assert invocation.palace_args == ["--dry-run", "config.json"]
    assert not invocation.serial


def test_parse_arguments_stops_reading_options_after_a_bare_separator():
    invocation = _exec.parse_arguments(["--", "--np", "config.json"])

    assert invocation.num_procs is None
    assert invocation.palace_args == ["--np", "config.json"]


def test_parse_arguments_rejects_a_rank_count_that_is_not_a_positive_integer(capsys):
    with pytest.raises(SystemExit) as excinfo:
        _exec.parse_arguments(["--np", "two", "config.json"])

    assert excinfo.value.code == 1
    assert "--np" in capsys.readouterr().err


def test_ranks_are_started_by_the_vendored_launcher_running_this_script(
    tmp_path, monkeypatch
):
    binary = _fake_binary(tmp_path)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec, "console_script_path", lambda: None)
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.main(["--np", "2", "config.json"])

    assert calls == [
        (str(launcher), [str(launcher), "-n", "2", str(binary), "config.json"])
    ]


def test_the_launcher_starts_the_console_script_so_every_rank_runs_the_guard(
    tmp_path, monkeypatch
):
    _fake_binary(tmp_path)
    launcher = _fake_launcher(tmp_path)
    script = tmp_path / "palace"
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec, "console_script_path", lambda: script)
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.main(["--np", "3", "--launcher-args", "-bind-to core", "config.json"])

    assert calls == [
        (
            str(launcher),
            [str(launcher), "-n", "3", "-bind-to", "core", str(script), "config.json"],
        )
    ]


def test_a_named_launcher_is_used_instead_of_the_vendored_one(tmp_path, monkeypatch):
    binary = _fake_binary(tmp_path)
    _fake_launcher(tmp_path)
    foreign = tmp_path / "foreign-mpiexec"
    foreign.write_text("#!/bin/sh\n")
    foreign.chmod(0o755)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec, "console_script_path", lambda: None)
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.main(["--np", "2", "--launcher", str(foreign), "config.json"])

    assert calls == [
        (str(foreign), [str(foreign), "-n", "2", str(binary), "config.json"])
    ]


def test_a_launcher_that_does_not_exist_is_an_error(tmp_path, monkeypatch, capsys):
    _fake_binary(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["--np", "2", "--launcher", "no-such-launcher", "config.json"])

    assert excinfo.value.code == 1
    assert "no-such-launcher" in capsys.readouterr().err


def test_serial_runs_the_binary_directly_even_with_a_rank_count(tmp_path, monkeypatch):
    binary = _fake_binary(tmp_path)
    _fake_launcher(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.main(["--serial", "--np", "2", "config.json"])

    assert calls == [(str(binary), [str(binary), "config.json"])]


def test_a_single_rank_needs_no_launcher(tmp_path, monkeypatch):
    binary = _fake_binary(tmp_path)
    _fake_launcher(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.main(["--np", "1", "config.json"])

    assert calls == [(str(binary), [str(binary), "config.json"])]


def test_thread_count_reaches_palace_through_the_environment(tmp_path, monkeypatch):
    _fake_binary(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec.os, "execv", lambda *_: None)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)

    _exec.main(["--nt", "4", "config.json"])

    assert os.environ["OMP_NUM_THREADS"] == "4"


def test_an_unset_thread_count_means_one_thread_not_one_per_core(tmp_path, monkeypatch):
    _fake_binary(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec.os, "execv", lambda *_: None)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)

    _exec.main(["config.json"])

    assert os.environ["OMP_NUM_THREADS"] == "1"


def test_help_prints_the_wrapper_options(capsys):
    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["--help"])

    assert excinfo.value.code == 0
    assert "--launcher-args" in capsys.readouterr().out
