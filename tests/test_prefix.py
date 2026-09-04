from wheelbuild import prefix


def test_unify_makes_lib64_an_alias_of_lib(tmp_path):
    (tmp_path / "lib").mkdir()

    prefix.unify_lib_directories(tmp_path)

    lib64 = tmp_path / "lib64"
    assert lib64.is_symlink()
    assert lib64.resolve() == (tmp_path / "lib").resolve()


def test_unify_moves_libraries_already_installed_into_lib64(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libmpi.so.12").write_text("mpi")
    (tmp_path / "lib64").mkdir()
    (tmp_path / "lib64" / "libzfp.so").write_text("zfp")

    prefix.unify_lib_directories(tmp_path)

    assert (tmp_path / "lib" / "libzfp.so").read_text() == "zfp"
    assert (tmp_path / "lib" / "libmpi.so.12").read_text() == "mpi"
    assert (tmp_path / "lib64").is_symlink()


def test_unify_is_idempotent(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libzfp.so").write_text("zfp")

    prefix.unify_lib_directories(tmp_path)
    prefix.unify_lib_directories(tmp_path)

    assert (tmp_path / "lib64").is_symlink()
    assert (tmp_path / "lib" / "libzfp.so").read_text() == "zfp"


def test_unify_creates_the_lib_directory_when_the_prefix_is_empty(tmp_path):
    prefix.unify_lib_directories(tmp_path)

    assert (tmp_path / "lib").is_dir()
    assert (tmp_path / "lib64").is_symlink()
