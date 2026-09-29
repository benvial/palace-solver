import zipfile
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
    assert "libpciaccess (vendored from the build image)" in text
    assert "GCC RUNTIME LIBRARY EXCEPTION" in text
    assert notices.GCC_SOURCE_URL in text
    assert "GCC 12.2.1" in text
    for library in notices.COMPILER_RUNTIME_LIBRARIES:
        assert library in text


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
        ["libmpi-1a2b3c4d.so.12.1.8", "libgfortran-83c28eba.so.5.0.0"],
    )
    prefix = _install_prefix(tmp_path / "install", ["libmpi.so.12"])

    assert notices.audit_wheel(wheel=wheel, install_prefix=prefix) == [
        "libgfortran",
        "libmpi",
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
