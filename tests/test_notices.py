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
    return _superbuild_tree(
        root, {name: f"{name} license text" for name in notices.REQUIRED_DEPENDENCIES}
    )


def test_harvest_collects_every_dependency_license_into_one_file(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    output = tmp_path / "THIRD-PARTY-NOTICES"

    notices.harvest(source_root=source_root, output=output)

    text = output.read_text()
    for name in notices.REQUIRED_DEPENDENCIES:
        assert f"{name} license text" in text


def test_harvest_fails_when_a_known_dependency_has_no_license_file(tmp_path):
    incomplete = dict.fromkeys(notices.REQUIRED_DEPENDENCIES, "text")
    del incomplete["mfem"]
    source_root = _superbuild_tree(tmp_path / "build", incomplete)

    with pytest.raises(notices.MissingLicenseError, match="mfem"):
        notices.harvest(source_root=source_root, output=tmp_path / "NOTICES")


def test_harvest_includes_the_cecill_c_text_and_a_mumps_source_pointer(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    output = tmp_path / "THIRD-PARTY-NOTICES"

    notices.harvest(source_root=source_root, output=output)

    text = output.read_text()
    assert "CeCILL-C FREE SOFTWARE LICENSE AGREEMENT" in text
    assert notices.MUMPS_SOURCE_URL in text


def test_harvest_finds_licenses_named_copying_in_nested_checkouts(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    nested = source_root / "extern" / "hypre" / "src" / "vendor"
    nested.mkdir(parents=True)
    (nested / "COPYING.LESSER").write_text("hypre lgpl fallback")

    notices.harvest(source_root=source_root, output=tmp_path / "NOTICES")

    assert "hypre lgpl fallback" in (tmp_path / "NOTICES").read_text()


def test_harvest_also_covers_dependencies_outside_the_required_list(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    extra = source_root / "extern" / "scalapack-2.2.0"
    extra.mkdir()
    (extra / "LICENSE").write_text("scalapack license text")

    notices.harvest(source_root=source_root, output=tmp_path / "NOTICES")

    assert "scalapack license text" in (tmp_path / "NOTICES").read_text()


def test_harvest_lists_each_license_once_across_source_and_build_directories(tmp_path):
    source_root = _full_tree(tmp_path / "build")
    build_dir = source_root / "extern" / "mumps-build"
    build_dir.mkdir()
    (build_dir / "LICENSE").write_text("mumps license text")

    text = notices.harvest(
        source_root=source_root, output=tmp_path / "NOTICES"
    ).read_text()

    assert text.count("mumps license text") == 1


def test_mumps_note_identifies_the_redistributed_source_checkout(tmp_path):
    source_root = _superbuild_tree(
        tmp_path / "build",
        {name: f"{name} license text" for name in notices.REQUIRED_DEPENDENCIES},
    )
    (source_root / "extern" / "mumps").rename(source_root / "extern" / "MUMPS_5.7.3")

    text = notices.harvest(
        source_root=source_root, output=tmp_path / "NOTICES"
    ).read_text()

    assert "MUMPS_5.7.3" in text
    assert notices.MUMPS_SOURCE_URL in text
