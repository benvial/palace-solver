"""Check that a packaged binary's shared libraries come from the wheel.

The question is the same on both platforms — can this binary find everything it
needs on a machine that is not the one that built it — but the evidence is not,
and neither is the tool.

On Linux ``ldd`` resolves each dependency and says ``not found`` when it cannot,
so the check is a search for that phrase. On macOS there is no ``ldd``, and
``otool -L`` does not resolve anything: it prints the install names recorded in
the Mach-O header. That turns out to be the more useful answer, because the
failure this guards against is exactly a *recorded* path. ``delocate`` rewrites
the install names of what it recognises as a library and copies it into the
wheel; a Mach-O it treats as data keeps the absolute path it was linked with,
which points into the build root and exists on no user's machine. So on Darwin
an install name is acceptable when it is loader-relative (``@loader_path``,
``@rpath``, ``@executable_path`` — inside the wheel, and the run proves the rest)
or when it names a library macOS itself ships. Anything else is a dependency the
wheel cannot satisfy, whether or not it happens to resolve on this machine.

One wrinkle belongs to ``otool -L`` rather than to the payload: asked about a
dylib it prints that dylib's *own* install id first, and ``delocate`` rewrites
the id of every library it copies to a deliberately unusable ``/DLC/`` path,
because dependents reach the bundled copy through ``@loader_path`` and nothing
should name it absolutely. The id is not a dependency, so it is read separately
with ``otool -D`` and dropped -- by value rather than by position, so a binary
that really does name another library's bundled copy is still reported.

On Windows the evidence is the PE import table, and like an install name it is
a *recorded* name rather than a resolution. It is read in-process with
``pefile`` instead of by a platform tool, so the reader is chosen by the file's
format rather than by the running platform: a Linux job can check a Windows
payload, and the tests need no Windows. A DLL import is satisfied when the DLL
sits beside the importer -- the flat layout, since an ``.exe`` has no RPATH and
the loader searches the application's own directory first -- or when Windows
itself ships it. Delay-loaded DLLs count: one is bound at its first call rather
than at start-up, so a missing one is a crash deferred to whichever code path
calls it first, which a smoke run may never take. The same walk, followed
transitively and stopped at the system DLLs, is the closure the Windows repair
copies (:func:`pe_import_closure`), so the repair and its check cannot disagree
about what a DLL needs.

Used by ``scripts/smoke-test.sh``, which has one payload and two platforms.
"""

from __future__ import annotations

import argparse
import functools
import platform
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from wheelbuild import platforms as platforms_module

#: The pefile a build installs to read PE import tables, here so the Windows
#: build driver installs the version the unit tests ran against from one
#: spelling. Exact rather than a floor: pefile is a parser of hostile input
#: that tightens its checks between releases, and a check that turns red needs
#: a commit to blame. The ``dev`` extra and the checks job name the same pin.
PEFILE_REQUIREMENT = "pefile==2024.8.26"

#: API-set contracts. The loader resolves the name through the API-set schema
#: on the running machine; it names a contract rather than a file, so no wheel
#: can carry one. The UCRT's ``api-ms-win-crt-*`` is the set that matters here.
_WINDOWS_API_SET_PREFIXES = ("api-ms-win-", "ext-ms-")

#: System DLLs the shipped list leaves out because Windows 7 lacked them, which
#: the wheel's Windows 10 floor guarantees: the UCRT's own implementation DLL.
_WINDOWS_FLOOR_DLLS = frozenset({"ucrtbase.dll"})

#: Every DLL present on every x64 Windows, from delvewheel; the file says where
#: it came from and how to regenerate it.
_WINDOWS_SYSTEM_DLL_LIST = (
    Path(__file__).resolve().parent / "data" / "windows-system-dlls.txt"
)

#: Substrings that name an MPI library: MPICH's ``libmpi`` on Linux and macOS,
#: MS-MPI's ``msmpi.dll`` on Windows.
_MPI_LIBRARY_MARKERS = ("libmpi", "msmpi")

#: Prefixes of the libraries macOS ships, which no wheel vendors and which are
#: present on every machine. The same two directories ``delocate`` refuses to
#: copy from, for the same reason.
_MACOS_SYSTEM_PREFIXES = ("/usr/lib/", "/System/")

#: The install-name prefixes that resolve relative to the loading binary, and so
#: travel with the wheel.
_LOADER_RELATIVE_PREFIXES = ("@loader_path", "@rpath", "@executable_path")

#: The dependency lister per platform, and the reader of what it prints. One
#: table rather than a cascade in each function, so a platform is added or found
#: unsupported in one place and the two halves cannot disagree about which
#: platforms are known.
_TOOLS = {"Linux": ("ldd",), "Darwin": ("otool", "-L")}

#: The install-id lister, where the platform has the concept. ELF has no
#: equivalent question: a soname is not printed by ``ldd`` and never appears in
#: its own dependency list, so Linux needs no entry and gets none.
_IDENTITY_TOOLS = {"Darwin": ("otool", "-D")}


@dataclass(frozen=True)
class LinkReport:
    """What one binary's dependencies are, and which of them travel with it."""

    binary: Path
    dependencies: tuple[str, ...]
    unsatisfied: tuple[str, ...]

    @property
    def links_mpi(self) -> bool:
        """Whether the binary links an MPI library at all.

        A Palace that links none is not a Palace that was built against the
        vendored MPI, whatever else the wheel carries.
        """
        return any(
            marker in name.lower()
            for name in self.dependencies
            for marker in _MPI_LIBRARY_MARKERS
        )

    @property
    def text(self) -> str:
        """Human-readable verdict, naming every unsatisfied dependency."""
        if not self.unsatisfied:
            return (
                f"{self.binary.name}: {len(self.dependencies)} dependencies, "
                "all from the wheel or the system"
            )
        listed = "\n  ".join(self.unsatisfied)
        return (
            f"{self.binary.name}: {len(self.unsatisfied)} dependencies the wheel "
            f"cannot satisfy:\n  {listed}"
        )


def dependency_tool(*, system: str | None = None) -> list[str]:
    """Return the command that lists a binary's dependencies on one platform.

    Args:
        system: ``platform.system()`` value; defaults to the running platform.

    Returns:
        The command, to which the binary is appended.

    Raises:
        platforms_module.UnsupportedPlatformError: For any other platform.
    """
    resolved = system or platform.system()
    tool = _TOOLS.get(resolved)
    if tool is None:
        raise platforms_module.UnsupportedPlatformError(
            f"no shared library lister for {resolved}"
        )
    return list(tool)


def identity_tool(*, system: str | None = None) -> list[str] | None:
    """Return the command that prints a binary's own install id, if any.

    Args:
        system: ``platform.system()`` value; defaults to the running platform.

    Returns:
        The command, to which the binary is appended, or None on a platform
        where a binary's dependency list cannot contain its own name.
    """
    tool = _IDENTITY_TOOLS.get(system or platform.system())
    return None if tool is None else list(tool)


def parse_identity(output: str) -> str | None:
    """Return the install id ``otool -D`` printed, or None for a non-dylib.

    ``otool -D`` prints the file it was asked about, then the id on its own
    line. An executable has no id, so the header is all there is.

    Args:
        output: Standard output of :func:`identity_tool`.

    Returns:
        The install id, or None.
    """
    for line in output.splitlines()[1:]:
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def parse(
    output: str,
    *,
    binary: Path,
    system: str | None = None,
    identity: str | None = None,
) -> LinkReport:
    """Read the dependency listing one platform's tool produced.

    Args:
        output: Standard output of :func:`dependency_tool`.
        binary: The binary it was run on, for the report.
        system: ``platform.system()`` value; defaults to the running platform.
        identity: The binary's own install id, from :func:`parse_identity`,
            which ``otool -L`` lists among the dependencies although it is not
            one.

    Returns:
        The dependencies found and those the wheel cannot satisfy.

    Raises:
        platforms_module.UnsupportedPlatformError: For a platform with no tool
            here.
    """
    resolved = system or platform.system()
    reader = _READERS.get(resolved)
    if reader is None:
        raise platforms_module.UnsupportedPlatformError(
            f"no shared library lister for {resolved}"
        )
    dependencies, unsatisfied = reader(output, identity)
    return LinkReport(
        binary=binary,
        dependencies=tuple(dependencies),
        unsatisfied=tuple(unsatisfied),
    )


def _parse_ldd(
    output: str, _identity: str | None = None
) -> tuple[list[str], list[str]]:
    """Return ``ldd``'s dependency names, and those it reported as not found.

    The install id is accepted and ignored: ELF has none in this listing, and
    the two readers share one signature so :func:`parse` needs no branch.
    """
    dependencies: list[str] = []
    unsatisfied: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # `libmpi.so.12 => /path/to/libmpi.so.12 (0x...)`, or a bare path for
        # the loader itself and for linux-vdso, which has no file at all. The
        # bare-path form carries the load address in the same field, so the
        # space is cut too and every entry is a name.
        name = stripped.split("=>", maxsplit=1)[0].split(" ", maxsplit=1)[0].strip()
        if not name:
            continue
        dependencies.append(name)
        if "not found" in stripped:
            unsatisfied.append(name)
    return dependencies, unsatisfied


def _parse_otool(
    output: str, identity: str | None = None
) -> tuple[list[str], list[str]]:
    """Return the install names ``otool -L`` printed, and those that escape.

    The first line is the file otool was asked about, printed unindented; every
    dependency line is indented by a tab. When the file is a dylib the first of
    those is its own install id, which ``identity`` names and which is dropped
    -- it is what the file *is*, not something it needs.
    """
    dependencies: list[str] = []
    unsatisfied: list[str] = []
    for line in output.splitlines():
        if not line.startswith(("\t", " ")):
            continue
        name = line.strip().split(" ", maxsplit=1)[0]
        if not name or name == identity:
            continue
        dependencies.append(name)
        if not _travels_with_the_wheel(name):
            unsatisfied.append(name)
    return dependencies, unsatisfied


_READERS = {"Linux": _parse_ldd, "Darwin": _parse_otool}


def _travels_with_the_wheel(install_name: str) -> bool:
    """Whether a Mach-O install name resolves on a machine that is not this one."""
    return install_name.startswith(
        _LOADER_RELATIVE_PREFIXES
    ) or install_name.startswith(_MACOS_SYSTEM_PREFIXES)


class UnresolvedImportError(FileNotFoundError):
    """A PE imports a DLL that is neither a system DLL nor anywhere searched."""


def is_windows_system_dll(name: str) -> bool:
    """Whether Windows itself provides the DLL a PE imports by ``name``.

    An API-set contract (``api-ms-win-*``, ``ext-ms-*``), ``ucrtbase.dll``, or a
    DLL every x64 Windows ships in ``System32``. The VC++ redistributables are
    not system DLLs: a wheel vendors them or declares them.

    Args:
        name: The DLL name as recorded in an import table, in any case.

    Returns:
        True when no wheel should carry it.
    """
    lowered = name.lower()
    return (
        lowered.startswith(_WINDOWS_API_SET_PREFIXES)
        or lowered in _WINDOWS_FLOOR_DLLS
        or lowered in _windows_system_dlls()
    )


@functools.cache
def _windows_system_dlls() -> frozenset[str]:
    """Return the shipped list of System32 DLLs, read once."""
    lines = _WINDOWS_SYSTEM_DLL_LIST.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip() for line in lines if line.strip() and not line.startswith("#")
    )


def pe_imports(binary: Path) -> tuple[str, ...]:
    """Return the DLLs a PE file imports, delay-loaded ones included.

    The one PE reader in this package: the link check reads one file's imports
    with it, and :func:`pe_import_closure` follows them.

    Names are as recorded, in table order, ordinary imports first. Each DLL is
    named once, compared without regard to case as the Windows loader compares
    them, so a DLL both imported and delay-loaded is one dependency.

    Args:
        binary: An executable or DLL.

    Returns:
        The imported DLL names.

    Raises:
        pefile.PEFormatError: If ``binary`` is not a PE file.
    """
    # Imported here rather than at the top: the Linux and macOS smoke tests run
    # this module under a bare runner Python that has no reason to carry a PE
    # parser, and reach none of this.
    import pefile  # noqa: PLC0415

    with pefile.PE(str(binary), fast_load=True) as image:
        # Only the two import directories, and only the DLL names of the first:
        # the solver is 93.5 MiB, and its symbol lists are not the question.
        image.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ],
            import_dllnames_only=True,
        )
        entries = [
            *getattr(image, "DIRECTORY_ENTRY_IMPORT", []),
            *getattr(image, "DIRECTORY_ENTRY_DELAY_IMPORT", []),
        ]
    names: dict[str, str] = {}
    for entry in entries:
        name = entry.dll.decode("ascii", errors="replace")
        names.setdefault(name.lower(), name)
    return tuple(names.values())


def pe_import_closure(
    binaries: Iterable[Path], search_path: Sequence[Path]
) -> tuple[Path, ...]:
    """Return every non-system DLL ``binaries`` load, directly or through another.

    The Windows repair copies exactly this, flat, beside the executables. The
    walk reads each binary's imports with :func:`pe_imports`, stops at a system
    DLL (:func:`is_windows_system_dll`), and looks every other name up in
    ``search_path``, in order, the first directory holding the name winning as
    the first ``PATH`` entry would. A DLL found is walked in turn.

    Args:
        binaries: The roots, such as the solver and the launcher executables.
            They are walked but not returned.
        search_path: Directories to find DLLs in, highest priority first: the
            install prefix's, the toolchain's, the MS-MPI redistributable's.

    Returns:
        The DLL files found, each once, in the order the walk reached them.

    Raises:
        UnresolvedImportError: If any import is neither a system DLL nor in
            ``search_path``, naming every such import and what imported it --
            a repair that guessed Windows had it would ship a wheel that cannot
            start.
    """
    available = _dll_index(search_path)
    found: dict[str, Path] = {}
    unresolved: list[str] = []
    seen: set[str] = set()
    queue = list(binaries)
    while queue:
        importer = queue.pop(0)
        for name in pe_imports(importer):
            key = name.lower()
            if key in seen or is_windows_system_dll(name):
                continue
            seen.add(key)
            location = available.get(key)
            if location is None:
                unresolved.append(f"{name} (imported by {importer.name})")
                continue
            found[key] = location
            queue.append(location)
    if unresolved:
        searched = ", ".join(str(directory) for directory in search_path)
        listed = "\n  ".join(unresolved)
        raise UnresolvedImportError(
            f"{len(unresolved)} DLLs are neither system DLLs nor in "
            f"{searched}:\n  {listed}"
        )
    return tuple(found.values())


def _dll_index(directories: Iterable[Path]) -> dict[str, Path]:
    """Map each lowercased file name in ``directories`` to its first location.

    Lowercased because the import table and the file system each have their own
    idea of a DLL's case, and Windows honours neither; the closure has to agree
    with Windows even when it runs on a case-sensitive file system.
    """
    index: dict[str, Path] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if child.is_file():
                index.setdefault(child.name.lower(), child)
    return index


def inspect_pe(binary: Path) -> LinkReport:
    """Read a PE's imports and check each is beside it or part of Windows.

    Args:
        binary: An executable or DLL in the payload.

    Returns:
        The report.
    """
    dependencies = pe_imports(binary)
    beside = _dll_index([binary.parent])
    unsatisfied = tuple(
        name
        for name in dependencies
        if not is_windows_system_dll(name) and name.lower() not in beside
    )
    return LinkReport(binary=binary, dependencies=dependencies, unsatisfied=unsatisfied)


def inspect_binary(binary: Path, *, system: str | None = None) -> LinkReport:
    """Read ``binary``'s dependencies and which of them the wheel satisfies.

    A PE file is read in-process, wherever this runs; anything else is handed
    to the running platform's tool.

    Args:
        binary: The packaged executable to inspect.
        system: ``platform.system()`` value; defaults to the running platform.
            Not consulted for a PE file.

    Returns:
        The report.

    Raises:
        platforms_module.UnsupportedPlatformError: For a platform with no tool
            here.
    """
    if platforms_module.is_pe(binary):
        return inspect_pe(binary)
    identity = None
    id_command = identity_tool(system=system)
    if id_command is not None:
        identity = parse_identity(_run([*id_command, str(binary)]))
    return parse(
        _run([*dependency_tool(system=system), str(binary)]),
        binary=binary,
        system=system,
        identity=identity,
    )


def _run(command: list[str]) -> str:
    """Return a lister's standard output, whatever its exit status.

    Not ``check=True``: ``ldd`` exits non-zero on a binary it could not fully
    resolve, which is the case being diagnosed rather than a reason to stop
    before reporting it.
    """
    return subprocess.run(command, capture_output=True, text=True, check=False).stdout


def main(argv: Sequence[str] | None = None) -> int:
    """Report each binary's dependencies, failing on anything the wheel misses.

    Every binary is reported before anything fails, because one unsatisfied
    dependency in the process manager and one in the solver are different
    diagnoses and a run that stops at the first hides the second. That includes
    an argument that names nothing: the smoke test passes the payload's ``bin``
    directory and the vendored-library directory in one invocation, so
    resolving the arguments up front threw away the whole report from the one
    that existed in order to complain about the one that did not. An absent
    path is therefore a finding reported at the end, alongside the unsatisfied
    install names, rather than a traceback before the first listing.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "binaries",
        type=Path,
        nargs="+",
        help="executables to inspect; a directory means every binary in it",
    )
    parser.add_argument(
        "--require-mpi",
        action="store_true",
        help="also fail when a binary links no MPI library at all",
    )
    args = parser.parse_args(argv)
    failed = False
    missing: list[Path] = []
    for path in args.binaries:
        # One argument at a time, so an absent one costs only its own report.
        try:
            binaries = _expand([path])
        except FileNotFoundError:
            missing.append(path)
            continue
        for binary in binaries:
            report = inspect_binary(binary)
            print(report.text, flush=True)
            if report.unsatisfied:
                failed = True
            if args.require_mpi and not report.links_mpi:
                print(f"ERROR: {binary.name} links no MPI library", flush=True)
                failed = True
    # After the listings rather than among them: the absence is a finding about
    # the repair step, and the listings are the evidence a reader wants first.
    for path in missing:
        print(f"ERROR: nothing to inspect at {path}", flush=True)
        failed = True
    return 1 if failed else 0


def _expand(paths: Sequence[Path]) -> list[Path]:
    """Return the binaries to inspect, expanding any directory named.

    A directory is expanded here rather than by the caller's shell because the
    payload directory holds a tracked ``.gitkeep`` and, on a wheel, whatever the
    repair tool added; only the compiled files have dependencies to list, and a
    shell glob that matches nothing would otherwise be passed through as a
    literal pattern and reported as a binary with no dependencies at all.

    Raises:
        FileNotFoundError: If a named path is neither a file nor a directory.
    """
    expanded: list[Path] = []
    for path in paths:
        if path.is_dir():
            expanded.extend(
                sorted(
                    child
                    for child in path.iterdir()
                    if child.is_file() and platforms_module.is_native_binary(child)
                )
            )
        elif path.is_file():
            expanded.append(path)
        else:
            # Not skipped: the caller names the directory the repair tool is
            # expected to have filled, so an absent one means the repair put the
            # libraries somewhere else -- exactly the thing being checked. Passed
            # through to the lister it would be reported as a binary with no
            # dependencies, which reads as a pass.
            raise FileNotFoundError(f"nothing to inspect at {path}")
    return expanded


if __name__ == "__main__":
    raise SystemExit(main())
