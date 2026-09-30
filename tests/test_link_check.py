import sys
from pathlib import Path

import pytest

from wheelbuild import link_check, platforms

LDD_RESOLVED = """\
\tlinux-vdso.so.1 (0x00007ffd1c5f2000)
\tlibmpi.so.12 => /venv/lib/palace_solver.libs/libmpi-4f1c.so.12 (0x00007f8e)
\tlibopenblas.so.0 => /venv/lib/palace_solver.libs/libopenblas-1b2c.so.0 (0x7f8d)
\tlibc.so.6 => /lib64/libc.so.6 (0x00007f8c)
\t/lib64/ld-linux-x86-64.so.2 (0x00007f8f)
"""

LDD_MISSING = """\
\tlibmpi.so.12 => /venv/lib/palace_solver.libs/libmpi-4f1c.so.12 (0x00007f8e)
\tlibgfortran.so.5 => not found
"""

OTOOL_REPAIRED = """\
/venv/lib/python3.13/site-packages/palace_solver/bin/palace-real:
\t@loader_path/../.dylibs/libmpi.12.dylib (compatibility version 0.0.0)
\t@loader_path/../.dylibs/libopenblas.0.dylib (compatibility version 0.0.0)
\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 1800.0.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0)
\t/System/Library/Frameworks/Accelerate.framework/Versions/A/Accelerate
"""

OTOOL_UNREPAIRED = """\
/venv/lib/python3.13/site-packages/palace_solver/bin/mpiexec.hydra:
\t/Users/runner/palace-build/install/lib/libmpi.12.dylib (compatibility version 0)
\t/opt/homebrew/opt/gcc/lib/gcc/15/libgfortran.5.dylib (compatibility version 0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0)
"""


OTOOL_VENDORED_LIBRARY = """\
/venv/lib/python3.13/site-packages/palace_solver/.dylibs/libpalace.dylib:
\t/DLC/palace_solver/.dylibs/libpalace.dylib (compatibility version 0.0.0)
\t@loader_path/libmpi.12.dylib (compatibility version 0.0.0)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0)
"""

OTOOL_IDENTITY = """\
/venv/lib/python3.13/site-packages/palace_solver/.dylibs/libpalace.dylib:
/DLC/palace_solver/.dylibs/libpalace.dylib
"""

OTOOL_NO_IDENTITY = """\
/venv/lib/python3.13/site-packages/palace_solver/bin/palace-real:
"""


def test_the_tool_is_the_platforms_own():
    assert link_check.dependency_tool(system="Linux") == ["ldd"]
    assert link_check.dependency_tool(system="Darwin") == ["otool", "-L"]


def test_an_unsupported_platform_has_no_tool():
    with pytest.raises(platforms.UnsupportedPlatformError):
        link_check.dependency_tool(system="Windows")


def test_ldd_names_every_dependency(tmp_path):
    report = link_check.parse(
        LDD_RESOLVED, binary=tmp_path / "palace-real", system="Linux"
    )

    assert "libmpi.so.12" in report.dependencies
    assert "libopenblas.so.0" in report.dependencies
    assert report.unsatisfied == ()
    assert report.links_mpi


def test_ldd_reports_a_library_the_loader_cannot_find(tmp_path):
    report = link_check.parse(
        LDD_MISSING, binary=tmp_path / "palace-real", system="Linux"
    )

    assert report.unsatisfied == ("libgfortran.so.5",)


def test_a_repaired_mach_o_depends_only_on_the_wheel_and_the_system(tmp_path):
    report = link_check.parse(
        OTOOL_REPAIRED, binary=tmp_path / "palace-real", system="Darwin"
    )

    assert report.unsatisfied == ()
    assert report.links_mpi


def test_an_unrepaired_mach_o_still_points_at_the_build_machine(tmp_path):
    """The failure this exists for: delocate rewrites what it recognises as a
    library, and an executable it treated as data keeps the absolute install
    name it was linked with — a path that exists only on the runner.
    """
    report = link_check.parse(
        OTOOL_UNREPAIRED, binary=tmp_path / "mpiexec.hydra", system="Darwin"
    )

    assert report.unsatisfied == (
        "/Users/runner/palace-build/install/lib/libmpi.12.dylib",
        "/opt/homebrew/opt/gcc/lib/gcc/15/libgfortran.5.dylib",
    )


def test_the_otool_header_line_is_not_a_dependency(tmp_path):
    """otool prints the file it was asked about first, unindented."""
    report = link_check.parse(
        OTOOL_REPAIRED, binary=tmp_path / "palace-real", system="Darwin"
    )

    assert not any(name.endswith("palace-real") for name in report.dependencies)


def test_a_binary_linking_no_mpi_is_not_the_solver(tmp_path):
    """A Palace built against a system MPI that auditwheel then vendored nothing
    for still runs on the build machine, and only there.
    """
    report = link_check.parse(
        "\tlibc.so.6 => /lib64/libc.so.6 (0x00007f8c)\n",
        binary=tmp_path / "palace-real",
        system="Linux",
    )

    assert not report.links_mpi


def test_the_report_names_the_binary_and_the_verdict():
    report = link_check.parse(
        LDD_MISSING, binary=Path("/venv/bin/palace-real"), system="Linux"
    )

    assert "palace-real" in report.text
    assert "libgfortran.so.5" in report.text


#: Both halves of this module are per-platform, and the two tests that shell out
#: to the real tool can only run where one of them exists.
needs_a_real_lister = pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"},
    reason="the dependency lister is ldd or otool",
)


@needs_a_real_lister
def test_main_reports_a_real_system_binary(capsys):
    """The one part no fixture can stand in for: that the tool is installed,
    that its output is the shape parsed above, and that a binary linking only
    system libraries passes.
    """
    binary = Path(sys.executable)

    assert link_check.main([str(binary)]) == 0
    assert binary.name in capsys.readouterr().out


@needs_a_real_lister
def test_main_fails_a_binary_that_links_no_mpi(capsys):
    assert link_check.main([sys.executable, "--require-mpi"]) == 1
    assert "no MPI library" in capsys.readouterr().out


def test_a_directory_expands_to_the_binaries_in_it(tmp_path, capsys):
    """What the smoke test passes: the payload's whole bin directory, which also
    holds a tracked .gitkeep and whatever the repair tool left behind.
    """
    (tmp_path / ".gitkeep").write_text("")
    (tmp_path / "notes.txt").write_text("not a binary")
    (tmp_path / "palace-real").write_bytes(b"\x7fELF\x02\x01\x01\x00rest")

    link_check.main([str(tmp_path)])

    assert "palace-real" in capsys.readouterr().out


def test_a_directory_with_no_binaries_inspects_nothing(tmp_path, capsys):
    (tmp_path / ".gitkeep").write_text("")

    assert link_check.main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_an_absent_path_is_a_failure_rather_than_nothing_to_do(tmp_path):
    """The smoke test names the directory the repair tool should have filled, so
    its absence is the finding — and a missing path handed to the lister would be
    reported as a binary with no dependencies, which reads as a pass.
    """
    with pytest.raises(FileNotFoundError):
        link_check.main([str(tmp_path / "dylibs-that-were-never-created")])


def test_a_dylibs_own_install_id_is_not_one_of_its_dependencies(tmp_path):
    """`otool -L` prints a dylib's own LC_ID_DYLIB as its first entry.

    delocate rewrites the id of every library it copies to a deliberately
    unusable `/DLC/` path, since dependents reach it through `@loader_path` and
    nothing should name the bundled copy absolutely. Read as a dependency it
    fails every test this module applies, which failed the macOS smoke test on
    all 39 vendored libraries at once.
    """
    report = link_check.parse(
        OTOOL_VENDORED_LIBRARY,
        binary=tmp_path / "libpalace.dylib",
        system="Darwin",
        identity="/DLC/palace_solver/.dylibs/libpalace.dylib",
    )

    assert report.unsatisfied == ()
    assert report.dependencies == (
        "@loader_path/libmpi.12.dylib",
        "/usr/lib/libSystem.B.dylib",
    )


def test_an_install_id_is_still_reported_when_it_is_not_the_files_own(tmp_path):
    """Only the file's own id is dropped, not every `/DLC/` path.

    A binary naming another library's bundled copy absolutely is exactly the
    unsatisfiable dependency this check exists to find.
    """
    report = link_check.parse(
        OTOOL_VENDORED_LIBRARY,
        binary=tmp_path / "libpalace.dylib",
        system="Darwin",
        identity="/DLC/palace_solver/.dylibs/libsomethingelse.dylib",
    )

    assert report.unsatisfied == ("/DLC/palace_solver/.dylibs/libpalace.dylib",)


def test_the_identity_tool_is_asked_only_on_darwin():
    assert link_check.identity_tool(system="Darwin") == ["otool", "-D"]
    assert link_check.identity_tool(system="Linux") is None


def test_parse_identity_reads_the_id_otool_printed():
    assert (
        link_check.parse_identity(OTOOL_IDENTITY)
        == "/DLC/palace_solver/.dylibs/libpalace.dylib"
    )


def test_parse_identity_is_none_for_a_file_that_is_not_a_dylib():
    """`otool -D` prints the header alone for an executable, which has no id."""
    assert link_check.parse_identity(OTOOL_NO_IDENTITY) is None
