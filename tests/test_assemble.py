from pathlib import Path

import pytest

from wheelbuild import assemble

ELF_MAGIC = b"\x7fELF\x02\x01\x01\x00"


def _install_tree(root: Path) -> Path:
    """Mimic Palace's install prefix: wrapper script plus the real ELF binary."""
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "palace").write_text('#!/bin/sh\nexec palace-x86_64.bin "$@"\n')
    (root / "bin" / "palace-x86_64.bin").write_bytes(ELF_MAGIC + b"binary")
    for launcher in ("mpiexec.hydra", "hydra_pmi_proxy", "mpichversion"):
        (root / "bin" / launcher).write_bytes(ELF_MAGIC + b"launcher")
    # MPICH installs mpiexec as a symlink to the Hydra binary.
    (root / "bin" / "mpiexec").symlink_to("mpiexec.hydra")
    (root / "bin" / "mpicc").write_text("#!/bin/sh\n# compiler wrapper\n")
    (root / "lib").mkdir()
    (root / "lib" / "libmfem.so.4.7").write_bytes(ELF_MAGIC + b"lib")
    (root / "lib" / "libHYPRE.so").write_bytes(ELF_MAGIC + b"lib")
    (root / "lib" / "cmake").mkdir()
    (root / "lib" / "cmake" / "mfem-config.cmake").write_text("cmake noise")
    return root


def test_stage_copies_the_real_elf_binary_as_palace_real(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palais_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    staged = package_dir / "bin" / "palace-real"
    assert staged.read_bytes().startswith(ELF_MAGIC)
    assert staged.stat().st_mode & 0o111


def test_stage_leaves_the_shared_libraries_to_auditwheel(tmp_path):
    """auditwheel vendors the dependency closure; staging it too doubles the wheel."""
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palais_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    assert list((package_dir / "lib").iterdir()) == []


def test_stage_fails_when_the_install_tree_has_no_palace_binary(tmp_path):
    empty = tmp_path / "install"
    (empty / "bin").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="Palace"):
        assemble.stage(install_prefix=empty, package_dir=tmp_path / "pkg")


def test_repair_command_vendors_every_library_including_mpi(tmp_path):
    command = assemble.repair_command(
        wheel=tmp_path / "dist" / "palais_solver-0.17.0-py3-none-linux_x86_64.whl",
        output_dir=tmp_path / "wheelhouse",
    )

    assert command[:2] == ["auditwheel", "repair"]
    assert "--exclude" not in command
    assert command[command.index("--plat") + 1] == assemble.PLATFORM_TAG


def test_retag_command_forces_the_python_agnostic_tag(tmp_path):
    wheel = tmp_path / "palais_solver-0.17.0-cp313-cp313-manylinux_2_28_x86_64.whl"
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
    package_dir = tmp_path / "pkg" / "palais_solver"
    (package_dir / "bin").mkdir(parents=True)
    (package_dir / "lib").mkdir()
    (package_dir / "bin" / ".gitkeep").write_text("")
    (package_dir / "lib" / ".gitkeep").write_text("")
    (package_dir / "lib" / "libstale.so").write_bytes(ELF_MAGIC + b"stale")

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    assert (package_dir / "bin" / ".gitkeep").exists()
    assert (package_dir / "lib" / ".gitkeep").exists()
    assert not (package_dir / "lib" / "libstale.so").exists()


def test_stage_ships_the_process_manager_so_multi_rank_runs_work(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palais_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    staged = {path.name for path in (package_dir / "bin").iterdir()}
    assert {"mpiexec", "mpiexec.hydra", "hydra_pmi_proxy"} <= staged


def test_stage_materialises_the_mpiexec_symlink_as_a_real_binary(tmp_path):
    """Wheels cannot carry symlinks, so the launcher must be a real file."""
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palais_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    staged = package_dir / "bin" / "mpiexec"
    assert not staged.is_symlink()
    assert staged.read_bytes().startswith(ELF_MAGIC)


def test_stage_leaves_out_the_mpi_compiler_wrappers(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palais_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    assert not (package_dir / "bin" / "mpicc").exists()


def testpick_wheel_returns_the_file_the_step_added(tmp_path):
    old = tmp_path / "old.whl"
    new = tmp_path / "new.whl"

    picked = assemble.pick_wheel(before={old}, after={old, new}, fallback=old)

    assert picked == new


def testpick_wheel_falls_back_when_the_step_added_nothing(tmp_path):
    """Retagging an already correctly tagged wheel leaves the same filename."""
    wheel = tmp_path / "wheel.whl"

    picked = assemble.pick_wheel(before={wheel}, after={wheel}, fallback=wheel)

    assert picked == wheel


def testpick_wheel_refuses_an_ambiguous_result(tmp_path):
    first = tmp_path / "first.whl"
    second = tmp_path / "second.whl"

    with pytest.raises(RuntimeError, match="ambiguous"):
        assemble.pick_wheel(before=set(), after={first, second}, fallback=first)


def test_clean_build_tree_removes_setuptools_stale_payload(tmp_path):
    """setuptools reuses build/, so yesterday's libraries would ship again."""
    stale = tmp_path / "build" / "lib.linux-x86_64-cpython-312" / "palais_solver"
    stale.mkdir(parents=True)
    (stale / "libstale.so").write_bytes(ELF_MAGIC)

    assemble.clean_build_tree(tmp_path)

    assert not (tmp_path / "build").exists()


def test_clean_build_tree_is_fine_with_a_clean_project(tmp_path):
    assemble.clean_build_tree(tmp_path)

    assert not (tmp_path / "build").exists()
