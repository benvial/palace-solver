import pytest

import pypalace_solver


def test_binary_path_points_at_packaged_binary(tmp_path, monkeypatch):
    package_dir = tmp_path / "pypalace_solver"
    binary = package_dir / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(pypalace_solver, "_PACKAGE_DIR", package_dir)

    assert pypalace_solver.binary_path() == binary


def test_binary_path_raises_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pypalace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="palace-real"):
        pypalace_solver.binary_path()


def test_mpiexec_path_points_at_the_vendored_process_manager(tmp_path, monkeypatch):
    package_dir = tmp_path / "pypalace_solver"
    launcher = package_dir / "bin" / "mpiexec"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(pypalace_solver, "_PACKAGE_DIR", package_dir)

    assert pypalace_solver.mpiexec_path() == launcher


def test_mpiexec_path_raises_when_the_wheel_carries_no_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(pypalace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="mpiexec"):
        pypalace_solver.mpiexec_path()


def test_lib_dir_points_at_the_auditwheel_vendor_directory(tmp_path, monkeypatch):
    package_dir = tmp_path / "pypalace_solver"
    package_dir.mkdir()
    vendored = tmp_path / "pypalace_solver.libs"
    vendored.mkdir()
    monkeypatch.setattr(pypalace_solver, "_PACKAGE_DIR", package_dir)

    assert pypalace_solver.lib_dir() == vendored
