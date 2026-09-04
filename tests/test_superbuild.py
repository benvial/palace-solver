from pathlib import Path

import pytest

from wheelbuild import mpich, superbuild

SPEC_FEATURE_FLAGS = [
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_SHARED_LIBS=ON",
    "-DPALACE_WITH_CUDA=OFF",
    "-DPALACE_WITH_HIP=OFF",
    "-DPALACE_WITH_64BIT_INT=OFF",
    "-DPALACE_WITH_OPENMP=ON",
    "-DPALACE_WITH_SUPERLU=ON",
    "-DPALACE_WITH_STRUMPACK=ON",
    "-DPALACE_WITH_STRUMPACK_BUTTERFLYPACK=OFF",
    "-DPALACE_WITH_STRUMPACK_ZFP=ON",
    "-DPALACE_WITH_MUMPS=ON",
    "-DPALACE_WITH_SLEPC=ON",
    "-DPALACE_WITH_ARPACK=ON",
    "-DPALACE_WITH_LIBXSMM=ON",
    "-DPALACE_WITH_GSLIB=ON",
]


def test_cmake_arguments_carry_the_full_spec_feature_set():
    arguments = superbuild.cmake_arguments(
        source_dir=Path("/src/palace"),
        install_prefix=Path("/opt/palace"),
        mpi_home=Path("/opt/mpi"),
    )

    for flag in SPEC_FEATURE_FLAGS:
        assert flag in arguments


def test_cmake_arguments_install_into_the_requested_prefix_from_the_source_tree():
    arguments = superbuild.cmake_arguments(
        source_dir=Path("/src/palace"),
        install_prefix=Path("/opt/palace"),
        mpi_home=Path("/opt/mpi"),
    )

    assert arguments[0] == "cmake"
    assert "-DCMAKE_INSTALL_PREFIX=/opt/palace" in arguments
    assert arguments[-1] == "/src/palace"


def test_cmake_arguments_point_at_the_mpich_wheel_prefix():
    arguments = superbuild.cmake_arguments(
        source_dir=Path("/src/palace"),
        install_prefix=Path("/opt/palace"),
        mpi_home=Path("/venv"),
    )

    assert "-DCMAKE_PREFIX_PATH=/venv" in arguments
    assert "-DMPI_HOME=/venv" in arguments


def test_cmake_arguments_enable_ccache_when_requested():
    with_ccache = superbuild.cmake_arguments(
        source_dir=Path("/src"),
        install_prefix=Path("/opt"),
        mpi_home=Path("/venv"),
        ccache=True,
    )
    without_ccache = superbuild.cmake_arguments(
        source_dir=Path("/src"),
        install_prefix=Path("/opt"),
        mpi_home=Path("/venv"),
        ccache=False,
    )

    assert "-DCMAKE_C_COMPILER_LAUNCHER=ccache" in with_ccache
    assert "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache" in with_ccache
    assert not [flag for flag in without_ccache if "COMPILER_LAUNCHER" in flag]


def test_mpi_home_is_the_prefix_of_the_vendored_mpich_build(tmp_path):
    for relative in mpich.REQUIRED_ARTEFACTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    assert superbuild.mpi_home(tmp_path) == tmp_path


def test_mpi_home_rejects_an_mpi_without_fortran_bindings(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libmpi.so.12").write_text("")

    with pytest.raises(FileNotFoundError, match="libmpifort"):
        superbuild.mpi_home(tmp_path)
