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

Used by ``scripts/smoke-test.sh``, which has one payload and two platforms.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from wheelbuild import platforms as platforms_module

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
        vendored MPICH, whatever else the wheel carries.
        """
        return any("libmpi" in name for name in self.dependencies)

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


def inspect_binary(binary: Path, *, system: str | None = None) -> LinkReport:
    """Run the platform's tool on ``binary`` and read its output.

    Args:
        binary: The packaged executable to inspect.
        system: ``platform.system()`` value; defaults to the running platform.

    Returns:
        The report.

    Raises:
        platforms_module.UnsupportedPlatformError: For a platform with no tool
            here.
    """
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
    diagnoses and a run that stops at the first hides the second.

    Raises:
        FileNotFoundError: If an argument is neither a file nor a directory.
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
    for binary in _expand(args.binaries):
        report = inspect_binary(binary)
        print(report.text, flush=True)
        if report.unsatisfied:
            failed = True
        if args.require_mpi and not report.links_mpi:
            print(f"ERROR: {binary.name} links no MPI library", flush=True)
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
