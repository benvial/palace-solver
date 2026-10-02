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

Windows is a third layout and a different MPI. Its repair copies the solver's
DLL closure flat into the directory beside the executables, under the names the
toolchain gave them, so a vendored library there is recognised by its ``.dll``
suffix rather than by its directory. The MPI is Microsoft's MS-MPI, shipped as
Microsoft built it and under Microsoft's license terms rather than this
package's, so its notice is those terms and Microsoft's third-party notices,
flagged as applying to the MS-MPI files alone; MPICH is not built there and is
not required of the harvest. The mingw-w64 runtime is linked statically into
every Windows binary and owes its notice although no file of it is vendored.
Because that repair is this project's own copy rather than a tool run after the
harvest, what it copies is known before the harvest runs, so on Windows the
notices are rendered from the payload and name only what it carries, and the
audit checks the shipped notices against the wheel.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable, Collection, Sequence
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

#: The dependencies required of the Windows harvest. MPICH has no Windows port,
#: so the Windows wheel vendors MS-MPI instead, which is not built here and is
#: noticed from Microsoft's own texts in ``data``.
WINDOWS_REQUIRED_DEPENDENCIES = tuple(
    name for name in REQUIRED_DEPENDENCIES if name != "mpich"
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

#: Libraries only the Windows wheel vendors beyond the GCC runtime, under the
#: name :func:`library_stem` reduces them to, mapped to the license each is
#: noticed under. ``msmpi`` is Microsoft's MPI runtime, under Microsoft's
#: Redistributable license terms; ``libmsmpifec`` is the gfortran bridge to it,
#: which MSYS2's ``mingw-w64-msmpi`` builds and distributes under the MIT
#: license; ``libwinpthread`` is mingw-w64's POSIX threads library, which the
#: GCC runtime links; ``zlib1`` is the toolchain's zlib, which Palace's
#: ``find_package(ZLIB)`` finds there. They count as covered only in a Windows
#: wheel, whose notices are the only ones that carry their texts.
WINDOWS_RUNTIME_LIBRARIES = {
    "libmsmpifec": "MIT",
    "libwinpthread": "MIT AND BSD-3-Clause",
    "msmpi": "LicenseRef-Microsoft-MPI-Redistributable",
    "zlib1": "Zlib",
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
#:
#: The Windows repair has no such directory: it copies the DLLs beside the
#: executables that import them, since Windows has no RPATH to point them
#: elsewhere. There the suffix is what marks a vendored library, and a Windows
#: wheel that carries no DLL fails the audit rather than passing on nothing.
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

# The Windows texts. Microsoft's two come from the x64 MSI inside msmpisetup.exe
# 10.1.12498.52, the MS-MPI 10.1.3 redistributable, SHA-256
# 47443829114d8d8670f77af98939fe876d33eceb35d0ce4e0e85efeec4d87213.
# MPI_Redistributables_TPN.txt is that file byte for byte, CRLF and all; its
# SHA-256 is pinned by a test. The license terms ship there only as
# MicrosoftMPI_Redistributable_EULA.rtf (224,476 bytes), SHA-256
# 125d29a463c724ddb5eed6a14370f5ce74dfd063cfca3eddad2d50082eb62106 -- both
# values are the ones wheelbuild.msmpi.MICROSOFT_TEXTS checks in the MSI -- so the text
# here is LibreOffice's plain-text export of it, with the list labels Word wrote
# into the RTF's own \listtext fallback rather than LibreOffice's renumbering,
# and the paragraphs wrapped at 79 columns; the words are the export's,
# unchanged. Microsoft-MPI-LICENSE.txt is LICENSE.txt of
# github.com/microsoft/Microsoft-MPI at f2d849f. The mingw-w64 texts are
# COPYING.MinGW-w64-runtime/COPYING.MinGW-w64-runtime.txt and
# mingw-w64-libraries/winpthreads/COPYING at mingw-w64 4564ee4b5, the commit
# MSYS2's crt and winpthreads packages build and install those files from, and
# zlib-LICENSE.txt is LICENSE of zlib 1.3.2, the release MSYS2's zlib packages.
_MSMPI_EULA_TEXT = _DATA / "MicrosoftMPI_Redistributable_EULA.txt"
_MSMPI_TPN_TEXT = _DATA / "MPI_Redistributables_TPN.txt"
_MSMPI_MIT_TEXT = _DATA / "Microsoft-MPI-LICENSE.txt"
_MINGW_RUNTIME_TEXT = _DATA / "COPYING.MinGW-w64-runtime.txt"
_WINPTHREADS_TEXT = _DATA / "winpthreads-COPYING.txt"
_ZLIB_TEXT = _DATA / "zlib-LICENSE.txt"

_HEADER = """\
THIRD-PARTY NOTICES for palace-solver
=======================================

This wheel redistributes the Palace solver (Apache-2.0) together with every
library it links: the dependencies built by Palace's superbuild, the MPICH and
OpenBLAS builds the wheel vendors, and the runtime libraries the wheel repair
step copies in from the build image. The license of each redistributed
component is reproduced below.
"""

_WINDOWS_HEADER = """\
THIRD-PARTY NOTICES for palace-solver
=======================================

This wheel redistributes the Palace solver (Apache-2.0) together with every
library it links: the dependencies built by Palace's superbuild, the OpenBLAS
build the wheel vendors, Microsoft's MS-MPI runtime, and the runtime libraries
the wheel repair step copies in from the build toolchain. The license of each
redistributed component is reproduced below. The MS-MPI files are Microsoft's
and are under Microsoft's license terms, not this package's; their section
names them.
"""

_MSMPI_NOTE = """\
The wheel vendors Microsoft MPI (MS-MPI): msmpi.dll, mpiexec.exe and smpd.exe,
byte for byte as Microsoft's x64 redistributable installer ships them. These
three files are licensed by Microsoft under the Microsoft MPI Redistributable
license terms reproduced below, not under this package's license or any other
license in this file, and anyone who uses or redistributes them as part of
this wheel does so under those terms. Microsoft's third-party notices for
MS-MPI follow the license terms. Both texts come from the MS-MPI 10.1.3
redistributable installer (msmpisetup.exe 10.1.12498.52): the notices are its
MPI_Redistributables_TPN.txt unchanged, and the license terms are its
MicrosoftMPI_Redistributable_EULA.rtf rendered as plain text.
"""

_LIBMSMPIFEC_NOTE = """\
libmsmpifec.dll is the gfortran bridge to MS-MPI: a helper library that MSYS2's
mingw-w64-msmpi package builds from its own sources and Microsoft's MS-MPI SDK
headers, and distributes under the MIT license. It is not one of Microsoft's
MS-MPI binaries and is not under the license terms above. The MIT license of
the MS-MPI sources its headers come from is reproduced below; the MPICH notice
those headers carry is in Microsoft's third-party notices above.
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


def _gcc_runtime_note(gcc_version: str | None, libraries: Sequence[str]) -> str:
    """Render the GCC runtime note, naming the libraries and their sources."""
    named = ", ".join(libraries)
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


class NoVendoredLibrariesError(RuntimeError):
    """Raised when a Windows payload names no DLL, so an audit would pass vacuously."""


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


def _missing_dependencies(
    collected: dict[str, list[Path]], required: Sequence[str]
) -> list[str]:
    harvested = " ".join(collected).lower()
    return [name for name in required if name not in harvested]


def _mumps_checkouts(collected: dict[str, list[Path]]) -> list[str]:
    return sorted(name for name in collected if "mumps" in name.lower())


def render(
    source_roots: Sequence[Path],
    *,
    gcc_version: str | None = None,
    system: str | None = None,
    vendored: Collection[str] | None = None,
) -> str:
    """Render the THIRD-PARTY-NOTICES body.

    Args:
        source_roots: Trees to harvest.
        gcc_version: Version of the compiler whose runtime the repair step will
            vendor, named in the GPL source pointer.
        system: ``platform.system()`` value of the platform the wheel is for;
            defaults to this one. ``Windows`` adds the MS-MPI and mingw-w64
            notices and does not require MPICH of the harvest.
        vendored: The libraries the repair step copies into the wheel, as
            :func:`library_stem` names them. When given, a library noticed from
            a text in ``data`` gets its section only if it is here; when not,
            every such section is written, worded as conditional on the
            payload. Required on Windows, where the repair is this project's
            own copy and its payload is known before the harvest.

    Returns:
        The complete notices text.

    Raises:
        MissingLicenseError: If a required dependency has no license file.
        NoVendoredLibrariesError: If a Windows payload names no library.
        ValueError: If a Windows rendering is not given its payload.
    """
    windows = (system or platform.system()) == "Windows"
    if windows:
        if vendored is None:
            raise ValueError(
                "the Windows notices name only what the payload carries, so "
                "they need the libraries the repair copies"
            )
        _require_libraries(vendored, payload="the payload given to the harvest")
    collected = collect(source_roots)
    required = WINDOWS_REQUIRED_DEPENDENCIES if windows else REQUIRED_DEPENDENCIES
    missing = _missing_dependencies(collected, required)
    if missing:
        searched = ", ".join(str(root) for root in source_roots)
        raise MissingLicenseError(
            f"no license file found under {searched} for: {', '.join(missing)}"
        )

    def ships(library: str) -> bool:
        return vendored is None or library in vendored

    sections = [_WINDOWS_HEADER if windows else _HEADER]
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
    sections.extend(_runtime_sections(gcc_version, ships))
    if windows:
        sections.extend(_windows_sections(ships))
    return "\n".join(sections)


def _runtime_sections(
    gcc_version: str | None, ships: Callable[[str], bool]
) -> list[str]:
    """Render the notices of what the toolchain and the build image provide."""
    sections = []
    gcc_runtime = [
        library
        for library, terms in COMPILER_RUNTIME_LIBRARIES.items()
        if terms == GCC_RUNTIME_LICENSE and ships(library)
    ]
    if gcc_runtime:
        sections.append(
            _section(
                "GCC runtime libraries (GPL-3.0 with the Runtime Library Exception)",
                _gcc_runtime_note(gcc_version, gcc_runtime),
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
    if ships("libquadmath"):
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
        if not ships(library):
            continue
        sections.append(
            _section(
                # Conditional, because on Linux and macOS this file is written
                # before the repair step that decides. libpciaccess reaches the
                # Linux payload through hwloc and the macOS one through nothing,
                # so a flat claim that it is redistributed would be false on one
                # of the two platforms this text ships on.
                f"{library} (vendored from the build image where the payload links it)",
                (_DATA / filename).read_text(encoding="utf-8"),
            )
        )
    return sections


def _windows_sections(ships: Callable[[str], bool]) -> list[str]:
    """Render the notices only a Windows wheel carries.

    MS-MPI's are written whatever the DLL closure holds, because ``mpiexec.exe``
    and ``smpd.exe`` are vendored beside it rather than through it, and the
    mingw-w64 runtime's because it is in every binary rather than in a file of
    its own. The rest follow the payload.
    """
    sections = [
        _section(
            "Microsoft MPI: msmpi.dll, mpiexec.exe, smpd.exe (Microsoft's terms)",
            _MSMPI_NOTE,
        ),
        _section(
            "Microsoft MPI Redistributable license terms",
            _MSMPI_EULA_TEXT.read_text(encoding="utf-8"),
        ),
        _section(
            "Microsoft MPI third-party notices",
            _MSMPI_TPN_TEXT.read_text(encoding="utf-8"),
        ),
    ]
    if ships("libmsmpifec"):
        sections.append(
            _section(
                "libmsmpifec (MIT, the gfortran bridge to MS-MPI)",
                f"{_LIBMSMPIFEC_NOTE}\n{_MSMPI_MIT_TEXT.read_text(encoding='utf-8')}",
            )
        )
    if ships("libwinpthread"):
        sections.append(
            _section(
                "libwinpthread (MIT and BSD-3-Clause, from the mingw-w64 toolchain)",
                _WINPTHREADS_TEXT.read_text(encoding="utf-8"),
            )
        )
    if ships("zlib1"):
        sections.append(
            _section(
                "zlib1 (zlib, vendored from the build toolchain)",
                _ZLIB_TEXT.read_text(encoding="utf-8"),
            )
        )
    sections.append(
        _section(
            "mingw-w64 runtime (linked statically into every Windows binary)",
            _MINGW_RUNTIME_TEXT.read_text(encoding="utf-8"),
        )
    )
    return sections


def _require_libraries(libraries: Collection[str], *, payload: str) -> None:
    """Refuse a Windows payload that names no library.

    The macOS audit once passed with nothing in it, because it read a layout it
    did not recognise as empty. A Windows payload always carries DLLs — MS-MPI's
    at the least — so one that carries none is a layout this module did not
    recognise, not a wheel with nothing to notice.
    """
    if not libraries:
        raise NoVendoredLibrariesError(
            f"{payload} carries no DLL, so there is nothing to audit; a Windows "
            "payload always vendors MS-MPI, so its DLLs are somewhere this "
            "module does not look"
        )


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

    A PE name is the toolchain's own, since the Windows repair copies without
    renaming. MinGW's libtool puts the DLL version after a hyphen, so
    ``libgfortran-5.dll`` reduces to ``libgfortran``; GCC's ``libgcc_s`` also
    carries its exception model, ``libgcc_s_seh-1.dll``, which is dropped so the
    one notice covers it on every platform. A name with no hyphenated version
    keeps what it has: ``zlib1.dll``, whose ``1`` is zlib's own spelling of its
    ABI, reduces to ``zlib1``. Windows file names are case-insensitive, so PE
    names are lowercased.

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
    if _is_dll(unhashed):
        stem = re.sub(r"-\d+$", "", unhashed[: -len(".dll")].lower())
        return re.sub(r"^libgcc_s_(seh|sjlj|dw2)$", "libgcc_s", stem)
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

    The Windows layout has no vendor directory to trust that way: its DLLs lie
    beside the executables, in a directory the repair shares with the package.
    There every ``.dll`` counts, wherever in the wheel it lies, so a DLL put
    somewhere unexpected is still audited rather than missed. No Linux or macOS
    wheel carries one.

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
            if member.parent.name.endswith(_VENDOR_DIRECTORIES) or _is_dll(member.name)
        }
    )


def payload_libraries(directory: Path) -> list[str]:
    """Return the libraries among the DLLs in a Windows payload directory.

    This is what the Windows harvest is told the wheel will carry: the directory
    the repair copied the DLL closure into, read the way :func:`vendored_libraries`
    reads the finished wheel, so the two name the same libraries.

    Args:
        directory: Where the repair copied the DLLs, beside the executables.

    Returns:
        One entry per DLL, as :func:`library_stem` names it, sorted.
    """
    return sorted(
        {
            library_stem(path.name)
            for path in directory.iterdir()
            if path.is_file() and _is_dll(path.name)
        }
    )


def _is_dll(name: str) -> bool:
    return name.lower().endswith(".dll")


def _is_windows_wheel(wheel: Path) -> bool:
    """Whether the filename's platform tag is a Windows one, as ``win_amd64``."""
    tags = wheel.name.removesuffix(".whl").rsplit("-", 1)[-1].split(".")
    return any(tag.startswith("win") for tag in tags)


def _shipped_notices(wheel: Path) -> str | None:
    """Return the THIRD-PARTY-NOTICES a wheel carries, or ``None``."""
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if Path(name).name == "THIRD-PARTY-NOTICES":
                return archive.read(name).decode("utf-8", errors="replace")
    return None


def audit_wheel(*, wheel: Path, install_prefix: Path) -> list[str]:
    """Check that every library the repair step vendored has a notice.

    A vendored library is accounted for when it was built here — it exists in
    the superbuild's install prefix, so the harvest walked its sources — or
    when it is one of the :data:`COMPILER_RUNTIME_LIBRARIES` or
    :data:`SYSTEM_LIBRARY_LICENSES` the notices cover from shipped texts.
    Anything else entered the wheel without a license section and fails the
    build, which is the guard the source-tree walk cannot provide for libraries
    that have no source tree.

    A wheel whose platform tag is a Windows one is held to three things more.
    The :data:`WINDOWS_RUNTIME_LIBRARIES` count as covered there and nowhere
    else. It must carry a DLL, since a payload that names none is a layout this
    module did not recognise rather than one with nothing to notice. And because
    its notices were rendered from the payload rather than for every payload,
    the THIRD-PARTY-NOTICES it ships must name each library it vendors that was
    not built here, which catches notices rendered from a different payload
    than the one packaged. MinGW installs DLLs into ``bin`` rather than ``lib``,
    so both are read for what was built here.

    Args:
        wheel: The repaired wheel.
        install_prefix: Superbuild install prefix, holding everything built
            here.

    Returns:
        The vendored library names, in the order reported.

    Raises:
        UnattributedLibraryError: If a vendored library is neither, or on
            Windows is not named by the notices the wheel ships.
        NoVendoredLibrariesError: If a Windows wheel vendors no DLL.
    """
    built_here = {
        library_stem(path.name)
        for path in (install_prefix / "lib").glob("*")
        if ".so" in path.name or path.name.endswith(".dylib") or _is_dll(path.name)
    }
    built_here.update(
        library_stem(path.name)
        for path in (install_prefix / "bin").glob("*")
        if _is_dll(path.name)
    )
    found = vendored_libraries(wheel)
    windows = _is_windows_wheel(wheel)
    if windows:
        _require_libraries(found, payload=wheel.name)
    covered = built_here.union(COMPILER_RUNTIME_LIBRARIES, SYSTEM_LIBRARY_LICENSES)
    if windows:
        covered.update(WINDOWS_RUNTIME_LIBRARIES)
    unattributed = [name for name in found if name not in covered]
    if unattributed:
        raise UnattributedLibraryError(
            f"{wheel.name} vendors libraries with no license notice: "
            f"{', '.join(unattributed)}. Either they are built by the "
            "superbuild and the harvest missed them, or they come from the "
            "build image and belong in COMPILER_RUNTIME_LIBRARIES, "
            "SYSTEM_LIBRARY_LICENSES or WINDOWS_RUNTIME_LIBRARIES with their "
            "license text in wheelbuild/data."
        )
    if windows:
        # Named, not parsed: every section a shipped text renders on Windows
        # carries its library's name, and only that section does.
        shipped = _shipped_notices(wheel) or ""
        unnamed = [
            name for name in found if name not in built_here and name not in shipped
        ]
        if unnamed:
            raise UnattributedLibraryError(
                f"{wheel.name} vendors libraries its THIRD-PARTY-NOTICES does not "
                f"name: {', '.join(unnamed)}. The Windows notices are rendered "
                "from the payload, so they were rendered from a different one "
                "than the wheel carries, or the wheel carries none."
            )
    return found


def harvest(
    *,
    source_roots: Sequence[Path],
    output: Path,
    gcc_version: str | None = None,
    system: str | None = None,
    vendored: Collection[str] | None = None,
) -> Path:
    """Write the harvested notices to ``output``.

    Args:
        source_roots: Trees to harvest.
        output: Destination file.
        gcc_version: Version of the compiler whose runtime the wheel will
            vendor, named in the GPL source pointer.
        system: Platform the wheel is for, as :func:`render` takes it.
        vendored: Libraries the wheel will carry, as :func:`render` takes them.

    Returns:
        The path written.
    """
    text = render(
        source_roots, gcc_version=gcc_version, system=system, vendored=vendored
    )
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
    parser.add_argument(
        "--system",
        default=None,
        help="platform.system() value of the platform the wheel is for "
        "(default: this one)",
    )
    parser.add_argument(
        "--payload-dir",
        type=Path,
        default=None,
        help="directory the Windows repair copied the DLL closure into; "
        "required on Windows, whose notices name only what it carries",
    )
    args = parser.parse_args(argv)
    system = args.system or platform.system()
    if system == "Windows" and args.payload_dir is None:
        parser.error("--payload-dir is required for a Windows wheel")
    vendored = (
        payload_libraries(args.payload_dir) if args.payload_dir is not None else None
    )
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
        system=system,
        vendored=vendored,
    )
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
