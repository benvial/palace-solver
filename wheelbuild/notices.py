"""Harvest third-party license notices from the superbuild source checkouts.

The wheel redistributes every dependency the superbuild compiles, so the
binary distribution must carry their license texts. MUMPS is the strict case:
CeCILL-C redistribution requires the license text and a pointer to the
corresponding sources, both added here on top of the harvested files.

The harvest fails the build when a known dependency contributes no license
file, so a superbuild layout change cannot silently drop a notice.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import Path

#: Dependencies the superbuild compiles into the wheel. A missing license file
#: for any of these fails the build.
REQUIRED_DEPENDENCIES = (
    "arpack",
    "gslib",
    "hypre",
    "libceed",
    "libxsmm",
    "mfem",
    "mumps",
    "petsc",
    "slepc",
    "strumpack",
    "superlu",
    "zfp",
)

#: Where the CeCILL-C obligation's "corresponding sources" pointer aims.
MUMPS_SOURCE_URL = "https://mumps-solver.org/index.php?page=dwnld"

#: File names that hold a license or copyright notice.
LICENSE_FILE_PATTERNS = (
    "LICENSE*",
    "License*",
    "license*",
    "COPYING*",
    "COPYRIGHT*",
    "NOTICE*",
)

_CECILL_C_TEXT = Path(__file__).resolve().parent / "data" / "CeCILL-C-V1-en.txt"

_HEADER = """\
THIRD-PARTY NOTICES for pypalace-solver
=======================================

This wheel redistributes the Palace solver (Apache-2.0) together with the
libraries its superbuild compiles. The license of each redistributed component
is reproduced below.
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


class MissingLicenseError(RuntimeError):
    """Raised when a dependency of the superbuild contributes no license file."""


def collect(source_root: Path) -> dict[str, list[Path]]:
    """Collect every license file below ``source_root``, by checkout.

    The superbuild keeps a source checkout and a build directory per
    dependency, and the build directory often copies the license file, so
    identical texts are reported once. Every checkout is harvested, not only
    the ones in :data:`REQUIRED_DEPENDENCIES`, because the superbuild also
    pulls in prerequisites (ScaLAPACK, METIS, ...) whose notices must ship too.

    Args:
        source_root: Superbuild directory holding the source checkouts.

    Returns:
        Mapping of checkout path (relative to ``source_root``) to its license
        files, in walk order.
    """
    collected: dict[str, list[Path]] = {}
    seen_texts: set[str] = set()
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.is_symlink() or not _is_license_file(path):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen_texts:
            continue
        seen_texts.add(digest)
        checkout = str(path.parent.relative_to(source_root))
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


def render(source_root: Path) -> str:
    """Render the THIRD-PARTY-NOTICES body.

    Args:
        source_root: Superbuild directory holding the source checkouts.

    Returns:
        The complete notices text.

    Raises:
        MissingLicenseError: If a required dependency has no license file.
    """
    collected = collect(source_root)
    missing = _missing_dependencies(collected)
    if missing:
        raise MissingLicenseError(
            f"no license file found under {source_root} for: {', '.join(missing)}"
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
    return "\n".join(sections)


def _section(title: str, body: str) -> str:
    rule = "-" * len(title)
    return f"\n{rule}\n{title}\n{rule}\n\n{body.rstrip()}\n"


def harvest(*, source_root: Path, output: Path) -> Path:
    """Write the harvested notices to ``output``.

    Args:
        source_root: Superbuild directory holding the source checkouts.
        output: Destination file.

    Returns:
        The path written.
    """
    text = render(source_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the notice harvester."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    path = harvest(source_root=args.source_root, output=args.output)
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
