from pathlib import Path, PureWindowsPath

import pytest

from wheelbuild import mpich, superbuild

SPEC_FEATURE_FLAGS = [
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_SHARED_LIBS=ON",
    "-DPALACE_WITH_CUDA=OFF",
    "-DPALACE_WITH_HIP=OFF",
    "-DPALACE_WITH_MAGMA=OFF",
    "-DPALACE_WITH_GPU_AWARE_MPI=OFF",
    "-DPALACE_WITH_64BIT_INT=OFF",
    "-DPALACE_WITH_64BIT_BLAS_INT=OFF",
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
    "-DPALACE_WITH_SUNDIALS=ON",
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


def test_mpi_home_is_the_prefix_of_the_vendored_mpich_build(tmp_path):
    for relative in mpich.required_artefacts():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    assert superbuild.mpi_home(tmp_path) == tmp_path


def test_mpi_home_rejects_an_mpi_without_fortran_bindings(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libmpi.so.12").write_text("")

    with pytest.raises(FileNotFoundError, match="libmpifort"):
        superbuild.mpi_home(tmp_path)


def test_cmake_arguments_put_the_install_prefix_on_the_darwin_link_line():
    """Palace links some STRUMPACK companions by bare package name.

    `palace/CMakeLists.txt` emits a plain `-lzfp`, and on Darwin the linker has
    no default search path that reaches the shared install prefix, so the build
    dies at 97% of libpalace.dylib with `ld: library 'zfp' not found` even
    though libzfp.dylib is installed in that very prefix. The Linux link line
    is deliberately untouched: it has shipped every wheel so far.
    """
    darwin = superbuild.cmake_arguments(
        source_dir=Path("/src/palace"),
        install_prefix=Path("/opt/palace"),
        mpi_home=Path("/opt/palace"),
        system="Darwin",
    )
    linux = superbuild.cmake_arguments(
        source_dir=Path("/src/palace"),
        install_prefix=Path("/opt/palace"),
        mpi_home=Path("/opt/palace"),
        system="Linux",
    )

    assert "-DCMAKE_EXE_LINKER_FLAGS=-L/opt/palace/lib" in darwin
    assert "-DCMAKE_SHARED_LINKER_FLAGS=-L/opt/palace/lib" in darwin
    assert not [flag for flag in linux if "LINKER_FLAGS" in flag]


# The Linux and macOS vectors, whole. Every Windows change lands beside these
# arms rather than in them, and this is what holds that to the byte.
LINUX_VECTOR = [
    "cmake",
    "-DCMAKE_INSTALL_PREFIX=/build/install",
    "-DCMAKE_PREFIX_PATH=/build/install",
    "-DMPI_HOME=/build/install",
    *SPEC_FEATURE_FLAGS,
    "/build/palace-0.18.1",
]


def test_the_linux_vector_is_unchanged_by_the_windows_arm():
    arguments = superbuild.cmake_arguments(
        source_dir=Path("/build/palace-0.18.1"),
        install_prefix=Path("/build/install"),
        mpi_home=Path("/build/install"),
        system="Linux",
    )

    assert arguments == LINUX_VECTOR


def test_the_darwin_vector_is_the_linux_one_plus_the_link_path():
    arguments = superbuild.cmake_arguments(
        source_dir=Path("/build/palace-0.18.1"),
        install_prefix=Path("/build/install"),
        mpi_home=Path("/build/install"),
        system="Darwin",
    )

    assert arguments == [
        *LINUX_VECTOR[:-1],
        "-DCMAKE_EXE_LINKER_FLAGS=-L/build/install/lib",
        "-DCMAKE_SHARED_LINKER_FLAGS=-L/build/install/lib",
        LINUX_VECTOR[-1],
    ]


def _windows_arguments(**overrides):
    return superbuild.windows_cmake_arguments(
        source_dir=PureWindowsPath("D:\\b\\palace-0.18.1"),
        install_prefix=PureWindowsPath("D:\\b\\install"),
        project_include=PureWindowsPath("D:\\b\\superbuild\\steps.cmake"),
        **overrides,
    )


def test_windows_links_the_stack_statically_and_trims_no_feature():
    arguments = _windows_arguments()
    features = [
        flag for flag in arguments if flag in superbuild.windows_feature_flags()
    ]

    assert "-DBUILD_SHARED_LIBS=OFF" in arguments
    assert "-DBUILD_SHARED_LIBS=ON" not in arguments
    assert [
        flag for flag in SPEC_FEATURE_FLAGS if flag != "-DBUILD_SHARED_LIBS=ON"
    ] == [flag for flag in features if flag != "-DBUILD_SHARED_LIBS=OFF"]


def test_windows_names_the_msys_makefiles_generator():
    arguments = _windows_arguments()

    assert arguments[:3] == ["cmake", "-G", "MSYS Makefiles"]


def test_windows_writes_every_path_with_forward_slashes():
    arguments = _windows_arguments()

    assert "-DCMAKE_INSTALL_PREFIX=D:/b/install" in arguments
    assert "-DCMAKE_PREFIX_PATH=D:/b/install" in arguments
    assert "-DCMAKE_PROJECT_INCLUDE=D:/b/superbuild/steps.cmake" in arguments
    assert arguments[-1] == "D:/b/palace-0.18.1"
    assert not [argument for argument in arguments if "\\" in argument]


def test_windows_links_against_the_prefix_without_a_link_time():
    """A link time in the PE makes every relink new bytes, and the gate and the
    wheel regression run are skipped on a fingerprint of those bytes."""
    arguments = _windows_arguments()
    flags = "-LD:/b/install/lib -Wl,--no-insert-timestamp"

    assert f"-DCMAKE_EXE_LINKER_FLAGS={flags}" in arguments
    assert f"-DCMAKE_SHARED_LINKER_FLAGS={flags}" in arguments


def test_windows_build_hands_libxsmm_the_link_flags(tmp_path, monkeypatch):
    """Palace drives LIBXSMM's make with no linker flags off Darwin, so
    libxsmm.dll is reached only through the environment of the build."""
    for name in ("prepare_palace", "discard_stale_dependencies", "apply_dependencies"):
        monkeypatch.setattr(superbuild.patches, name, lambda *_args: None)
    monkeypatch.setattr(superbuild.patches, "verify", lambda *_args: None)
    calls = []
    monkeypatch.setattr(
        superbuild,
        "check_call",
        lambda command, **kwargs: calls.append((command, kwargs.get("env"))),
    )

    superbuild.run_windows(
        source_dir=tmp_path / "palace",
        build_dir=tmp_path / "superbuild",
        install_prefix=tmp_path / "install",
        jobs=4,
    )

    build = next(
        env for command, env in calls if command == ["cmake", "--build", ".", "-j4"]
    )
    assert build == {"ELDFLAGS": "-Wl,--no-insert-timestamp"}


def test_windows_leaves_mpi_to_findmpi():
    """MS-MPI comes from MSYS2's mingw-w64-msmpi; there is no MPICH prefix."""
    assert not [flag for flag in _windows_arguments() if flag.startswith("-DMPI_HOME")]


def test_no_platform_passes_a_compiler_launcher():
    """Palace 0.18.1 forwards no CMAKE_<LANG>_COMPILER_LAUNCHER to any
    sub-project, and the superbuild's own project compiles nothing, so a
    launcher here wraps no compile."""
    for arguments in (
        LINUX_VECTOR,
        superbuild.cmake_arguments(
            source_dir=Path("/src"),
            install_prefix=Path("/opt"),
            mpi_home=Path("/opt"),
            system="Darwin",
        ),
        _windows_arguments(),
    ):
        assert not [flag for flag in arguments if "LAUNCHER" in flag]


def test_windows_run_settles_every_patch_before_the_build_and_checks_after(
    tmp_path, monkeypatch
):
    """The carried-patch recipe's order is the point of it: nothing compiles
    until all of them are applied, and the build is not trusted until they are
    proved still there. Palace's tests, whose sources need Palace's patches,
    come after that proof."""
    events = []
    monkeypatch.setattr(
        superbuild.patches,
        "prepare_palace",
        lambda _source: events.append("prepare palace"),
    )
    monkeypatch.setattr(
        superbuild.patches,
        "discard_stale_dependencies",
        lambda _build: events.append("discard stale"),
    )
    monkeypatch.setattr(
        superbuild.patches,
        "apply_dependencies",
        lambda _build: events.append("apply dependencies"),
    )
    monkeypatch.setattr(
        superbuild.patches,
        "verify",
        lambda _source, _build: events.append("verify"),
    )

    def record(command, **_kwargs):
        if "unit-tests" in command:
            events.append("build tests")
        elif command[:2] == ["cmake", "--install"]:
            events.append("install tests")
        elif "--target" in command:
            events.append(
                "pre-pass " + " ".join(command[command.index("--target") + 1 :])
            )
        elif command[:2] == ["cmake", "--build"]:
            events.append("build")
        else:
            events.append("configure")

    monkeypatch.setattr(superbuild, "check_call", record)

    superbuild.run(
        source_dir=tmp_path / "palace",
        build_dir=tmp_path / "superbuild",
        install_prefix=tmp_path / "install",
        prefix=tmp_path / "unused",
        jobs=4,
        system="Windows",
    )

    assert events == [
        "prepare palace",
        "discard stale",
        "configure",
        "pre-pass " + " ".join(superbuild.patches.patch_targets()),
        "apply dependencies",
        "build",
        "verify",
        "build tests",
        "install tests",
    ]
    include = tmp_path / "superbuild" / superbuild.WINDOWS_PROJECT_INCLUDE
    assert include.read_text() == superbuild.patches.PROJECT_INCLUDE


def test_windows_run_does_not_ask_for_an_mpich(tmp_path, monkeypatch):
    for name in ("prepare_palace", "discard_stale_dependencies", "apply_dependencies"):
        monkeypatch.setattr(superbuild.patches, name, lambda *_args: None)
    monkeypatch.setattr(superbuild.patches, "verify", lambda *_args: None)
    monkeypatch.setattr(superbuild, "check_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        superbuild, "mpi_home", lambda _prefix: pytest.fail("Windows has no MPICH")
    )

    superbuild.run(
        source_dir=tmp_path / "palace",
        build_dir=tmp_path / "superbuild",
        install_prefix=tmp_path / "install",
        prefix=tmp_path / "missing",
        jobs=1,
        system="Windows",
    )
