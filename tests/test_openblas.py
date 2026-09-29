import platform
from pathlib import Path

import pytest

from wheelbuild import openblas

BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-wheel.sh"


def install_tree(tmp_path, *, system="Linux", corename=None):
    """Write the files an OpenBLAS install of one platform would carry.

    Args:
        tmp_path: Directory to treat as the install prefix.
        system: The platform whose library naming the tree uses.
        corename: The core the install claims to be built for, recorded the way
            ``make install`` records it. Omitted for a tree that carries no
            ``openblas_config.h`` at all.
    """
    for relative in openblas.required_artefacts(system=system):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    if corename is not None:
        header = tmp_path / openblas.CONFIG_HEADER
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text(
            "#define OPENBLAS_NUM_CORES 8\n"
            f'#define OPENBLAS_CHAR_CORENAME "{corename}"\n'
        )
    return tmp_path


@pytest.fixture
def arm_runner(monkeypatch):
    """Run the rest of the test as if this were the aarch64 build runner.

    The CLI takes no architecture argument: which platform it is on is the one
    fact it is never told, because in the build it is never told it either.
    """
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(platform, "system", lambda: "Linux")


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
    install_tree(tmp_path)

    assert openblas.validate(tmp_path, system="Linux", machine="x86_64") == tmp_path


def test_validate_rejects_an_install_without_the_cblas_header(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libopenblas.so").write_text("")

    with pytest.raises(FileNotFoundError, match=r"cblas\.h"):
        openblas.validate(tmp_path, system="Linux", machine="x86_64")


def test_required_artefacts_name_the_dylib_on_darwin():
    assert Path("lib/libopenblas.dylib") in openblas.required_artefacts(system="Darwin")


def test_validate_accepts_a_darwin_install_tree(tmp_path):
    """Darwin arm64 is an architecture with a CPU baseline, so it names one."""
    install_tree(tmp_path, system="Darwin", corename="ARMV8")

    assert openblas.validate(tmp_path, system="Darwin", machine="arm64") == tmp_path


def test_validate_rejects_a_darwin_install_without_the_library(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "cblas.h").write_text("")

    with pytest.raises(FileNotFoundError, match="libopenblas"):
        openblas.validate(tmp_path, system="Darwin", machine="arm64")


def test_build_arguments_pin_the_cpu_baseline_on_aarch64():
    """Left to autodetect, the common objects take the build host's -march.

    See BASELINE_TARGETS in wheelbuild/openblas.py for why only arm needs this.
    """
    arguments = openblas.build_arguments(jobs=8, system="Linux", machine="aarch64")

    assert "TARGET=ARMV8" in arguments
    assert "DYNAMIC_ARCH=1" in arguments


def test_build_arguments_pin_the_cpu_baseline_on_darwin_arm64():
    """Darwin arm64 reads the same Makefile.arm64, so it needs the same pin."""
    arguments = openblas.build_arguments(jobs=8, system="Darwin", machine="arm64")

    assert "TARGET=ARMV8" in arguments


def test_build_arguments_are_unchanged_on_x86_64():
    """x86_64 needs no TARGET and keeps the vector that has shipped every wheel
    so far, so this change cannot have altered the one platform that ships.
    """
    assert openblas.build_arguments(jobs=8, system="Linux", machine="x86_64") == [
        "make",
        "-j8",
        "DYNAMIC_ARCH=1",
        "USE_OPENMP=1",
        "INTERFACE64=0",
        "NO_STATIC=1",
    ]


def test_validate_rejects_an_aarch64_install_built_for_a_newer_core(tmp_path):
    """The failure this whole pin exists to prevent, caught in the build.

    A NEOVERSEN2 baseline passes every test that runs on the arm runner and
    faults on the older hardware manylinux_2_28_aarch64 admits.
    """
    install_tree(tmp_path, corename="NEOVERSEN2")

    with pytest.raises(openblas.CpuBaselineError, match="NEOVERSEN2"):
        openblas.validate(tmp_path, system="Linux", machine="aarch64")


def test_validate_accepts_an_aarch64_install_at_the_baseline(tmp_path):
    install_tree(tmp_path, corename="ARMV8")

    assert openblas.validate(tmp_path, system="Linux", machine="aarch64") == tmp_path


def test_validate_ignores_the_core_on_x86_64(tmp_path):
    """Deliberate asymmetry: Makefile.x86_64 adds no -march under DYNAMIC_ARCH,
    so an x86_64 install configured for SKYLAKEX is still portable and every
    wheel shipped so far was built that way.
    """
    install_tree(tmp_path, corename="SKYLAKEX")

    assert openblas.validate(tmp_path, system="Linux", machine="x86_64") == tmp_path


def test_validate_refuses_an_arm_install_with_no_config_header(tmp_path):
    """Unable to tell is not the same as fine — an unprovable baseline fails."""
    install_tree(tmp_path)

    with pytest.raises(openblas.CpuBaselineError, match=r"openblas_config\.h"):
        openblas.validate(tmp_path, system="Linux", machine="aarch64")


def test_validate_refuses_an_arm_install_whose_header_names_no_core(tmp_path):
    install_tree(tmp_path)
    (tmp_path / openblas.CONFIG_HEADER).write_text("#define OPENBLAS_NUM_CORES 8\n")

    with pytest.raises(openblas.CpuBaselineError, match="OPENBLAS_CHAR_CORENAME"):
        openblas.validate(tmp_path, system="Linux", machine="aarch64")


def test_installed_core_reads_the_name_out_of_the_installed_header(tmp_path):
    install_tree(tmp_path, corename="NEOVERSEV2")

    assert openblas.installed_core(tmp_path) == "NEOVERSEV2"


@pytest.mark.usefixtures("arm_runner")
def test_check_exits_zero_for_an_install_at_the_baseline(tmp_path, capsys):
    """The verdict scripts/build-wheel.sh branches on."""
    install_tree(tmp_path, corename="ARMV8")

    code = openblas.main(["--check", "--prefix", str(tmp_path)])

    assert code == 0
    assert "ARMV8" in capsys.readouterr().out


@pytest.mark.usefixtures("arm_runner")
def test_check_asks_for_a_rebuild_from_a_clean_tree_for_a_stale_install(
    tmp_path, capsys
):
    """A cached tree from before the baseline was pinned.

    Its own verdict, distinct from "nothing installed", because make does not
    notice a changed TARGET: the source tree's objects have to go too.
    """
    install_tree(tmp_path, corename="NEOVERSEN2")

    code = openblas.main(["--check", "--prefix", str(tmp_path)])

    assert code == openblas.CHECK_WRONG_CPU
    assert "NEOVERSEN2" in capsys.readouterr().out


@pytest.mark.usefixtures("arm_runner")
def test_check_asks_for_a_plain_build_when_nothing_is_installed(tmp_path):
    """The case the old `[[ -f libopenblas.so ]]` guard covered.

    A distinct verdict from the stale one: there are no wrongly compiled
    objects to throw away, so a build in place is right and keeps whatever a
    partial build got through.
    """
    code = openblas.main(["--check", "--prefix", str(tmp_path / "absent")])

    assert code == openblas.CHECK_NO_INSTALL


def test_check_passes_on_x86_64_whatever_core_the_install_names(tmp_path):
    """It inspects an install and needs no source tree; and x86_64 has no
    baseline to hold it to, so a complete install is a passing install.
    """
    install_tree(tmp_path, corename="SKYLAKEX")

    assert openblas.main(["--check", "--prefix", str(tmp_path)]) == 0


def test_a_build_still_requires_a_source_directory(tmp_path):
    with pytest.raises(SystemExit):
        openblas.main(["--prefix", str(tmp_path)])


def test_the_build_script_asks_what_the_openblas_is_not_whether_it_exists():
    """`[[ -f lib/libopenblas.so ]]` was the wrong question.

    A restored cache satisfies it with a library built for the wrong CPU, and
    the exact key changing is no protection: the restore-keys are what hand
    that tree back. Asserted rather than run because the script needs a
    manylinux container and forty minutes.
    """
    script = BUILD_SCRIPT.read_text()

    assert "wheelbuild.openblas" in script
    assert "--check" in script
    assert "libopenblas.so" not in script


def test_the_build_script_discards_the_tree_only_for_the_stale_verdict():
    """A missing install must not cost a re-extract: on x86_64, which pins no
    baseline, that is the only failing verdict there is, and the objects in the
    tree are good.
    """
    script = BUILD_SCRIPT.read_text()

    assert f"== {openblas.CHECK_WRONG_CPU} " in script
