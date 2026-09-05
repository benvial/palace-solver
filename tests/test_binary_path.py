import pytest

import palais_solver


def test_binary_path_points_at_packaged_binary(tmp_path, monkeypatch):
    package_dir = tmp_path / "palais_solver"
    binary = package_dir / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(palais_solver, "_PACKAGE_DIR", package_dir)

    assert palais_solver.binary_path() == binary


def test_binary_path_raises_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(palais_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="palace-real"):
        palais_solver.binary_path()


def test_mpiexec_path_points_at_the_vendored_process_manager(tmp_path, monkeypatch):
    package_dir = tmp_path / "palais_solver"
    launcher = package_dir / "bin" / "mpiexec"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(palais_solver, "_PACKAGE_DIR", package_dir)

    assert palais_solver.mpiexec_path() == launcher


def test_mpiexec_path_raises_when_the_wheel_carries_no_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(palais_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="mpiexec"):
        palais_solver.mpiexec_path()


def test_lib_dir_points_at_the_auditwheel_vendor_directory(tmp_path, monkeypatch):
    package_dir = tmp_path / "palais_solver"
    package_dir.mkdir()
    vendored = tmp_path / "palais_solver.libs"
    vendored.mkdir()
    monkeypatch.setattr(palais_solver, "_PACKAGE_DIR", package_dir)

    assert palais_solver.lib_dir() == vendored
