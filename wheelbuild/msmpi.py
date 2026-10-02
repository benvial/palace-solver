"""Fetch and verify the MS-MPI redistributable that the Windows wheel vendors.

On Windows the wheel carries Microsoft's MS-MPI rather than an MPICH of its own:
the x64 ``msmpi.dll`` and the ``mpiexec.exe`` and ``smpd.exe`` that launch it,
byte-identical to Microsoft's files, with ``libmsmpifec.dll`` from MSYS2 beside
them. Microsoft publishes them only inside ``msmpisetup.exe``, and running that
installer needs an elevated install, so the files are carved out of it instead.

The installer is not one archive but a 32-bit stub with two MSIs appended, x86
then x64. A plain ``7z x`` reads the first cabinet only, which is the x86 one,
and its ``msmpi.dll`` loads into no 64-bit process. So the installer is split
at its embedded file signatures (``7z x -t#``), every MSI found is unpacked,
and the one whose cabinet holds ``msmpi64.dll`` is taken. That name is the
MSI's File-table key, not a file name: the row's ``FileName`` is ``msmpi.dll``,
which is what the installer writes to ``System32`` and what every x64 program
imports, so the file is written out under it. ``msmpires.dll`` is left behind;
two ranks launch without it.

Every byte is checked rather than trusted. The installer is pinned by its
SHA-256, and each file taken from it by its own, recorded here against that
installer, so a mismatch fails naming the file. That also makes taking the
wrong MSI a failure rather than a silent x86 wheel: the x86 cabinet has no
``msmpi64.dll``, and its ``mpiexec.exe`` and ``smpd.exe`` hash differently.

Nothing here needs Windows -- ``7z`` runs anywhere -- so a Linux job proves the
fetch. The installer is fetched on each run into a directory of the caller's
choosing, outside the cached build root: it is 7.8 MB, its hash pins its bytes
more tightly than a cache key could, and the build tree links against MSYS2's
import library rather than against these files, so the tree does not depend on
this module and the module stays out of the cache key.

Microsoft's two texts that the notices need, its Redistributable EULA and its
MPI third-party notices, are committed under ``wheelbuild/data`` rather than
extracted at build time. Their hashes are recorded here as well and checked in
the same x64 MSI, which is what ties the committed copies to this installer.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from wheelbuild._process import check_call

#: MS-MPI release vendored into the Windows wheel. ``wheelbuild.pin_check``
#: holds it to the series a user-installed MS-MPI's ``mpiexec`` is proved
#: against.
MSMPI_VERSION = "10.1.3"

#: Microsoft's download of the 10.1.3 installer (Download Center item 105289).
#: Its files carry the build number, 10.1.12498.52, not the release.
INSTALLER_URL = (
    "https://download.microsoft.com/download/7/2/7/"
    "72731ebb-b63c-4170-ade7-836966263a8f/msmpisetup.exe"
)

#: SHA-256 of ``msmpisetup.exe`` 10.1.3.
INSTALLER_SHA256 = "47443829114d8d8670f77af98939fe876d33eceb35d0ce4e0e85efeec4d87213"

#: The MSI File-table key that only the x64 MSI has, which is how it is told
#: from the x86 one.
X64_MARKER = "msmpi64.dll"


@dataclass(frozen=True)
class RedistributableFile:
    """One file the wheel takes from the x64 MSI."""

    #: The name it is written out under, which is the MSI's ``FileName``.
    name: str
    #: Its File-table key, which is what ``7z`` names it in the cabinet.
    msi_key: str
    #: SHA-256 of Microsoft's bytes.
    sha256: str


#: What the wheel vendors from the installer, all from the x64 MSI. No
#: ``msmpires.dll``.
REDISTRIBUTABLE_FILES = (
    RedistributableFile(
        name="msmpi.dll",
        msi_key=X64_MARKER,
        sha256="d8f74689e83d7e5a7277de15b221edea91f529529f8a16fd0e510236c0705dcb",
    ),
    RedistributableFile(
        name="mpiexec.exe",
        msi_key="mpiexec.exe",
        sha256="161d18ac9f66d28add3aec8719f834297d0dc08ce07ec536c8aed5a9172fca8e",
    ),
    RedistributableFile(
        name="smpd.exe",
        msi_key="smpd.exe",
        sha256="739df32db5a8879d7e721052acbe457f99afe7897c982d99c9483ae736a7b1ab",
    ),
)

#: SHA-256 of Microsoft's two texts as the x64 MSI carries them (the x86 MSI's
#: copies are byte-identical). The copies committed for the notices must match.
MICROSOFT_TEXTS = {
    "MicrosoftMPI_Redistributable_EULA.rtf": (
        "125d29a463c724ddb5eed6a14370f5ce74dfd063cfca3eddad2d50082eb62106"
    ),
    "MPI_Redistributables_TPN.txt": (
        "e202e6c77b4ecb7be69e39d647be54bb5d076d90522e533d11dbaaacd618d7f8"
    ),
}


class ChecksumMismatchError(RuntimeError):
    """A file's SHA-256 is not the one recorded for it."""


class MissingX64InstallerError(RuntimeError):
    """The installer does not hold exactly one MSI with the x64 runtime."""


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(path: Path, expected: str, *, label: str | None = None) -> Path:
    """Check a file against its recorded SHA-256.

    Args:
        path: File to check.
        expected: The hex SHA-256 recorded for it.
        label: How to name the file in the error; defaults to its file name.

    Returns:
        ``path``.

    Raises:
        ChecksumMismatchError: If the digests differ.
    """
    actual = sha256(path)
    if actual != expected:
        raise ChecksumMismatchError(
            f"{label or path.name}: SHA-256 is {actual}, expected {expected} "
            f"(MS-MPI {MSMPI_VERSION}, read from {path})"
        )
    return path


def fetch(
    installer: Path,
    *,
    url: str = INSTALLER_URL,
    expected: str = INSTALLER_SHA256,
) -> Path:
    """Download the installer unless a verified copy is already there.

    Args:
        installer: Where to write ``msmpisetup.exe``.
        url: Where to download it from.
        expected: Its recorded SHA-256.

    Returns:
        The verified installer.

    Raises:
        ChecksumMismatchError: If the downloaded bytes are not the pinned ones;
            nothing is left at ``installer`` then.
    """
    if installer.is_file() and sha256(installer) == expected:
        print(f"reusing {installer}")
        return installer
    installer.parent.mkdir(parents=True, exist_ok=True)
    partial = installer.with_name(installer.name + ".part")
    print(f"downloading {url}", flush=True)
    # The URL is a module constant or a caller's argument, never user input.
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        partial.write_bytes(response.read())
    try:
        verify(partial, expected, label=installer.name)
    except ChecksumMismatchError:
        partial.unlink()
        raise
    partial.replace(installer)
    return installer


def unpack(installer: Path, work_dir: Path, *, seven_zip: str = "7z") -> list[Path]:
    """Split the installer into its MSIs and unpack each one.

    Args:
        installer: ``msmpisetup.exe``.
        work_dir: Empty scratch directory.
        seven_zip: The 7-Zip executable.

    Returns:
        One directory per MSI, holding its cabinet's files under their
        File-table keys.
    """
    parts = work_dir / "parts"
    check_call([seven_zip, "x", "-t#", str(installer), f"-o{parts}", "-y"])
    trees = []
    for msi in sorted(parts.glob("*.msi")):
        tree = work_dir / msi.stem
        check_call([seven_zip, "x", str(msi), f"-o{tree}", "-y"])
        trees.append(tree)
    return trees


def x64_tree(trees: Sequence[Path]) -> Path:
    """Pick the unpacked MSI that carries the x64 runtime.

    Args:
        trees: Unpacked MSIs, as :func:`unpack` returns them.

    Returns:
        The one holding :data:`X64_MARKER`.

    Raises:
        MissingX64InstallerError: If none does, or more than one.
    """
    found = [tree for tree in trees if (tree / X64_MARKER).is_file()]
    if len(found) != 1:
        raise MissingX64InstallerError(
            f"expected exactly one MSI holding {X64_MARKER} in the MS-MPI "
            f"installer, found {len(found)} among "
            + (", ".join(str(tree) for tree in trees) or "no MSIs")
        )
    return found[0]


def take(
    tree: Path,
    out_dir: Path,
    *,
    files: Sequence[RedistributableFile] = REDISTRIBUTABLE_FILES,
) -> list[Path]:
    """Verify the redistributable files in an unpacked MSI and copy them out.

    Every file is checked before any is copied, so a failure leaves
    ``out_dir`` without a partial set.

    Args:
        tree: The unpacked x64 MSI.
        out_dir: Where to write them, under their installed names.
        files: What to take.

    Returns:
        The written files.

    Raises:
        FileNotFoundError: If the MSI lacks one of them -- as the x86 MSI lacks
            ``msmpi64.dll``.
        ChecksumMismatchError: If one is not Microsoft's x64 file.
    """
    for entry in files:
        source = tree / entry.msi_key
        if not source.is_file():
            raise FileNotFoundError(
                f"{entry.name}: no {entry.msi_key} in {tree}; is this the x64 MSI?"
            )
        label = entry.name
        if entry.msi_key != entry.name:
            label = f"{entry.name} (MSI key {entry.msi_key})"
        verify(source, entry.sha256, label=label)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for entry in files:
        target = out_dir / entry.name
        shutil.copyfile(tree / entry.msi_key, target)
        written.append(target)
    return written


def verify_texts(tree: Path, texts: Mapping[str, str] = MICROSOFT_TEXTS) -> None:
    """Check Microsoft's licence texts in an unpacked MSI against their hashes.

    Args:
        tree: The unpacked x64 MSI.
        texts: File name to recorded SHA-256.

    Raises:
        ChecksumMismatchError: If one differs.
    """
    for name, expected in texts.items():
        verify(tree / name, expected)


def run(
    out_dir: Path,
    *,
    installer: Path,
    work_dir: Path,
    seven_zip: str = "7z",
) -> list[Path]:
    """Fetch the installer, take the x64 files and verify all of them.

    Args:
        out_dir: Where ``msmpi.dll``, ``mpiexec.exe`` and ``smpd.exe`` go.
        installer: Where ``msmpisetup.exe`` is kept; a verified copy already
            there is reused.
        work_dir: Empty scratch directory for the unpacked MSIs.
        seven_zip: The 7-Zip executable.

    Returns:
        The written files.
    """
    fetch(installer)
    tree = x64_tree(unpack(installer, work_dir, seven_zip=seven_zip))
    verify_texts(tree)
    return take(tree, out_dir)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the MS-MPI fetch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument(
        "--installer",
        type=Path,
        help="where msmpisetup.exe is kept (default: a temporary directory)",
    )
    parser.add_argument("--seven-zip", default="7z")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as scratch:
        written = run(
            args.out_dir,
            installer=args.installer or Path(scratch) / "msmpisetup.exe",
            work_dir=Path(scratch) / "unpacked",
            seven_zip=args.seven_zip,
        )
    for path in written:
        print(f"{path}  {sha256(path)}")
    print(f"MS-MPI {MSMPI_VERSION} x64 redistributable verified into {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
