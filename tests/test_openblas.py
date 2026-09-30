import platform
import subprocess
from pathlib import Path

import pytest

from wheelbuild import openblas

BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-wheel.sh"
MACOS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-macos.sh"

#: Every driver that branches on a `--check` verdict. They have to agree, and
#: each holds the numbers as shell literals rather than importing them.
DRIVERS = (BUILD_SCRIPT, MACOS_SCRIPT)


def install_tree(tmp_path, *, system="Linux", machine=None, corename=None, stamp=True):
    """Write the files an OpenBLAS install of one platform would carry.

    Args:
        tmp_path: Directory to treat as the install prefix.
        system: The platform whose library naming the tree uses.
        machine: The machine whose build arguments the tree records; defaults
            to the usual one for ``system``.
        corename: The core the install claims to be built for, recorded the way
            ``make install`` records it. Omitted for a tree that carries no
            ``openblas_config.h`` at all.
        stamp: Record today's build arguments. False leaves them out, which is
            what an install built before the stamp existed looks like.
    """
    machine = machine or ("arm64" if system == "Darwin" else "x86_64")
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
    if stamp:
        openblas.write_build_stamp(
            tmp_path, openblas.build_arguments(jobs=4, system=system, machine=machine)
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
    install_tree(tmp_path, machine="aarch64", corename="NEOVERSEN2")

    with pytest.raises(openblas.CpuBaselineError, match="NEOVERSEN2"):
        openblas.validate(tmp_path, system="Linux", machine="aarch64")


def test_validate_accepts_an_aarch64_install_at_the_baseline(tmp_path):
    install_tree(tmp_path, machine="aarch64", corename="ARMV8")

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
    install_tree(tmp_path, machine="aarch64")

    with pytest.raises(openblas.CpuBaselineError, match=r"openblas_config\.h"):
        openblas.validate(tmp_path, system="Linux", machine="aarch64")


def test_validate_refuses_an_arm_install_whose_header_names_no_core(tmp_path):
    install_tree(tmp_path, machine="aarch64")
    (tmp_path / openblas.CONFIG_HEADER).write_text("#define OPENBLAS_NUM_CORES 8\n")

    with pytest.raises(openblas.CpuBaselineError, match="OPENBLAS_CHAR_CORENAME"):
        openblas.validate(tmp_path, system="Linux", machine="aarch64")


def test_installed_core_reads_the_name_out_of_the_installed_header(tmp_path):
    install_tree(tmp_path, corename="NEOVERSEV2")

    assert openblas.installed_core(tmp_path) == "NEOVERSEV2"


@pytest.mark.usefixtures("arm_runner")
def test_check_exits_zero_for_an_install_at_the_baseline(tmp_path, capsys):
    """The verdict scripts/build-wheel.sh branches on."""
    install_tree(tmp_path, machine="aarch64", corename="ARMV8")

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
    install_tree(tmp_path, machine="aarch64", corename="NEOVERSEN2")

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


@pytest.mark.parametrize("driver", DRIVERS)
def test_every_driver_discards_the_tree_only_for_the_stale_verdict(driver):
    """A missing install must not cost a re-extract: on x86_64, which pins no
    baseline, that is the only failing verdict there is, and the objects in the
    tree are good.
    """
    script = driver.read_text()

    assert "unpack_openblas" in _case_arm(script, openblas.CHECK_WRONG_CPU)
    assert "unpack_openblas" not in _case_arm(script, openblas.CHECK_NO_INSTALL)


def _case_arm(script, verdict):
    """The body of one arm of a driver's `case "$openblas_verdict"`."""
    start = script.index(f"  {verdict})") + len(f"  {verdict})")
    return script[start : script.index(";;", start)]


def test_build_arguments_disable_sve_on_darwin_arm64():
    """OpenBLAS's own NO_SVE lives inside `ifndef MACOSX_DEPLOYMENT_TARGET`.

    The macOS build has to set a deployment target — without one, clang infers
    it from the SDK capped at the running system and Palace's binaries exceed
    the floor the wheel claims — and setting it at all re-enables the SVE
    kernels on hardware that has none. The two are one change, not two:
    Makefile.system:442 at 0.3.34.
    """
    arguments = openblas.build_arguments(jobs=8, system="Darwin", machine="arm64")

    assert "NO_SVE=1" in arguments


def test_build_arguments_leave_sve_alone_on_linux_aarch64():
    """Linux aarch64 runs on hardware that has SVE, and DYNAMIC_ARCH dispatches
    to it at run time; NO_SVE=1 would drop those kernels from the build.
    """
    arguments = openblas.build_arguments(jobs=8, system="Linux", machine="aarch64")

    assert "NO_SVE=1" not in arguments


OTOOL_OUTPUT = """\
lib/libopenblas.dylib:
\t/opt/build/install/lib/libopenblas.0.dylib (compatibility version 0.0.0)
\t/opt/homebrew/opt/gcc/lib/gcc/15/libgomp.1.dylib (compatibility version 1.0.0)
\t@rpath/libomp.dylib (compatibility version 5.0.0, current version 5.0.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)
"""


FAT_OTOOL_OUTPUT = """\
lib/libopenblas.dylib (architecture arm64):
\tlibgomp.1.dylib (compatibility version 1.0.0, current version 1.0.0)
lib/libopenblas.dylib (architecture x86_64):
\t@rpath/libomp.dylib (compatibility version 5.0.0, current version 5.0.0)
"""


def test_linked_libraries_are_the_basenames_otool_reports():
    names = openblas.parse_linked_libraries(OTOOL_OUTPUT)

    assert "libgomp.1.dylib" in names
    assert "libomp.dylib" in names
    assert "libopenblas.dylib:" not in names


def test_linked_libraries_include_a_bare_install_name():
    """A dependency recorded without a path is still a dependency.

    Matching on a leading / or @ would drop exactly the entry this check
    exists to find, and drop it silently: the library would pass.
    """
    names = openblas.parse_linked_libraries(
        "lib/libopenblas.dylib:\n\tlibgomp.1.dylib (compatibility version 1.0.0)\n"
    )

    assert names == ("libgomp.1.dylib",)


def test_linked_libraries_span_every_slice_of_a_universal_binary():
    """Each slice gets its own unindented header, and both slices ship."""
    names = openblas.parse_linked_libraries(FAT_OTOOL_OUTPUT)

    assert names == ("libgomp.1.dylib", "libomp.dylib")


@pytest.fixture
def darwin_install(monkeypatch, tmp_path):
    """An OpenBLAS install on a Mac, with otool's answer under test control."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    install_tree(tmp_path, system="Darwin", corename="ARMV8")

    def linked(names):
        monkeypatch.setattr(openblas, "linked_libraries", lambda _: tuple(names))

    return linked


def test_validate_rejects_a_darwin_library_linking_two_openmp_runtimes(
    darwin_install, tmp_path
):
    """clang compiles the C with libomp while gfortran links the dylib and
    re-adds -lgomp behind OpenBLAS's own substitution (Makefile.system:661
    rewrites FEXTRALIB only). Two runtimes in one process is OpenBLAS issue
    #5156, whose maintainer verdict is "That won't work at all" — a wrong
    library rather than a failed build, so nothing downstream would notice.
    """
    darwin_install(["libgomp.1.dylib", "libomp.dylib", "libSystem.B.dylib"])

    with pytest.raises(openblas.MixedOpenMPRuntimeError, match="libgomp"):
        openblas.validate(tmp_path, system="Darwin", machine="arm64")


def test_validate_accepts_a_darwin_library_with_one_openmp_runtime(
    darwin_install, tmp_path
):
    darwin_install(["libgomp.1.dylib", "libSystem.B.dylib"])

    assert openblas.validate(tmp_path, system="Darwin", machine="arm64") == tmp_path


def test_check_reports_a_mixed_openmp_install_as_its_own_verdict(
    darwin_install, tmp_path, capsys
):
    """Distinct from both build verdicts: no rebuild of any kind fixes it, so
    the macOS driver stops rather than recompiling the same wrong library.
    """
    darwin_install(["libgomp.1.dylib", "libomp.dylib"])

    code = openblas.main(["--check", "--prefix", str(tmp_path)])

    assert code == openblas.CHECK_MIXED_OPENMP
    assert "libomp" in capsys.readouterr().out


def test_check_reports_an_uninspectable_install_as_its_own_verdict(
    monkeypatch, tmp_path, capsys
):
    """`otool` missing is a fact about this machine, not about the install.

    Reported as CHECK_NO_INSTALL — which is what an uncaught exception's exit
    code would be — it buys a full rebuild of a library that is already there,
    and then fails on the same missing tool anyway.
    """
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    install_tree(tmp_path, system="Darwin", corename="ARMV8")

    def no_otool(_):
        raise openblas.InstallInspectionError("otool is not installed")

    monkeypatch.setattr(openblas, "linked_libraries", no_otool)

    code = openblas.main(["--check", "--prefix", str(tmp_path)])

    assert code == openblas.CHECK_CANNOT_INSPECT
    assert "otool" in capsys.readouterr().out


def test_validate_rejects_an_install_that_records_no_build_arguments(tmp_path):
    """The general case of the bug the CPU baseline check closed for one flag.

    A restored tree can predate any build argument, and the core name in
    openblas_config.h is evidence about one of them. An install that cannot say
    what built it is not an install this build can vouch for.
    """
    install_tree(tmp_path, corename="SKYLAKEX", stamp=False)

    with pytest.raises(openblas.BuildRecipeError, match="does not record"):
        openblas.validate(tmp_path, system="Linux", machine="x86_64")


def test_validate_rejects_a_darwin_install_built_before_sve_was_disabled(tmp_path):
    """The stale tree this ticket's own change creates.

    NO_SVE=1 leaves no trace in the install — the core name is ARMV8 either
    way — so without the stamp the fallback restore-keys hand back a tree built
    without it and nothing asks again.
    """
    install_tree(tmp_path, system="Darwin", corename="ARMV8")
    openblas.write_build_stamp(
        tmp_path,
        [
            argument
            for argument in openblas.build_arguments(
                jobs=4, system="Darwin", machine="arm64"
            )
            if argument != "NO_SVE=1"
        ],
    )

    with pytest.raises(openblas.BuildRecipeError, match="NO_SVE=1"):
        openblas.validate(tmp_path, system="Darwin", machine="arm64")


def test_the_stamp_ignores_how_many_jobs_the_build_used():
    """A tree is not different for having been built on a bigger machine."""
    four = openblas.recipe(
        openblas.build_arguments(jobs=4, system="Linux", machine="x86_64")
    )
    sixteen = openblas.recipe(
        openblas.build_arguments(jobs=16, system="Linux", machine="x86_64")
    )

    assert four == sixteen
    assert not [argument for argument in four if argument.startswith("-j")]


def test_a_built_install_records_what_built_it(tmp_path, monkeypatch):
    """The stamp is written by the build, so a rebuild clears a stale verdict."""
    install_tree(tmp_path, corename="SKYLAKEX", stamp=False)
    monkeypatch.setattr(openblas, "check_call", lambda *_args, **_kwargs: None)

    openblas.run(source_dir=tmp_path, prefix=tmp_path, jobs=2)

    assert openblas.stamped_recipe(tmp_path) == openblas.recipe(
        openblas.build_arguments(jobs=2)
    )


def test_no_verdict_is_an_exit_code_something_else_already_means():
    """1 and 2 are not available to a verdict. An unhandled exception exits 1
    and argparse exits 2 on a usage error, and a driver branching on the number
    cannot tell either of those from a verdict it was told to act on -- which
    for the verdict that used to hold 1 meant rebuilding in place after a
    question that was never answered.
    """
    verdicts = {
        openblas.CHECK_NO_INSTALL,
        openblas.CHECK_WRONG_CPU,
        openblas.CHECK_MIXED_OPENMP,
        openblas.CHECK_CANNOT_INSPECT,
    }

    assert len(verdicts) == 4
    assert not verdicts & {1, 2}


def test_an_otool_that_fails_is_an_uninspectable_install(monkeypatch, tmp_path):
    """A non-zero `otool -L` is not "nothing is installed". Reaching main as a
    CalledProcessError it would exit 1, and a driver reading that as a verdict
    rebuilds in place on the strength of a question that was never answered.
    """
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")

    def failing_otool(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["otool", "-L"], stderr="truncated")

    monkeypatch.setattr(openblas.subprocess, "run", failing_otool)

    with pytest.raises(openblas.InstallInspectionError):
        openblas.linked_libraries(tmp_path / "libopenblas.dylib")


def test_check_reports_a_failing_otool_as_uninspectable(monkeypatch, tmp_path, capsys):
    """End to end through main: the verdict a driver acts on, not exit 1."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    install_tree(tmp_path, system="Darwin", corename="ARMV8")
    real_run = openblas.subprocess.run

    def failing_otool(command, *args, **kwargs):
        if command and command[0] == "otool":
            raise subprocess.CalledProcessError(1, command, stderr="truncated")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(openblas.subprocess, "run", failing_otool)

    code = openblas.main(["--check", "--prefix", str(tmp_path)])

    assert code == openblas.CHECK_CANNOT_INSPECT
    assert "otool" in capsys.readouterr().out


@pytest.mark.parametrize("driver", DRIVERS)
def test_every_driver_rebuilds_only_for_the_verdicts_that_say_to(driver):
    """Neither driver may treat "not zero" as "build it": that reads an
    unhandled exception, and every verdict a later version of this module
    adds, as an instruction to spend 40 minutes on a rebuild that will not fix
    it. The two drivers ask the same question, so they answer it the same way.
    """
    script = driver.read_text()

    assert f"{openblas.CHECK_NO_INSTALL})" in script
    assert f"{openblas.CHECK_WRONG_CPU})" in script
    # The catch-all that makes an unrecognised verdict -- an unhandled
    # exception's 1 included -- fatal rather than a rebuild.
    assert "*)" in script
    assert "not rebuilding" in script
