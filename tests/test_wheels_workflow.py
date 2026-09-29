"""Facts about the wheel matrix that are cheaper to assert than to run.

The wheel job takes 30-60 minutes per platform, so a mistyped row is an
expensive way to find out. These are the properties that hold for every row and
that a second platform made possible to get wrong.
"""

import re
from pathlib import Path

import pytest
import yaml

from wheelbuild.platforms import MACOS_DEPLOYMENT_TARGET, platform_tag

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "wheels.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def rows(workflow):
    return workflow["jobs"]["wheel"]["strategy"]["matrix"]["include"]


@pytest.fixture(scope="module")
def steps(workflow):
    return workflow["jobs"]["wheel"]["steps"]


@pytest.fixture(scope="module")
def named_step(steps):
    def find(name):
        return next(step for step in steps if step.get("name") == name)

    return find


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


def test_the_macos_row_claims_the_tag_the_build_would_derive(rows):
    """On Darwin the tag is a floor the build compiles to, so nothing on the
    runner can be asked what it should be — it has to match the constant the
    driver exports as MACOSX_DEPLOYMENT_TARGET.
    """
    expected = platform_tag(
        system="Darwin", machine="arm64", macos_version=MACOS_DEPLOYMENT_TARGET
    )
    row = next(row for row in rows if row["runner"].startswith("macos"))

    assert row["tag"] == expected


def test_every_row_names_the_build_root_the_cache_uses(rows, build_cache, named_step):
    """macOS cannot use /build — the root volume is sealed — and Mach-O records
    absolute install names, so a row whose build root and cache path disagree
    restores a tree the build then rebuilds beside.
    """
    save = named_step("Save build cache")

    for row in rows:
        assert row["build_root"]
    assert build_cache["with"]["path"] == "${{ matrix.build_root }}"
    assert save["with"]["path"] == "${{ matrix.build_root }}"


@pytest.mark.parametrize(
    "name",
    [
        "Create the build directory the cache and the container share",
        "Build wheel",
        "Take ownership of what the container wrote as root",
    ],
)
def test_the_container_only_steps_run_only_for_a_row_with_an_image(named_step, name):
    """The macOS row has no container: there is nothing to mount a build
    directory into and nothing writing as root.
    """
    assert "matrix.image" in named_step(name)["if"]


def test_a_row_without_an_image_builds_on_the_runner(named_step):
    build = named_step("Build Palace on the runner")

    assert build["if"] == "${{ !matrix.image }}"
    assert "scripts/build-macos.sh" in build["run"]
    assert build["env"]["BUILD_ROOT"] == "${{ matrix.build_root }}"


def test_the_readability_guard_covers_every_row(named_step):
    """A silent half-saved cache costs the same hour on any platform, so the
    guard is not the container's: it runs wherever a tree is about to be saved,
    which is why it cannot be spelled with a GNU-only `find`.
    """
    guard = named_step("Check the build tree is readable before saving it")
    save = named_step("Save build cache")

    assert "matrix.image" not in guard["if"]
    assert "wheelbuild.cache_guard" in guard["run"]
    assert "steps.cache-guard.outcome == 'success'" in save["if"]


@pytest.mark.parametrize(
    "name",
    [
        "Smoke test in a clean virtual environment",
        "Launcher interoperability",
        "Report wheel size",
    ],
)
def test_the_wheel_steps_run_only_for_a_row_that_builds_one(named_step, name):
    assert named_step(name)["if"] == "matrix.wheel"


def test_the_artifact_is_uploaded_only_for_a_row_that_builds_a_wheel(steps):
    """An upload of a path that does not exist fails the job, and a row with no
    wheel would otherwise publish nothing under a name the publish job may
    later collect.
    """
    upload = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")
    )

    assert upload["if"] == "matrix.wheel"


def test_a_row_that_builds_no_wheel_still_proves_what_it_built(named_step):
    """Otherwise the macOS row asserts nothing at all: a superbuild that
    installs a binary which cannot run two ranks is not a platform that is
    halfway done, it is a platform that is not started.
    """
    verify = named_step("Verify the install prefix")

    assert verify["if"] == "${{ !matrix.wheel }}"
    assert "scripts/verify-install.sh" in verify["run"]


def test_the_cache_key_covers_the_macos_build_driver(build_cache):
    """It is the whole macOS recipe — toolchain, deployment target, CMake pin —
    so an edit to it changes the bytes in that platform's tree.
    """
    assert "scripts/build-macos.sh" in build_cache["with"]["key"]
