from pathlib import Path

import pytest

from wheelbuild import openblas


def test_build_arguments_select_a_portable_multi_architecture_library():
    arguments = openblas.build_arguments(jobs=8)

    # The wheel ships to unknown CPUs, so kernels are chosen at runtime.
    assert "DYNAMIC_ARCH=1" in arguments
    assert "USE_OPENMP=1" in arguments
    assert "INTERFACE64=0" in arguments
    assert "-j8" in arguments


def test_install_arguments_install_shared_libraries_into_the_prefix():
    arguments = openblas.install_arguments(prefix=Path("/opt/palace"))

    assert arguments[0] == "make"
    assert "install" in arguments
    assert "PREFIX=/opt/palace" in arguments


def test_source_url_points_at_the_pinned_release():
    assert openblas.OPENBLAS_VERSION in openblas.source_url()
    assert openblas.source_url().endswith(".tar.gz")


def test_validate_accepts_an_install_with_library_and_headers(tmp_path):
    for relative in openblas.required_artefacts():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    assert openblas.validate(tmp_path) == tmp_path


def test_validate_rejects_an_install_without_the_cblas_header(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libopenblas.so").write_text("")

    with pytest.raises(FileNotFoundError, match=r"cblas\.h"):
        openblas.validate(tmp_path)


def test_required_artefacts_name_the_dylib_on_darwin():
    assert Path("lib/libopenblas.dylib") in openblas.required_artefacts(system="Darwin")


def test_validate_accepts_a_darwin_install_tree(tmp_path):
    for relative in openblas.required_artefacts(system="Darwin"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    assert openblas.validate(tmp_path, system="Darwin") == tmp_path


def test_validate_rejects_a_darwin_install_without_the_library(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "cblas.h").write_text("")

    with pytest.raises(FileNotFoundError, match="libopenblas"):
        openblas.validate(tmp_path, system="Darwin")
