"""Facts about the wheel matrix that are cheaper to assert than to run.

The wheel job takes 30-60 minutes per platform, so a mistyped row is an
expensive way to find out. These are the properties that hold for every row and
that a second platform made possible to get wrong.
"""

import re
from pathlib import Path

import pytest
import yaml

from wheelbuild.platforms import platform_tag

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "wheels.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def rows(workflow):
    return workflow["jobs"]["wheel"]["strategy"]["matrix"]["include"]


@pytest.fixture(scope="module")
def build_cache(workflow):
    """The step whose key decides which trees a run may restore."""
    steps = workflow["jobs"]["wheel"]["steps"]
    return next(step for step in steps if step.get("id") == "build-cache")


def test_both_linux_platforms_have_a_row(rows):
    tags = {row["tag"] for row in rows}

    assert {"manylinux_2_28_x86_64", "manylinux_2_28_aarch64"} <= tags


def test_every_row_has_a_distinct_tag(rows):
    """The tag names the cache namespace and the artifact; a duplicate loses one."""
    tags = [row["tag"] for row in rows]

    assert len(tags) == len(set(tags))


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_the_row_tag_is_what_the_build_would_derive(rows, machine):
    """The job fails in seconds on a mismatch; this fails before it is pushed."""
    expected = platform_tag(system="Linux", machine=machine)
    row = next(row for row in rows if row["tag"] == expected)

    assert row["image"].endswith(expected)


def test_an_arm_row_names_an_arm_runner(rows):
    """Every platform builds natively, so the runner and the image must agree."""
    for row in rows:
        assert row["runner"].endswith("-arm") == row["tag"].endswith("aarch64")


def test_no_row_names_a_moving_runner_label(rows):
    """A moving label changes the image, and on macOS the published filename."""
    for row in rows:
        assert not row["runner"].endswith("-latest")


def test_the_artifact_name_carries_the_platform(workflow):
    """Two rows uploading one name means the second silently overwrites the first."""
    steps = workflow["jobs"]["wheel"]["steps"]
    upload = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")
    )

    assert "matrix.tag" in upload["with"]["name"]


def test_the_cache_key_is_namespaced_by_platform(build_cache):
    """Without the tag component the two platforms restore each other's tree."""
    assert "PLATFORM_TAG" in build_cache["with"]["key"]


def test_cache_cleanup_does_not_derive_a_tag_from_its_own_runner(workflow):
    """That is the shape that deletes every other platform's live cache."""
    steps = workflow["jobs"]["cache-cleanup"]["steps"]
    script = "".join(step.get("run", "") for step in steps)

    assert "platform_tag" not in script
    assert "scripts/prune-build-caches.sh" in script


def test_the_cache_key_covers_the_openblas_build_module(build_cache):
    """A tree is only as reusable as the key admits.

    wheelbuild/openblas.py decides the CPU baseline the vendored library is
    compiled for, so an edit to it has to orphan the entries built before it.
    """
    assert "wheelbuild/openblas.py" in build_cache["with"]["key"]


def test_the_cleanup_job_hashes_exactly_what_the_cache_key_hashes(
    workflow, build_cache
):
    """The pruner matches on the trailing hash, so a divergent file list here
    makes every live entry look stale and deletes it.
    """
    cleanup = workflow["jobs"]["cache-cleanup"]["steps"]
    keyed = _hashed_files(build_cache["with"]["key"])
    pruned = _hashed_files(
        next(step["env"]["INPUTS_HASH"] for step in cleanup if "env" in step)
    )

    assert keyed is not None
    assert keyed == pruned


def _hashed_files(expression):
    """The argument list of the hashFiles() call in one workflow expression."""
    found = re.search(r"hashFiles\((.*?)\)", expression, re.DOTALL)
    return None if found is None else found.group(1)
