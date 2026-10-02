from pathlib import Path

import pytest
from pe_files import write_pe

from wheelbuild import assemble, link_check, platforms

ELF_MAGIC = b"\x7fELF\x02\x01\x01\x00"
MACH_O_MAGIC = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"


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


def _darwin_install_tree(root: Path) -> Path:
    """The same tree as a macOS superbuild leaves: Mach-O binaries, no ELF."""
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "palace").write_text('#!/bin/sh\nexec palace-arm64.bin "$@"\n')
    (root / "bin" / "palace-arm64.bin").write_bytes(MACH_O_MAGIC + b"binary")
    for launcher in ("mpiexec.hydra", "hydra_pmi_proxy"):
        (root / "bin" / launcher).write_bytes(MACH_O_MAGIC + b"launcher")
    (root / "bin" / "mpiexec").symlink_to("mpiexec.hydra")
    (root / "bin" / "mpicc").write_text("#!/bin/sh\n# compiler wrapper\n")
    return root


def test_stage_copies_the_real_elf_binary_as_palace_real(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palace_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    staged = package_dir / "bin" / "palace-real"
    assert staged.read_bytes().startswith(ELF_MAGIC)
    assert staged.stat().st_mode & 0o111


def test_stage_leaves_the_shared_libraries_to_auditwheel(tmp_path):
    """auditwheel vendors the dependency closure; staging it too doubles the wheel."""
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palace_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    assert list((package_dir / "lib").iterdir()) == []


def test_stage_fails_when_the_install_tree_has_no_palace_binary(tmp_path):
    empty = tmp_path / "install"
    (empty / "bin").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="Palace"):
        assemble.stage(install_prefix=empty, package_dir=tmp_path / "pkg")


def test_find_palace_binary_accepts_a_mach_o_install_tree(tmp_path):
    """The macOS payload is Mach-O, so an ELF-only filter would find nothing."""
    install_prefix = _darwin_install_tree(tmp_path / "install")

    assert assemble.find_palace_binary(install_prefix).name == "palace-arm64.bin"


def test_stage_ships_the_mach_o_process_manager_binaries(tmp_path):
    install_prefix = _darwin_install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palace_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    shipped = {path.name for path in (package_dir / "bin").iterdir()}
    assert {"mpiexec", "mpiexec.hydra", "hydra_pmi_proxy"} <= shipped
    # Build-time shell wrappers stay out whichever platform they came from.
    assert "mpicc" not in shipped


def test_repair_command_vendors_every_library_including_mpi(tmp_path):
    command = assemble.repair_command(
        wheel=tmp_path / "dist" / "palace_solver-0.17.0-py3-none-linux_x86_64.whl",
        output_dir=tmp_path / "wheelhouse",
    )

    assert command[:2] == ["auditwheel", "repair"]
    assert "--exclude" not in command
    assert command[command.index("--plat") + 1] == platforms.platform_tag()


def test_retag_command_forces_the_python_agnostic_tag(tmp_path):
    wheel = tmp_path / "palace_solver-0.17.0-cp313-cp313-manylinux_2_28_x86_64.whl"
    command = assemble.retag_command(wheel)

    assert command[:2] == ["wheel", "tags"]
    assert command[command.index("--python-tag") + 1] == "py3"
    assert command[command.index("--abi-tag") + 1] == "none"
    assert command[command.index("--platform-tag") + 1] == platforms.platform_tag()
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
    package_dir = tmp_path / "pkg" / "palace_solver"
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
    package_dir = tmp_path / "pkg" / "palace_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    staged = {path.name for path in (package_dir / "bin").iterdir()}
    assert {"mpiexec", "mpiexec.hydra", "hydra_pmi_proxy"} <= staged


def test_stage_materialises_the_mpiexec_symlink_as_a_real_binary(tmp_path):
    """Wheels cannot carry symlinks, so the launcher must be a real file."""
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palace_solver"

    assemble.stage(install_prefix=install_prefix, package_dir=package_dir)

    staged = package_dir / "bin" / "mpiexec"
    assert not staged.is_symlink()
    assert staged.read_bytes().startswith(ELF_MAGIC)


def test_stage_leaves_out_the_mpi_compiler_wrappers(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palace_solver"

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
    stale = tmp_path / "build" / "lib.linux-x86_64-cpython-312" / "palace_solver"
    stale.mkdir(parents=True)
    (stale / "libstale.so").write_bytes(ELF_MAGIC)

    assemble.clean_build_tree(tmp_path)

    assert not (tmp_path / "build").exists()


def test_clean_build_tree_is_fine_with_a_clean_project(tmp_path):
    assemble.clean_build_tree(tmp_path)

    assert not (tmp_path / "build").exists()


def test_repair_command_on_darwin_drives_delocate(tmp_path):
    """auditwheel is Linux-only: it reads ELF headers and writes RPATHs."""
    command = assemble.repair_command(
        wheel=tmp_path / "dist" / "palace_solver-0.17.0-py3-none-macosx_15_0_arm64.whl",
        output_dir=tmp_path / "wheelhouse",
        system="Darwin",
        machine="arm64",
    )

    assert command[0] == "delocate-wheel"
    assert command[command.index("--wheel-dir") + 1] == str(tmp_path / "wheelhouse")
    assert command[command.index("--require-archs") + 1] == "arm64"
    assert command[-1].endswith(".whl")


def test_repair_command_on_darwin_asks_for_no_platform_tag(tmp_path):
    """delocate computes the tag from the payload's largest minos rather than
    honouring one, so passing a tag would be a value it overrules anyway.
    """
    command = assemble.repair_command(
        wheel=tmp_path / "palace_solver-0.17.0-py3-none-macosx_15_0_arm64.whl",
        output_dir=tmp_path / "wheelhouse",
        system="Darwin",
        machine="arm64",
    )

    assert "--plat" not in command
    assert not any("macosx" in argument for argument in command[:-1])


def test_retag_command_on_darwin_keeps_the_tag_delocate_computed(tmp_path):
    """The Python and ABI tags are ours to force; the platform tag is evidence."""
    wheel = tmp_path / "palace_solver-0.17.0-cp313-cp313-macosx_15_0_arm64.whl"
    command = assemble.retag_command(wheel, system="Darwin")

    assert command[:2] == ["wheel", "tags"]
    assert command[command.index("--python-tag") + 1] == "py3"
    assert command[command.index("--abi-tag") + 1] == "none"
    assert "--platform-tag" not in command
    assert command[-1] == str(wheel)


def test_retag_command_on_linux_forces_the_manylinux_tag(tmp_path):
    """auditwheel already applied it; the retag must not undo that."""
    wheel = tmp_path / "palace_solver-0.17.0-cp313-cp313-manylinux_2_28_x86_64.whl"
    command = assemble.retag_command(wheel, system="Linux", machine="x86_64")

    assert command[command.index("--platform-tag") + 1] == "manylinux_2_28_x86_64"


def test_retag_command_on_windows_forces_win_amd64(tmp_path):
    """No Windows repair tool computes the tag from the payload as delocate
    does, so, as on Linux, this step is what puts the platform tag on the wheel.
    """
    wheel = tmp_path / "palace_solver-0.17.0-cp313-cp313-win_amd64.whl"
    command = assemble.retag_command(wheel, system="Windows", machine="AMD64")

    assert command == [
        "wheel",
        "tags",
        "--python-tag",
        "py3",
        "--abi-tag",
        "none",
        "--platform-tag",
        "win_amd64",
        "--remove",
        str(wheel),
    ]


def test_retag_command_rejects_windows_arm64(tmp_path):
    wheel = tmp_path / "palace_solver-0.17.0-cp313-cp313-win_arm64.whl"

    with pytest.raises(platforms.UnsupportedPlatformError, match="arm64"):
        assemble.retag_command(wheel, system="Windows", machine="ARM64")


def test_retag_command_rejects_a_platform_with_no_wheel(tmp_path):
    wheel = tmp_path / "palace_solver-0.17.0-cp313-cp313-any.whl"

    with pytest.raises(platforms.UnsupportedPlatformError, match="FreeBSD"):
        assemble.retag_command(wheel, system="FreeBSD")


def test_the_delocate_pin_is_the_version_that_refuses_a_payload_over_the_floor():
    """0.13.0 raises on a library above MACOSX_DEPLOYMENT_TARGET; earlier
    versions computed the tag but only warned, which publishes a wheel claiming
    a macOS it cannot run on.
    """
    assert assemble.DELOCATE_REQUIREMENT == "delocate>=0.13.0"


def test_wheel_platform_tags_reads_the_filename(tmp_path):
    wheel = tmp_path / "palace_solver-0.17.0-py3-none-macosx_15_0_arm64.whl"

    assert assemble.wheel_platform_tags(wheel) == ["macosx_15_0_arm64"]


def test_wheel_platform_tags_splits_a_compressed_tag_set(tmp_path):
    wheel = tmp_path / "x-1.0-py3-none-macosx_11_0_arm64.macosx_11_0_x86_64.whl"

    assert assemble.wheel_platform_tags(wheel) == [
        "macosx_11_0_arm64",
        "macosx_11_0_x86_64",
    ]


def test_verify_platform_tag_accepts_the_tag_the_platform_claims(tmp_path):
    wheel = tmp_path / f"palace_solver-0.17.0-py3-none-{platforms.platform_tag()}.whl"

    assemble.verify_platform_tag(wheel)


def test_verify_platform_tag_refuses_a_higher_macos_floor(tmp_path):
    """delocate raises on a payload file above the target, so the route left open
    is a newer SDK *major*, which it writes into the filename instead. A wheel
    tagged higher than the metadata claims installs on fewer machines than
    advertised, silently.
    """
    wheel = tmp_path / "palace_solver-0.17.0-py3-none-macosx_26_0_arm64.whl"

    with pytest.raises(assemble.PlatformTagError, match="macosx_15_0_arm64"):
        assemble.verify_platform_tag(wheel, expected="macosx_15_0_arm64")


def test_verify_platform_tag_refuses_a_lower_macos_floor(tmp_path):
    """Lower is not a bonus: the classifiers, the README and the TestPyPI dry
    run all name one floor, and a wheel is not the place that decision changes.
    """
    wheel = tmp_path / "palace_solver-0.17.0-py3-none-macosx_11_0_arm64.whl"

    with pytest.raises(assemble.PlatformTagError, match="macosx_11_0_arm64"):
        assemble.verify_platform_tag(wheel, expected="macosx_15_0_arm64")


def test_verify_platform_tag_refuses_a_wheel_claiming_several_platforms(tmp_path):
    """A universal2 or fat tag set means the payload is not what was built."""
    wheel = tmp_path / "x-1.0-py3-none-macosx_15_0_arm64.macosx_15_0_x86_64.whl"

    with pytest.raises(assemble.PlatformTagError):
        assemble.verify_platform_tag(wheel, expected="macosx_15_0_arm64")


def test_build_environment_adds_nothing_on_linux():
    """The raw wheel's tag is replaced by `auditwheel repair --plat` anyway."""
    assert assemble.build_environment(system="Linux", machine="x86_64") == {}


def test_build_environment_pins_the_host_platform_on_darwin(monkeypatch):
    """A universal2 interpreter would otherwise tag an arm64-only payload fat.

    GitHub's macOS runners install a universal2 CPython, so
    ``sysconfig.get_platform()`` reports ``macosx-15.0-universal2`` and the raw
    wheel claims both architectures. delocate then reads that tag as the
    architectures the payload must have and fails on the missing x86_64 half.
    """
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "15.0")

    environment = assemble.build_environment(system="Darwin", machine="arm64")

    assert environment == {"_PYTHON_HOST_PLATFORM": "macosx-15.0-arm64"}


def test_the_host_platform_names_the_same_platform_as_the_wheel_tag(monkeypatch):
    """One derivation, two spellings: sysconfig dots what a wheel tag joins."""
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "15.0")
    environment = assemble.build_environment(system="Darwin", machine="arm64")

    spelled = environment["_PYTHON_HOST_PLATFORM"].replace("-", "_").replace(".", "_")

    assert spelled == platforms.platform_tag(system="Darwin", machine="arm64")


def test_build_environment_follows_the_floor_the_build_exported(monkeypatch):
    """The host platform is not read off the constant: it tracks the build.

    A raw wheel tagged for a floor the build did not compile to is the same
    mistake as guessing the tag, one step earlier.
    """
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "16.0")

    environment = assemble.build_environment(system="Darwin", machine="arm64")

    assert environment == {"_PYTHON_HOST_PLATFORM": "macosx-16.0-arm64"}


def test_verify_size_refuses_a_wheel_over_the_pypi_upload_limit(tmp_path):
    wheel = tmp_path / "palace_solver-0.18.1-py3-none-macosx_15_0_arm64.whl"
    wheel.write_bytes(b"0" * (assemble.PYPI_SIZE_LIMIT_BYTES + 1))

    with pytest.raises(assemble.WheelTooLargeError) as excinfo:
        assemble.verify_size(wheel)

    message = str(excinfo.value)
    assert wheel.name in message
    # The build stops here so a human can choose between shrinking the payload
    # and asking PyPI to raise the limit; the message has to name the second
    # option, because nothing else in the repository does.
    assert "limit request" in message


def test_verify_size_returns_the_report_for_a_wheel_under_the_limit(tmp_path):
    wheel = tmp_path / "palace_solver-0.18.1-py3-none-macosx_15_0_arm64.whl"
    wheel.write_bytes(b"0" * 1024)

    assert assemble.verify_size(wheel).size_bytes == 1024


def _msmpi_dir(root: Path) -> Path:
    root.mkdir(parents=True)
    for name in ("msmpi.dll", "mpiexec.exe", "smpd.exe"):
        (root / name).write_bytes(b"MZ" + name.encode())
    return root


def test_stage_ships_ms_mpis_launchers_but_leaves_its_dll_to_the_repair(tmp_path):
    install_prefix = _install_tree(tmp_path / "install")
    package_dir = tmp_path / "pkg" / "palace_solver"

    assemble.stage(
        install_prefix=install_prefix,
        package_dir=package_dir,
        msmpi_dir=_msmpi_dir(tmp_path / "msmpi"),
    )

    staged = {path.name for path in (package_dir / "bin").iterdir()}
    assert {"mpiexec.exe", "smpd.exe"} <= staged
    assert "msmpi.dll" not in staged
    assert (package_dir / "bin" / "smpd.exe").read_bytes() == b"MZsmpd.exe"


def test_check_msmpi_dir_requires_the_fetched_mpi_on_windows():
    with pytest.raises(ValueError, match="required on Windows"):
        assemble.check_msmpi_dir(None, system="Windows")
    assemble.check_msmpi_dir(Path("D:/b-msmpi"), system="Windows")


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_check_msmpi_dir_refuses_it_where_mpich_is_vendored(system):
    with pytest.raises(ValueError, match="Windows wheel only"):
        assemble.check_msmpi_dir(Path("msmpi"), system=system)
    assemble.check_msmpi_dir(None, system=system)


def test_main_refuses_to_assemble_on_windows_without_the_fetched_mpi(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(assemble.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        assemble, "build", lambda **_: pytest.fail("assembled without MS-MPI")
    )

    with pytest.raises(SystemExit) as exited:
        assemble.main(
            ["--install-prefix", str(tmp_path), "--output-dir", str(tmp_path / "out")]
        )

    assert exited.value.code == 2
    assert "--msmpi-dir is required on Windows" in capsys.readouterr().err


def _windows_install_tree(root: Path) -> Path:
    """A Windows superbuild's prefix: Palace's PE binary keeps its ``.bin``."""
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "palace").write_text('#!/bin/sh\nexec palace-x86_64.bin "$@"\n')
    write_pe(
        root / "bin" / "palace-x86_64.bin",
        ["KERNEL32.dll", "msmpi.dll", "libopenblas.dll"],
    )
    write_pe(root / "bin" / "libopenblas.dll", ["KERNEL32.dll"])
    return root


def _windows_inputs(root: Path) -> dict[str, Path]:
    """The three directories the Windows driver hands assembly."""
    msmpi = root / "msmpi"
    payload = root / "payload"
    msmpi.mkdir(parents=True)
    payload.mkdir()
    write_pe(msmpi / "msmpi.dll", ["KERNEL32.dll"])
    write_pe(msmpi / "mpiexec.exe", ["KERNEL32.dll"])
    write_pe(msmpi / "smpd.exe", ["KERNEL32.dll"])
    write_pe(payload / "msmpi.dll", ["KERNEL32.dll"])
    write_pe(payload / "libopenblas.dll", ["KERNEL32.dll"])
    return {
        "install": _windows_install_tree(root / "install"),
        "msmpi": msmpi,
        "payload": payload,
    }


def test_find_palace_binary_accepts_a_pe_install_tree(tmp_path):
    install_prefix = _windows_install_tree(tmp_path / "install")

    assert assemble.find_palace_binary(install_prefix).name == "palace-x86_64.bin"


def test_stage_names_a_pe_solver_with_its_exe(tmp_path):
    """Windows runs only files named .exe, and palace_solver looks for this one."""
    inputs = _windows_inputs(tmp_path)
    package_dir = tmp_path / "pkg" / "palace_solver"

    staged = assemble.stage(
        install_prefix=inputs["install"],
        package_dir=package_dir,
        msmpi_dir=inputs["msmpi"],
        payload_dir=inputs["payload"],
    )

    assert staged == package_dir / "bin" / "palace-real.exe"


def test_stage_puts_the_repaired_dlls_beside_the_executables(tmp_path):
    inputs = _windows_inputs(tmp_path)
    package_dir = tmp_path / "pkg" / "palace_solver"

    assemble.stage(
        install_prefix=inputs["install"],
        package_dir=package_dir,
        msmpi_dir=inputs["msmpi"],
        payload_dir=inputs["payload"],
    )

    assert sorted(path.name for path in (package_dir / "bin").iterdir()) == [
        "libopenblas.dll",
        "mpiexec.exe",
        "msmpi.dll",
        "palace-real.exe",
        "smpd.exe",
    ]
    assert not any((package_dir / "lib").iterdir())


def test_check_payload_dir_requires_the_repair_on_windows():
    with pytest.raises(ValueError, match="required on Windows"):
        assemble.check_payload_dir(None, system="Windows")
    assemble.check_payload_dir(Path("D:/b-payload"), system="Windows")


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_check_payload_dir_refuses_it_where_a_tool_repairs_the_wheel(system):
    with pytest.raises(ValueError, match="Windows wheel only"):
        assemble.check_payload_dir(Path("payload"), system=system)
    assemble.check_payload_dir(None, system=system)


def test_main_refuses_to_assemble_on_windows_without_the_repair(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(assemble.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        assemble, "build", lambda **_: pytest.fail("assembled an unrepaired payload")
    )

    with pytest.raises(SystemExit) as exited:
        assemble.main(
            [
                f"--install-prefix={tmp_path}",
                f"--output-dir={tmp_path / 'out'}",
                f"--msmpi-dir={tmp_path / 'msmpi'}",
            ]
        )

    assert exited.value.code == 2
    assert "--payload-dir is required on Windows" in capsys.readouterr().err


def test_verify_payload_links_passes_a_flat_payload(tmp_path):
    write_pe(tmp_path / "palace-real.exe", ["KERNEL32.dll", "MSMPI.DLL"])
    write_pe(tmp_path / "msmpi.dll", ["api-ms-win-crt-heap-l1-1-0.dll"])
    (tmp_path / ".gitkeep").write_text("")

    reports = assemble.verify_payload_links(tmp_path)

    assert [report.binary.name for report in reports] == [
        "msmpi.dll",
        "palace-real.exe",
    ]


def test_verify_payload_links_names_every_file_with_a_dll_not_beside_it(
    tmp_path, capsys
):
    """The repair searched the toolchain too; the user's machine will not."""
    write_pe(tmp_path / "palace-real.exe", ["libgfortran-5.dll"])
    write_pe(tmp_path / "smpd.exe", ["msmpi.dll"])

    with pytest.raises(
        link_check.UnresolvedImportError, match=r"palace-real\.exe, smpd\.exe"
    ):
        assemble.verify_payload_links(tmp_path)
    output = capsys.readouterr().out
    assert "libgfortran-5.dll" in output
    assert "msmpi.dll" in output


def test_verify_payload_links_refuses_to_pass_an_empty_payload(tmp_path):
    (tmp_path / ".gitkeep").write_text("")

    with pytest.raises(link_check.UnresolvedImportError, match="no PE file"):
        assemble.verify_payload_links(tmp_path)


def test_build_on_windows_retags_the_wheel_it_built_without_a_repair_tool(
    tmp_path, monkeypatch
):
    """The repair ran before the wheel existed, so the built wheel goes
    straight to `wheel tags`, which writes beside its input -- so that input
    has to be in the output directory, where the other platforms' repair
    leaves it.
    """
    inputs = _windows_inputs(tmp_path)
    project_dir = tmp_path / "project"
    output_dir = tmp_path / "wheelhouse"
    output_dir.mkdir()
    calls = []

    def fake_check_call(command, **_):
        calls.append(command)
        if command[:3] == ["python", "-m", "build"]:
            out = Path(command[command.index("--outdir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "palace_solver-1.0-cp313-cp313-win_amd64.whl").write_bytes(b"PK")
        elif command[:2] == ["wheel", "tags"]:
            wheel = Path(command[-1])
            wheel.rename(wheel.with_name("palace_solver-1.0-py3-none-win_amd64.whl"))
        else:
            pytest.fail(f"unexpected command {command}")

    monkeypatch.setattr(assemble.platform, "system", lambda: "Windows")
    monkeypatch.setattr(assemble.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(assemble, "check_call", fake_check_call)
    monkeypatch.setattr(assemble.msmpi, "verify_wheel", lambda _: [])
    monkeypatch.setattr(
        assemble.notices_module, "audit_wheel", lambda **_: ["libopenblas", "msmpi"]
    )

    final = assemble.build(
        project_dir=project_dir,
        install_prefix=inputs["install"],
        output_dir=output_dir,
        msmpi_dir=inputs["msmpi"],
        payload_dir=inputs["payload"],
    )

    assert final == output_dir / "palace_solver-1.0-py3-none-win_amd64.whl"
    assert [command[0] for command in calls] == ["python", "wheel"]
    assert Path(calls[1][-1]).parent == output_dir


def test_build_on_windows_link_checks_the_payload_before_packaging_it(
    tmp_path, monkeypatch
):
    inputs = _windows_inputs(tmp_path)
    (inputs["payload"] / "libopenblas.dll").unlink()
    monkeypatch.setattr(
        assemble,
        "check_call",
        lambda *_, **__: pytest.fail("packaged a broken payload"),
    )

    with pytest.raises(link_check.UnresolvedImportError, match=r"palace-real\.exe"):
        assemble.build(
            project_dir=tmp_path / "project",
            install_prefix=inputs["install"],
            output_dir=tmp_path / "wheelhouse",
            msmpi_dir=inputs["msmpi"],
            payload_dir=inputs["payload"],
        )
