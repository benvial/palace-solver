import io
import sys
import zipfile

import pytest

import palace_solver

#: What pip and uv write as the body of the ``palace`` console script, less
#: the shebang: the import of this package is what identifies it.
SCRIPT_SOURCE = "import sys\nfrom palace_solver._exec import main\nsys.exit(main())\n"


@pytest.fixture(params=["linux", "darwin"])
def posix(request, monkeypatch):
    """Run the test as on Linux and as on macOS, where nothing has a suffix."""
    monkeypatch.setattr(sys, "platform", request.param)


@pytest.fixture
def windows(monkeypatch):
    """Run the test as on Windows, whatever the host."""
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.mark.usefixtures("posix")
def test_binary_path_points_at_packaged_binary(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    binary = package_dir / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)

    assert palace_solver.binary_path() == binary


@pytest.mark.usefixtures("posix")
def test_binary_path_raises_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="palace-real"):
        palace_solver.binary_path()


@pytest.mark.usefixtures("posix")
def test_mpiexec_path_points_at_the_vendored_process_manager(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    launcher = package_dir / "bin" / "mpiexec"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)

    assert palace_solver.mpiexec_path() == launcher


@pytest.mark.usefixtures("posix")
def test_mpiexec_path_raises_when_the_wheel_carries_no_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="mpiexec"):
        palace_solver.mpiexec_path()


@pytest.mark.usefixtures("posix")
def test_posix_lookups_ignore_windows_executables(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    (package_dir / "bin").mkdir(parents=True)
    (package_dir / "bin" / "palace-real.exe").write_bytes(b"MZ")
    (package_dir / "bin" / "mpiexec.exe").write_bytes(b"MZ")
    _windows_console_script(tmp_path / "bin", _pip_launcher(SCRIPT_SOURCE))
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)
    _point_at_environment(tmp_path, monkeypatch)

    with pytest.raises(FileNotFoundError, match="palace-real is missing"):
        palace_solver.binary_path()
    with pytest.raises(FileNotFoundError, match="mpiexec is missing"):
        palace_solver.mpiexec_path()
    assert palace_solver.console_script_path() is None


@pytest.mark.usefixtures("windows")
def test_binary_path_finds_the_exe_on_windows(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    binary = package_dir / "bin" / "palace-real.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ")
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)

    assert palace_solver.binary_path() == binary


@pytest.mark.usefixtures("windows")
def test_binary_path_on_windows_does_not_take_a_suffixless_file(tmp_path, monkeypatch):
    # Windows cannot run it, so finding it would only move the failure later.
    package_dir = tmp_path / "palace_solver"
    (package_dir / "bin").mkdir(parents=True)
    (package_dir / "bin" / "palace-real").write_text("#!/bin/sh\n")
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)

    with pytest.raises(FileNotFoundError, match=r"palace-real\.exe is missing"):
        palace_solver.binary_path()


@pytest.mark.usefixtures("windows")
def test_mpiexec_path_finds_the_exe_on_windows(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    launcher = package_dir / "bin" / "mpiexec.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"MZ")
    (package_dir / "bin" / "mpiexec").write_text("#!/bin/sh\n")
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)

    assert palace_solver.mpiexec_path() == launcher


@pytest.mark.usefixtures("windows")
def test_mpiexec_path_on_windows_names_the_exe_it_missed(tmp_path, monkeypatch):
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match=r"mpiexec\.exe is missing"):
        palace_solver.mpiexec_path()


def test_lib_dir_points_at_the_auditwheel_vendor_directory(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    package_dir.mkdir()
    vendored = tmp_path / "palace_solver.libs"
    vendored.mkdir()
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)
    monkeypatch.setattr(sys, "platform", "linux")

    assert palace_solver.lib_dir() == vendored


def test_lib_dir_points_at_the_delocate_vendor_directory_on_macos(
    tmp_path, monkeypatch
):
    """delocate bundles inside the package, auditwheel beside it, and callers
    that set a library path from this get a directory that does not exist if the
    difference is not honoured.
    """
    package_dir = tmp_path / "palace_solver"
    package_dir.mkdir()
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert palace_solver.lib_dir() == package_dir / ".dylibs"


def _console_script(directory, body):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "palace"
    script.write_text(body)
    script.chmod(0o755)
    return script


def _stored_zip(source):
    """Return ``source`` as the ``__main__.py`` of an uncompressed zip archive."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("__main__.py", source)
    return archive.getvalue()


def _pip_launcher(source):
    """Return ``palace.exe`` as pip writes it, through distlib.

    A prebuilt launcher executable, then the shebang naming the interpreter,
    then the script as a stored zip archive the launcher hands to Python.
    """
    stub = b"MZ\x90\x00" + bytes(range(256)) * 4
    shebang = b"#!C:\\venv\\Scripts\\python.exe\n"
    return stub + shebang + _stored_zip(source)


def _uv_launcher(source):
    """Return ``palace.exe`` as uv writes it.

    uv's trampoline keeps the interpreter path and the stored zip archive as
    ``RCDATA`` resources inside the executable, so the archive sits in the
    middle of the file rather than at its end.
    """
    head = b"MZ\x90\x00" + bytes(range(256)) * 4 + b".rsrc\x00\x00\x00"
    python = "C:\\venv\\Scripts\\python.exe".encode("utf-16-le")
    return head + python + _stored_zip(source) + bytes(512)


def _windows_console_script(directory, content):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "palace.exe"
    script.write_bytes(content)
    return script


def _point_at_environment(root, monkeypatch):
    """Make ``root`` the environment whose scripts the lookup searches."""
    monkeypatch.setattr(palace_solver.sys, "executable", str(root / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(root))


@pytest.mark.usefixtures("posix")
def test_the_console_script_is_found_beside_the_interpreter(tmp_path, monkeypatch):
    script = _console_script(
        tmp_path / "bin", "#!/usr/bin/python\nfrom palace_solver._exec import main\n"
    )
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(tmp_path))

    assert palace_solver.console_script_path() == script


@pytest.mark.usefixtures("posix")
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


@pytest.mark.usefixtures("posix")
def test_no_console_script_is_no_error(tmp_path, monkeypatch):
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(tmp_path))

    assert palace_solver.console_script_path() is None


@pytest.mark.parametrize("launcher", [_pip_launcher, _uv_launcher])
@pytest.mark.usefixtures("windows")
def test_the_console_script_is_found_as_an_exe_on_windows(
    tmp_path, monkeypatch, launcher
):
    script = _windows_console_script(tmp_path / "bin", launcher(SCRIPT_SOURCE))
    _point_at_environment(tmp_path, monkeypatch)

    assert palace_solver.console_script_path() == script


@pytest.mark.usefixtures("windows")
def test_the_console_script_is_found_in_the_scripts_directory_on_windows(
    tmp_path, monkeypatch
):
    # A base interpreter, not a virtual environment: python.exe sits one level
    # above the Scripts directory that pip writes into.
    script = _windows_console_script(tmp_path / "Scripts", _pip_launcher(SCRIPT_SOURCE))
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(
        palace_solver.sysconfig, "get_path", lambda _: str(tmp_path / "Scripts")
    )

    assert palace_solver.console_script_path() == script


@pytest.mark.parametrize("launcher", [_pip_launcher, _uv_launcher])
@pytest.mark.usefixtures("windows")
def test_an_exe_belonging_to_something_else_is_not_our_console_script(
    tmp_path, monkeypatch, launcher
):
    _windows_console_script(
        tmp_path / "bin",
        launcher("import sys\nfrom other_palace.cli import main\nsys.exit(main())\n"),
    )
    _point_at_environment(tmp_path, monkeypatch)

    assert palace_solver.console_script_path() is None


@pytest.mark.usefixtures("windows")
def test_windows_ignores_a_suffixless_console_script(tmp_path, monkeypatch):
    _console_script(
        tmp_path / "bin", "#!/usr/bin/python\nfrom palace_solver._exec import main\n"
    )
    _point_at_environment(tmp_path, monkeypatch)

    assert palace_solver.console_script_path() is None


@pytest.mark.usefixtures("posix")
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


@pytest.mark.usefixtures("posix")
def test_executable_path_falls_back_to_the_binary(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    binary = package_dir / "bin" / "palace-real"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)
    monkeypatch.setattr(palace_solver.sys, "executable", str(tmp_path / "bin/python"))
    monkeypatch.setattr(palace_solver.sysconfig, "get_path", lambda _: str(tmp_path))

    assert palace_solver.executable_path() == binary


@pytest.mark.usefixtures("windows")
def test_executable_path_prefers_the_console_script_on_windows(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    binary = package_dir / "bin" / "palace-real.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ")
    script = _windows_console_script(tmp_path / "bin", _pip_launcher(SCRIPT_SOURCE))
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)
    _point_at_environment(tmp_path, monkeypatch)

    assert palace_solver.executable_path() == script


@pytest.mark.usefixtures("windows")
def test_executable_path_falls_back_to_the_exe_on_windows(tmp_path, monkeypatch):
    package_dir = tmp_path / "palace_solver"
    binary = package_dir / "bin" / "palace-real.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ")
    monkeypatch.setattr(palace_solver, "_PACKAGE_DIR", package_dir)
    _point_at_environment(tmp_path, monkeypatch)

    assert palace_solver.executable_path() == binary
