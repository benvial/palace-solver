import pytest

import palace_solver


def test_binary_path_points_at_packaged_binary(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    binary = package_dir / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)

    assert palace_solver.binary_path() == binary


def test_binary_path_raises_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="palace-real"):
        palace_solver.binary_path()


def test_mpiexec_path_points_at_the_vendored_process_manager(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    launcher = package_dir / "bin" / "mpiexec"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)

    assert palace_solver.mpiexec_path() == launcher


def test_mpiexec_path_raises_when_the_wheel_carries_no_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="mpiexec"):
        palace_solver.mpiexec_path()


def test_lib_dir_points_at_the_auditwheel_vendor_directory(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    package_dir.mkdir()
    vendored = tmp_path / "palace_solver.libs"
    vendored.mkdir()
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)

    assert palace_solver.lib_dir() == vendored


def _console_script(directory, body):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "palace"
    script.write_text(body)
    script.chmod(0o755)
    return script


def test_the_console_script_is_found_beside_the_interpreter(tmp_path, monkeypatch):
    script = _console_script(
        tmp_path / "bin", "#!/usr/bin/python\nfrom palace_solver._exec import main\n"
    )
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(tmp_path))

    assert palace_solver.console_script_path() == script


def test_a_palace_belonging_to_something_else_is_not_our_console_script(
    tmp_path, monkeypatch
):
    # A Palace built from source, or another distribution's launcher, may sit
    # beside the interpreter. Handing that back would silently run a different
    # solver than the one this wheel ships.
    _console_script(tmp_path / "bin", '#!/bin/sh\nexec /opt/palace/bin/palace "$@"\n')
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(tmp_path))

    assert palace_solver.console_script_path() is None


def test_no_console_script_is_no_error(tmp_path, monkeypatch):
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(tmp_path))

    assert palace_solver.console_script_path() is None


def test_executable_path_prefers_the_guarded_console_script(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    binary = package_dir / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    script = _console_script(
        tmp_path / "bin", "#!/usr/bin/python\nfrom palace_solver._exec import main\n"
    )
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(tmp_path))

    assert palace_solver.executable_path() == script


def test_executable_path_falls_back_to_the_binary(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    binary = package_dir / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(tmp_path))

    assert palace_solver.executable_path() == binary
