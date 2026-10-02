import os
import signal
import subprocess
from pathlib import Path

import pytest

import palace_solver
from palace_solver import _exec, _launcher


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


# Windows. These run on every platform: the platform is passed in as
# ``system``, and the Windows version and the child process are stubbed.

WINDOWS = "win32"
INTEL_PROXY = Path(
    r"C:\Program Files (x86)\Intel\oneAPI\mpi\latest\bin\hydra_pmi_proxy.exe"
)
# STATUS_CONTROL_C_EXIT: what a process ended by Ctrl-Break exits with.
CTRL_BREAK_STATUS = 0xC000013A


def _forget_env(monkeypatch, *names):
    # Set first, so that monkeypatch restores the variable's absence too when
    # the code under test sets it.
    for name in names:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


class _Runs(list):
    """Commands a stubbed ``subprocess.run`` was given, and the status it gives."""

    returncode = 0


@pytest.fixture
def windows(tmp_path, monkeypatch):
    """A Windows 10 host as the console scripts see it, with a stubbed child.

    Returns the list of commands run, each with the SIGINT disposition in force
    while it ran; set ``returncode`` on the list to choose the child's status.
    """
    _fake_binary(tmp_path)
    _fake_launcher(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec, "console_script_path", lambda: None)
    monkeypatch.setattr(_exec, "_windows_version", lambda: (10, 0, 20348))
    monkeypatch.setattr(
        _launcher,
        "parent_executable",
        lambda **_: Path(r"C:\Windows\System32\cmd.exe"),
    )
    monkeypatch.setattr(_exec.os, "execv", lambda *_: pytest.fail("exec'd"))
    _forget_env(monkeypatch, "MSMPI_LOCAL_ONLY", "PMI_KVS", "PMI_RANK", "PMI_SIZE")

    runs = _Runs()

    def run(command, **_):
        runs.append((command, signal.getsignal(signal.SIGINT)))
        return subprocess.CompletedProcess(command, runs.returncode)

    monkeypatch.setattr(_exec.subprocess, "run", run)
    return runs


def test_on_windows_palace_runs_the_binary_and_exits_with_its_status(tmp_path, windows):
    windows.returncode = 3

    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["config.json", "--verbose"], system=WINDOWS)

    assert excinfo.value.code == 3
    binary = str(tmp_path / "bin" / "palace-real")
    assert [command for command, _ in windows] == [[binary, "config.json", "--verbose"]]


def test_on_windows_ctrl_c_is_left_to_the_child_while_it_runs(monkeypatch, windows):
    # The console delivers Ctrl-C to the child itself. Were the wrapper to take
    # it as KeyboardInterrupt, subprocess.run would kill the solver on the way
    # out; ignored, it raises nothing in the wrapper at all.
    def run(command, **_):
        windows.append((command, signal.getsignal(signal.SIGINT)))
        signal.raise_signal(signal.SIGINT)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(_exec.subprocess, "run", run)
    before = signal.getsignal(signal.SIGINT)

    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["config.json"], system=WINDOWS)

    assert excinfo.value.code == 0
    assert windows[0][1] is signal.SIG_IGN
    assert signal.getsignal(signal.SIGINT) is before


def test_on_windows_a_ctrl_break_status_reaches_the_caller_intact(windows):
    windows.returncode = CTRL_BREAK_STATUS

    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["config.json"], system=WINDOWS)

    # Signed, so that every Python passes it to Windows as the same 32 bits.
    assert excinfo.value.code == CTRL_BREAK_STATUS - (1 << 32)
    assert excinfo.value.code & 0xFFFFFFFF == CTRL_BREAK_STATUS


def test_on_windows_palace_mpiexec_runs_the_vendored_launcher(tmp_path, windows):
    # MS-MPI's mpiexec has no local-only option: a launch naming no hosts is
    # local already. The arguments go through untouched.
    windows.returncode = 1

    with pytest.raises(SystemExit) as excinfo:
        _exec.mpiexec(["-n", "2", "palace", "config.json"], system=WINDOWS)

    assert excinfo.value.code == 1
    launcher = str(tmp_path / "bin" / "mpiexec")
    assert [command for command, _ in windows] == [
        [launcher, "-n", "2", "palace", "config.json"]
    ]
    assert windows[0][1] is signal.SIG_IGN


def test_on_windows_ranks_are_started_by_running_the_launcher(tmp_path, windows):
    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["--np", "2", "config.json"], system=WINDOWS)

    assert excinfo.value.code == 0
    launcher = str(tmp_path / "bin" / "mpiexec")
    binary = str(tmp_path / "bin" / "palace-real")
    assert [command for command, _ in windows] == [
        [launcher, "-n", "2", binary, "config.json"]
    ]


#: Both console scripts, each with a command line it accepts.
BOTH_SCRIPTS = pytest.mark.parametrize(
    ("script", "argv"),
    [(_exec.main, ["config.json"]), (_exec.mpiexec, ["-n", "2", "palace"])],
    ids=["palace", "palace-mpiexec"],
)


@BOTH_SCRIPTS
@pytest.mark.usefixtures("windows")
def test_on_windows_both_scripts_keep_ms_mpi_local(script, argv):
    with pytest.raises(SystemExit):
        script(argv, system=WINDOWS)

    assert os.environ["MSMPI_LOCAL_ONLY"] == "1"


@BOTH_SCRIPTS
@pytest.mark.usefixtures("windows")
def test_on_windows_a_users_own_local_only_setting_is_kept(monkeypatch, script, argv):
    monkeypatch.setenv("MSMPI_LOCAL_ONLY", "0")

    with pytest.raises(SystemExit):
        script(argv, system=WINDOWS)

    assert os.environ["MSMPI_LOCAL_ONLY"] == "0"


def test_on_windows_a_foreign_hydra_launch_is_refused_before_anything_runs(
    monkeypatch, windows, capsys
):
    # The spike's reproduction, through the console script: a Hydra proxy
    # handing a rank PMI_RANK and PMI_SIZE but no PMI_KVS.
    monkeypatch.setattr(_launcher, "parent_executable", lambda **_: INTEL_PROXY)
    monkeypatch.setenv("PMI_RANK", "1")
    monkeypatch.setenv("PMI_SIZE", "2")

    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["config.json"], system=WINDOWS)

    assert excinfo.value.code == 1
    assert "PMI_KVS" in capsys.readouterr().err
    assert windows == []


@BOTH_SCRIPTS
def test_windows_older_than_the_floor_is_refused_by_name(
    monkeypatch, windows, capsys, script, argv
):
    monkeypatch.setattr(_exec, "_windows_version", lambda: (6, 3, 9600))

    with pytest.raises(SystemExit) as excinfo:
        script(argv, system=WINDOWS)

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Windows 10" in err
    assert "Windows Server 2016" in err
    assert "6.3" in err
    assert windows == []
    assert "MSMPI_LOCAL_ONLY" not in os.environ


@BOTH_SCRIPTS
@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_on_posix_both_scripts_still_exec_and_leave_ms_mpi_alone(
    tmp_path, monkeypatch, script, argv, system
):
    _fake_binary(tmp_path)
    _fake_launcher(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec, "_windows_version", lambda: pytest.fail("asked"))
    monkeypatch.setattr(_exec.subprocess, "run", lambda *_, **__: pytest.fail("ran"))
    monkeypatch.setattr(_launcher, "parent_executable", lambda **_: None)
    _forget_env(monkeypatch, "MSMPI_LOCAL_ONLY")
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    script(argv, system=system)

    assert len(calls) == 1
    assert "MSMPI_LOCAL_ONLY" not in os.environ
