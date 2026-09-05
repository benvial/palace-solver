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
