import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import palace_solver
from palace_solver import _exec, _launcher

HYDRA_OUTPUT = """HYDRA build details:
    Version:                                 5.0.1
    Release Date:                            Fri Apr 10 09:45:31 AM CDT 2026
    CC:                              cc
"""

PROXY = Path("/venv/bin/hydra_pmi_proxy")
SHELL = Path("/usr/bin/bash")


def test_pmi_variables_are_a_rendezvous():
    assert _launcher.has_rendezvous({"PMI_RANK": "0", "PMI_SIZE": "2"})


def test_pmix_variables_are_a_rendezvous():
    assert _launcher.has_rendezvous({"PMIX_RANK": "0"})


def test_open_mpi_variables_are_a_rendezvous():
    assert _launcher.has_rendezvous({"OMPI_COMM_WORLD_RANK": "0"})


def test_an_ordinary_environment_carries_no_rendezvous():
    assert not _launcher.has_rendezvous({"PATH": "/usr/bin", "HOME": "/root"})


def test_hydras_informational_variables_are_not_a_rendezvous():
    # Hydra exports these alongside the real rendezvous, and keeps exporting
    # them if the rendezvous is missing, so they must not count as one.
    assert not _launcher.has_rendezvous(
        {"PMI_HOSTNAME": "node01", "MPI_LOCALNRANKS": "2", "MPI_LOCALRANKID": "0"}
    )


def test_a_rank_left_with_only_informational_variables_is_refused():
    reason = _launcher.refusal_reason(
        {"PMI_HOSTNAME": "node01", "MPI_LOCALNRANKS": "2"}, PROXY
    )

    assert reason is not None
    assert "palace-mpiexec" in reason


def test_a_process_manager_parent_is_recognised():
    assert _launcher.launched_by_process_manager(PROXY)
    assert _launcher.launched_by_process_manager(Path("/opt/slurm/bin/slurmstepd"))


def test_an_ordinary_parent_is_not_a_process_manager():
    assert not _launcher.launched_by_process_manager(SHELL)


def test_an_unreadable_parent_is_not_a_process_manager():
    assert not _launcher.launched_by_process_manager(None)


def test_the_requested_rank_count_is_read_from_the_launcher():
    assert _launcher.requested_ranks({"MPI_LOCALNRANKS": "2"}) == 2
    assert _launcher.requested_ranks({"SLURM_NTASKS": "8"}) == 8


def test_an_unreadable_rank_count_is_no_count_at_all():
    assert _launcher.requested_ranks({}) is None
    assert _launcher.requested_ranks({"MPI_LOCALNRANKS": "many"}) is None


def test_a_direct_run_is_never_refused():
    assert _launcher.refusal_reason({"PATH": "/usr/bin"}, SHELL) is None


def test_a_launch_with_a_working_rendezvous_is_never_refused():
    assert (
        _launcher.refusal_reason(
            {"PMI_RANK": "0", "PMI_SIZE": "2", "MPI_LOCALNRANKS": "2"}, PROXY
        )
        is None
    )


def test_a_multi_rank_launch_without_a_rendezvous_is_refused():
    reason = _launcher.refusal_reason({"MPI_LOCALNRANKS": "2"}, PROXY)

    assert reason is not None
    assert "MPI_COMM_WORLD" in reason
    assert "palace-mpiexec" in reason


def test_a_launch_that_asked_for_one_rank_is_not_refused():
    # A deliberate single-rank launch needs no rendezvous: one process alone is
    # a correct MPI_COMM_WORLD, not a split one.
    assert _launcher.refusal_reason({"SLURM_NTASKS": "1"}, PROXY) is None


def test_a_launch_of_unknown_width_without_a_rendezvous_is_refused():
    reason = _launcher.refusal_reason({}, PROXY)

    assert reason is not None
    assert "palace-mpiexec" in reason


def test_the_override_lets_a_rendezvous_less_launch_through():
    assert (
        _launcher.refusal_reason(
            {"MPI_LOCALNRANKS": "2", _launcher.OVERRIDE_ENV: "1"}, PROXY
        )
        is None
    )


def test_hydra_version_is_read_from_the_build_details():
    assert _launcher.parse_hydra_version(HYDRA_OUTPUT) == "5.0.1"


def test_unrecognised_launcher_output_yields_no_version():
    assert _launcher.parse_hydra_version("mpirun (Open MPI) 5.0.3") is None


def _note(parent_exe, probe, vendored_bin=Path("/wheel/bin")):
    return _launcher.version_note(
        parent_exe,
        vendored_version="4.3.2",
        vendored_bin=vendored_bin,
        probe=probe,
    )


def test_the_same_major_series_is_worth_no_remark():
    assert _note(PROXY, lambda _: "4.1.0") is None


def test_another_major_series_is_remarked_on_but_not_refused():
    note = _note(PROXY, lambda _: "5.0.1")

    assert note is not None
    assert "4.3.2" in note
    assert "5.0.1" in note
    # The pairing is unsupported, not broken: the note must not read as a refusal.
    assert _launcher.refusal_reason({"PMI_RANK": "0", "PMI_SIZE": "2"}, PROXY) is None


def test_the_vendored_launcher_is_never_probed():
    probed = []

    assert _note(Path("/wheel/bin/hydra_pmi_proxy"), probed.append) is None
    assert probed == []


def test_a_direct_run_is_never_probed():
    probed = []

    assert _note(SHELL, probed.append) is None
    assert probed == []


def test_an_undeterminable_launcher_version_is_not_remarked_on():
    assert _note(PROXY, lambda _: None) is None


def _fake_binary(tmp_path):
    binary = tmp_path / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_palace_refuses_to_start_without_a_rendezvous(tmp_path, monkeypatch, capsys):
    _fake_binary(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_exec.os, "execv", lambda *_: pytest.fail("exec'd"))
    monkeypatch.setattr(_launcher, "version_note", lambda *_, **__: None)
    monkeypatch.setattr(
        _launcher, "refusal_reason", lambda *_, **__: "ranks are not talking"
    )

    with pytest.raises(SystemExit) as excinfo:
        _exec.main(["config.json"])

    assert excinfo.value.code == 1
    assert "ranks are not talking" in capsys.readouterr().err


def test_palace_reports_a_version_remark_and_still_runs(tmp_path, monkeypatch, capsys):
    binary = _fake_binary(tmp_path)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(_launcher, "version_note", lambda *_, **__: "another MPICH")
    monkeypatch.setattr(_launcher, "refusal_reason", lambda *_, **__: None)
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.main(["config.json"])

    assert calls == [(str(binary), [str(binary), "config.json"])]
    assert "another MPICH" in capsys.readouterr().err


def test_the_vendored_launcher_itself_is_not_guarded(tmp_path, monkeypatch):
    launcher = tmp_path / "bin" / "mpiexec"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(
        _launcher, "refusal_reason", lambda *_, **__: pytest.fail("guarded")
    )
    calls = []
    monkeypatch.setattr(
        _exec.os, "execv", lambda path, argv: calls.append((path, argv))
    )

    _exec.mpiexec(["-n", "2"])

    assert calls


# The reader the running host does not use. Both branches are exercised from
# either platform: the foreign one is the half a Linux test run would otherwise
# never touch, and it is the half that was wrong.
FOREIGN_SYSTEM = "linux" if sys.platform == "darwin" else "darwin"


def _running_cat():
    """A process this test knows the executable of, kept alive on its stdin."""
    known = Path(shutil.which("cat")).resolve()
    return known, subprocess.Popen([str(known)], stdin=subprocess.PIPE)


def test_a_process_is_reported_as_the_binary_it_was_started_from():
    # Asked about a process started from a binary this test chose, the reader
    # names that binary. sys.executable is deliberately not the comparison: a
    # macOS framework build runs from inside Python.app while sys.executable
    # names the bin/ stub beside it, so the two disagree for a reason that says
    # nothing about the reader.
    known, child = _running_cat()
    try:
        assert _launcher.process_executable(child.pid) == known
    finally:
        child.kill()
        child.wait()


def test_a_process_that_has_gone_reports_nothing():
    # The guard runs inside every rank, so a parent that has already exited
    # must leave it with no answer rather than an exception. Asked of a pid
    # this test has watched die rather than of a low number, which on Darwin
    # would be kernel_task rather than nothing at all.
    _, child = _running_cat()
    child.kill()
    child.wait()

    assert _launcher.process_executable(child.pid) is None


def test_the_reader_for_another_platform_answers_nothing_rather_than_guessing():
    # Off Linux there is no /proc to read, and off Darwin no libproc to ask.
    # Either way the wrong reader must decline, not raise.
    assert _launcher.process_executable(os.getpid(), system=FOREIGN_SYSTEM) is None


def test_a_rank_reads_the_process_that_launched_it():
    # The guard's whole premise, exercised against real processes rather than a
    # path fixture: what it keys off is the parent's executable, and the way to
    # read that differs per platform. This test process stands in for the
    # process manager.
    repo_root = Path(__file__).resolve().parent.parent
    program = (
        "from palace_solver import _launcher\nprint(_launcher.parent_executable())"
    )
    reported = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    ).stdout.strip()

    assert Path(reported) == _launcher.process_executable(os.getpid())


# Windows. Every test below runs on every platform: the platform is passed in
# as ``system`` and the Windows process readers are stubbed, so none of the
# ctypes calls they make is reached off Windows.

WINDOWS = "win32"
SMPD = Path(r"C:\Users\me\venv\Lib\site-packages\palace_solver\bin\smpd.exe")
# Intel MPI's Hydra proxy: the Hydra-family launcher a Windows user may have.
INTEL_PROXY = Path(
    r"C:\Program Files (x86)\Intel\oneAPI\mpi\latest\bin\hydra_pmi_proxy.exe"
)
CMD = Path(r"C:\Windows\System32\cmd.exe")
# What MS-MPI's smpd hands each rank it starts.
MSMPI_RANK_ENVIRONMENT = {
    "PMI_KVS": "{2b3c1f9e-0d7a-4e57-9a43-5a1c0f6e7d21}",
    "PMI_DOMAIN": "{8f6f2a0e-5b8f-4b4e-a2b6-0c4d1d2e9a11}",
    "PMI_PORT": "{6b1f2e3d-9c8a-4f7e-b6d5-4c3b2a190807}",
    "PMI_RANK": "1",
    "PMI_SIZE": "2",
    "PMI_SMPD_KEY": "1",
    "MSMPI_LOCAL_ONLY": "1",
}
# The spike's reproduction on a windows-2025 runner: the binary, given these
# and no PMI_KVS, ran as a silent singleton and exited 0.
SPIKE_REPRODUCTION = {"PMI_RANK": "1", "PMI_SIZE": "2"}


def test_the_posix_rendezvous_set_is_unchanged():
    expected = (
        "PMI_FD",
        "PMI_PORT",
        "PMI_RANK",
        "PMIX_RANK",
        "PMIX_NAMESPACE",
        "PMIX_SERVER_URI",
        "OMPI_COMM_WORLD_RANK",
    )

    assert expected == _launcher.RENDEZVOUS_VARIABLES
    assert _launcher.rendezvous_variables(system="linux") == expected
    assert _launcher.rendezvous_variables(system="darwin") == expected


def test_on_windows_the_rendezvous_is_pmi_kvs_alone():
    assert _launcher.rendezvous_variables(system=WINDOWS) == ("PMI_KVS",)
    assert _launcher.has_rendezvous({"PMI_KVS": "{guid}"}, system=WINDOWS)


def test_on_windows_what_a_hydra_launcher_sets_is_no_rendezvous():
    # MS-MPI's PMI client reads PMI_KVS alone, so everything a Hydra-family
    # launcher sets still leaves each rank a singleton.
    hydra = {"PMI_RANK": "0", "PMI_SIZE": "2", "PMI_FD": "5", "PMI_PORT": "1:2"}

    assert not _launcher.has_rendezvous(hydra, system=WINDOWS)
    assert _launcher.has_rendezvous(hydra, system="linux")


def test_smpd_is_a_process_manager():
    assert "smpd" in _launcher.PROCESS_MANAGERS
    assert _launcher.launched_by_process_manager(SMPD, system=WINDOWS)


def test_on_windows_a_parent_is_named_without_its_exe_and_in_any_case():
    assert _launcher.launched_by_process_manager(INTEL_PROXY, system=WINDOWS)
    assert _launcher.launched_by_process_manager(
        Path(r"C:\MPI\BIN\SMPD.EXE"), system=WINDOWS
    )
    assert not _launcher.launched_by_process_manager(CMD, system=WINDOWS)
    assert not _launcher.launched_by_process_manager(None, system=WINDOWS)


def test_on_posix_a_parent_name_is_still_compared_exactly():
    for name in ("smpd.exe", "MPIEXEC", "hydra_pmi_proxy.exe"):
        assert not _launcher.launched_by_process_manager(
            Path("/opt/mpi/bin") / name, system="linux"
        )


def test_a_foreign_hydra_launch_on_windows_is_refused():
    # The regression case: PMI_RANK without PMI_KVS, under a Hydra proxy.
    reason = _launcher.refusal_reason(SPIKE_REPRODUCTION, INTEL_PROXY, system=WINDOWS)

    assert reason is not None
    assert "PMI_KVS" in reason
    assert "palace-mpiexec" in reason
    assert _launcher.OVERRIDE_ENV in reason


def test_the_same_launch_on_posix_is_still_let_through():
    assert _launcher.refusal_reason(SPIKE_REPRODUCTION, PROXY, system="linux") is None


def test_the_override_lets_a_foreign_launch_through_on_windows():
    environ = dict(SPIKE_REPRODUCTION, **{_launcher.OVERRIDE_ENV: "1"})

    assert _launcher.refusal_reason(environ, INTEL_PROXY, system=WINDOWS) is None


def test_ranks_of_ms_mpis_launcher_are_let_through():
    assert (
        _launcher.refusal_reason(MSMPI_RANK_ENVIRONMENT, SMPD, system=WINDOWS) is None
    )


def test_an_smpd_rank_without_pmi_kvs_is_refused():
    environ = dict(MSMPI_RANK_ENVIRONMENT)
    del environ["PMI_KVS"]

    assert _launcher.refusal_reason(environ, SMPD, system=WINDOWS) is not None


def test_a_direct_run_on_windows_is_never_refused():
    # A PMI_RANK set by hand in a shell is no launcher's doing.
    assert _launcher.refusal_reason(SPIKE_REPRODUCTION, CMD, system=WINDOWS) is None
    assert _launcher.refusal_reason({}, None, system=WINDOWS) is None


def test_no_launcher_is_probed_for_its_version_on_windows():
    probed = []

    note = _launcher.version_note(
        INTEL_PROXY,
        vendored_bin=SMPD.parent,
        probe=probed.append,
        system=WINDOWS,
    )

    assert note is None
    assert probed == []


def test_on_windows_a_process_is_read_through_the_windows_reader(monkeypatch):
    monkeypatch.setattr(_launcher, "_windows_executable", lambda _: SMPD)
    monkeypatch.setattr(_launcher, "_proc_executable", lambda _: pytest.fail("/proc"))

    assert _launcher.process_executable(42, system=WINDOWS) == SMPD


@pytest.mark.skipif(sys.platform == "win32", reason="the real readers answer here")
def test_off_windows_the_windows_readers_answer_nothing():
    # They are stand-ins there, so asking about real processes as if this were
    # Windows reads nothing and raises nothing.
    assert _launcher.process_executable(os.getpid(), system=WINDOWS) is None
    assert _launcher.parent_executable(system=WINDOWS) is None


@pytest.fixture
def windows_process(tmp_path, monkeypatch):
    """Stand in for this process's view of a Windows process tree.

    Returns a function taking the tree as ``{pid: (executable, parent pid)}``
    and this process's parent pid. The console script's stub, a venv's
    ``python.exe`` redirector and the base interpreter this process runs are
    real files, so that a path compares as Windows compares it.
    """
    scripts = tmp_path / "venv" / "Scripts"
    base = tmp_path / "Python313"
    for directory in (scripts, base):
        directory.mkdir(parents=True)
    paths = {
        "stub": scripts / "palace.exe",
        "redirector": scripts / "python.exe",
        "base": base / "python.exe",
    }
    for path in paths.values():
        path.write_text(path.name)
    # As pip's launcher entry point leaves it: the stub's path less its .exe.
    monkeypatch.setattr(
        _launcher.sys, "argv", [str(paths["stub"].with_suffix("")), "config.json"]
    )
    monkeypatch.setattr(_launcher.sys, "executable", str(paths["redirector"]))

    def install(table, parent_pid, image=paths["base"]):
        table = {os.getpid(): (image, parent_pid), **table}
        monkeypatch.setattr(_launcher.os, "getppid", lambda: parent_pid)
        monkeypatch.setattr(
            _launcher,
            "_windows_executable",
            lambda pid: table[pid][0] if pid in table else None,
        )
        monkeypatch.setattr(
            _launcher,
            "_windows_parent_pid",
            lambda pid: table[pid][1] if pid in table else None,
        )

    install.paths = paths
    return install


def test_on_windows_the_parent_is_read_past_the_console_script_stub(windows_process):
    # Outside a venv: smpd -> palace.exe (the stub) -> python (this process).
    paths = windows_process.paths
    windows_process(
        {10: (paths["stub"], 5), 5: (SMPD, 1)},
        parent_pid=10,
        image=paths["redirector"],
    )

    assert _launcher.parent_executable(system=WINDOWS) == SMPD


def test_on_windows_the_parent_is_read_past_a_venv_redirector(windows_process):
    # In a venv: hydra proxy -> palace.exe -> python.exe redirector -> base python.
    paths = windows_process.paths
    windows_process(
        {20: (paths["redirector"], 10), 10: (paths["stub"], 5), 5: (INTEL_PROXY, 1)},
        parent_pid=20,
    )

    assert _launcher.parent_executable(system=WINDOWS) == INTEL_PROXY


def test_on_windows_a_stub_named_with_its_exe_is_skipped_too(
    windows_process, monkeypatch
):
    # A launcher that leaves sys.argv[0] as the image it runs.
    paths = windows_process.paths
    monkeypatch.setattr(_launcher.sys, "argv", [str(paths["stub"])])
    windows_process(
        {20: (paths["redirector"], 10), 10: (paths["stub"], 5), 5: (SMPD, 1)},
        parent_pid=20,
    )

    assert _launcher.parent_executable(system=WINDOWS) == SMPD


def test_on_windows_another_python_program_is_a_parent_like_any_other(
    windows_process, monkeypatch
):
    # Outside a venv a Python program that runs this one shares its
    # interpreter, and is not one of its launchers.
    paths = windows_process.paths
    monkeypatch.setattr(_launcher.sys, "argv", ["-c"])
    windows_process(
        {10: (paths["redirector"], 5), 5: (SMPD, 1)},
        parent_pid=10,
        image=paths["redirector"],
    )

    assert _launcher.parent_executable(system=WINDOWS) == paths["redirector"]


def test_on_windows_a_parent_that_is_not_ours_is_reported_as_it_is(windows_process):
    windows_process({10: (CMD, 5)}, parent_pid=10)

    assert _launcher.parent_executable(system=WINDOWS) == CMD


def test_on_windows_a_stub_named_another_way_is_still_recognised(
    tmp_path, windows_process, monkeypatch
):
    # QueryFullProcessImageNameW gives the long name where sys.argv[0] may hold
    # an 8.3 short one; both name one file. A link stands in for the alias.
    paths = windows_process.paths
    alias = tmp_path / "PALACE~1.EXE"
    try:
        alias.symlink_to(paths["stub"])
    except OSError:
        pytest.skip("this account may not create symbolic links")
    monkeypatch.setattr(_launcher.sys, "argv", [str(alias)])
    windows_process({10: (paths["stub"], 5), 5: (SMPD, 1)}, parent_pid=10)

    assert _launcher.parent_executable(system=WINDOWS) == SMPD


def test_on_windows_an_ancestor_that_cannot_be_read_is_no_parent(windows_process):
    windows_process({10: (windows_process.paths["stub"], None)}, parent_pid=10)

    assert _launcher.parent_executable(system=WINDOWS) is None


def test_on_windows_the_walk_up_is_bounded(windows_process):
    # Past a stub and a redirector the walk stops and reports what it finds
    # rather than climbing any further.
    paths = windows_process.paths
    windows_process(
        {
            30: (paths["redirector"], 20),
            20: (paths["stub"], 10),
            10: (paths["redirector"], 5),
            5: (SMPD, 1),
        },
        parent_pid=30,
    )

    assert _launcher.parent_executable(system=WINDOWS) == paths["redirector"]


def test_on_posix_the_parent_read_never_asks_windows(monkeypatch):
    def refuse(_):
        pytest.fail("asked Windows")

    monkeypatch.setattr(_launcher, "_windows_executable", refuse)
    monkeypatch.setattr(_launcher, "_windows_parent_pid", refuse)

    _launcher.parent_executable(system="linux")
    _launcher.parent_executable(system="darwin")
