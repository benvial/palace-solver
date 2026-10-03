"""Apply the carried patches the Windows build needs, and prove they stayed.

Eleven fixes, one file each, under ``wheelbuild/data/patches/windows/<owner>/``.
The owner is the upstream whose files a patch edits -- Palace or one of the six
dependencies its superbuild fetches -- and ``NN`` in ``NN-<slug>.patch`` is the
apply order within it. Linux and macOS never read this directory: only the
Windows arm of :func:`wheelbuild.superbuild.run` calls into this module, so
the other platforms build byte-identical trees to the ones they built before.

The recipe, decided in ``.scratch/windows-wheel/issues/02-carried-patch-hook.md``:

1. Palace's own patches are committed onto the release-tarball checkout before
   the superbuild is configured. The commit is made reproducibly (fixed
   identity and date) and the release tag ``v<version>`` is moved onto it, so
   ``palace --version`` reports the release as it does on the other platforms;
   the tarball commit keeps ``upstream-v<version>``.
2. The superbuild is configured with a project include that gives every
   ExternalProject a ``<name>-patch`` step target, and those six are built:
   every tree fetched and upstream-patched, nothing compiled.
3. Each dependency patch then goes over its fetched tree, on top of upstream's
   own patches.

All of them are therefore settled minutes into a cold build, before the first
object compiles, rather than whenever ExternalProject happens to reach each
dependency two hours in -- so a Palace bump surfaces every stale patch at once.

Each apply is three-state. ``git apply --check`` passes: apply it.
``git apply --reverse --check`` passes: it is already there, touch nothing.
Neither: fail, naming the patch. ``--ignore-whitespace`` is never passed; it
would hide genuine upstream drift. Line endings are ruled out separately:
``.gitattributes`` marks the patches ``-text``, so a Windows checkout cannot
give them CRLF, which is how the spike's first runs skipped every patch.

The patch files are not part of the build cache key, so editing one never sends
the Windows row cold. Instead a digest of each owner's patches is stamped beside
its tree; on a mismatch the dependency's source, stamp and build directories are
discarded (it and everything configured against it rebuild), or Palace's
checkout is reset to the tarball commit and re-patched.

After the superbuild, :func:`verify` reverse-checks every one. That is the only
defence against an upstream patch step re-running -- MFEM's starts with
``git reset --hard`` -- and silently stripping ours.

One fix is checked later than the rest: ``mumps/01-mingw-mpi.patch`` adds a
patch to the scivision MUMPS wrapper, which applies it at MUMPS's configure to
a tarball the wrapper fetches itself. Its header says why that is accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from wheelbuild._process import check_call

#: Where the carried patches live, one directory per owner.
PATCH_ROOT = Path(__file__).resolve().parent / "data" / "patches" / "windows"

#: The tag on the release-tarball commit, which the patched commit is made on.
UPSTREAM_TAG_PREFIX = "upstream-"

#: The identity and date the Palace patch commit is made with, so that the same
#: patches on the same tarball always give the same commit.
COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "palace-solver",
    "GIT_AUTHOR_EMAIL": "palace-solver@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "palace-solver",
    "GIT_COMMITTER_EMAIL": "palace-solver@localhost",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}

#: The trailer in the Palace patch commit's message that records which patches
#: it carries.
DIGEST_TRAILER = "Carried-Patches-Digest"


@dataclass(frozen=True)
class Dependency:
    """A superbuild dependency that carries patches."""

    #: Its directory under :data:`PATCH_ROOT`.
    owner: str
    #: Its ExternalProject name, which names the ``<name>-patch`` step target.
    target: str
    #: Its ``SOURCE_DIR`` under the superbuild's ``extern/``.
    source: str
    #: Everything under ``extern/`` that holds state built from that source:
    #: the source itself, the ExternalProject ``PREFIX`` (its stamps, and the
    #: build tree for the in-source builds) and any ``BINARY_DIR``.
    discard: tuple[str, ...]


#: The six dependencies, in the order Palace's superbuild names them. Their
#: directories are Palace v0.18.1's ``cmake/External*.cmake``.
DEPENDENCIES = (
    Dependency("hypre", "hypre", "hypre", ("hypre", "hypre-cmake", "hypre-build")),
    Dependency("libxsmm", "libxsmm", "libxsmm", ("libxsmm", "libxsmm-cmake")),
    Dependency("libceed", "libCEED", "libCEED", ("libCEED", "libCEED-cmake")),
    Dependency(
        "strumpack",
        "strumpack",
        "STRUMPACK",
        ("STRUMPACK", "STRUMPACK-cmake", "STRUMPACK-build"),
    ),
    Dependency("mumps", "mumps", "mumps", ("mumps", "mumps-cmake", "mumps-build")),
    Dependency("mfem", "mfem", "mfem", ("mfem", "mfem-cmake", "mfem-build")),
)

#: Palace's own patches, applied to the source checkout rather than to a tree
#: under ``extern/``.
PALACE = "palace"

#: Every owner directory under :data:`PATCH_ROOT`.
OWNERS = (PALACE, *(dependency.owner for dependency in DEPENDENCIES))

#: The project include that turns on the pre-pass. A directory property, which
#: ExternalProject defines as inherited, so it reaches every ExternalProject_Add
#: in the superbuild without editing Palace's CMake.
PROJECT_INCLUDE = """\
# Written by wheelbuild.superbuild on Windows only. Gives every ExternalProject
# a <name>-patch target, so the carried patches can be applied to the fetched
# trees before anything is compiled. See wheelbuild/patches.py.
set_property(DIRECTORY PROPERTY EP_STEP_TARGETS patch)
"""


class PatchError(RuntimeError):
    """A carried patch neither applies nor is already applied."""


def owner_patches(owner: str, *, root: Path = PATCH_ROOT) -> list[Path]:
    """The patches one owner carries, in apply order."""
    return sorted((root / owner).glob("*.patch"))


def carried_patches(*, root: Path = PATCH_ROOT) -> list[Path]:
    """Every carried patch: Palace's first, then each dependency's."""
    return [patch for owner in OWNERS for patch in owner_patches(owner, root=root)]


def digest(owner: str, *, root: Path = PATCH_ROOT) -> str:
    """A fingerprint of one owner's patches, names and bytes both.

    A renamed patch changes the apply order, so the name is part of it.
    """
    hasher = hashlib.sha256()
    for patch in owner_patches(owner, root=root):
        hasher.update(patch.name.encode() + b"\0")
        hasher.update(patch.read_bytes() + b"\0")
    return hasher.hexdigest()


def _label(patch: Path, root: Path) -> str:
    return patch.relative_to(root).as_posix()


def _git_apply_succeeds(tree: Path, patch: Path, *flags: str) -> bool:
    result = subprocess.run(
        ["git", "apply", *flags, str(patch)],
        cwd=tree,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def apply(tree: Path, patch: Path, *, root: Path = PATCH_ROOT) -> str:
    """Apply one patch to ``tree``, or confirm it is already there.

    Args:
        tree: The source tree the patch's paths are relative to.
        patch: The patch file.
        root: The patch root, for naming the patch in messages.

    Returns:
        ``"applied"`` or ``"already applied"``.

    Raises:
        PatchError: If the patch neither applies nor reverses cleanly, which
            is what an upstream change under it looks like.
    """
    label = _label(patch, root)
    if _git_apply_succeeds(tree, patch, "--check"):
        # nowarn: MUMPS's Fortran carries trailing blanks the patch must match.
        check_call(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=tree)
        outcome = "applied"
    elif _git_apply_succeeds(tree, patch, "--reverse", "--check"):
        outcome = "already applied"
    else:
        raise PatchError(
            f"carried patch {label} does not apply to {tree}: the upstream source "
            "under it has changed, so the patch has to be rebased or retired"
        )
    print(f"{label}: {outcome}", flush=True)
    return outcome


def _git_output(tree: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=tree, check=True, capture_output=True, text=True
    ).stdout.strip()


def palace_version(source_dir: Path) -> str:
    """The release the checkout was made from, read off its upstream tag."""
    tags = _git_output(source_dir, "tag", "--list", f"{UPSTREAM_TAG_PREFIX}v*")
    found = tags.split()
    if len(found) != 1:
        raise PatchError(
            f"{source_dir} must carry exactly one {UPSTREAM_TAG_PREFIX}v* tag on its "
            f"release-tarball commit, found {found or 'none'}"
        )
    return found[0].removeprefix(UPSTREAM_TAG_PREFIX).removeprefix("v")


def _commit_message(version: str, owner_digest: str) -> str:
    return (
        f"Palace v{version} with the Windows carried patches\n\n"
        f"{DIGEST_TRAILER}: {owner_digest}\n"
    )


def prepare_palace(source_dir: Path, *, root: Path = PATCH_ROOT) -> bool:
    """Commit Palace's carried patches onto its tarball checkout.

    Nothing is touched when the release tag already names a commit carrying
    these exact patches: Palace's CMake reconfigures on every change to the
    checkout's ``HEAD``, so recommitting an unchanged tree would relink the
    solver for nothing.

    Args:
        source_dir: Palace's checkout, whose tarball commit is tagged
            ``upstream-v<version>``.
        root: The patch root.

    Returns:
        Whether the checkout was re-patched.
    """
    version = palace_version(source_dir)
    release_tag = f"v{version}"
    wanted = digest(PALACE, root=root)
    head = _git_output(source_dir, "rev-parse", "HEAD")
    tagged = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{release_tag}"],
        cwd=source_dir,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if tagged == head:
        message = _git_output(source_dir, "log", "-1", "--format=%B", head)
        # Tracked files only: that is what `git describe --dirty`, and so
        # `palace --version`, looks at.
        clean = not _git_output(
            source_dir, "status", "--porcelain", "--untracked-files=no"
        )
        if f"{DIGEST_TRAILER}: {wanted}" in message and clean:
            print(f"palace: carried patches already committed as {release_tag}")
            return False

    print(f"palace: committing the carried patches onto {release_tag}", flush=True)
    upstream = f"{UPSTREAM_TAG_PREFIX}{release_tag}"
    check_call(["git", "reset", "--quiet", "--hard", upstream], cwd=source_dir)
    check_call(["git", "clean", "--quiet", "-fdx"], cwd=source_dir)
    for patch in owner_patches(PALACE, root=root):
        apply(source_dir, patch, root=root)
    check_call(["git", "add", "--all"], cwd=source_dir)
    check_call(
        [
            "git",
            "commit",
            "--quiet",
            "--no-verify",
            "--message",
            _commit_message(version, wanted),
        ],
        cwd=source_dir,
        env=COMMIT_ENVIRONMENT,
    )
    check_call(["git", "tag", "--force", release_tag], cwd=source_dir)
    return True


def _stamp(build_dir: Path, dependency: Dependency) -> Path:
    return build_dir / "extern" / f".carried-patches-{dependency.owner}"


def discard_stale_dependencies(
    build_dir: Path, *, root: Path = PATCH_ROOT
) -> list[str]:
    """Discard every fetched dependency whose patches changed since it was patched.

    Run before the superbuild is configured. A tree whose stamp is missing is
    discarded too: it was fetched but never fully patched, or patched by a run
    that predates the stamps, and either way what it carries is unknown.

    Args:
        build_dir: The superbuild's build directory.
        root: The patch root.

    Returns:
        The owners discarded.
    """
    discarded = []
    for dependency in DEPENDENCIES:
        stamp = _stamp(build_dir, dependency)
        source = build_dir / "extern" / dependency.source
        recorded = stamp.read_text().strip() if stamp.is_file() else None
        if recorded == digest(dependency.owner, root=root):
            continue
        if recorded is None and not source.exists():
            continue
        for name in dependency.discard:
            shutil.rmtree(build_dir / "extern" / name, ignore_errors=True)
        stamp.unlink(missing_ok=True)
        print(f"{dependency.owner}: carried patches changed, discarded its tree")
        discarded.append(dependency.owner)
    return discarded


def patch_targets() -> list[str]:
    """The step targets the pre-pass builds: fetch and upstream-patch, no more."""
    return [f"{dependency.target}-patch" for dependency in DEPENDENCIES]


def apply_dependencies(build_dir: Path, *, root: Path = PATCH_ROOT) -> None:
    """Apply each dependency's patches to its fetched tree, then stamp it.

    Args:
        build_dir: The superbuild's build directory, after the pre-pass.
        root: The patch root.

    Raises:
        PatchError: If a tree is missing or a patch does not apply.
    """
    for dependency in DEPENDENCIES:
        source = build_dir / "extern" / dependency.source
        if not source.is_dir():
            raise PatchError(
                f"{dependency.target}-patch left no {source}: the pre-pass did not "
                "fetch it, so its carried patches cannot be applied"
            )
        for patch in owner_patches(dependency.owner, root=root):
            apply(source, patch, root=root)
        _stamp(build_dir, dependency).write_text(
            digest(dependency.owner, root=root) + "\n"
        )


def verify(source_dir: Path, build_dir: Path, *, root: Path = PATCH_ROOT) -> None:
    """Prove every carried patch is still in the tree the superbuild compiled.

    Raises:
        PatchError: Naming every patch that is no longer applied.
    """
    trees = {PALACE: source_dir} | {
        dependency.owner: build_dir / "extern" / dependency.source
        for dependency in DEPENDENCIES
    }
    missing = [
        _label(patch, root)
        for owner, tree in trees.items()
        for patch in owner_patches(owner, root=root)
        if not tree.is_dir()
        or not _git_apply_succeeds(tree, patch, "--reverse", "--check")
    ]
    if missing:
        raise PatchError(
            "carried patches missing from the built tree, most likely stripped by "
            "an upstream patch step that re-ran: " + ", ".join(missing)
        )
    print(
        f"all {len(carried_patches(root=root))} carried patches are in the built tree"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """List the carried patches, or verify a built tree carries them."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="print every carried patch with its owner")
    check = commands.add_parser("verify", help="reverse-check a built tree")
    check.add_argument("--source-dir", type=Path, required=True)
    check.add_argument("--build-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "list":
        for patch in carried_patches():
            print(_label(patch, PATCH_ROOT))
        return 0
    verify(args.source_dir, args.build_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
