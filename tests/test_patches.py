"""The carried patches the Windows build applies, and the recipe that applies them.

A Windows build takes over two hours cold, so a patch set that changed by
accident, a patch that arrived with CRLF, or an apply that is not idempotent on
a warm cache are all things to catch here rather than there.
"""

import subprocess
from pathlib import Path

import pytest

from wheelbuild import patches

ROOT = Path(__file__).resolve().parents[1]

#: The fixes ADR-0007 carries, one file each, plus SaveIteration's copy fallback
#: (adaptive runs aborting on 0.18.1.post3) and the three the upstream test gate
#: needs to build, link and stage Palace's tests. Adding or dropping one is a
#: decision, so it is a visible edit here.
CARRIED = [
    "palace/01-memoryreporting.patch",
    "palace/02-static-scalapack.patch",
    "palace/03-slepc-msys-petsc-dir.patch",
    "palace/04-windows-cxx.patch",
    "palace/05-saveiteration-copy.patch",
    "palace/06-windows-test-cxx.patch",
    "palace/07-windows-regression-staging.patch",
    "palace/08-windows-test-zlib-order.patch",
    "hypre/01-mingw-ffs.patch",
    "libxsmm/01-llp64.patch",
    "libceed/01-windows.patch",
    "strumpack/01-llp64.patch",
    "mumps/01-mingw-mpi.patch",
    "mfem/01-binary-ifgzstream.patch",
]


def _labels(found):
    return [path.relative_to(patches.PATCH_ROOT).as_posix() for path in found]


def test_the_carried_set_is_exactly_the_carried_fixes():
    assert _labels(patches.carried_patches()) == CARRIED


def test_nothing_else_sits_in_the_patch_directory():
    """A stray file (a .diff, an .orig left by `patch`) would be silently
    skipped by the glob, which is how a fix comes to be believed carried."""
    on_disk = sorted(
        path.relative_to(patches.PATCH_ROOT).as_posix()
        for path in patches.PATCH_ROOT.rglob("*")
        if path.is_file()
    )

    assert on_disk == sorted(CARRIED)


def test_every_owner_directory_has_a_patch_and_names_a_known_owner():
    owners = {path.name for path in patches.PATCH_ROOT.iterdir() if path.is_dir()}

    assert owners == set(patches.OWNERS)
    for owner in patches.OWNERS:
        assert patches.owner_patches(owner)


@pytest.mark.parametrize("label", CARRIED)
def test_no_patch_carries_a_carriage_return(label):
    """CRLF made the spike's first runs skip every patch."""
    assert b"\r" not in (patches.PATCH_ROOT / label).read_bytes()


@pytest.mark.parametrize("label", CARRIED)
def test_every_patch_is_checked_out_byte_for_byte(label):
    """`-text` is what stops a Windows checkout converting them."""
    path = (patches.PATCH_ROOT / label).relative_to(ROOT).as_posix()
    result = subprocess.run(
        ["git", "check-attr", "text", "--", path],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == f"{path}: text: unset"


@pytest.mark.parametrize("label", CARRIED)
def test_every_patch_says_what_it_fixes_before_its_first_hunk(label):
    text = (patches.PATCH_ROOT / label).read_text()
    header = text.split("\ndiff --git ", 1)[0].split("\n--- ", 1)[0]

    assert header.startswith("Carried patch, Windows only:")


def test_the_mumps_exception_is_documented_in_its_patch():
    """The one fix whose staleness the pre-pass cannot see: it patches a
    tarball the scivision wrapper fetches at MUMPS's configure."""
    header = (patches.PATCH_ROOT / "mumps/01-mingw-mpi.patch").read_text()

    assert "cmake/mumps_mingw_mpi.patch" in header
    assert "pre-pass" in header


def test_the_pre_pass_builds_one_patch_target_per_dependency():
    assert patches.patch_targets() == [
        "hypre-patch",
        "libxsmm-patch",
        "libCEED-patch",
        "strumpack-patch",
        "mumps-patch",
        "mfem-patch",
    ]


def test_the_project_include_turns_on_patch_step_targets():
    assert "set_property(DIRECTORY PROPERTY EP_STEP_TARGETS patch)" in (
        patches.PROJECT_INCLUDE
    )


# -- the recipe, against real git trees ------------------------------------


def _git(tree, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tree),
            "GIT_CONFIG_NOSYSTEM": "1",
            **patches.COMMIT_ENVIRONMENT,
        },
    ).stdout.strip()


def _repository(tree, files):
    tree.mkdir(parents=True)
    for name, text in files.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(text)
    _git(tree, "init", "--quiet")
    _git(tree, "add", "--all")
    _git(tree, "commit", "--quiet", "--message", "upstream")
    return tree


def _patch(root, owner, name, path, change):
    """Write a one-line unified diff for ``path`` under ``root/owner``.

    ``change`` is the line before and the line after.
    """
    before, after = change
    directory = root / owner
    directory.mkdir(parents=True, exist_ok=True)
    patch = directory / name
    patch.write_text(
        "Carried patch, Windows only: a test.\n\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{before}\n+{after}\n"
    )
    return patch


@pytest.fixture
def root(tmp_path):
    return tmp_path / "patches"


def test_apply_applies_then_recognises_its_own_work(tmp_path, root):
    tree = _repository(tmp_path / "tree", {"a.txt": "old\n"})
    patch = _patch(root, "palace", "01-x.patch", "a.txt", ("old", "new"))

    assert patches.apply(tree, patch, root=root) == "applied"
    assert (tree / "a.txt").read_text() == "new\n"
    assert patches.apply(tree, patch, root=root) == "already applied"
    assert (tree / "a.txt").read_text() == "new\n"


def test_apply_fails_naming_a_patch_upstream_moved_under(tmp_path, root):
    tree = _repository(tmp_path / "tree", {"a.txt": "rewritten upstream\n"})
    patch = _patch(root, "mfem", "01-x.patch", "a.txt", ("old", "new"))

    with pytest.raises(patches.PatchError, match=r"mfem/01-x\.patch"):
        patches.apply(tree, patch, root=root)


def test_apply_does_not_forgive_whitespace_drift(tmp_path, root):
    """No --ignore-whitespace: it would hide real upstream drift."""
    tree = _repository(tmp_path / "tree", {"a.txt": "old  \n"})
    patch = _patch(root, "mfem", "01-x.patch", "a.txt", ("old", "new"))

    with pytest.raises(patches.PatchError):
        patches.apply(tree, patch, root=root)


def test_the_digest_changes_with_a_patch_and_with_its_name(root):
    patch = _patch(root, "mfem", "01-x.patch", "a.txt", ("old", "new"))
    first = patches.digest("mfem", root=root)

    patch.write_text(patch.read_text().replace("+new", "+newer"))
    edited = patches.digest("mfem", root=root)
    patch.rename(root / "mfem" / "02-x.patch")
    renamed = patches.digest("mfem", root=root)

    assert len({first, edited, renamed}) == 3


@pytest.fixture
def palace_checkout(tmp_path):
    tree = _repository(
        tmp_path / "palace-0.18.1",
        {"palace/version.txt": "upstream\n", "README": "palace\n"},
    )
    _git(tree, "tag", "upstream-v0.18.1")
    return tree


def _describe(tree):
    return _git(tree, "describe", "--tags", "--always", "--dirty")


def test_palace_patches_are_committed_under_the_release_tag(palace_checkout, root):
    """`palace --version` comes from `git describe --tags --dirty`: the release
    tag on the patched commit is what makes it print the release, not -dirty."""
    _patch(root, "palace", "01-x.patch", "palace/version.txt", ("upstream", "patched"))

    assert patches.prepare_palace(palace_checkout, root=root) is True

    assert _describe(palace_checkout) == "v0.18.1"
    assert (palace_checkout / "palace/version.txt").read_text() == "patched\n"
    assert _git(palace_checkout, "rev-parse", "upstream-v0.18.1") == _git(
        palace_checkout, "rev-parse", "HEAD~1"
    )


def test_an_unchanged_palace_checkout_is_left_alone(palace_checkout, root):
    """Palace's CMake reconfigures on every change to HEAD, so recommitting the
    same tree on each warm run would relink the solver for nothing."""
    _patch(root, "palace", "01-x.patch", "palace/version.txt", ("upstream", "patched"))
    patches.prepare_palace(palace_checkout, root=root)
    head = _git(palace_checkout, "rev-parse", "HEAD")

    assert patches.prepare_palace(palace_checkout, root=root) is False
    assert _git(palace_checkout, "rev-parse", "HEAD") == head


def test_the_palace_commit_is_reproducible(tmp_path, root):
    """Same patches on the same tarball, same commit: nothing about the run
    that made it leaks into the tree Palace stamps."""
    _patch(root, "palace", "01-x.patch", "palace/version.txt", ("upstream", "patched"))
    heads = []
    for name in ("one", "two"):
        tree = _repository(tmp_path / name, {"palace/version.txt": "upstream\n"})
        _git(tree, "tag", "upstream-v0.18.1")
        patches.prepare_palace(tree, root=root)
        heads.append(_git(tree, "rev-parse", "HEAD"))

    assert heads[0] == heads[1]


def test_an_edited_palace_patch_is_reapplied_from_the_tarball(palace_checkout, root):
    patch = _patch(
        root, "palace", "01-x.patch", "palace/version.txt", ("upstream", "patched")
    )
    patches.prepare_palace(palace_checkout, root=root)
    patch.write_text(patch.read_text().replace("+patched", "+patched again"))

    assert patches.prepare_palace(palace_checkout, root=root) is True

    assert (palace_checkout / "palace/version.txt").read_text() == "patched again\n"
    assert _describe(palace_checkout) == "v0.18.1"
    assert _git(palace_checkout, "rev-list", "--count", "HEAD") == "2"


def test_a_modified_palace_checkout_is_repatched(palace_checkout, root):
    """A tracked file edited after the commit would stamp -dirty."""
    _patch(root, "palace", "01-x.patch", "palace/version.txt", ("upstream", "patched"))
    patches.prepare_palace(palace_checkout, root=root)
    (palace_checkout / "README").write_text("edited\n")

    assert patches.prepare_palace(palace_checkout, root=root) is True
    assert _describe(palace_checkout) == "v0.18.1"


def test_a_checkout_without_its_upstream_tag_is_refused(tmp_path, root):
    tree = _repository(tmp_path / "palace", {"a.txt": "x\n"})

    with pytest.raises(patches.PatchError, match="upstream-v"):
        patches.prepare_palace(tree, root=root)


def _dependency_trees(build_dir, *, upstream="old"):
    for dependency in patches.DEPENDENCIES:
        _repository(
            build_dir / "extern" / dependency.source, {"a.txt": f"{upstream}\n"}
        )
        for name in dependency.discard[1:]:
            (build_dir / "extern" / name).mkdir(parents=True, exist_ok=True)


def _dependency_patches(root):
    for dependency in patches.DEPENDENCIES:
        _patch(root, dependency.owner, "01-x.patch", "a.txt", ("old", "new"))


def test_dependency_patches_go_over_the_fetched_trees_and_are_stamped(tmp_path, root):
    build_dir = tmp_path / "superbuild"
    _dependency_trees(build_dir)
    _dependency_patches(root)

    patches.apply_dependencies(build_dir, root=root)

    for dependency in patches.DEPENDENCIES:
        source = build_dir / "extern" / dependency.source
        assert (source / "a.txt").read_text() == "new\n"
    assert patches.discard_stale_dependencies(build_dir, root=root) == []


def test_a_dependency_whose_patches_changed_is_discarded(tmp_path, root):
    build_dir = tmp_path / "superbuild"
    _dependency_trees(build_dir)
    _dependency_patches(root)
    patches.apply_dependencies(build_dir, root=root)
    mumps = root / "mumps" / "01-x.patch"
    mumps.write_text(mumps.read_text().replace("+new", "+newer"))

    assert patches.discard_stale_dependencies(build_dir, root=root) == ["mumps"]

    for name in ("mumps", "mumps-cmake", "mumps-build"):
        assert not (build_dir / "extern" / name).exists()
    assert (build_dir / "extern" / "mfem" / "a.txt").read_text() == "new\n"


def test_a_fetched_tree_with_no_stamp_is_discarded(tmp_path, root):
    """Fetched but never fully patched: what it carries is unknown."""
    build_dir = tmp_path / "superbuild"
    _dependency_trees(build_dir)
    _dependency_patches(root)

    discarded = patches.discard_stale_dependencies(build_dir, root=root)

    assert discarded == [dependency.owner for dependency in patches.DEPENDENCIES]


def test_a_cold_build_directory_discards_nothing(tmp_path, root):
    _dependency_patches(root)

    assert patches.discard_stale_dependencies(tmp_path / "superbuild", root=root) == []


def test_a_dependency_the_pre_pass_did_not_fetch_fails_by_name(tmp_path, root):
    build_dir = tmp_path / "superbuild"
    _dependency_trees(build_dir)
    _dependency_patches(root)
    subprocess.run(["rm", "-rf", str(build_dir / "extern" / "STRUMPACK")], check=True)

    with pytest.raises(patches.PatchError, match="strumpack-patch"):
        patches.apply_dependencies(build_dir, root=root)


def test_verify_names_every_patch_an_upstream_step_stripped(
    tmp_path, palace_checkout, root
):
    """MFEM's patch step starts with `git reset --hard`; were it to re-run
    during the build, the tree would compile without our fix and nothing else
    would notice."""
    _patch(root, "palace", "01-x.patch", "palace/version.txt", ("upstream", "patched"))
    build_dir = tmp_path / "superbuild"
    _dependency_trees(build_dir)
    _dependency_patches(root)
    patches.prepare_palace(palace_checkout, root=root)
    patches.apply_dependencies(build_dir, root=root)
    patches.verify(palace_checkout, build_dir, root=root)

    _git(build_dir / "extern" / "mfem", "reset", "--quiet", "--hard")

    with pytest.raises(patches.PatchError, match=r"mfem/01-x\.patch") as raised:
        patches.verify(palace_checkout, build_dir, root=root)
    assert "palace/" not in str(raised.value)
