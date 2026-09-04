from pathlib import Path

import pytest

from wheelbuild import linkage


def test_rpath_from_package_bin_finds_the_vendored_libraries(tmp_path):
    origin = tmp_path / "pypalace_solver" / "bin"
    origin.mkdir(parents=True)
    (tmp_path / "pypalace_solver" / "lib").mkdir()

    entries = linkage.rpath_entries("bin")
    resolved = [
        Path(entry.replace("$ORIGIN", str(origin))).resolve() for entry in entries
    ]

    assert (tmp_path / "pypalace_solver" / "lib").resolve() in resolved


def test_rpath_from_package_lib_finds_its_siblings(tmp_path):
    origin = tmp_path / "pypalace_solver" / "lib"
    origin.mkdir(parents=True)

    entries = linkage.rpath_entries("lib")
    resolved = [
        Path(entry.replace("$ORIGIN", str(origin))).resolve() for entry in entries
    ]

    assert origin.resolve() in resolved


def test_rpath_entries_reject_an_unknown_component():
    with pytest.raises(ValueError, match="share"):
        linkage.rpath_entries("share")


def test_patchelf_command_sets_the_joined_rpath():
    command = linkage.patchelf_command(
        Path("/wheel/pypalace_solver/bin/palace-real"), "bin"
    )

    assert command[0] == "patchelf"
    assert "--force-rpath" in command
    rpath = command[command.index("--set-rpath") + 1]
    assert rpath == ":".join(linkage.rpath_entries("bin"))
    assert command[-1] == "/wheel/pypalace_solver/bin/palace-real"
