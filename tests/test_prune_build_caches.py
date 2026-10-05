"""The cache pruner keeps one live entry per platform, not one overall.

This is the only piece of the wheel workflow that deletes anything, and it runs
unattended on every push to `main`. The failure it has to be proved against is
the one the second matrix row created: a pruner that knows only the platform it
happens to be running on deletes every other platform's live entry and hands it
a 40-minute cold build.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prune-build-caches.sh"

VERSION = "0.18.1"
INPUTS_HASH = "abc123"
WINDOWS_PATCHES_HASH = "fed987"

X86 = f"superbuild-manylinux_2_28_x86_64-{VERSION}-{INPUTS_HASH}"
AARCH64 = f"superbuild-manylinux_2_28_aarch64-{VERSION}-{INPUTS_HASH}"
MACOS = f"superbuild-macosx_15_0_arm64-{VERSION}-{INPUTS_HASH}"
WINDOWS = f"superbuild-win_amd64-{VERSION}-{INPUTS_HASH}-{WINDOWS_PATCHES_HASH}"


@pytest.fixture
def fake_gh(tmp_path):
    """Put a `gh` on PATH that lists canned caches and records its deletions."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    listing = tmp_path / "caches.tsv"
    deleted = tmp_path / "deleted.txt"
    deleted.write_text("")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "$2" == "list" ]]; then\n'
        f'  cat "{listing}"\n'
        'elif [[ "$2" == "delete" ]]; then\n'
        '  if [[ "$3" == "explode" ]]; then\n'
        '    echo "gh: cache not found" >&2; exit 1\n'
        "  fi\n"
        f'  echo "$3" >> "{deleted}"\n'
        "else\n"
        '  echo "unexpected gh invocation: $*" >&2; exit 64\n'
        "fi\n"
    )
    gh.chmod(0o755)

    class Harness:
        def __init__(self):
            self.bin_dir = bin_dir
            self.env = {
                "PATH": f"{bin_dir}:{Path(shutil.which('bash')).parent}:/usr/bin:/bin",
                "GH_TOKEN": "fake",
            }

        def caches(self, *entries):
            listing.write_text("".join(f"{i}\t{key}\n" for i, key in entries))

        def invoke(self, *argv, check=True):
            return subprocess.run(
                [str(SCRIPT), *argv],
                capture_output=True,
                text=True,
                check=check,
                env=self.env,
            )

        def run(self, *args, check=True):
            return self.invoke(
                VERSION, INPUTS_HASH, WINDOWS_PATCHES_HASH, *args, check=check
            )

        @property
        def deleted(self):
            return deleted.read_text().split()

    return Harness()


def test_every_platform_built_from_the_current_tree_survives(fake_gh):
    """The regression the second matrix row introduced: one current key per row."""
    fake_gh.caches(("1", X86), ("2", AARCH64), ("3", MACOS), ("4", WINDOWS))

    fake_gh.run()

    assert fake_gh.deleted == []


def test_a_windows_entry_from_older_patches_is_deleted(fake_gh):
    """Windows keys its carried patches, so a patch edit orphans its entry."""
    fake_gh.caches(
        ("1", X86),
        ("2", WINDOWS),
        ("3", f"superbuild-win_amd64-{VERSION}-{INPUTS_HASH}-000000"),
    )

    fake_gh.run()

    assert fake_gh.deleted == ["3"]


def test_a_windows_entry_saved_before_the_patches_were_keyed_is_deleted(fake_gh):
    """It ends in the other rows' suffix, but no Windows run can restore it
    exactly any more, so nothing would ever replace it."""
    fake_gh.caches(
        ("1", X86),
        ("2", WINDOWS),
        ("3", f"superbuild-win_amd64-{VERSION}-{INPUTS_HASH}"),
    )

    fake_gh.run()

    assert fake_gh.deleted == ["3"]


def test_only_a_windows_entry_carries_the_patches_hash(fake_gh):
    """The extra component is the Windows form; on another row it is stale."""
    fake_gh.caches(("1", X86), ("2", f"{AARCH64}-{WINDOWS_PATCHES_HASH}"))

    fake_gh.run()

    assert fake_gh.deleted == ["2"]


def test_nothing_is_deleted_when_no_windows_entry_matches_the_patches_hash(fake_gh):
    """A wrong patches hash would leave the other rows matching and delete only
    the live Windows entry, the one that costs most to rebuild."""
    fake_gh.caches(
        ("1", X86),
        ("2", f"superbuild-win_amd64-{VERSION}-{INPUTS_HASH}-000000"),
        ("3", f"superbuild-manylinux_2_28_x86_64-0.18.0-{INPUTS_HASH}"),
    )

    result = fake_gh.run(check=False)

    assert result.returncode != 0
    assert fake_gh.deleted == []


def test_an_entry_from_an_older_version_is_deleted(fake_gh):
    fake_gh.caches(
        ("1", X86), ("2", f"superbuild-manylinux_2_28_x86_64-0.18.0-{INPUTS_HASH}")
    )

    fake_gh.run()

    assert fake_gh.deleted == ["2"]


def test_an_entry_from_an_older_script_hash_is_deleted(fake_gh):
    fake_gh.caches(
        ("1", AARCH64), ("2", f"superbuild-manylinux_2_28_aarch64-{VERSION}-def456")
    )

    fake_gh.run()

    assert fake_gh.deleted == ["2"]


def test_a_retired_platform_is_deleted_once_the_tree_moves_on(fake_gh):
    """No platform list to maintain: an entry nothing refreshes ages out on its own."""
    fake_gh.caches(
        ("1", X86), ("2", f"superbuild-manylinux_2_28_ppc64le-0.18.0-{INPUTS_HASH}")
    )

    fake_gh.run()

    assert fake_gh.deleted == ["2"]


def test_a_key_that_merely_contains_the_current_suffix_is_not_kept(fake_gh):
    """The match is anchored at the end, so a prefix collision is still stale."""
    fake_gh.caches(("1", X86), ("2", f"{X86}-retry"))

    fake_gh.run()

    assert fake_gh.deleted == ["2"]


def test_dry_run_reports_without_deleting(fake_gh):
    fake_gh.caches(
        ("1", X86), ("2", f"superbuild-manylinux_2_28_x86_64-0.18.0-{INPUTS_HASH}")
    )

    result = fake_gh.run("--dry-run")

    assert fake_gh.deleted == []
    assert "would delete" in result.stdout


def test_a_missing_inputs_hash_deletes_nothing(fake_gh):
    """hashFiles returning empty would otherwise make every entry stale at once."""
    fake_gh.caches(("1", X86))

    result = fake_gh.invoke(VERSION, "", WINDOWS_PATCHES_HASH, check=False)

    assert result.returncode != 0
    assert fake_gh.deleted == []


def test_nothing_is_deleted_when_no_entry_matches_the_current_tree(fake_gh):
    """The job runs only after every row saved, so a total miss is a bad key.

    The two components the keep-rule rests on are derived, not read back from
    the caches, and a version or hash that is wrong but non-empty would make
    every live entry look stale in one unattended run. Refuse instead.
    """
    fake_gh.caches(("1", f"superbuild-manylinux_2_28_x86_64-0.18.0-{INPUTS_HASH}"))

    result = fake_gh.run(check=False)

    assert result.returncode != 0
    assert fake_gh.deleted == []


def test_one_failed_deletion_does_not_abandon_the_rest(fake_gh):
    """A stale entry a concurrent run already deleted must not strand the others."""
    fake_gh.caches(
        ("1", X86),
        ("explode", f"superbuild-manylinux_2_28_x86_64-0.18.0-{INPUTS_HASH}"),
        ("3", f"superbuild-manylinux_2_28_aarch64-0.18.0-{INPUTS_HASH}"),
    )

    result = fake_gh.run(check=False)

    assert fake_gh.deleted == ["3"]
    assert result.returncode != 0
    assert "explode" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ("--dry-run", VERSION, INPUTS_HASH, WINDOWS_PATCHES_HASH),
        (VERSION, INPUTS_HASH, WINDOWS_PATCHES_HASH, "--dryrun"),
        (VERSION, INPUTS_HASH, WINDOWS_PATCHES_HASH, "--dry-run", "extra"),
        (VERSION, INPUTS_HASH, "--dry-run"),
        (VERSION, INPUTS_HASH, ""),
        (VERSION,),
    ],
)
def test_a_misspelled_invocation_deletes_nothing(fake_gh, argv):
    """Every way of getting the arguments wrong ends in a suffix matching nothing."""
    fake_gh.caches(("1", X86), ("2", AARCH64))

    result = fake_gh.invoke(*argv, check=False)

    assert result.returncode == 64
    assert fake_gh.deleted == []
