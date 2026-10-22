import base64
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from wheelbuild import hwcaps, platforms

WHEEL = "palace_solver-0.18.1-py3-none-manylinux_2_28_x86_64.whl"
DIST_INFO = "palace_solver-0.18.1.dist-info"

#: The four libraries as the superbuild installs them, by the interface the
#: loader matches them by, and as the 0.18.1.post4 wheel carries them.
BUILT = {
    "libpalace.so": hwcaps.Interface(
        "libpalace.so", ("libmfem.so.4.9.0", "libceed.so", "libmpi.so.12", "libc.so.6")
    ),
    "libmfem.so.4.9.0": hwcaps.Interface(
        "libmfem.so.4.9.0", ("libHYPRE.so.300", "libmpi.so.12", "libc.so.6")
    ),
    "libHYPRE.so.3.0.0": hwcaps.Interface(
        "libHYPRE.so.300", ("libmpi.so.12", "libm.so.6")
    ),
    "libceed.so": hwcaps.Interface("libceed.so", ("libxsmm.so.1", "libm.so.6")),
}
REPAIRED = {
    "libpalace-192ee1a6.so": hwcaps.Interface(
        "libpalace-192ee1a6.so",
        (
            "libmfem-7df8df2e.so.4.9.0",
            "libceed-efea9e9d.so",
            "libmpi-4f355533.so.12.5.2",
            "libc.so.6",
        ),
    ),
    "libmfem-7df8df2e.so.4.9.0": hwcaps.Interface(
        "libmfem-7df8df2e.so.4.9.0",
        ("libHYPRE-ae26c80a.so.3.0.0", "libmpi-4f355533.so.12.5.2", "libc.so.6"),
    ),
    "libHYPRE-ae26c80a.so.3.0.0": hwcaps.Interface(
        "libHYPRE-ae26c80a.so.3.0.0", ("libmpi-4f355533.so.12.5.2", "libm.so.6")
    ),
    "libceed-efea9e9d.so": hwcaps.Interface(
        "libceed-efea9e9d.so", ("libxsmm-c9d33030.so.1.17.0", "libm.so.6")
    ),
}


def _elf(path: Path, interface: hwcaps.Interface) -> Path:
    """A stand-in shared library that records its interface as its contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([interface.soname, list(interface.needed)]))
    return path


def _read_interface(path: Path) -> hwcaps.Interface:
    soname, needed = json.loads(Path(path).read_text())
    return hwcaps.Interface(soname, tuple(needed))


def _fake_tools(calls: list[list[str]]):
    """strip does nothing; patchelf rewrites a stand-in's recorded interface."""

    def check_call(command, **_kwargs):
        calls.append(command)
        if command[0] == "strip":
            return
        assert command[0] == "patchelf"
        target = Path(command[-1])
        interface = _read_interface(target)
        needed = list(interface.needed)
        soname = interface.soname
        arguments = command[1:-1]
        while arguments:
            option = arguments.pop(0)
            if option == "--set-soname":
                soname = arguments.pop(0)
            elif option == "--replace-needed":
                old, new = arguments.pop(0), arguments.pop(0)
                needed = [new if name == old else name for name in needed]
            elif option == "--set-rpath":
                arguments.pop(0)
        _elf(target, hwcaps.Interface(soname, tuple(needed)))

    return check_call


def _prefix(root: Path, interfaces=BUILT) -> Path:
    for name, interface in interfaces.items():
        _elf(root / "lib" / name, interface)
    (root / "lib" / "libmfem.so").symlink_to("libmfem.so.4.9.0")
    (root / "lib" / "libHYPRE.so.300").symlink_to("libHYPRE.so.3.0.0")
    return root


def _wheel(path: Path) -> Path:
    """A repaired wheel: the four libraries, one more, and a valid RECORD."""
    files = {
        "palace_solver/__init__.py": b"",
        "palace_solver/bin/palace-real": b"\x7fELF",
        "palace_solver.libs/libmpi-4f355533.so.12.5.2": b"\x7fELF",
        f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\n",
        f"{DIST_INFO}/METADATA": b"Name: palace-solver\n",
    }
    for name, interface in REPAIRED.items():
        files[f"palace_solver.libs/{name}"] = json.dumps(
            [interface.soname, list(interface.needed)]
        ).encode()
    rows = [hwcaps.record_line(name, data) for name, data in files.items()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 5, 6, 17, 0))
            info.external_attr = 0o100755 << 16
            archive.writestr(info, data)
        archive.writestr(
            f"{DIST_INFO}/RECORD", "\n".join(rows) + f"\n{DIST_INFO}/RECORD,,\n"
        )
    return path


def _verify_record(wheel: Path) -> list[str]:
    """Check every RECORD row against the archive; return the archive names."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        record = archive.read(f"{DIST_INFO}/RECORD").decode().splitlines()
        rows = {line.split(",")[0]: line for line in record}
        assert set(rows) == set(names)
        for name in names:
            if name.endswith("/RECORD"):
                continue
            data = archive.read(name)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(
                b"="
            )
            assert rows[name] == f"{name},sha256={digest.decode()},{len(data)}"
    return names


@pytest.fixture
def grafting(tmp_path, monkeypatch):
    """A repaired wheel, both prefixes, and stand-in ELF tools."""
    calls: list[list[str]] = []
    monkeypatch.setattr(hwcaps, "require_platform", lambda **_: None)
    monkeypatch.setattr(hwcaps, "read_interface", _read_interface)
    monkeypatch.setattr(hwcaps, "check_call", _fake_tools(calls))
    return {
        "wheel": _wheel(tmp_path / WHEEL),
        "install": _prefix(tmp_path / "install"),
        "hwcaps": _prefix(tmp_path / "install-x86-64-v3"),
        "calls": calls,
    }


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", True),
        ("Linux", "aarch64", False),
        ("Darwin", "arm64", False),
        ("Windows", "AMD64", False),
    ],
)
def test_only_linux_x86_64_ships_the_copies_by_default(system, machine, expected):
    assert hwcaps.enabled_by_default(system=system, machine=machine) is expected


def test_the_copies_are_refused_off_linux_x86_64():
    with pytest.raises(platforms.UnsupportedPlatformError, match="Linux x86_64"):
        hwcaps.require_platform(system="Linux", machine="aarch64")


def test_the_copies_are_compiled_for_the_level_they_are_installed_under():
    assert "-march=x86-64-v3" in hwcaps.COMPILE_FLAGS.split()
    assert PurePosixPath("glibc-hwcaps/x86-64-v3") == hwcaps.SUBDIRECTORY


def test_a_copy_lives_beside_its_baseline_one_level_down():
    """glibc searches <dir>/glibc-hwcaps/x86-64-v3/ for the same file name."""
    assert hwcaps.hwcaps_member("palace_solver.libs/libmfem-7df8df2e.so.4.9.0") == (
        PurePosixPath(
            "palace_solver.libs/glibc-hwcaps/x86-64-v3/libmfem-7df8df2e.so.4.9.0"
        )
    )


def test_select_finds_each_library_past_its_symlinks(tmp_path):
    prefix = _prefix(tmp_path)

    found = hwcaps.select(prefix / "lib")

    assert {name: path.name for name, path in found.items()} == {
        "libpalace": "libpalace.so",
        "libmfem": "libmfem.so.4.9.0",
        "libHYPRE": "libHYPRE.so.3.0.0",
        "libceed": "libceed.so",
    }


def test_select_refuses_a_missing_library(tmp_path):
    prefix = _prefix(tmp_path)
    (prefix / "lib" / "libceed.so").unlink()

    with pytest.raises(FileNotFoundError, match="libceed"):
        hwcaps.select(prefix / "lib")


def test_select_refuses_two_files_for_one_library(tmp_path):
    """Two would mean two versions installed, and no way to say which ships."""
    prefix = _prefix(tmp_path)
    _elf(prefix / "lib" / "libmfem.so.4.8.0", BUILT["libmfem.so.4.9.0"])

    with pytest.raises(FileNotFoundError, match=r"libmfem\.so\.4\.8\.0"):
        hwcaps.select(prefix / "lib")


def test_a_copy_with_another_soname_is_not_the_baseline_rebuilt():
    baseline = BUILT["libmfem.so.4.9.0"]
    copy = hwcaps.Interface("libmfem.so.4.10.0", baseline.needed)

    with pytest.raises(hwcaps.InterfaceMismatchError, match=r"4\.10\.0"):
        hwcaps.check_same_build("libmfem", baseline, copy)


def test_a_copy_needing_another_library_is_not_the_baseline_rebuilt():
    baseline = BUILT["libceed.so"]
    copy = hwcaps.Interface("libceed.so", ("libm.so.6",))

    with pytest.raises(hwcaps.InterfaceMismatchError, match="NEEDED"):
        hwcaps.check_same_build("libceed", baseline, copy)


def test_renames_are_the_ones_auditwheel_applied():
    renamed = hwcaps.renames(BUILT["libpalace.so"], REPAIRED["libpalace-192ee1a6.so"])

    assert renamed == {
        "libmfem.so.4.9.0": "libmfem-7df8df2e.so.4.9.0",
        "libceed.so": "libceed-efea9e9d.so",
        "libmpi.so.12": "libmpi-4f355533.so.12.5.2",
    }


def test_renames_follow_auditwheel_to_the_vendored_file_name():
    """auditwheel names the dependency after the file, not after its SONAME."""
    built = hwcaps.Interface("libHYPRE.so.300", ("libopenblas.so.0", "libm.so.6"))
    repaired = hwcaps.Interface(
        "libHYPRE-ae26c80a.so.3.0.0", ("libopenblasp-r0-a160b4b8.3.34.so", "libm.so.6")
    )

    assert hwcaps.renames(built, repaired) == {
        "libopenblas.so.0": "libopenblasp-r0-a160b4b8.3.34.so"
    }


def test_renames_refuse_lists_that_do_not_correspond():
    built = hwcaps.Interface("libceed.so", ("libm.so.6", "libxsmm.so.1"))

    with pytest.raises(hwcaps.InterfaceMismatchError, match="cannot match"):
        hwcaps.renames(built, REPAIRED["libceed-efea9e9d.so"])


def test_renames_refuse_lists_of_different_lengths():
    built = hwcaps.Interface("libceed.so", ("libxsmm.so.1",))

    with pytest.raises(hwcaps.InterfaceMismatchError, match="cannot match"):
        hwcaps.renames(built, REPAIRED["libceed-efea9e9d.so"])


def test_patchelf_gives_the_copy_the_repaired_name_and_an_rpath_one_level_up():
    command = hwcaps.patchelf_command(
        Path("/w/libceed-efea9e9d.so"),
        REPAIRED["libceed-efea9e9d.so"],
        {"libxsmm.so.1": "libxsmm-c9d33030.so.1.17.0"},
    )

    assert command == [
        "patchelf",
        "--set-soname",
        "libceed-efea9e9d.so",
        "--replace-needed",
        "libxsmm.so.1",
        "libxsmm-c9d33030.so.1.17.0",
        "--force-rpath",
        "--set-rpath",
        "$ORIGIN/../..",
        "/w/libceed-efea9e9d.so",
    ]


def test_graft_adds_a_twin_of_every_library_and_keeps_the_record_valid(grafting):
    added = hwcaps.graft(
        grafting["wheel"],
        install_prefix=grafting["install"],
        hwcaps_prefix=grafting["hwcaps"],
    )

    assert sorted(added) == sorted(
        f"palace_solver.libs/glibc-hwcaps/x86-64-v3/{name}" for name in REPAIRED
    )
    names = _verify_record(grafting["wheel"])
    assert names[-1] == f"{DIST_INFO}/RECORD"
    assert all(name.startswith(DIST_INFO) for name in names[-3:])
    with zipfile.ZipFile(grafting["wheel"]) as archive:
        for name, interface in REPAIRED.items():
            copy = archive.getinfo(f"palace_solver.libs/glibc-hwcaps/x86-64-v3/{name}")
            soname, needed = json.loads(archive.read(copy))
            assert hwcaps.Interface(soname, tuple(needed)) == interface
            assert copy.external_attr == 0o100755 << 16


def test_graft_strips_each_copy_before_patching_it(grafting):
    hwcaps.graft(
        grafting["wheel"],
        install_prefix=grafting["install"],
        hwcaps_prefix=grafting["hwcaps"],
    )

    tools = [command[0] for command in grafting["calls"]]
    assert tools == ["strip", "patchelf"] * 4


def test_graft_refuses_a_copy_from_another_build(grafting):
    _elf(
        grafting["hwcaps"] / "lib" / "libHYPRE.so.3.0.0",
        hwcaps.Interface(
            "libHYPRE.so.300", ("libmpi.so.12", "libm.so.6", "libgomp.so.1")
        ),
    )
    before = grafting["wheel"].read_bytes()

    with pytest.raises(hwcaps.InterfaceMismatchError, match="libHYPRE"):
        hwcaps.graft(
            grafting["wheel"],
            install_prefix=grafting["install"],
            hwcaps_prefix=grafting["hwcaps"],
        )
    assert grafting["wheel"].read_bytes() == before


@pytest.fixture
def building(tmp_path, monkeypatch):
    """The paths build() takes, a host that has the level, and a stand-in run."""
    monkeypatch.setattr(hwcaps, "require_platform", lambda **_: None)
    monkeypatch.setattr(hwcaps, "host_supports_level", lambda *_: True)
    runs = []

    def run(**kwargs):
        runs.append(kwargs)
        _prefix(kwargs["install_prefix"])

    monkeypatch.setattr(hwcaps.superbuild, "run", run)
    paths = {
        "source_dir": tmp_path / "palace-0.18.1",
        "build_dir": tmp_path / "superbuild-x86-64-v3",
        "install_prefix": tmp_path / "install-x86-64-v3",
        "prefix": tmp_path / "install",
        "jobs": 4,
    }
    return paths, runs


def test_build_runs_the_superbuild_with_the_copies_flags_and_no_tests(building):
    paths, runs = building

    assert hwcaps.build(**paths) is True

    (run,) = runs
    assert run["compile_flags"] == hwcaps.COMPILE_FLAGS
    assert run["tests"] is False
    assert run["install_prefix"] == paths["install_prefix"]
    assert run["prefix"] == paths["prefix"]


def test_build_keeps_the_prefix_and_drops_the_tree(building):
    """The build cache saves the build root; a second tree would double it."""
    paths, _runs = building

    hwcaps.build(**paths)

    assert not paths["build_dir"].exists()
    assert hwcaps.stamped(paths["install_prefix"]) is not None
    assert (paths["install_prefix"] / "lib" / "libmfem.so.4.9.0").is_file()


def test_a_stamped_prefix_is_not_rebuilt(building):
    paths, runs = building
    hwcaps.build(**paths)

    assert hwcaps.build(**paths) is False
    assert len(runs) == 1


def test_a_prefix_stamped_for_another_palace_is_rebuilt(building):
    paths, runs = building
    hwcaps.build(**paths)
    stale = hwcaps.stamped(paths["install_prefix"])
    hwcaps.stamp(paths["install_prefix"], [*stale[:-1], "/build/palace-0.18.0"])

    assert hwcaps.build(**paths) is True
    assert len(runs) == 2


def test_a_tree_left_by_a_failed_attempt_is_resumed_when_its_stamp_matches(
    building, monkeypatch
):
    paths, _runs = building
    arguments = hwcaps.superbuild.cmake_arguments(
        source_dir=paths["source_dir"],
        install_prefix=paths["install_prefix"],
        mpi_home=paths["prefix"],
        compile_flags=hwcaps.COMPILE_FLAGS,
    )
    hwcaps.stamp(paths["build_dir"], arguments)
    progress = paths["build_dir"] / "extern" / "mfem-build" / "done"
    progress.parent.mkdir(parents=True)
    progress.write_text("")
    seen = []
    monkeypatch.setattr(
        hwcaps.superbuild,
        "run",
        lambda **kwargs: (
            seen.append(progress.exists()),
            _prefix(kwargs["install_prefix"]),
        ),
    )

    hwcaps.build(**paths)

    assert seen == [True]


def test_a_tree_from_another_recipe_is_discarded(building, monkeypatch):
    paths, _runs = building
    hwcaps.stamp(paths["build_dir"], ["cmake", "-DCMAKE_C_FLAGS=-O2", "/elsewhere"])
    leftover = paths["build_dir"] / "extern" / "stale"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("")
    seen = []
    monkeypatch.setattr(
        hwcaps.superbuild,
        "run",
        lambda **kwargs: (
            seen.append(leftover.exists()),
            _prefix(kwargs["install_prefix"]),
        ),
    )

    hwcaps.build(**paths)

    assert seen == [False]


def test_build_refuses_the_baseline_prefix(building):
    """The copies' prefix is emptied before a build."""
    paths, runs = building

    with pytest.raises(ValueError, match="prefix of their own"):
        hwcaps.build(**{**paths, "install_prefix": paths["prefix"]})
    assert runs == []


def test_build_refuses_a_host_without_the_level(building, monkeypatch):
    """PETSc's configure runs what it compiles, and would die of SIGILL."""
    paths, runs = building
    monkeypatch.setattr(hwcaps, "host_supports_level", lambda *_: False)

    with pytest.raises(RuntimeError, match="cannot run x86-64-v3 code"):
        hwcaps.build(**paths)
    assert runs == []


def test_the_default_subcommand_prints_what_the_driver_reads(capsys, monkeypatch):
    monkeypatch.setattr(hwcaps.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hwcaps.platform, "machine", lambda: "aarch64")

    assert hwcaps.main(["default"]) == 0
    assert capsys.readouterr().out == "0\n"
