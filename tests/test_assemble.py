from pathlib import Path

import pytest

from wheelbuild import assemble

ELF_MAGIC = b"\x7fELF\x02\x01\x01\x00"


def _install_tree(root: Path) -> Path:
    """Mimic Palace's install prefix: wrapper script plus the real ELF binary."""
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "palace").write_text('#!/bin/sh\nexec palace-x86_64.bin "$@"\n')
    (root / "bin" / "palace-x86_64.bin").write_bytes(ELF_MAGIC + b"binary")
    (root / "lib").mkdir()
    (root / "lib" / "libmfem.so.4.7").write_bytes(ELF_MAGIC + b"lib")
    (root / "lib" / "libHYPRE.so").write_bytes(ELF_MAGIC + b"lib")
    (root / "lib" / "cmake").mkdir()
    (root / "lib" / "cmake" / "mfem-config.cmake").write_text("cmake noise")
    return root


def test_stage_copies_the_real_elf_binary_as_palace_real(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "pypalace_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    staged = package_dir / "bin" / "palace-real"
    assert staged.read_bytes().startswith(ELF_MAGIC)
    assert staged.stat().st_mode & 0o111


def test_stage_copies_shared_libraries_and_skips_build_system_files(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "pypalace_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    staged = {path.name for path in (package_dir / "lib").iterdir()}
    assert staged == {"libmfem.so.4.7", "libHYPRE.so"}


def test_stage_fails_when_the_install_tree_has_no_palace_binary(tmp_path):
    empty = tmp_path / "install"
    (empty / "bin").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="Palace"):
        assemble.stage(install_prefix=empty, package_dir=tmp_path / "pkg")


def test_repair_command_excludes_libmpi_and_targets_the_spec_platform(tmp_path):
    command = assemble.repair_command(
        wheel=tmp_path / "dist" / "pypalace_solver-0.17.0-py3-none-linux_x86_64.whl",
        output_dir=tmp_path / "wheelhouse",
    )

    assert command[:2] == ["auditwheel", "repair"]
    assert "--exclude" in command
    assert command[command.index("--exclude") + 1] == "libmpi*"
    assert command[command.index("--plat") + 1] == assemble.PLATFORM_TAG


def test_retag_command_forces_the_python_agnostic_tag(tmp_path):
    wheel = tmp_path / "pypalace_solver-0.17.0-cp313-cp313-manylinux_2_28_x86_64.whl"
    command = assemble.retag_command(wheel)

    assert command[:2] == ["wheel", "tags"]
    assert command[command.index("--python-tag") + 1] == "py3"
    assert command[command.index("--abi-tag") + 1] == "none"
    assert command[command.index("--platform-tag") + 1] == assemble.PLATFORM_TAG
    assert command[-1] == str(wheel)


def test_size_report_flags_a_wheel_over_the_pypi_upload_limit(tmp_path):
    wheel = tmp_path / "big.whl"
    wheel.write_bytes(b"0" * (assemble.PYPI_SIZE_LIMIT_BYTES + 1))

    report = assemble.size_report(wheel)

    assert report.exceeds_pypi_limit
    assert "100" in report.text


def test_size_report_accepts_a_wheel_under_the_pypi_upload_limit(tmp_path):
    wheel = tmp_path / "small.whl"
    wheel.write_bytes(b"0" * 1024)

    report = assemble.size_report(wheel)

    assert not report.exceeds_pypi_limit
    assert "small.whl" in report.text


def test_stage_replaces_the_payload_but_keeps_directory_placeholders(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "pypalace_solver"
    (package_dir / "bin").mkdir(parents=True)
    (package_dir / "lib").mkdir()
    (package_dir / "bin" / ".gitkeep").write_text("")
    (package_dir / "lib" / ".gitkeep").write_text("")
    (package_dir / "lib" / "libstale.so").write_bytes(ELF_MAGIC + b"stale")

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    assert (package_dir / "bin" / ".gitkeep").exists()
    assert (package_dir / "lib" / ".gitkeep").exists()
    assert not (package_dir / "lib" / "libstale.so").exists()
