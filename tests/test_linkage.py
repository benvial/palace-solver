from pathlib import Path

import pytest

from wheelbuild import linkage


def _fake_site_packages(prefix: Path) -> Path:
    """Lay out a venv the way pip installs the mpich wheel and our wheel."""
    (prefix / "lib").mkdir(parents=True)
    (prefix / "lib" / "libmpi.so.12").write_text("")
    site_packages = prefix / "lib" / "python3.13" / "site-packages"
    (site_packages / "pypalace_solver" / "lib").mkdir(parents=True)
    (site_packages / "pypalace_solver" / "bin").mkdir()
    return site_packages


def test_rpath_from_package_lib_resolves_to_the_mpich_wheel_library(tmp_path):
    site_packages = _fake_site_packages(tmp_path)
    origin = site_packages / "pypalace_solver" / "lib"

    entries = linkage.rpath_entries("lib")
    resolved = [
        Path(entry.replace("$ORIGIN", str(origin))).resolve() for entry in entries
    ]

    assert (tmp_path / "lib").resolve() in resolved


def test_rpath_from_package_bin_resolves_to_both_vendored_and_mpi_libraries(tmp_path):
    site_packages = _fake_site_packages(tmp_path)
    origin = site_packages / "pypalace_solver" / "bin"

    entries = linkage.rpath_entries("bin")
    resolved = [
        Path(entry.replace("$ORIGIN", str(origin))).resolve() for entry in entries
    ]

    assert (site_packages / "pypalace_solver" / "lib").resolve() in resolved
    assert (tmp_path / "lib").resolve() in resolved


def test_rpath_entries_reject_an_unknown_component():
    with pytest.raises(ValueError, match="share"):
        linkage.rpath_entries("share")


def test_patchelf_command_sets_the_joined_rpath():
    command = linkage.patchelf_command(
        Path("/wheel/pypalace_solver/bin/palace-real"), "bin"
    )

    assert command[0] == "patchelf"
    assert "--force-rpath" in command
    assert "--set-rpath" in command
    rpath = command[command.index("--set-rpath") + 1]
    assert rpath == ":".join(linkage.rpath_entries("bin"))
    assert command[-1] == "/wheel/pypalace_solver/bin/palace-real"


def test_rpath_from_the_auditwheel_vendor_dir_resolves_to_the_mpich_library(tmp_path):
    site_packages = _fake_site_packages(tmp_path)
    origin = site_packages / "pypalace_solver.libs"
    origin.mkdir()

    entries = linkage.rpath_entries("libs")
    resolved = [
        Path(entry.replace("$ORIGIN", str(origin))).resolve() for entry in entries
    ]

    assert (tmp_path / "lib").resolve() in resolved


def test_append_rpath_command_keeps_the_rpath_auditwheel_wrote():
    command = linkage.append_rpath_command(
        Path("/w/pypalace_solver/bin/palace-real"),
        existing="$ORIGIN/../../pypalace_solver.libs",
        component="bin",
    )

    rpath = command[command.index("--set-rpath") + 1].split(":")
    assert rpath[0] == "$ORIGIN/../../pypalace_solver.libs"
    assert linkage.MPI_LIB_RPATH in rpath


def test_append_rpath_command_does_not_duplicate_the_mpi_entry():
    command = linkage.append_rpath_command(
        Path("/w/pypalace_solver/lib/libmfem.so"),
        existing=f"$ORIGIN:{linkage.MPI_LIB_RPATH}",
        component="lib",
    )

    rpath = command[command.index("--set-rpath") + 1].split(":")
    assert rpath.count(linkage.MPI_LIB_RPATH) == 1


def test_elf_targets_labels_each_payload_file_with_its_component(tmp_path):
    elf = b"\x7fELF\x02\x01\x01\x00"
    root = tmp_path / "unpacked"
    (root / "pypalace_solver" / "bin").mkdir(parents=True)
    (root / "pypalace_solver" / "lib").mkdir()
    (root / "pypalace_solver.libs").mkdir()
    (root / "pypalace_solver" / "bin" / "palace-real").write_bytes(elf)
    (root / "pypalace_solver" / "lib" / "libmfem.so").write_bytes(elf)
    (root / "pypalace_solver.libs" / "libHYPRE-abc123.so").write_bytes(elf)
    (root / "pypalace_solver" / "__init__.py").write_text("# not an ELF file")

    targets = dict(linkage.elf_targets(root))

    assert targets == {
        root / "pypalace_solver" / "bin" / "palace-real": "bin",
        root / "pypalace_solver" / "lib" / "libmfem.so": "lib",
        root / "pypalace_solver.libs" / "libHYPRE-abc123.so": "libs",
    }
