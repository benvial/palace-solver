import struct
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
        link_check.dependency_tool(system="FreeBSD")


def test_windows_has_no_tool_because_a_pe_is_read_in_process():
    """There is nothing to shell out to: pefile reads the import table, which is
    what lets the same check run against a Windows payload from Linux.
    """
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


def test_an_absent_path_is_a_failure_rather_than_nothing_to_do(tmp_path, capsys):
    """The smoke test names the directory the repair tool should have filled, so
    its absence is the finding — and a missing path handed to the lister would be
    reported as a binary with no dependencies, which reads as a pass.
    """
    assert link_check.main([str(tmp_path / "dylibs-that-were-never-created")]) == 1
    assert "dylibs-that-were-never-created" in capsys.readouterr().out


def test_every_binary_is_reported_before_an_absent_path_fails(tmp_path, capsys):
    """An absent vendor directory and an unsatisfied install name are different
    diagnoses, and the absent directory is named second by the smoke test. A
    run that resolved the arguments before listing anything threw away the
    evidence from the one it could read — which is the whole output on the
    platform this check exists for.
    """
    present = tmp_path / "bin"
    present.mkdir()
    (present / "palace-real").write_bytes(b"\x7fELF\x02\x01\x01\x00rest")

    code = link_check.main([str(present), str(tmp_path / "absent")])

    output = capsys.readouterr().out
    assert code == 1
    assert "palace-real" in output
    assert "absent" in output


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


# --- Windows: the PE reader ------------------------------------------------
#
# A PE import table is read in-process with pefile rather than by a platform
# tool, so these run on any host. The files are built here rather than
# committed: a synthetic PE that pefile itself parses is as honest as a real
# one for the only part the reader looks at, the two import directories, and
# the builder shows exactly which bytes that is.

#: Where the one section of a synthetic PE starts, in the file and in memory.
_FILE_ALIGNMENT = 0x200
_SECTION_RVA = 0x1000


class _ImportSection:
    """The bytes of one ``.idata`` section, laid out as they are appended.

    Every structure that points at another does so by RVA, so a structure is
    placed first and its address read back from :meth:`add`.
    """

    def __init__(self):
        self.data = bytearray()

    def add(self, blob):
        """Append ``blob`` 8-byte aligned and return its RVA."""
        self.data.extend(b"\0" * (-len(self.data) % 8))
        rva = _SECTION_RVA + len(self.data)
        self.data.extend(blob)
        return rva

    def put(self, rva, blob):
        """Overwrite the bytes at ``rva``, to fill a table reserved earlier."""
        offset = rva - _SECTION_RVA
        self.data[offset : offset + len(blob)] = blob

    def thunks(self, dll):
        """Return the name, lookup table and address table for one DLL.

        pefile drops a descriptor whose tables are empty, as a loader would
        have nothing to bind, so every DLL imports one function.
        """
        hint_name = self.add(struct.pack("<H", 0) + b"Function\0")
        name = self.add(dll.encode() + b"\0")
        lookup = self.add(struct.pack("<QQ", hint_name, 0))
        address = self.add(struct.pack("<QQ", hint_name, 0))
        return name, lookup, address


def _portable_executable(imports=(), delayed=()):
    """Return a minimal x86-64 PE importing ``imports``, delay-loading ``delayed``.

    One section holding both directories, and nothing a loader would need to
    run it: no code, no entry point. The two directory entries are what any PE
    reader goes to, and they are filled as a linker fills them.
    """
    section = _ImportSection()
    import_table = section.add(b"\0" * 20 * (len(imports) + 1))
    delay_table = section.add(b"\0" * 32 * (len(delayed) + 1))
    for index, dll in enumerate(imports):
        name, lookup, address = section.thunks(dll)
        # IMAGE_IMPORT_DESCRIPTOR: OriginalFirstThunk, TimeDateStamp,
        # ForwarderChain, Name, FirstThunk.
        section.put(
            import_table + 20 * index,
            struct.pack("<IIIII", lookup, 0, 0, name, address),
        )
    for index, dll in enumerate(delayed):
        name, lookup, address = section.thunks(dll)
        handle = section.add(b"\0" * 8)
        # IMAGE_DELAYLOAD_DESCRIPTOR: Attributes (1: the fields are RVAs),
        # DllNameRVA, ModuleHandleRVA, ImportAddressTableRVA,
        # ImportNameTableRVA, BoundImportAddressTableRVA,
        # UnloadInformationTableRVA, TimeDateStamp.
        section.put(
            delay_table + 32 * index,
            struct.pack("<IIIIIIII", 1, name, handle, address, lookup, 0, 0, 0),
        )

    raw = bytes(section.data) + b"\0" * (-len(section.data) % _FILE_ALIGNMENT)
    directories = [(0, 0)] * 16
    directories[1] = (import_table, 20 * (len(imports) + 1))  # IMPORT
    directories[13] = (delay_table, 32 * (len(delayed) + 1))  # DELAY_IMPORT
    optional_header = struct.pack(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        0x20B,  # PE32+
        *(0, 0, 0, len(raw), 0, 0, 0),
        0x140000000,  # ImageBase
        _SECTION_RVA,  # SectionAlignment
        _FILE_ALIGNMENT,
        *(6, 0, 0, 0, 6, 0, 0),
        _SECTION_RVA + len(raw),  # SizeOfImage
        _FILE_ALIGNMENT,  # SizeOfHeaders
        0,
        3,  # IMAGE_SUBSYSTEM_WINDOWS_CUI
        *(0, 0x100000, 0x1000, 0x100000, 0x1000, 0),
        len(directories),
    ) + b"".join(struct.pack("<II", *entry) for entry in directories)
    headers = (
        b"MZ".ljust(0x3C, b"\0")
        + struct.pack("<I", 0x40)  # e_lfanew
        + b"PE\0\0"
        # IMAGE_FILE_HEADER: AMD64, one section, an executable image.
        + struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, len(optional_header), 0x22)
        + optional_header
        + struct.pack(
            "<8sIIIIIIHHI",
            b".idata",
            len(section.data),
            _SECTION_RVA,
            len(raw),
            _FILE_ALIGNMENT,
            *(0, 0, 0, 0),
            0xC0000040,  # initialised data, readable, writable
        )
    )
    return headers.ljust(_FILE_ALIGNMENT, b"\0") + raw


def _write_pe(path, imports=(), delayed=()):
    path.write_bytes(_portable_executable(imports, delayed))
    return path


#: What the spike measured palace-real.exe importing directly, less all but two
#: of the UCRT contracts, which add nothing a test of the prefix does not.
PALACE_IMPORTS = (
    "api-ms-win-crt-runtime-l1-1-0.dll",
    "api-ms-win-crt-heap-l1-1-0.dll",
    "KERNEL32.dll",
    "ADVAPI32.dll",
    "GDI32.dll",
    "USER32.dll",
    "libceed.dll",
    "libopenblas.dll",
    "msmpi.dll",
    "libmsmpifec.dll",
    "zlib1.dll",
    "libgcc_s_seh-1.dll",
    "libstdc++-6.dll",
    "libgfortran-5.dll",
    "libgomp-1.dll",
    "libwinpthread-1.dll",
)


def test_pe_imports_reads_the_import_table(tmp_path):
    binary = _write_pe(tmp_path / "palace-real.exe", PALACE_IMPORTS)

    assert link_check.pe_imports(binary) == PALACE_IMPORTS


def test_pe_imports_counts_delay_loaded_dlls(tmp_path):
    """A delay-loaded DLL is bound at its first call rather than at start-up,
    so a missing one is a crash deferred to whichever code path calls it first
    -- one a smoke run may never take. It is a dependency all the same.
    """
    binary = _write_pe(
        tmp_path / "mpiexec.exe", ["KERNEL32.dll"], delayed=["libdelayed.dll"]
    )

    assert link_check.pe_imports(binary) == ("KERNEL32.dll", "libdelayed.dll")


def test_pe_imports_names_each_dll_once(tmp_path):
    """Windows matches DLL names without regard to case, and a DLL both
    imported and delay-loaded is still one file.
    """
    binary = _write_pe(
        tmp_path / "smpd.exe", ["KERNEL32.dll", "msmpi.dll"], delayed=["kernel32.dll"]
    )

    assert link_check.pe_imports(binary) == ("KERNEL32.dll", "msmpi.dll")


def test_a_pe_with_no_imports_has_no_dependencies(tmp_path):
    """A resource-only DLL, such as a message table, imports nothing."""
    binary = _write_pe(tmp_path / "resources.dll")

    assert link_check.pe_imports(binary) == ()


@pytest.mark.parametrize(
    "name",
    [
        "api-ms-win-crt-runtime-l1-1-0.dll",
        "API-MS-WIN-CORE-SYNCH-L1-2-0.DLL",
        "ext-ms-win-ntuser-window-l1-1-0.dll",
        "KERNEL32.dll",
        "ntdll.dll",
        "ADVAPI32.dll",
        "WS2_32.dll",
        "msvcrt.dll",
        "ucrtbase.dll",
    ],
)
def test_the_system_dlls_are_the_api_sets_the_ucrt_and_windows_own(name):
    assert link_check.is_windows_system_dll(name)


@pytest.mark.parametrize(
    "name",
    [
        "msmpi.dll",
        "libgcc_s_seh-1.dll",
        "libgfortran-5.dll",
        "zlib1.dll",
        # A VC++ redistributable is something a wheel vendors or declares, not
        # part of Windows: delvewheel's line, and this module's.
        "vcruntime140.dll",
        "msvcp140.dll",
        "vcomp140.dll",
    ],
)
def test_a_runtime_the_wheel_must_carry_is_not_a_system_dll(name):
    assert not link_check.is_windows_system_dll(name)


def _flat_payload(directory):
    """A repaired Windows payload: every DLL beside the executables."""
    directory.mkdir()
    _write_pe(
        directory / "palace-real.exe",
        ["KERNEL32.dll", "msmpi.dll", "libgfortran-5.dll"],
    )
    _write_pe(directory / "msmpi.dll", ["KERNEL32.dll", "ADVAPI32.dll"])
    _write_pe(directory / "libgfortran-5.dll", ["KERNEL32.dll", "libquadmath-0.dll"])
    _write_pe(directory / "libquadmath-0.dll", ["api-ms-win-crt-heap-l1-1-0.dll"])
    return directory


def test_a_flat_payload_satisfies_every_import(tmp_path):
    payload = _flat_payload(tmp_path / "bin")

    report = link_check.inspect_binary(payload / "palace-real.exe")

    assert report.unsatisfied == ()
    assert report.dependencies == ("KERNEL32.dll", "msmpi.dll", "libgfortran-5.dll")
    assert report.links_mpi


def test_a_dll_the_wheel_left_behind_is_unsatisfied(tmp_path):
    """The failure this exists for: an import neither beside the importer nor
    part of Windows, which on the build runner resolved from MSYS2 on PATH.
    """
    payload = _flat_payload(tmp_path / "bin")
    (payload / "libquadmath-0.dll").unlink()

    report = link_check.inspect_binary(payload / "libgfortran-5.dll")

    assert report.unsatisfied == ("libquadmath-0.dll",)


def test_a_dll_beside_the_importer_matches_without_regard_to_case(tmp_path):
    """Windows' file system does not care, so neither may the check, even when
    it runs on a Linux one that does.
    """
    payload = tmp_path / "bin"
    payload.mkdir()
    _write_pe(payload / "palace-real.exe", ["MSMPI.DLL"])
    _write_pe(payload / "msmpi.dll")

    assert link_check.inspect_binary(payload / "palace-real.exe").unsatisfied == ()


def test_a_delay_loaded_dll_must_travel_with_the_wheel(tmp_path):
    payload = tmp_path / "bin"
    payload.mkdir()
    _write_pe(payload / "mpiexec.exe", ["KERNEL32.dll"], delayed=["libdelayed.dll"])

    report = link_check.inspect_binary(payload / "mpiexec.exe")

    assert report.unsatisfied == ("libdelayed.dll",)


def test_the_pe_reader_is_chosen_by_the_file_not_the_host(tmp_path):
    """A PE is read the same way wherever the check runs, so a Linux job can
    check a Windows payload; no platform tool is asked.
    """
    payload = _flat_payload(tmp_path / "bin")

    report = link_check.inspect_binary(payload / "palace-real.exe", system="Linux")

    assert report.unsatisfied == ()


def test_the_ms_mpi_runtime_counts_as_linking_mpi(tmp_path):
    payload = _flat_payload(tmp_path / "bin")

    assert link_check.main([str(payload / "palace-real.exe"), "--require-mpi"]) == 0


def test_main_expands_a_directory_of_pe_files(tmp_path, capsys):
    """What the Windows smoke test passes: the flat bin directory, holding the
    executables and every vendored DLL.
    """
    payload = _flat_payload(tmp_path / "bin")
    (payload / ".gitkeep").write_text("")
    (payload / "MZ-but-not-a-pe.txt").write_text("MZ, and then nothing like a PE")

    assert link_check.main([str(payload)]) == 0
    output = capsys.readouterr().out
    assert "palace-real.exe" in output
    assert "libquadmath-0.dll" in output
    assert "MZ-but-not-a-pe" not in output


def test_main_fails_a_payload_missing_a_dll(tmp_path, capsys):
    payload = _flat_payload(tmp_path / "bin")
    (payload / "msmpi.dll").unlink()

    assert link_check.main([str(payload)]) == 1
    assert "msmpi.dll" in capsys.readouterr().out


# --- Windows: the closure the repair copies ---------------------------------


def _build_tree(root):
    """Where a Windows build leaves the DLLs a repair collects: the install
    prefix, the toolchain's own bin directory, the MS-MPI redistributable.
    """
    install, toolchain, msmpi = root / "install", root / "ucrt64", root / "msmpi"
    for directory in (install, toolchain, msmpi):
        directory.mkdir(parents=True)
    _write_pe(
        install / "palace-real.exe",
        ["KERNEL32.dll", "libceed.dll", "msmpi.dll", "libgfortran-5.dll"],
    )
    _write_pe(install / "libceed.dll", ["KERNEL32.dll", "libxsmm.dll"])
    _write_pe(install / "libxsmm.dll", ["api-ms-win-crt-heap-l1-1-0.dll"])
    _write_pe(
        toolchain / "libgfortran-5.dll", ["libquadmath-0.dll", "libgcc_s_seh-1.dll"]
    )
    _write_pe(toolchain / "libquadmath-0.dll", ["libgcc_s_seh-1.dll"])
    _write_pe(toolchain / "libgcc_s_seh-1.dll", ["KERNEL32.dll"])
    _write_pe(msmpi / "msmpi.dll", ["KERNEL32.dll", "ADVAPI32.dll"])
    _write_pe(msmpi / "mpiexec.exe", ["KERNEL32.dll"], delayed=["libdelayed.dll"])
    _write_pe(msmpi / "libdelayed.dll")
    return install, toolchain, msmpi


def test_the_closure_is_every_non_system_dll_reached(tmp_path):
    install, toolchain, msmpi = _build_tree(tmp_path)

    closure = link_check.pe_import_closure(
        [install / "palace-real.exe"], [install, toolchain, msmpi]
    )

    assert sorted(path.name for path in closure) == [
        "libceed.dll",
        "libgcc_s_seh-1.dll",
        "libgfortran-5.dll",
        "libquadmath-0.dll",
        "libxsmm.dll",
        "msmpi.dll",
    ]
    assert install / "libxsmm.dll" in closure
    assert toolchain / "libquadmath-0.dll" in closure


def test_the_closure_of_several_roots_names_each_dll_once(tmp_path):
    """The repair walks the solver and the launcher together, and both reach
    the same runtimes; a flat copy wants each file once.
    """
    install, toolchain, msmpi = _build_tree(tmp_path)
    _write_pe(msmpi / "smpd.exe", ["msmpi.dll"], delayed=["libdelayed.dll"])

    closure = link_check.pe_import_closure(
        [install / "palace-real.exe", msmpi / "mpiexec.exe", msmpi / "smpd.exe"],
        [install, toolchain, msmpi],
    )

    names = [path.name for path in closure]
    assert len(names) == len(set(names))
    assert "libdelayed.dll" in names


def test_the_closure_takes_the_first_directory_that_has_a_dll(tmp_path):
    """Search order is the caller's, as PATH order is the loader's: a DLL the
    install prefix built wins over a same-named one the toolchain ships.
    """
    install, toolchain, msmpi = _build_tree(tmp_path)
    _write_pe(toolchain / "libceed.dll", ["KERNEL32.dll"])

    closure = link_check.pe_import_closure(
        [install / "palace-real.exe"], [install, toolchain, msmpi]
    )

    assert install / "libceed.dll" in closure
    assert toolchain / "libceed.dll" not in closure


def test_the_closure_refuses_a_dll_it_cannot_find(tmp_path):
    """An import that is neither found nor a system DLL is an error, not a
    guess that Windows has it -- a guess the wheel would then ship. Every one
    is named, with what imported it.
    """
    install, toolchain, msmpi = _build_tree(tmp_path)
    (toolchain / "libquadmath-0.dll").unlink()
    (msmpi / "msmpi.dll").unlink()

    with pytest.raises(link_check.UnresolvedImportError) as raised:
        link_check.pe_import_closure(
            [install / "palace-real.exe"], [install, toolchain, msmpi]
        )

    message = str(raised.value)
    assert "libquadmath-0.dll" in message
    assert "libgfortran-5.dll" in message
    assert "msmpi.dll" in message
