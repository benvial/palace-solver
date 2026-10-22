"""Ship x86-64-v3 copies of the compute libraries beside the baseline ones.

The Linux x86_64 wheel is compiled for the x86-64 baseline, which has neither
AVX2 nor FMA. Built from the same recipe with ``-O3 -march=x86-64-v3``, Palace
v0.18.1 ran gsim's adaptive (p-multigrid) cases 11-14 % faster on a Zen 3 host,
and MUMPS-direct cases no faster; the time was in libmfem, libpalace, libHYPRE
and libceed. Those four, and only those, get a second copy compiled for
x86-64-v3, and the wheel carries it in ``palace_solver.libs/glibc-hwcaps/
x86-64-v3/``. See ``docs/adr/0008-ship-x86-64-v3-copies-through-glibc-hwcaps.md``.

Nothing chooses between the two at run time but the dynamic loader. glibc 2.33
and newer search ``<dir>/glibc-hwcaps/x86-64-v3/`` before ``<dir>`` for every
directory on a library search path -- the binary's RPATH included -- when the
CPU supports that level. An older glibc does not know the subdirectory and
loads the baseline, and so does a CPU without AVX2. So a copy has to be
indistinguishable from the library it stands in for to everything but the CPU:
the same file name, SONAME and NEEDED entries as the one ``auditwheel``
repaired, which this module copies from that one rather than predicting, and
an RPATH that reaches the rest of the vendored libraries from one directory
further down.

Two halves, one per build stage. :func:`build` runs a second superbuild with
the copies' flags into a prefix of its own, reusing the baseline's MPICH and
OpenBLAS. :func:`graft` then puts the copies into the repaired wheel, refusing
any whose SONAME or NEEDED entries differ from the baseline build's: two builds
of the same tree with the same feature flags agree on both, and a difference
means they are not the same libraries.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import platform
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wheelbuild import platforms, superbuild
from wheelbuild import prefix as prefix_module
from wheelbuild._process import check_call
from wheelbuild.notices import library_stem

#: The glibc-hwcaps level the copies are compiled for. x86-64-v3 is the Haswell
#: (2013) and Zen 1 (2017) feature set: AVX2, FMA, BMI2, MOVBE.
LEVEL = "x86-64-v3"

#: What the copies are compiled with, on top of ``Release``'s ``-O3 -DNDEBUG``.
#: The ``-O3`` is the measured recipe's and changes nothing CMake's Release
#: flags do not already set; ``-march`` is the whole difference.
COMPILE_FLAGS = f"-O3 -march={LEVEL}"

#: The libraries that get a copy, by :func:`wheelbuild.notices.library_stem`.
#: These are where the measured time went. MUMPS, ScaLAPACK, STRUMPACK and
#: SuperLU_DIST spend theirs in OpenBLAS, which already dispatches per CPU,
#: and METIS takes no ``-march`` from the superbuild at all. Each copy costs
#: its compressed size again, against PyPI's per-file limit.
LIBRARIES = ("libpalace", "libmfem", "libHYPRE", "libceed")

#: Where the copies go, relative to the repaired wheel's vendor directory.
SUBDIRECTORY = PurePosixPath("glibc-hwcaps") / LEVEL

#: The copies' RPATH: the vendor directory, two levels up. ``auditwheel`` gives
#: the baseline libraries ``$ORIGIN``, which from here would name the
#: subdirectory, and writes it as DT_RPATH rather than DT_RUNPATH, as this does.
RPATH = "$ORIGIN/../.."

#: Where a prefix, and a build directory, record the configure line that
#: produced them. A prefix whose stamp matches is up to date and is not
#: rebuilt; a build directory whose stamp does not match is discarded, because
#: it belongs to another Palace or another recipe.
BUILD_STAMP = Path(".hwcaps-build-arguments")

#: The program :func:`host_supports_level` compiles. Configuring the copies runs
#: programs compiled with their flags -- PETSc's configure does -- so a build
#: host without the level fails there, with an illegal instruction and no hint.
_LEVEL_PROBE = f'int main(void) {{ return !__builtin_cpu_supports("{LEVEL}"); }}\n'


#: The content hash ``auditwheel`` inserts into the name of every library it
#: vendors: ``libmfem.so.4.9.0`` is shipped as ``libmfem-7df8df2e.so.4.9.0``.
_VENDORED_NAME = re.compile(r"-[0-9a-f]{8}(?:\.|$)")


class InterfaceMismatchError(RuntimeError):
    """Raised when a copy's dynamic interface is not its baseline library's."""


@dataclass(frozen=True)
class Interface:
    """What the dynamic loader matches a shared library by."""

    soname: str
    needed: tuple[str, ...]


def enabled_by_default(
    *, system: str | None = None, machine: str | None = None
) -> bool:
    """Whether a build ships the copies when nobody says otherwise.

    Args:
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.

    Returns:
        True on Linux x86_64, the only platform with glibc-hwcaps and the level.
    """
    return (system or platform.system()) == "Linux" and (
        machine or platform.machine()
    ) == "x86_64"


def require_platform(*, system: str | None = None, machine: str | None = None) -> None:
    """Refuse to build or graft the copies anywhere but Linux x86_64.

    Raises:
        platforms.UnsupportedPlatformError: Anywhere else.
    """
    if not enabled_by_default(system=system, machine=machine):
        raise platforms.UnsupportedPlatformError(
            f"{LEVEL} copies are for the Linux x86_64 wheel only: glibc-hwcaps is "
            "a glibc mechanism and the level an x86-64 one"
        )


def host_supports_level(compiler: str | None = None) -> bool:
    """Whether the build machine can run code compiled for :data:`LEVEL`.

    Args:
        compiler: C compiler to probe with; ``$CC``, else ``cc``.

    Returns:
        True if a program compiled here reports the level supported.
    """
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "probe.c"
        source.write_text(_LEVEL_PROBE)
        probe = Path(scratch) / "probe"
        subprocess.run(
            [compiler or os.environ.get("CC", "cc"), str(source), "-o", str(probe)],
            check=True,
        )
        return subprocess.run([str(probe)], check=False).returncode == 0


def stamped(directory: Path) -> tuple[str, ...] | None:
    """Return the configure line ``directory`` records, or None if it has none."""
    try:
        text = (directory / BUILD_STAMP).read_text()
    except OSError:
        return None
    return tuple(line for line in text.splitlines() if line)


def stamp(directory: Path, arguments: Sequence[str]) -> None:
    """Record the configure line that produced ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / BUILD_STAMP).write_text("\n".join(arguments) + "\n")


def build(
    *,
    source_dir: Path,
    build_dir: Path,
    install_prefix: Path,
    prefix: Path,
    jobs: int,
    libraries: Sequence[str] = LIBRARIES,
) -> bool:
    """Build the copies into a prefix of their own, unless it is up to date.

    The whole superbuild runs again with :data:`COMPILE_FLAGS`: the copies have
    to come from the same tree and feature flags as the baseline, and MFEM and
    Palace compile against every other dependency's headers. Palace's tests are
    left out; the upstream test gate runs the baseline's.

    The build directory is deleted once the prefix is stamped, because the
    build cache saves the build root and a second superbuild tree would double
    the Linux x86_64 entry against a cache budget four platforms share. A tree
    left by a failed attempt is kept and resumed when its stamp matches.

    Args:
        source_dir: Palace source tree, the baseline's.
        build_dir: Scratch directory for this superbuild.
        install_prefix: Where the copies' tree is installed.
        prefix: The baseline prefix holding MPICH and OpenBLAS.
        jobs: Parallel build jobs.
        libraries: The libraries the prefix has to hold to be up to date.

    Returns:
        True if it built, False if the prefix was already up to date.

    Raises:
        platforms.UnsupportedPlatformError: Off Linux x86_64.
        ValueError: If ``install_prefix`` is the baseline ``prefix``.
        RuntimeError: If the build machine cannot run code built for the level.
    """
    require_platform()
    if install_prefix.resolve() == prefix.resolve():
        # The prefix is emptied before a build, and this one holds the baseline.
        raise ValueError(f"the {LEVEL} copies need a prefix of their own, not {prefix}")
    arguments = tuple(
        superbuild.cmake_arguments(
            source_dir=source_dir,
            install_prefix=install_prefix,
            mpi_home=prefix,
            compile_flags=COMPILE_FLAGS,
        )
    )
    if stamped(install_prefix) == arguments and _holds(install_prefix, libraries):
        print(f"{LEVEL} copies in {install_prefix} are up to date", flush=True)
        return False
    if not host_supports_level():
        raise RuntimeError(
            f"this machine cannot run {LEVEL} code, which configuring the copies "
            f"needs; build on one that can, or turn the copies off"
        )
    shutil.rmtree(install_prefix, ignore_errors=True)
    if stamped(build_dir) != arguments:
        shutil.rmtree(build_dir, ignore_errors=True)
    stamp(build_dir, arguments)
    prefix_module.unify_lib_directories(install_prefix)
    superbuild.run(
        source_dir=source_dir,
        build_dir=build_dir,
        install_prefix=install_prefix,
        prefix=prefix,
        jobs=jobs,
        compile_flags=COMPILE_FLAGS,
        tests=False,
    )
    stamp(install_prefix, arguments)
    shutil.rmtree(build_dir)
    return True


def _holds(install_prefix: Path, libraries: Sequence[str]) -> bool:
    try:
        select(install_prefix / "lib", libraries)
    except FileNotFoundError:
        return False
    return True


def select(directory: Path, libraries: Sequence[str] = LIBRARIES) -> dict[str, Path]:
    """Find each library's one real file in ``directory``.

    Symbolic links are skipped -- an install prefix holds ``libmfem.so`` and
    ``libHYPRE.so.300`` as links to the file -- and so are subdirectories.

    Args:
        directory: A library directory, an install prefix's or a wheel's.
        libraries: Library names, as :func:`wheelbuild.notices.library_stem`
            gives them.

    Returns:
        The file of each library, by name.

    Raises:
        FileNotFoundError: If a library has no file, or more than one.
    """
    files: dict[str, list[Path]] = {name: [] for name in libraries}
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or ".so" not in path.name:
                continue
            name = library_stem(path.name)
            if name in files:
                files[name].append(path)
    for name, found in files.items():
        if len(found) != 1:
            raise FileNotFoundError(
                f"expected one {name} in {directory}, found "
                f"{[path.name for path in found] or 'none'}"
            )
    return {name: found[0] for name, found in files.items()}


def read_interface(path: Path) -> Interface:
    """Read a shared library's SONAME and NEEDED entries with ``patchelf``."""

    def ask(option: str) -> list[str]:
        output = subprocess.run(
            ["patchelf", option, str(path)], check=True, capture_output=True, text=True
        ).stdout
        return [line.strip() for line in output.splitlines() if line.strip()]

    sonames = ask("--print-soname")
    return Interface(
        soname=sonames[0] if sonames else "", needed=tuple(ask("--print-needed"))
    )


def check_same_build(name: str, baseline: Interface, copy: Interface) -> None:
    """Refuse a copy that is not the baseline library rebuilt.

    Both come from the same Palace tree with the same feature flags, so they
    name the same SONAME and need the same libraries, in the same order. A
    difference means another version or another configuration, and a copy
    loaded in the baseline's place would then be a different library.

    Raises:
        InterfaceMismatchError: If either differs.
    """
    if copy != baseline:
        raise InterfaceMismatchError(
            f"the {LEVEL} {name} is not the baseline one rebuilt: SONAME "
            f"{copy.soname!r} against {baseline.soname!r}, NEEDED {list(copy.needed)} "
            f"against {list(baseline.needed)}"
        )


def renames(built: Interface, repaired: Interface) -> dict[str, str]:
    """Return the NEEDED renames ``auditwheel`` applied to one library.

    ``patchelf --replace-needed`` rewrites an entry in place, so the two lists
    correspond by position. The new name is the vendored file's, not the old
    SONAME with a hash in it -- ``libopenblas.so.0`` becomes
    ``libopenblasp-r0-a160b4b8.3.34.so`` -- so a renamed entry is recognised by
    the content hash ``auditwheel`` puts in every name it writes.

    Args:
        built: The library as the superbuild installed it.
        repaired: The same library as the wheel carries it.

    Returns:
        Old name to new, for every entry that changed.

    Raises:
        InterfaceMismatchError: If the lists do not correspond.
    """
    if len(built.needed) != len(repaired.needed) or any(
        old != new and not _VENDORED_NAME.search(new)
        for old, new in zip(built.needed, repaired.needed, strict=True)
    ):
        raise InterfaceMismatchError(
            f"cannot match {list(built.needed)} to the repaired {list(repaired.needed)}"
        )
    return {
        old: new
        for old, new in zip(built.needed, repaired.needed, strict=True)
        if old != new
    }


def patchelf_command(
    library: Path, repaired: Interface, renamed: dict[str, str]
) -> list[str]:
    """Return the ``patchelf`` call that gives a copy its baseline's interface."""
    command = ["patchelf", "--set-soname", repaired.soname]
    for old, new in renamed.items():
        command += ["--replace-needed", old, new]
    return [*command, "--force-rpath", "--set-rpath", RPATH, str(library)]


def graft(
    wheel: Path,
    *,
    install_prefix: Path,
    hwcaps_prefix: Path,
    libraries: Sequence[str] = LIBRARIES,
) -> list[str]:
    """Add the copies to a repaired wheel, in place.

    Each copy is the baseline library's twin: its SONAME and NEEDED entries are
    checked against the baseline build's (:func:`check_same_build`), then
    rewritten the way ``auditwheel`` rewrote the baseline's, and the result is
    checked against the repaired baseline. It is stripped of its static symbol
    table, which no loader reads, to spend less of the per-file limit; the
    dynamic symbols stay.

    Args:
        wheel: The wheel ``auditwheel`` repaired.
        install_prefix: The baseline superbuild's install prefix.
        hwcaps_prefix: The prefix :func:`build` installed the copies into.
        libraries: The libraries to copy.

    Returns:
        The archive names added.

    Raises:
        platforms.UnsupportedPlatformError: Off Linux x86_64.
        InterfaceMismatchError: If a copy is not its baseline library rebuilt.
        FileNotFoundError: If a library is missing from any of the three places.
    """
    require_platform()
    built = select(install_prefix / "lib", libraries)
    copies = select(hwcaps_prefix / "lib", libraries)
    with zipfile.ZipFile(wheel) as archive:
        vendor = vendor_directory(archive.namelist())
        added: dict[str, tuple[Path, zipfile.ZipInfo]] = {}
        with tempfile.TemporaryDirectory() as scratch:
            extracted = Path(scratch)
            for name in libraries:
                member = _member_for(archive.namelist(), vendor, name)
                repaired_path = Path(archive.extract(member, extracted / "repaired"))
                repaired = read_interface(repaired_path)
                if copies[name].name != built[name].name:
                    raise InterfaceMismatchError(
                        f"the {LEVEL} {name} is {copies[name].name}, the baseline "
                        f"{built[name].name}"
                    )
                baseline = read_interface(built[name])
                check_same_build(name, baseline, read_interface(copies[name]))
                target = extracted / "copies" / PurePosixPath(member).name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(copies[name], target)
                check_call(["strip", "--strip-unneeded", str(target)])
                check_call(
                    patchelf_command(target, repaired, renames(baseline, repaired))
                )
                if read_interface(target) != repaired:
                    raise InterfaceMismatchError(
                        f"the {LEVEL} {name} does not match {member} after patchelf"
                    )
                arcname = str(hwcaps_member(member))
                added[arcname] = (target, archive.getinfo(member))
            rewrite(wheel, added)
    for arcname in added:
        print(f"{LEVEL} copy: {arcname}", flush=True)
    return list(added)


def vendor_directory(names: Sequence[str]) -> PurePosixPath:
    """Return the top-level ``*.libs`` directory ``auditwheel`` vendored into.

    Raises:
        FileNotFoundError: If the wheel has none, or more than one.
    """
    found = {
        PurePosixPath(name).parts[0]
        for name in names
        if len(PurePosixPath(name).parts) > 1
        and PurePosixPath(name).parts[0].endswith(".libs")
    }
    if len(found) != 1:
        raise FileNotFoundError(f"expected one vendor directory, found {sorted(found)}")
    return PurePosixPath(found.pop())


def _member_for(names: Sequence[str], vendor: PurePosixPath, library: str) -> str:
    matches = [
        name
        for name in names
        if PurePosixPath(name).parent == vendor
        and library_stem(PurePosixPath(name).name) == library
    ]
    if len(matches) != 1:
        raise FileNotFoundError(f"expected one {library} in {vendor}, found {matches}")
    return matches[0]


def hwcaps_member(member: str) -> PurePosixPath:
    """Return the archive name of a vendored library's copy.

    ``palace_solver.libs/libmfem-7df8df2e.so.4.9.0`` becomes
    ``palace_solver.libs/glibc-hwcaps/x86-64-v3/libmfem-7df8df2e.so.4.9.0``.
    """
    path = PurePosixPath(member)
    return path.parent / SUBDIRECTORY / path.name


def record_line(arcname: str, data: bytes) -> str:
    """Return a wheel ``RECORD`` row for a file."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{arcname},sha256={digest.decode()},{len(data)}"


def rewrite(wheel: Path, added: dict[str, tuple[Path, zipfile.ZipInfo]]) -> None:
    """Rewrite ``wheel`` with files added and its ``RECORD`` regenerated.

    Each added file takes the permissions of the member named beside it. The
    ``.dist-info`` directory stays last and ``RECORD`` last within it, as the
    wheel tools write them.

    Args:
        wheel: The wheel, replaced in place.
        added: Archive name to the file to store and the member whose
            permissions it takes.
    """
    temporary = wheel.with_name(wheel.name + ".partial")
    with zipfile.ZipFile(wheel) as source:
        infos = [info for info in source.infolist() if not info.is_dir()]
        record = next(
            info.filename
            for info in infos
            if info.filename.endswith(".dist-info/RECORD")
        )
        dist_info = PurePosixPath(record).parent
        body = [
            info
            for info in infos
            if PurePosixPath(info.filename).parts[0] != str(dist_info)
        ]
        tail = [
            info
            for info in infos
            if PurePosixPath(info.filename).parts[0] == str(dist_info)
            and info.filename != record
        ]
        rows = []
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
            for info in body:
                data = source.read(info)
                target.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
                rows.append(record_line(info.filename, data))
            for arcname, (path, like) in added.items():
                data = path.read_bytes()
                info = zipfile.ZipInfo(arcname, date_time=like.date_time)
                info.external_attr = like.external_attr
                target.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
                rows.append(record_line(arcname, data))
            for info in tail:
                data = source.read(info)
                target.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
                rows.append(record_line(info.filename, data))
            rows.append(f"{record},,")
            target.writestr(
                record, "\n".join(rows) + "\n", compress_type=zipfile.ZIP_DEFLATED
            )
    temporary.replace(wheel)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: build the copies, or ask whether to."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("default", help="print 1 if this platform ships the copies")
    build_parser = commands.add_parser("build", help="build the copies")
    build_parser.add_argument("--source-dir", type=Path, required=True)
    build_parser.add_argument("--build-dir", type=Path, required=True)
    build_parser.add_argument("--install-prefix", type=Path, required=True)
    build_parser.add_argument("--prefix", type=Path, required=True)
    build_parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    if args.command == "default":
        print(int(enabled_by_default()))
        return 0
    build(
        source_dir=args.source_dir,
        build_dir=args.build_dir,
        install_prefix=args.install_prefix,
        prefix=args.prefix,
        jobs=args.jobs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
