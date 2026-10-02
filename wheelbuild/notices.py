"""Harvest third-party license notices from the superbuild source checkouts.

The wheel redistributes every dependency the superbuild compiles, so the
binary distribution must carry their license texts. MUMPS is the strict case:
CeCILL-C redistribution requires the license text and a pointer to the
corresponding sources, both added here on top of the harvested files.

The harvest fails the build when a known dependency contributes no license
file, so a superbuild layout change cannot silently drop a notice.

One class of redistributed library has no source checkout to walk: what the
toolchain and the build image provide rather than what the superbuild compiles.
The compiler runtime — ``libgfortran``, ``libgomp``, ``libquadmath`` — is the
bulk of it, and ``libpciaccess``, which reaches the payload through hwloc, is
the rest. These enter the wheel *after* this harvest, as a side effect of the
wheel repair step. Their notices are therefore added from the texts shipped in
``wheelbuild/data``, and :func:`audit_wheel` checks the finished wheel so that
a library neither harvested nor named here fails the build rather than shipping
unnoticed.

The compiler runtime is not one license. Most of it is GPL-3.0 with the GCC
Runtime Library Exception, but ``libquadmath`` is the GNU Library General
Public License, version 2 or any later version, and GCC ships it beside the
LGPL 2.1 text. :data:`COMPILER_RUNTIME_LIBRARIES` therefore maps each runtime
to its license rather than listing them, so that neither note can be written
for a library the other covers.

The audit is the same question on both platforms asked of two different file
name conventions, because the repair tools differ in where they put what they
copy and in what they call it. ``auditwheel`` bundles into a
``palace_solver.libs`` directory beside the package and renames each library
with a hash of its contents; ``delocate`` bundles into a ``.dylibs`` directory
*inside* the package and copies each library under the name it already had,
which on Mach-O carries the soversion before the suffix rather than after it.
:func:`library_stem` and :func:`vendored_libraries` own both conventions, since
a name the audit cannot parse is a hard build failure rather than a silent gap.

The macOS toolchain is GCC, chosen by ``scripts/build-macos.sh``, so the OpenMP
runtime the wheel vendors there is GCC's ``libgomp`` and the note above covers
it. A clang toolchain would vendor LLVM's ``libomp`` instead, which is
Apache-2.0 with the LLVM exception and a different note with a different source
pointer. That note is deliberately not written here: an unshipped license in a
THIRD-PARTY-NOTICES is a claim about the payload that is not true. ``libomp``
is absent from :data:`COMPILER_RUNTIME_LIBRARIES` for the same reason, so a
toolchain change that vendors it fails the audit and whoever makes that change
writes the note then.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import Path

#: Dependencies compiled into the wheel. A missing license file for any of
#: these fails the build. ``mpich`` and ``openblas`` are built beside the
#: superbuild rather than by it, and are vendored just the same.
REQUIRED_DEPENDENCIES = (
    "arpack",
    "gslib",
    "hypre",
    "libceed",
    "libxsmm",
    "mfem",
    "mpich",
    "mumps",
    "openblas",
    "petsc",
    "slepc",
    "strumpack",
    "sundials",
    "superlu",
    "zfp",
)

#: Where the CeCILL-C obligation's "corresponding sources" pointer aims.
MUMPS_SOURCE_URL = "https://mumps-solver.org/index.php?page=dwnld"

#: The license of the GCC runtime libraries, as an SPDX expression. The
#: Exception permits this redistribution but does not remove the obligation to
#: reproduce the notice.
GCC_RUNTIME_LICENSE = "GPL-3.0-or-later WITH GCC-exception-3.1"

#: The license of ``libquadmath``, as an SPDX expression. Its sources grant the
#: GNU Library General Public License "version 2 of the License, or (at your
#: option) any later version"; the text GCC ships with it, in
#: ``libquadmath/COPYING.LIB``, is the LGPL 2.1, which is the version the wheel
#: redistributes it under.
LIBQUADMATH_LICENSE = "LGPL-2.0-or-later"

#: Runtime libraries that come from the compiler rather than from a source
#: checkout, under the name :func:`library_stem` reduces them to on either
#: platform, mapped to the license each is noticed under.
#: ``libgfortran``, ``libgomp`` and ``libquadmath`` are vendored because the
#: manylinux_2_28 policy whitelist does not cover them, though GCC builds no
#: ``libquadmath`` for aarch64 Linux and that wheel carries none; ``libstdc++``
#: and ``libgcc_s`` are whitelisted on Linux and so are not vendored there, but
#: the macOS wheel carries them, which the first macOS build confirmed.
#: LLVM's ``libomp`` is deliberately not here — see the module docstring.
COMPILER_RUNTIME_LIBRARIES = {
    "libgcc_s": GCC_RUNTIME_LICENSE,
    "libgfortran": GCC_RUNTIME_LICENSE,
    "libgomp": GCC_RUNTIME_LICENSE,
    "libquadmath": LIBQUADMATH_LICENSE,
    "libstdc++": GCC_RUNTIME_LICENSE,
}

#: Where the GPL's "corresponding sources" pointer aims for the GCC runtime.
GCC_SOURCE_URL = "https://gcc.gnu.org/mirrors.html"

#: Libraries the repair step may vendor out of the build image rather than from
#: the compiler or from anything built here, mapped to the license text shipped
#: for them in ``data``. ``libpciaccess`` arrives through hwloc, which the
#: vendored MPICH links to discover the machine's topology on Linux; the macOS
#: hwloc uses no such library. The notice is written whether or not that build's
#: payload ended up carrying it, because the harvest runs before the repair that
#: decides.
SYSTEM_LIBRARY_LICENSES = {
    "libpciaccess": "libpciaccess-COPYING.txt",
}

#: Where a repair tool leaves what it copied, as a suffix of the directory
#: name. ``auditwheel`` writes ``palace_solver.libs`` beside the package and
#: ``delocate`` writes ``.dylibs`` inside it, so neither ends with the other's
#: suffix and one test covers both. A wheel repaired by neither has no such
#: directory and audits as carrying nothing, so the audit is not what catches a
#: repair that did not run: on Linux
#: :func:`wheelbuild.assemble.verify_platform_tag` does, because an unrepaired
#: wheel is tagged ``linux_<arch>``, and on macOS, where an unrepaired wheel
#: already carries the ``macosx`` tag, it is the smoke test's link check, which
#: reads the payload's install names and is handed a vendor directory that is
#: not there.
_VENDOR_DIRECTORIES = (".libs", ".dylibs")

#: File names that hold a license or copyright notice.
LICENSE_FILE_PATTERNS = (
    "LICENSE*",
    "License*",
    "license*",
    "COPYING*",
    "COPYRIGHT*",
    "NOTICE*",
)

_DATA = Path(__file__).resolve().parent / "data"
_CECILL_C_TEXT = _DATA / "CeCILL-C-V1-en.txt"
_GPL_3_TEXT = _DATA / "GPL-3.0.txt"
_GCC_EXCEPTION_TEXT = _DATA / "GCC-Runtime-Library-Exception-3.1.txt"
_LGPL_2_1_TEXT = _DATA / "LGPL-2.1.txt"

_HEADER = """\
THIRD-PARTY NOTICES for palace-solver
=======================================

This wheel redistributes the Palace solver (Apache-2.0) together with every
library it links: the dependencies built by Palace's superbuild, the MPICH and
OpenBLAS builds the wheel vendors, and the runtime libraries the wheel repair
step copies in from the build image. The license of each redistributed
component is reproduced below.
"""


def _mumps_note(checkouts: list[str]) -> str:
    """Render the CeCILL-C obligation note, naming the sources redistributed."""
    identification = ", ".join(checkouts) if checkouts else "see the section above"
    return (
        "MUMPS is distributed under the CeCILL-C license. Its complete license "
        "text is reproduced below. The binary in this wheel was built from the "
        f"MUMPS source checkout(s) {identification}, whose corresponding sources "
        f"are available from {MUMPS_SOURCE_URL}.\n"
    )


def _gcc_release(gcc_version: str | None) -> str:
    return f"GCC {gcc_version}" if gcc_version else "the GCC release used to build it"


def _gcc_runtime_note(gcc_version: str | None) -> str:
    """Render the GCC runtime note, naming the libraries and their sources."""
    named = ", ".join(
        library
        for library, terms in COMPILER_RUNTIME_LIBRARIES.items()
        if terms == GCC_RUNTIME_LICENSE
    )
    release = _gcc_release(gcc_version)
    return (
        "The wheel repair step copies the GCC runtime libraries the payload "
        f"links ({named}, whichever of them the platform does not provide) out "
        "of the toolchain and into the wheel. They are licensed under the GNU "
        "General Public License version 3 with the GCC Runtime Library "
        "Exception version 3.1, both reproduced below; the Exception is what "
        "permits this redistribution without extending the GPL to the rest of "
        f"the wheel. The binaries were produced by {release}, whose "
        f"corresponding sources are available from {GCC_SOURCE_URL}.\n"
    )


def _libquadmath_note(gcc_version: str | None) -> str:
    """Render the libquadmath note: its license, its sources, and relinking."""
    return (
        "libquadmath is the one GCC runtime library not under the GPL. Its "
        "sources grant the GNU Library General Public License version 2 or, at "
        "your option, any later version; GCC distributes it with the GNU Lesser "
        "General Public License version 2.1, reproduced below, and it is "
        "redistributed here under that version. The wheel repair step copies it "
        "into the wheel where the payload links it. It is shipped as its own "
        "shared library, which the solver loads at run time rather than "
        "containing, so a compatible build of it can be substituted. The binary "
        f"was produced by {_gcc_release(gcc_version)}, whose corresponding "
        f"sources are available from {GCC_SOURCE_URL}.\n"
    )


def detect_gcc_version(compiler: str | None = None) -> str | None:
    """Return the GCC release that built the payload, or ``None``.

    The GPL's source pointer has to name a release, so the notices record the
    compiler that actually built the payload rather than a version written down
    by hand and left to drift.

    Which compiler that is comes from the build's own environment rather than
    from the name ``gcc``. On a macOS runner ``gcc`` on ``PATH`` is Apple
    clang, and the runtime the wheel vendors is the Homebrew GCC that
    ``scripts/build-macos.sh`` selected and exported as ``CC``; naming Apple
    clang's version beside a pointer to the GCC sources would be a notice that
    describes nothing in the wheel. The Linux driver exports neither variable,
    so it falls through to ``gcc`` and is unchanged.

    The answer is refused rather than reported when it does not come from GCC.
    ``-dumpfullversion`` is a GCC spelling that some clang releases accept and
    answer with their own version, which is the failure mode that looks like a
    success.

    Args:
        compiler: Compiler to ask; defaults to ``CC``, then ``FC``, then the
            ``gcc`` on ``PATH``. The macOS driver refuses to set one of ``CC``,
            ``CXX`` and ``FC`` without the others, so they name one toolchain.

    Returns:
        The release, such as ``15.3.0``, or ``None`` when no GCC answered.
    """
    executable = compiler or _resolve_compiler()
    version = _ask(executable, "-dumpfullversion")
    if version is None or not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    identification = _ask(executable, "--version")
    # Every GCC front end prints the FSF copyright line; Apple clang prints no
    # such line, and neither does LLVM's. Deliberately not a test for "GCC" in
    # the version banner, which several distributions replace with their own
    # package name.
    if identification is None or "Free Software Foundation" not in identification:
        return None
    return version


def _resolve_compiler() -> str:
    """Return the compiler the build exported, or the ``gcc`` on PATH."""
    return os.environ.get("CC") or os.environ.get("FC") or "gcc"


def _ask(executable: str, flag: str) -> str | None:
    """Run ``executable flag`` and return its output, or ``None`` if it failed."""
    try:
        completed = subprocess.run(
            [executable, flag],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


class MissingLicenseError(RuntimeError):
    """Raised when a dependency of the superbuild contributes no license file."""


class UnattributedLibraryError(RuntimeError):
    """Raised when a wheel vendors a library no notice in it accounts for."""


def collect(source_roots: Sequence[Path]) -> dict[str, list[Path]]:
    """Collect every license file below the given trees, by checkout.

    The superbuild keeps a source checkout and a build directory per
    dependency, and the build directory often copies the license file, so
    identical texts are reported once. Every checkout is harvested, not only
    the ones in :data:`REQUIRED_DEPENDENCIES`, because the superbuild also
    pulls in prerequisites (ScaLAPACK, METIS, ...) whose notices must ship too.

    Args:
        source_roots: Trees to walk — the superbuild directory plus the source
            trees of the runtimes built beside it (MPICH, OpenBLAS).

    Returns:
        Mapping of checkout name to its license files, in walk order.
    """
    collected: dict[str, list[Path]] = {}
    seen_texts: set[str] = set()
    for root in source_roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or not _is_license_file(path):
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in seen_texts:
                continue
            seen_texts.add(digest)
            relative = path.parent.relative_to(root)
            checkout = str(root.name / relative) if str(relative) != "." else root.name
            collected.setdefault(checkout, []).append(path)
    return collected


def _is_license_file(path: Path) -> bool:
    return bool(path.stat().st_size) and any(
        fnmatch(path.name, pattern) for pattern in LICENSE_FILE_PATTERNS
    )


def _missing_dependencies(collected: dict[str, list[Path]]) -> list[str]:
    harvested = " ".join(collected).lower()
    return [name for name in REQUIRED_DEPENDENCIES if name not in harvested]


def _mumps_checkouts(collected: dict[str, list[Path]]) -> list[str]:
    return sorted(name for name in collected if "mumps" in name.lower())


def render(source_roots: Sequence[Path], *, gcc_version: str | None = None) -> str:
    """Render the THIRD-PARTY-NOTICES body.

    Args:
        source_roots: Trees to harvest.
        gcc_version: Version of the compiler whose runtime the repair step will
            vendor, named in the GPL source pointer.

    Returns:
        The complete notices text.

    Raises:
        MissingLicenseError: If a required dependency has no license file.
    """
    collected = collect(source_roots)
    missing = _missing_dependencies(collected)
    if missing:
        searched = ", ".join(str(root) for root in source_roots)
        raise MissingLicenseError(
            f"no license file found under {searched} for: {', '.join(missing)}"
        )
    sections = [_HEADER]
    for checkout, files in collected.items():
        for path in files:
            sections.append(
                _section(
                    f"{checkout} — {path.name}",
                    path.read_text(encoding="utf-8", errors="replace"),
                )
            )
    sections.append(
        _section(
            "MUMPS redistribution (CeCILL-C)",
            _mumps_note(_mumps_checkouts(collected)),
        )
    )
    sections.append(
        _section("CeCILL-C license text", _CECILL_C_TEXT.read_text(encoding="utf-8"))
    )
    sections.append(
        _section(
            "GCC runtime libraries (GPL-3.0 with the Runtime Library Exception)",
            _gcc_runtime_note(gcc_version),
        )
    )
    sections.append(
        _section(
            "GCC Runtime Library Exception 3.1",
            _GCC_EXCEPTION_TEXT.read_text(encoding="utf-8"),
        )
    )
    sections.append(
        _section(
            "GNU General Public License version 3",
            _GPL_3_TEXT.read_text(encoding="utf-8"),
        )
    )
    sections.append(
        _section(
            # Conditional for the same reason as the system libraries below:
            # GCC builds no libquadmath for aarch64 Linux, so that payload
            # never carries it.
            "libquadmath (LGPL, vendored where the payload links it)",
            _libquadmath_note(gcc_version),
        )
    )
    sections.append(
        _section(
            "GNU Lesser General Public License version 2.1",
            _LGPL_2_1_TEXT.read_text(encoding="utf-8"),
        )
    )
    for library, filename in SYSTEM_LIBRARY_LICENSES.items():
        sections.append(
            _section(
                # Conditional, because this file is written before the repair
                # step that decides. libpciaccess reaches the Linux payload
                # through hwloc and the macOS one through nothing, so a flat
                # claim that it is redistributed would be false on one of the
                # two platforms this text ships on.
                f"{library} (vendored from the build image where the payload links it)",
                (_DATA / filename).read_text(encoding="utf-8"),
            )
        )
    return "\n".join(sections)


def _section(title: str, body: str) -> str:
    rule = "-" * len(title)
    return f"\n{rule}\n{title}\n{rule}\n\n{body.rstrip()}\n"


def library_stem(name: str) -> str:
    """Return the library name behind a vendored file name.

    ``auditwheel`` renames what it copies, inserting a hash of the contents
    after the part of the name before its first dot. That is the end of the
    name for ``libgfortran.so.5.0.0``, which becomes
    ``libgfortran-83c28eba.so.5.0.0``, but not for a library whose version is
    in the name: ``libopenblasp-r0.3.34.so`` becomes
    ``libopenblasp-r0-a160b4b8.3.34.so``, with the hash in the middle. The
    name with the hash taken out is what a notice can be matched against.

    ``delocate`` renames nothing — it refuses a payload where two libraries
    share a basename rather than disambiguating them — so on Mach-O the whole
    name is the library's own. The soversion is the difference that matters:
    ELF appends it after ``.so``, where dropping the suffix drops it too, while
    Mach-O puts it *before* ``.dylib``, so ``libgfortran.5.dylib`` would
    otherwise reduce to ``libgfortran.5`` and match no notice. Every trailing
    numeric component is therefore dropped there. The cost is that a Mach-O
    library carrying its version in its name cannot be told from one carrying a
    soversion — ``libopenblasp-r0.3.34.dylib`` reduces to ``libopenblasp-r0``
    where the ELF spelling keeps the version — which is harmless because both
    sides of every comparison in :func:`audit_wheel` come through here.

    Which convention applies is decided by the file name rather than by the
    running platform: the name is the evidence, and the same audit reads names
    out of a wheel and out of an install prefix.

    Args:
        name: File name as it appears in the wheel's vendored library
            directory.

    Returns:
        The library name, without the hash, the extension or the soversion.
    """
    unhashed = re.sub(r"-[0-9a-f]{6,}(?=\.|$)", "", name)
    if ".dylib" in unhashed:
        return re.sub(r"(\.\d+)+$", "", unhashed.split(".dylib", maxsplit=1)[0])
    return unhashed.split(".so", maxsplit=1)[0]


def vendored_libraries(wheel: Path) -> list[str]:
    """Return the library names a built wheel carries, deduplicated and sorted.

    Everything in a vendor directory counts. Only the repair tool writes there
    and it writes nothing but the libraries it copied, so a filter on ``.so``
    or ``.dylib`` would add no precision and would subtract a guarantee: a file
    whose name the filter did not expect would be dropped silently, and this
    function is the audit's only view of what the repair added. A name
    :func:`library_stem` cannot reduce to a known library therefore fails the
    build, which is the direction to fail in.

    Args:
        wheel: The repaired wheel.

    Returns:
        One entry per vendored library, as :func:`library_stem` names it.
    """
    with zipfile.ZipFile(wheel) as archive:
        # Directory entries are optional in a zip and carry a trailing slash,
        # which Path() drops, so they are excluded by name rather than by path.
        members = [Path(name) for name in archive.namelist() if not name.endswith("/")]
    return sorted(
        {
            library_stem(member.name)
            for member in members
            if member.parent.name.endswith(_VENDOR_DIRECTORIES)
        }
    )


def audit_wheel(*, wheel: Path, install_prefix: Path) -> list[str]:
    """Check that every library the repair step vendored has a notice.

    A vendored library is accounted for when it was built here — it exists in
    the superbuild's install prefix, so the harvest walked its sources — or
    when it is one of the :data:`COMPILER_RUNTIME_LIBRARIES` or
    :data:`SYSTEM_LIBRARY_LICENSES` the notices cover from shipped texts.
    Anything else entered the wheel without a license section and fails the
    build, which is the guard the source-tree walk cannot provide for libraries
    that have no source tree.

    Args:
        wheel: The repaired wheel.
        install_prefix: Superbuild install prefix, holding everything built
            here.

    Returns:
        The vendored library names, in the order reported.

    Raises:
        UnattributedLibraryError: If a vendored library is neither.
    """
    built_here = {
        library_stem(path.name)
        for path in (install_prefix / "lib").glob("*")
        if ".so" in path.name or path.name.endswith(".dylib")
    }
    found = vendored_libraries(wheel)
    covered = built_here.union(COMPILER_RUNTIME_LIBRARIES, SYSTEM_LIBRARY_LICENSES)
    unattributed = [name for name in found if name not in covered]
    if unattributed:
        raise UnattributedLibraryError(
            f"{wheel.name} vendors libraries with no license notice: "
            f"{', '.join(unattributed)}. Either they are built by the "
            "superbuild and the harvest missed them, or they come from the "
            "build image and belong in COMPILER_RUNTIME_LIBRARIES or "
            "SYSTEM_LIBRARY_LICENSES with their license text in "
            "wheelbuild/data."
        )
    return found


def harvest(
    *, source_roots: Sequence[Path], output: Path, gcc_version: str | None = None
) -> Path:
    """Write the harvested notices to ``output``.

    Args:
        source_roots: Trees to harvest.
        output: Destination file.
        gcc_version: Version of the compiler whose runtime the wheel will
            vendor, named in the GPL source pointer.

    Returns:
        The path written.
    """
    text = render(source_roots, gcc_version=gcc_version)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the notice harvester."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        action="append",
        dest="source_roots",
        help="tree to harvest; repeat for the runtimes built beside the superbuild",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--gcc-version",
        default=None,
        help="compiler release named in the GCC runtime source pointer "
        "(default: what the build's own CC, FC or the gcc on PATH reports)",
    )
    args = parser.parse_args(argv)
    gcc_version = args.gcc_version
    if not gcc_version:
        compiler = _resolve_compiler()
        gcc_version = detect_gcc_version(compiler)
        if gcc_version is None:
            # Not fatal: the GPL's obligation is the license text and a source
            # pointer, and both are written either way. But an unnamed release
            # in a macOS build is the toolchain going unrecognised rather than
            # the compiler being unusual, so it is said out loud.
            print(
                f"warning: {compiler} reported no GCC release, so the notices "
                "name none; pass --gcc-version to say which built the payload",
                file=sys.stderr,
            )
    path = harvest(
        source_roots=args.source_roots,
        output=args.output,
        gcc_version=gcc_version,
    )
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
