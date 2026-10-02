import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from wheelbuild import notices


def _superbuild_tree(root: Path, licensed: dict[str, str]) -> Path:
    """Mimic the superbuild's extern/<dep>/ source checkouts."""
    extern = root / "extern"
    for name, text in licensed.items():
        checkout = extern / name
        checkout.mkdir(parents=True)
        (checkout / "LICENSE").write_text(text)
    return root


def _full_tree(root: Path) -> Path:
    """A superbuild tree carrying every license the harvester demands."""
    return _superbuild_tree(
        root, {name: f"{name} license text" for name in notices.REQUIRED_DEPENDENCIES}
    )


def test_harvest_collects_every_dependency_license_into_one_file(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    output = tmp_path / "THIRD-PARTY-NOTICES"

    notices.harvest(source_roots=[source_root], output=output)

    text = output.read_text()
    for name in notices.REQUIRED_DEPENDENCIES:
        assert f"{name} license text" in text


def test_harvest_fails_when_a_known_dependency_has_no_license_file(tmp_path):
    incomplete = dict.fromkeys(notices.REQUIRED_DEPENDENCIES, "text")
    del incomplete["mfem"]
    source_root = _superbuild_tree(tmp_path / "build", incomplete)

    with pytest.raises(notices.MissingLicenseError, match="mfem"):
        notices.harvest(source_roots=[source_root], output=tmp_path / "NOTICES")


def test_harvest_includes_the_cecill_c_text_and_a_mumps_source_pointer(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    output = tmp_path / "THIRD-PARTY-NOTICES"

    notices.harvest(source_roots=[source_root], output=output)

    text = output.read_text()
    assert "CeCILL-C FREE SOFTWARE LICENSE AGREEMENT" in text
    assert notices.MUMPS_SOURCE_URL in text


def test_harvest_finds_licenses_named_copying_in_nested_checkouts(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    nested = source_root / "extern" / "hypre" / "src" / "vendor"
    nested.mkdir(parents=True)
    (nested / "COPYING.LESSER").write_text("hypre lgpl fallback")

    notices.harvest(source_roots=[source_root], output=tmp_path / "NOTICES")

    assert "hypre lgpl fallback" in (tmp_path / "NOTICES").read_text()


def test_harvest_also_covers_dependencies_outside_the_required_list(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    extra = source_root / "extern" / "scalapack-2.2.0"
    extra.mkdir()
    (extra / "LICENSE").write_text("scalapack license text")

    notices.harvest(source_roots=[source_root], output=tmp_path / "NOTICES")

    assert "scalapack license text" in (tmp_path / "NOTICES").read_text()


def test_harvest_lists_each_license_once_across_source_and_build_directories(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    build_dir = source_root / "extern" / "mumps-build"
    build_dir.mkdir()
    (build_dir / "LICENSE").write_text("mumps license text")

    text = notices.harvest(
        source_roots=[source_root], output=tmp_path / "NOTICES"
    ).read_text()

    assert text.count("mumps license text") == 1


def test_mumps_note_identifies_the_redistributed_source_checkout(tmp_path):
    source_root = _superbuild_tree(
        tmp_path / "build",
        {name: f"{name} license text" for name in notices.REQUIRED_DEPENDENCIES},
    )
    (source_root / "extern" / "mumps").rename(source_root / "extern" / "MUMPS_5.7.3")

    text = notices.harvest(
        source_roots=[source_root], output=tmp_path / "NOTICES"
    ).read_text()

    assert "MUMPS_5.7.3" in text
    assert notices.MUMPS_SOURCE_URL in text


def test_harvest_covers_vendored_runtimes_built_outside_the_superbuild(tmp_path):
    """MPICH and OpenBLAS are built beside the superbuild, and both are shipped."""
    superbuild = _full_tree(tmp_path / "superbuild")
    mpich = tmp_path / "mpich-4.3.2"
    mpich.mkdir()
    (mpich / "COPYRIGHT").write_text("mpich copyright text")
    openblas = tmp_path / "OpenBLAS-0.3.34"
    openblas.mkdir()
    (openblas / "LICENSE").write_text("openblas license text")

    text = notices.harvest(
        source_roots=[superbuild, mpich, openblas], output=tmp_path / "NOTICES"
    ).read_text()

    assert "mpich copyright text" in text
    assert "openblas license text" in text


def test_harvest_fails_when_a_vendored_runtime_library_has_no_license(tmp_path):
    """Forgetting to harvest the MPICH tree must fail the build, not ship silently."""
    without_mpich = {
        name: f"{name} license text"
        for name in notices.REQUIRED_DEPENDENCIES
        if name != "mpich"
    }
    superbuild = _superbuild_tree(tmp_path / "superbuild", without_mpich)

    with pytest.raises(notices.MissingLicenseError, match="mpich"):
        notices.harvest(source_roots=[superbuild], output=tmp_path / "NOTICES")


def test_harvest_covers_the_compiler_runtime_the_repair_step_vendors(tmp_path):
    """libgfortran and friends have no source checkout, so no walk can find them."""
    source_root = _full_tree(tmp_path / "build")

    text = notices.harvest(
        source_roots=[source_root], output=tmp_path / "NOTICES", gcc_version="12.2.1"
    ).read_text()

    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "libpciaccess (vendored from the build image where the payload" in text
    assert "GCC RUNTIME LIBRARY EXCEPTION" in text
    assert notices.GCC_SOURCE_URL in text
    assert "GCC 12.2.1" in text
    for library in notices.COMPILER_RUNTIME_LIBRARIES:
        assert library in text


def test_each_compiler_runtime_library_is_pinned_to_its_own_license():
    """libquadmath is not the GPL-3 runtime; a regrouping must not fold it back."""
    assert notices.COMPILER_RUNTIME_LIBRARIES == {
        "libgcc_s": "GPL-3.0-or-later WITH GCC-exception-3.1",
        "libgfortran": "GPL-3.0-or-later WITH GCC-exception-3.1",
        "libgomp": "GPL-3.0-or-later WITH GCC-exception-3.1",
        "libquadmath": "LGPL-2.0-or-later",
        "libstdc++": "GPL-3.0-or-later WITH GCC-exception-3.1",
    }


def test_harvest_notices_libquadmath_under_the_lgpl_not_the_gpl(tmp_path):
    source_root = _full_tree(tmp_path / "build")

    text = notices.harvest(
        source_roots=[source_root], output=tmp_path / "NOTICES", gcc_version="14.2.1"
    ).read_text()

    gpl_note = text.split("GCC runtime libraries (GPL-3.0")[1].split(
        "GCC Runtime Library Exception 3.1"
    )[0]
    assert "libgfortran" in gpl_note
    assert "libquadmath" not in gpl_note
    # The note names the LGPL too, so the next section is found by its title line.
    lgpl_note = text.split("libquadmath (LGPL")[1].split(
        "\nGNU Lesser General Public License version 2.1\n"
    )[0]
    assert "GCC 14.2.1" in lgpl_note
    assert notices.GCC_SOURCE_URL in lgpl_note
    assert "GNU LESSER GENERAL PUBLIC LICENSE\n\t\t       Version 2.1" in text


def _wheel_carrying(path: Path, vendored: list[str]) -> Path:
    """Write a wheel whose .libs directory holds the given file names."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("palace_solver/__init__.py", "")
        for name in vendored:
            archive.writestr(f"palace_solver.libs/{name}", "\x7fELF")
    return path


def _install_prefix(root: Path, libraries: list[str]) -> Path:
    (root / "lib").mkdir(parents=True)
    for name in libraries:
        (root / "lib" / name).write_text("")
    return root


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("libgfortran-83c28eba.so.5.0.0", "libgfortran"),
        ("libmpi-1a2b3c4d.so.12.1.8", "libmpi"),
        # auditwheel hashes the part before the first dot, which for a library
        # carrying its version in the name puts the hash in the middle.
        ("libopenblasp-r0-a160b4b8.3.34.so", "libopenblasp-r0.3.34"),
        ("libpciaccess-9f8e7d6c.so.0.11.1", "libpciaccess"),
        ("libmpi.so.12", "libmpi"),
        ("libomp-abcdef12.dylib", "libomp"),
    ],
)
def test_library_stem_strips_auditwheels_hash_and_the_soversion(name, expected):
    assert notices.library_stem(name) == expected


def test_audit_accepts_libraries_built_here_and_the_named_compiler_runtime(tmp_path):
    wheel = _wheel_carrying(
        tmp_path / "palace_solver-0.18.1-py3-none-any.whl",
        [
            "libmpi-1a2b3c4d.so.12.1.8",
            "libgfortran-83c28eba.so.5.0.0",
            "libquadmath-2284e583.so.0.0.0",
        ],
    )
    prefix = _install_prefix(tmp_path / "install", ["libmpi.so.12"])

    assert notices.audit_wheel(wheel=wheel, install_prefix=prefix) == [
        "libgfortran",
        "libmpi",
        "libquadmath",
    ]


def test_audit_fails_when_the_repair_step_vendors_an_unaccounted_library(tmp_path):
    """The failure the source-tree walk structurally cannot produce."""
    wheel = _wheel_carrying(
        tmp_path / "palace_solver-0.18.1-py3-none-any.whl",
        ["libmpi-1a2b3c4d.so.12.1.8", "libsomething-5e6f7a8b.so.2"],
    )
    prefix = _install_prefix(tmp_path / "install", ["libmpi.so.12"])

    with pytest.raises(notices.UnattributedLibraryError, match="libsomething"):
        notices.audit_wheel(wheel=wheel, install_prefix=prefix)


def test_audit_matches_a_library_whose_version_is_in_its_name(tmp_path):
    """OpenBLAS is the case the hash lands in the middle of."""
    wheel = _wheel_carrying(
        tmp_path / "palace_solver-0.18.1-py3-none-any.whl",
        ["libopenblasp-r0-a160b4b8.3.34.so"],
    )
    prefix = _install_prefix(tmp_path / "install", ["libopenblasp-r0.3.34.so"])

    assert notices.audit_wheel(wheel=wheel, install_prefix=prefix) == [
        "libopenblasp-r0.3.34"
    ]


def test_audit_accepts_a_library_vendored_from_the_build_image(tmp_path):
    """libpciaccess reaches the payload through hwloc and is built by nobody here."""
    wheel = _wheel_carrying(
        tmp_path / "palace_solver-0.18.1-py3-none-any.whl",
        ["libpciaccess-9f8e7d6c.so.0.11.1"],
    )
    prefix = _install_prefix(tmp_path / "install", [])

    assert notices.audit_wheel(wheel=wheel, install_prefix=prefix) == ["libpciaccess"]


#: The macOS wheel's vendored payload, as delocate wrote it in run 36705373848
#: — the first macOS wheel this project built. Names are verbatim, because the
#: point of the fixture is that delocate does not rename what it copies.
_MACOS_VENDORED = (
    "libHYPRE.3.0.0.dylib",
    "libarpack.2.1.0.dylib",
    "libceed.dylib",
    "libdmumps.5.7.3.2.dylib",
    "libfmt.12.1.0.dylib",
    "libgcc_s.1.1.dylib",
    "libgfortran.5.dylib",
    "libgomp.1.dylib",
    "libgs.dylib",
    "libmetis.dylib",
    "libmfem.4.9.0.dylib",
    "libmpi.12.dylib",
    "libmpicxx.12.dylib",
    "libmpifort.12.dylib",
    "libmumps_common.5.7.3.2.dylib",
    "libnlohmann_json_schema_validator.2.4.0.dylib",
    "libopenblasp-r0.3.34.dylib",
    "libpalace.dylib",
    "libparmetis.dylib",
    "libparpack.2.1.0.dylib",
    "libpetsc.3.24.3.dylib",
    "libpmpi.12.dylib",
    "libpord.dylib",
    "libquadmath.0.dylib",
    "libscalapack.2.2.2.dylib",
    "libscn.4.dylib",
    "libslepc.3.24.1.dylib",
    "libstdc++.6.dylib",
    "libstrumpack.8.0.0.dylib",
    "libsundials_arkode.6.5.0.dylib",
    "libsundials_core.7.5.0.dylib",
    "libsundials_cvodes.7.5.0.dylib",
    "libsundials_kinsol.7.5.0.dylib",
    "libsundials_nvecmpiplusx.7.5.0.dylib",
    "libsundials_nvecparallel.7.5.0.dylib",
    "libsundials_nvecserial.7.5.0.dylib",
    "libsuperlu_dist.9.2.1.dylib",
    "libxsmm.1.dylib",
    "libzfp.1.0.1.dylib",
)

#: The five of them the toolchain provides rather than the superbuild.
_MACOS_COMPILER_RUNTIME = (
    "libgcc_s.1.1.dylib",
    "libgfortran.5.dylib",
    "libgomp.1.dylib",
    "libquadmath.0.dylib",
    "libstdc++.6.dylib",
)


def _macos_wheel_carrying(path: Path, vendored: Sequence[str]) -> Path:
    """Write a wheel shaped the way delocate leaves one.

    delocate bundles into a ``.dylibs`` directory *inside* the package, not a
    ``<package>.libs`` directory beside it, and copies each library under the
    name it already had.
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("palace_solver/__init__.py", "")
        for name in vendored:
            archive.writestr(f"palace_solver/.dylibs/{name}", "\xcf\xfa\xed\xfe")
    return path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Mach-O puts the version before the suffix, so the whole trailing
        # run of numeric components is the soversion.
        ("libgfortran.5.dylib", "libgfortran"),
        ("libgcc_s.1.1.dylib", "libgcc_s"),
        ("libstdc++.6.dylib", "libstdc++"),
        ("libmumps_common.5.7.3.2.dylib", "libmumps_common"),
        (
            "libnlohmann_json_schema_validator.2.4.0.dylib",
            "libnlohmann_json_schema_validator",
        ),
        ("libpalace.dylib", "libpalace"),
    ],
)
def test_library_stem_drops_the_mach_o_soversion(name, expected):
    assert notices.library_stem(name) == expected


def test_vendored_libraries_reads_delocates_dylibs_directory(tmp_path):
    """auditwheel's directory is beside the package; delocate's is inside it."""
    wheel = _macos_wheel_carrying(
        tmp_path / "palace_solver-0.18.1-py3-none-macosx_15_0_arm64.whl",
        ["libmpi.12.dylib", "libgfortran.5.dylib"],
    )

    assert notices.vendored_libraries(wheel) == ["libgfortran", "libmpi"]


def test_audit_accounts_for_every_library_the_macos_wheel_carries(tmp_path):
    """The whole payload of the first macOS wheel, against its install prefix."""
    wheel = _macos_wheel_carrying(
        tmp_path / "palace_solver-0.18.1-py3-none-macosx_15_0_arm64.whl",
        _MACOS_VENDORED,
    )
    prefix = _install_prefix(
        tmp_path / "install",
        [name for name in _MACOS_VENDORED if name not in _MACOS_COMPILER_RUNTIME],
    )

    assert len(notices.audit_wheel(wheel=wheel, install_prefix=prefix)) == 39


def test_audit_fails_on_an_openmp_runtime_the_notices_do_not_cover(tmp_path):
    """A clang toolchain would vendor libomp, which is not the GCC runtime."""
    wheel = _macos_wheel_carrying(
        tmp_path / "palace_solver-0.18.1-py3-none-macosx_15_0_arm64.whl",
        ["libmpi.12.dylib", "libomp.dylib"],
    )
    prefix = _install_prefix(tmp_path / "install", ["libmpi.12.dylib"])

    with pytest.raises(notices.UnattributedLibraryError, match="libomp"):
        notices.audit_wheel(wheel=wheel, install_prefix=prefix)


def _fake_compiler(path: Path, *, version: str, vendor: str) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-dumpfullversion" ]; then\n'
        f"  echo {version}\n"
        "  exit 0\n"
        "fi\n"
        f'if [ "$1" = "--version" ]; then\n  echo "{vendor}"\n  exit 0\nfi\n'
        "exit 1\n"
    )
    path.chmod(0o755)
    return path


def test_detect_gcc_version_asks_the_compiler_the_build_used(tmp_path, monkeypatch):
    """`gcc` on PATH is Apple clang on a macOS runner; CC names the real one."""
    compiler = _fake_compiler(
        tmp_path / "gcc-15",
        version="15.3.0",
        vendor="gcc-15 (Homebrew GCC 15.3.0) 15.3.0\n"
        "Copyright (C) 2025 Free Software Foundation, Inc.",
    )
    monkeypatch.setenv("CC", str(compiler))

    assert notices.detect_gcc_version() == "15.3.0"


def test_detect_gcc_version_refuses_a_compiler_that_is_not_gcc(tmp_path, monkeypatch):
    """Apple clang answers -dumpfullversion on some releases, with its own version."""
    compiler = _fake_compiler(
        tmp_path / "cc",
        version="17.0.0",
        vendor="Apple clang version 17.0.0 (clang-1700.0.13.3)",
    )
    monkeypatch.setenv("CC", str(compiler))

    assert notices.detect_gcc_version() is None


def test_audit_refuses_a_vendored_file_whose_name_it_cannot_reduce(tmp_path):
    """Everything the repair tool put there counts, whatever it is called."""
    path = tmp_path / "palace_solver-0.18.1-py3-none-macosx_15_0_arm64.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("palace_solver/__init__.py", "")
        # A zip may or may not carry directory entries; this one does, and it
        # is not a library.
        archive.writestr("palace_solver/.dylibs/", "")
        archive.writestr("palace_solver/.dylibs/libmpi.12.dylib", "\xcf\xfa\xed\xfe")
        archive.writestr("palace_solver/.dylibs/Python", "\xcf\xfa\xed\xfe")
    prefix = _install_prefix(tmp_path / "install", ["libmpi.12.dylib"])

    with pytest.raises(notices.UnattributedLibraryError, match="Python"):
        notices.audit_wheel(wheel=path, install_prefix=prefix)
