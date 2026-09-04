from pathlib import Path

import pytest

from wheelbuild import mpich


def test_configure_arguments_enable_the_fortran_bindings_palace_needs():
    arguments = mpich.configure_arguments(
        source_dir=Path("/src/mpich-4.3.2"), prefix=Path("/opt/mpich")
    )

    assert arguments[0] == "/src/mpich-4.3.2/configure"
    assert "--enable-fortran=all" in arguments
    assert "--enable-shared" in arguments
    assert "--prefix=/opt/mpich" in arguments


def test_configure_arguments_build_the_hydra_process_manager():
    arguments = mpich.configure_arguments(
        source_dir=Path("/src"), prefix=Path("/opt/mpich")
    )

    assert "--with-pm=hydra" in arguments


def test_source_url_points_at_the_pinned_release():
    assert mpich.MPICH_VERSION in mpich.source_url()
    assert mpich.source_url().endswith(".tar.gz")


def test_install_is_validated_by_the_fortran_artefacts(tmp_path):
    for relative in mpich.REQUIRED_ARTEFACTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    assert mpich.validate(tmp_path) == tmp_path


def test_validate_rejects_an_install_without_fortran_support(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libmpi.so.12").write_text("")

    with pytest.raises(FileNotFoundError, match="libmpifort"):
        mpich.validate(tmp_path)
