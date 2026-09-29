"""Harvest third-party license notices from the superbuild source checkouts.

The wheel redistributes every dependency the superbuild compiles, so the
binary distribution must carry their license texts. MUMPS is the strict case:
CeCILL-C redistribution requires the license text and a pointer to the
corresponding sources, both added here on top of the harvested files.

The harvest fails the build when a known dependency contributes no license
file, so a superbuild layout change cannot silently drop a notice.

One class of redistributed library has no source checkout to walk. The
compiler runtime — ``libgfortran``, ``libgomp``, ``libquadmath`` — arrives from
the toolchain image and enters the wheel *after* this harvest, as a side effect
of ``auditwheel repair``. Its notices are therefore added from the texts
shipped in ``wheelbuild/data``, and :func:`audit_wheel` checks the finished
wheel so that a library neither harvested nor named here fails the build rather
than shipping unnoticed.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
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

#: Runtime libraries that come from the compiler rather than from a source
#: checkout, keyed by the name they carry before ``auditwheel`` adds its hash.
#: ``libgfortran``, ``libgomp`` and ``libquadmath`` are vendored because the
#: manylinux_2_28 policy whitelist does not cover them; ``libstdc++`` and
#: ``libgcc_s`` are whitelisted on Linux and so are not vendored there, but a
#: macOS wheel built with a GCC toolchain would carry them, and they are under
#: the same license. All of them are GPL-3.0 with the GCC Runtime Library
#: Exception, which permits this redistribution but does not remove the
#: obligation to reproduce the notice.
COMPILER_RUNTIME_LIBRARIES = (
    "libgcc_s",
    "libgfortran",
    "libgomp",
    "libquadmath",
    "libstdc++",
)

#: Where the GPL's "corresponding sources" pointer aims for the GCC runtime.
GCC_SOURCE_URL = "https://gcc.gnu.org/mirrors.html"

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

_HEADER = """\
THIRD-PARTY NOTICES for palace-solver
=======================================

This wheel redistributes the Palace solver (Apache-2.0) together with every
library it links: the dependencies built by Palace's superbuild, the MPICH and
OpenBLAS builds the wheel vendors, and the compiler runtime libraries the wheel
repair step copies in from the toolchain. The license of each redistributed
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


def _gcc_runtime_note(gcc_version: str | None) -> str:
    """Render the GCC runtime note, naming the libraries and their sources."""
    named = ", ".join(COMPILER_RUNTIME_LIBRARIES)
    release = (
        f"GCC {gcc_version}" if gcc_version else "the GCC release used to build it"
    )
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


def detect_gcc_version() -> str | None:
    """Return the version of the ``gcc`` on PATH, or ``None`` if there is none.

    The GPL's source pointer has to name a release, so the notices record the
    compiler that actually built the payload rather than a version written down
    by hand and left to drift.
    """
    try:
        completed = subprocess.run(
            ["gcc", "-dumpfullversion"],
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
    return "\n".join(sections)


def _section(title: str, body: str) -> str:
    rule = "-" * len(title)
    return f"\n{rule}\n{title}\n{rule}\n\n{body.rstrip()}\n"


def library_stem(name: str) -> str:
    """Return the library name behind a vendored file name.

    ``auditwheel`` renames what it copies, inserting a hash of the contents:
    ``libgfortran-83c28eba.so.5.0.0``. The name before that hash is what a
    notice can be matched against.

    Args:
        name: File name as it appears in the wheel's vendored library
            directory.

    Returns:
        The library name, without the hash, the extension or the soversion.
    """
    stem = re.split(r"\.so|\.dylib", name, maxsplit=1)[0]
    return re.sub(r"-[0-9a-f]{6,}$", "", stem)


def vendored_libraries(wheel: Path) -> list[str]:
    """Return the library names a built wheel carries, deduplicated and sorted.

    Args:
        wheel: The repaired wheel.

    Returns:
        One entry per vendored library, as :func:`library_stem` names it.
    """
    with zipfile.ZipFile(wheel) as archive:
        members = [Path(name) for name in archive.namelist()]
    return sorted(
        {
            library_stem(member.name)
            for member in members
            if member.parent.name.endswith(".libs")
            and (".so" in member.name or member.name.endswith(".dylib"))
        }
    )


def audit_wheel(*, wheel: Path, install_prefix: Path) -> list[str]:
    """Check that every library the repair step vendored has a notice.

    A vendored library is accounted for when it was built here — it exists in
    the superbuild's install prefix, so the harvest walked its sources — or
    when it is one of the :data:`COMPILER_RUNTIME_LIBRARIES` the notices cover
    from shipped texts. Anything else entered the wheel without a license
    section and fails the build, which is the guard the source-tree walk cannot
    provide for libraries that have no source tree.

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
    unattributed = [
        name
        for name in found
        if name not in built_here and name not in COMPILER_RUNTIME_LIBRARIES
    ]
    if unattributed:
        raise UnattributedLibraryError(
            f"{wheel.name} vendors libraries with no license notice: "
            f"{', '.join(unattributed)}. Either they are built by the "
            "superbuild and the harvest missed them, or they come from the "
            "toolchain and belong in COMPILER_RUNTIME_LIBRARIES with their "
            "license text in wheelbuild/data."
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
        "(default: what `gcc -dumpfullversion` reports)",
    )
    args = parser.parse_args(argv)
    path = harvest(
        source_roots=args.source_roots,
        output=args.output,
        gcc_version=args.gcc_version or detect_gcc_version(),
    )
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
