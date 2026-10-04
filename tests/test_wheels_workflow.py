"""Facts about the wheel matrix that are cheaper to assert than to run.

The wheel job takes 30-60 minutes per platform, so a mistyped row is an
expensive way to find out. These are the properties that hold for every row and
that a second platform made possible to get wrong.
"""

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from wheelbuild import matrix
from wheelbuild.link_check import PEFILE_REQUIREMENT
from wheelbuild.platforms import (
    MACOS_DEPLOYMENT_TARGET,
    platform_tag,
    supported_platform_tags,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "wheels.yml"


def _step_index(job, needle):
    """Where a step naming ``needle`` sits in ``job``'s step list.

    Both publishing jobs are checked for the order of the same two steps, and
    the thing that identifies a step is either what it runs or what it uses, so
    the two are searched together.

    Args:
        job: A parsed workflow job.
        needle: Substring of the step's ``run`` or ``uses``.

    Returns:
        The index of the first matching step.
    """
    return next(
        index
        for index, step in enumerate(job["steps"])
        if needle in str(step.get("run", "")) + str(step.get("uses", ""))
    )


def _publish_step(job):
    """The step in ``job`` that uploads to an index."""
    return next(
        step for step in job["steps"] if "pypi-publish" in str(step.get("uses", ""))
    )


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def rows():
    """The matrix rows, which live in .github/wheel-matrix.toml."""
    return matrix.rows()


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


def test_the_msmpi_fetch_stays_out_of_the_cache_key(build_cache):
    """The MS-MPI installer is fetched on every run, its bytes pinned by hash,
    and the build tree links against MSYS2's import library rather than these
    files. Keying the module would send every row cold for an edit that cannot
    change what was built.
    """
    assert "wheelbuild/msmpi.py" not in build_cache["with"]["key"]


def test_the_checks_job_fetches_and_verifies_msmpi(workflow):
    """The real download is what proves the recorded hashes are Microsoft's."""
    runs = [step.get("run", "") for step in workflow["jobs"]["checks"]["steps"]]

    assert any("python -m wheelbuild.msmpi" in run for run in runs)


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
        "Build wheel in the container",
        "Take ownership of what the container wrote as root",
    ],
)
def test_the_container_only_steps_run_only_for_a_row_with_an_image(named_step, name):
    """The macOS row has no container: there is nothing to mount a build
    directory into and nothing writing as root.
    """
    assert "matrix.image" in named_step(name)["if"]


def test_a_row_without_an_image_builds_on_the_runner(named_step):
    build = named_step("Build wheel on the runner")

    assert build["if"] == "${{ !matrix.image && runner.os != 'Windows' }}"
    assert "scripts/build-macos.sh" in build["run"]
    assert build["env"]["BUILD_ROOT"] == "${{ matrix.build_root }}"


def test_the_windows_row_builds_under_msys2(named_step):
    """MSYS2's bash, not the job's Git for Windows bash: the driver needs its
    toolchain, make and POSIX python on PATH, and MSYSTEM set."""
    build = named_step("Build wheel under MSYS2")

    assert build["if"] == "runner.os == 'Windows'"
    assert build["shell"] == "msys2 {0}"
    assert "scripts/build-windows.sh" in build["run"]
    assert build["env"]["BUILD_ROOT"] == "${{ matrix.build_root }}"


def test_msys2_is_provisioned_as_adr_0007_fixes_it(named_step):
    """The image's MSYS2, nothing newer, and no package cache of its own
    against the shared 10 GB budget."""
    setup = named_step("Provision MSYS2 UCRT64")

    assert setup["if"] == "runner.os == 'Windows'"
    assert setup["uses"].startswith("msys2/setup-msys2@")
    assert setup["with"]["msystem"] == "UCRT64"
    for key in ("release", "update", "cache"):
        assert setup["with"][key] is False
    packages = setup["with"]["install"].split()
    for package in ("gcc", "gcc-fortran", "libgomp", "ccache", "pkgconf", "msmpi"):
        assert f"mingw-w64-ucrt-x86_64-{package}" in packages
    for package in ("make", "git", "patch", "python"):
        assert package in packages


def test_the_steps_that_run_on_windows_do_not_call_python3(named_step):
    """The Windows toolcache has no python3, and the job's bash would find the
    Store alias or nothing."""
    for name in (
        "Resolve Palace version and platform tag",
        "Check the build tree is readable before saving it",
        "Check the wheel's metadata the way PyPI will",
        "Report wheel size",
    ):
        assert "python3" not in named_step(name)["run"]


def test_the_wheel_job_runs_its_steps_in_bash(workflow):
    """Windows would otherwise run every shared step in PowerShell."""
    assert workflow["jobs"]["wheel"]["defaults"]["run"]["shell"] == "bash"


def test_the_windows_row_claims_the_tag_the_build_would_derive(rows):
    expected = platform_tag(system="Windows", machine="AMD64")
    row = next(row for row in rows if row["runner"].startswith("windows"))

    assert row["tag"] == expected == "win_amd64"


def test_the_windows_build_root_is_short_and_on_d(rows):
    """Tools without a long-path manifest stop at MAX_PATH, and C: is slower."""
    row = next(row for row in rows if row["tag"] == "win_amd64")

    assert row["build_root"] == "D:\\b"


def test_every_keyed_file_is_checked_out_with_lf_on_every_runner(build_cache):
    """hashFiles hashes the checkout's bytes. A CRLF checkout on Windows gave
    that row's key a different hash from the one the Linux pruner computes, so
    main's cleanup would have deleted the live Windows entry on every push."""
    keyed = re.findall(r"'([^']+)'", _hashed_files(build_cache["with"]["key"]))
    result = subprocess.run(
        ["git", "check-attr", "eol", "--", *keyed],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [f"{path}: eol: lf" for path in keyed]


def test_the_cache_key_covers_the_windows_build_driver(build_cache):
    assert "scripts/build-windows.sh" in build_cache["with"]["key"]


def test_the_carried_patches_stay_out_of_the_cache_key(build_cache):
    """An edited patch resets only its own tree (wheelbuild/patches.py); keying
    the directory would send the whole Windows row cold instead."""
    key = build_cache["with"]["key"]

    assert "patches" not in key


# -- the matrix is computed, so a dispatch can narrow it ---------------------


def test_the_wheel_matrix_is_the_plan_jobs_rows(workflow):
    wheel = workflow["jobs"]["wheel"]

    assert wheel["needs"] == ["plan"]
    assert wheel["strategy"]["matrix"] == {
        "include": "${{ fromJSON(needs.plan.outputs.rows) }}"
    }


def test_the_plan_job_reads_the_rows_with_wheelbuild_matrix(workflow):
    plan = workflow["jobs"]["plan"]
    select = next(step for step in plan["steps"] if step.get("id") == "rows")

    assert plan["outputs"]["rows"] == "${{ steps.rows.outputs.rows }}"
    assert "python3 -m wheelbuild.matrix" in select["run"]
    assert "windows_only" in select["env"]["ONLY"]
    assert "win_amd64" in select["env"]["ONLY"]


def test_only_a_dispatch_can_narrow_the_matrix(workflow):
    """Pull requests and pushes always build every row: narrowing on anything
    else would be the path-filter gating ADR-0007 rules out."""
    plan = workflow["jobs"]["plan"]
    select = next(step for step in plan["steps"] if step.get("id") == "rows")

    assert select["env"]["ONLY"].startswith(
        "${{ github.event_name == 'workflow_dispatch' && inputs.windows_only"
    )


def test_a_windows_only_dry_run_is_refused_before_anything_builds(workflow):
    plan = workflow["jobs"]["plan"]
    refuse = next(step for step in plan["steps"] if "Refuse" in step.get("name", ""))

    assert "inputs.windows_only && inputs.testpypi" in refuse["if"]
    assert "exit 1" in refuse["run"]


def test_matrix_select_keeps_every_row_without_a_narrowing(rows):
    assert matrix.select(rows, None) == rows
    assert [row["tag"] for row in matrix.select(rows, "win_amd64")] == ["win_amd64"]


def test_matrix_select_refuses_a_tag_no_row_has(rows):
    """A narrowing that selects nothing would be a run that builds nothing and
    reports success."""
    with pytest.raises(matrix.MatrixError, match="win_arm64"):
        matrix.select(rows, "win_arm64")


def test_matrix_main_writes_the_rows_to_the_job_output(tmp_path, monkeypatch, rows):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert matrix.main(["--only", "win_amd64"]) == 0

    name, value = output.read_text().strip().split("=", 1)
    assert name == "rows"
    assert value == json.dumps(matrix.select(rows, "win_amd64"))


def _replacement(named_step):
    return named_step("Drop the superseded Windows entry this iteration run restored")


def test_an_iteration_run_replaces_its_entry_before_it_builds(steps, named_step):
    """Deleting after the save is too late: for the length of the build the
    branch holds two Windows entries, and with main's three that is over the
    cap, where eviction takes one of main's."""
    names = [step.get("name") for step in steps]
    replace = names.index(_replacement(named_step)["name"])

    assert names.index("Restore build cache") < replace
    assert replace < names.index("Build wheel under MSYS2")


def test_the_replacement_deletes_only_this_branchs_superseded_key(named_step):
    replace = _replacement(named_step)

    assert "inputs.windows_only" in replace["if"]
    assert "github.ref != 'refs/heads/main'" in replace["if"]
    assert (
        "steps.build-cache.outputs.cache-matched-key != "
        "steps.build-cache.outputs.cache-primary-key"
    ) in replace["if"]
    assert replace["env"]["MATCHED"] == (
        "${{ steps.build-cache.outputs.cache-matched-key }}"
    )
    assert 'gh cache delete "$MATCHED"' in replace["run"]
    assert '--ref "$GITHUB_REF"' in replace["run"]
    assert "--all" not in replace["run"]


def test_only_the_replacement_step_is_handed_the_token(workflow, steps, named_step):
    """The job needs actions: write for that step alone; the build runs code it
    downloads, so the token is neither in its environment nor in .git/config."""
    wheel = workflow["jobs"]["wheel"]
    holders = [step for step in steps if "github.token" in str(step.get("env", {}))]

    assert wheel["permissions"] == {"contents": "read", "actions": "write"}
    assert holders == [_replacement(named_step)]
    checkout = steps[0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["persist-credentials"] is False


def test_both_build_steps_write_their_wheel_where_the_shared_steps_look(named_step):
    """The steps after the build name `wheelhouse/*.whl` with no row value in
    the path, so the two drivers have to agree about it without being told.
    """
    smoke = named_step("Smoke test in a clean virtual environment")

    assert "wheelhouse/*.whl" in smoke["run"]
    for name in (
        "Build wheel in the container",
        "Build wheel on the runner",
        "Build wheel under MSYS2",
    ):
        assert "OUTPUT_DIR" not in named_step(name).get("env", {})


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
    ["Smoke test in a clean virtual environment", "Report wheel size"],
)
def test_the_wheel_steps_run_for_every_row(named_step, name):
    """Every platform now emits a wheel, so these are unconditional. A row that
    built one and tested nothing would be the worst of the three states.
    """
    assert "if" not in named_step(name)


def test_the_size_step_fails_on_a_wheel_pypi_would_reject(named_step):
    """The step writes the job summary, so it is the size verdict a human
    reads -- and a step that prints OVER and exits 0 is how an oversized wheel
    reaches a tag that cannot be taken back. The build already refuses one
    (`wheelbuild.assemble.verify_size`); this is the same refusal on the
    directory the artifact is uploaded from.
    """
    run = named_step("Report wheel size")["run"]

    assert "sys.exit(0)" not in run
    assert "exceeds_pypi_limit" in run


def test_the_artifact_is_uploaded_for_every_row(steps):
    """One artifact per row, whatever the publish job currently collects: a gate
    here would drop a platform silently, since an artifact that was never
    uploaded is not an error until something downloads it.
    """
    upload = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")
    )

    assert "if" not in upload


def test_every_row_proves_the_launcher(rows, named_step):
    """A platform that builds a wheel it cannot launch ranks with is not done,
    so this step runs everywhere rather than on the platforms that happened to
    have a foreign mpiexec first. The `interop` row value that gated it is gone
    rather than set true everywhere: a gate nothing closes is how a platform
    comes to be tested on one branch and not another.
    """
    assert "if" not in named_step("Launcher interoperability")
    for row in rows:
        assert "interop" not in row


def test_no_row_carries_a_flag_for_whether_it_builds_a_wheel(rows):
    """It existed for the one row that stopped at an install prefix. Keeping it
    once every row builds one leaves a gate nothing closes, which is how a
    platform comes to be tested on one branch and not another.
    """
    for row in rows:
        assert "wheel" not in row


def test_the_row_shape_differs_only_by_whether_there_is_a_container(rows):
    """Every row carries the same values but `image`, which is the one genuine
    difference in kind between the platforms — no container on macOS. Any other
    divergence is a step that one platform silently skips.
    """
    keys = {frozenset(row) - {"image"} for row in rows}

    assert len(keys) == 1


def test_the_cache_key_covers_the_macos_build_driver(build_cache):
    """It is the whole macOS recipe — toolchain, deployment target, CMake pin —
    so an edit to it changes the bytes in that platform's tree.
    """
    assert "scripts/build-macos.sh" in build_cache["with"]["key"]


@pytest.fixture(scope="module")
def publish(workflow):
    return workflow["jobs"]["publish"]


@pytest.fixture(scope="module")
def upload(steps):
    return next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")
    )


@pytest.fixture(scope="module")
def download(publish):
    return next(
        s
        for s in publish["steps"]
        if str(s.get("uses", "")).startswith("actions/download-artifact")
    )


def test_the_supported_platform_set_is_exactly_the_matrix_rows(rows):
    """`wheelbuild.platforms` is what the release check counts wheels against,
    and the matrix is what builds them. A platform added to one and not the
    other publishes a release short a wheel, or fails a release that is
    complete.
    """
    assert set(supported_platform_tags()) == {row["tag"] for row in rows}


def test_publish_collects_every_row_by_pattern_not_by_name(upload, download):
    """Naming one artifact is how the job came to collect the x86_64 wheel
    alone. The pattern is the upload's own name with the row value wildcarded,
    so a renamed artifact cannot be collected by one and missed by the other.
    """
    assert "name" not in download["with"]
    assert download["with"]["pattern"] == upload["with"]["name"].replace(
        "${{ matrix.tag }}", "*"
    )


def test_publish_merges_the_artifacts_into_one_directory(download):
    """Without this each artifact lands in a subdirectory of its own and the
    upload step, which publishes a flat directory, finds no wheel at all.
    """
    assert download["with"]["merge-multiple"] is True
    assert download["with"]["path"] == "dist"


def test_publish_waits_for_every_platform(publish):
    """Charting decision 1: one release, all three wheels. A row that fails
    fails the `wheel` job, and a dependent job with no always()/!cancelled()
    override is then skipped -- which is the only thing standing between a
    partial matrix and an unrepairable release.
    """
    assert set(publish["needs"]) == {"checks", "wheel", "wheel-regression"}
    assert publish["if"] == "startsWith(github.ref, 'refs/tags/v')"


def test_publish_checks_the_wheel_set_before_uploading(publish):
    """The download step treats matching no artifact as a warning and the
    upload step publishes whatever the directory holds, so nothing between them
    would notice a missing platform.
    """
    assert _step_index(publish, "release_check") < _step_index(publish, "pypi-publish")


@pytest.fixture(scope="module")
def dry_run(workflow):
    return workflow["jobs"]["publish-testpypi"]


def test_the_dry_run_is_never_triggered_by_a_tag(workflow, dry_run):
    """The dry run exists to happen *before* the tag. A condition that also
    matched `refs/tags/v*` would upload the same three wheels to two indexes
    from one event, which is the opposite of a rehearsal: the thing it is meant
    to de-risk would already have happened by the time it reported.
    """
    assert "refs/tags" not in dry_run["if"]
    assert dry_run["if"] == (
        "github.event_name == 'workflow_dispatch' && inputs.testpypi"
    )
    assert workflow[True]["workflow_dispatch"]["inputs"]["testpypi"]["default"] is False


def test_the_dry_run_waits_for_every_platform_as_the_real_publish_does(
    dry_run, publish
):
    """It proves nothing about a three-wheel release if it can run on a subset,
    and the whole question it answers -- does this *set* of filenames upload --
    needs the set to be complete.
    """
    assert set(dry_run["needs"]) == set(publish["needs"])


def test_the_dry_run_collects_the_wheels_the_way_the_real_publish_does(
    dry_run, download
):
    """A rehearsal that assembles its directory differently rehearses a
    different upload.
    """
    collect = next(
        step
        for step in dry_run["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact")
    )

    assert collect["with"] == download["with"]


def test_the_dry_run_checks_the_wheel_set_before_uploading(dry_run):
    """Same gate, same order as `publish`: an incomplete directory would upload
    a subset to TestPyPI and report that a three-wheel release is proven.
    """
    assert _step_index(dry_run, "release_check") < _step_index(dry_run, "pypi-publish")


def test_the_dry_run_uploads_to_testpypi_and_the_real_publish_does_not(
    dry_run, publish
):
    """One mistyped repository-url and the dry run is the release."""
    rehearsal = _publish_step(dry_run)

    assert rehearsal["with"]["repository-url"] == "https://test.pypi.org/legacy/"
    assert "repository-url" not in _publish_step(publish).get("with", {})


def test_the_dry_run_does_not_skip_a_file_already_on_testpypi(dry_run):
    """skip-existing would make the rehearsal repeatable where the event it
    rehearses is not: a green run over a version TestPyPI already holds proves
    nothing about the wheels in the directory. The remedy for a collision is a
    .postN bump, which costs nothing before a tag exists.
    """
    assert "skip-existing" not in _publish_step(dry_run).get("with", {})


def test_the_dry_run_uses_its_own_environment_and_no_token(dry_run, publish):
    """Trusted publishing on both sides -- a TestPyPI API token on the
    workstation is the thing this project has avoided from the start. The
    environments are separate because they are separate publishers and a
    required reviewer on the release gate should not also gate a rehearsal.
    """
    assert dry_run["permissions"] == {"id-token": "write"}
    assert dry_run["environment"] != publish["environment"]


def test_the_metadata_is_validated_on_every_row_before_any_upload(named_step):
    """`twine check` is the half of the TestPyPI dry run that needs no index:
    PyPI validates a wheel's metadata and its rendered description more
    strictly than `wheel` does, and finding that out at upload time is finding
    it out after the tag exists.
    """
    check = named_step("Check the wheel's metadata the way PyPI will")

    assert "if" not in check
    assert "twine check --strict" in check["run"]


@pytest.fixture(scope="module")
def releasing():
    """The release procedure, which moved out of the README in favour of a
    document an operator reads start to finish."""
    return (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dispatch_inputs(workflow):
    # PyYAML reads the `on:` key as the boolean True.
    return workflow[True]["workflow_dispatch"]["inputs"]


def test_the_procedure_names_the_input_a_release_operator_has_to_tick(
    releasing, dispatch_inputs
):
    """The release procedure tells an operator to run the workflow with one
    named input, by its label in the Actions UI and by its name on the command
    line. A renamed input leaves that instruction describing a flag the
    workflow does not have, and the operator finds that out mid-release.
    """
    assert set(dispatch_inputs) == {"testpypi", "windows_only"}, (
        "a further input would need its own check here"
    )

    assert "-f testpypi=true" in releasing
    assert dispatch_inputs["testpypi"]["description"] in releasing


def test_the_iteration_input_is_off_by_default_and_says_it_is_no_release(
    dispatch_inputs, releasing
):
    """windows_only is a developer's input, so the release procedure must not
    name it, and its label must say why."""
    windows_only = dispatch_inputs["windows_only"]

    assert windows_only["default"] is False
    assert "never a release" in windows_only["description"]
    assert "windows_only" not in releasing


def test_the_procedure_names_both_publishing_environments(releasing, dry_run, publish):
    """Each environment is half of a trusted publisher registered outside this
    repository against that exact string, so the procedure's prerequisite list
    is the only place the names can be checked against the jobs that use them.
    """
    for environment in (dry_run["environment"], publish["environment"]):
        assert f"`{environment}`" in releasing


def test_a_dispatched_run_cannot_be_cancelled_by_a_push_to_the_same_ref(workflow):
    """The dry run is dispatched against main, so without the event in the
    concurrency group it shares one with every push to main and
    cancel-in-progress throws away whichever started first -- three
    40-minute builds, or the rehearsal a release is waiting on.
    """
    group = workflow["concurrency"]["group"]

    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert "github.event_name" in group
    assert "github.ref" in group


#: The linter's version is pinned rather than floating, and the pin is spelled
#: in two places that install it -- the `checks` job and the ``dev`` extra a
#: contributor installs from. ``==`` rather than a lower bound: the failure this
#: prevents is a *new* release adding a rule, which a floor does not hold back.
RUFF_REQUIREMENT = re.compile(r"ruff==(\d+\.\d+\.\d+)")


def test_the_checks_job_pins_the_linter(workflow):
    """An unpinned ruff turns a green branch red without a commit touching its
    code, and makes a local run unable to predict CI. It has happened twice on
    this repository: 0.16 began formatting Python inside Markdown, and 0.16.9
    added ISC004. A pin moves both into a commit that says so.
    """
    install = next(
        step["run"]
        for step in workflow["jobs"]["checks"]["steps"]
        if "pip install" in str(step.get("run", ""))
    )

    assert RUFF_REQUIREMENT.search(install), install


def test_the_pinned_linter_is_the_one_a_contributor_installs(workflow):
    """Two spellings of the version is how "it passed locally" and "it failed in
    CI" come to be true at once. The dev extra is what a contributor installs,
    so it has to name the version the gate runs.
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        extra = tomllib.load(handle)["project"]["optional-dependencies"]["dev"]
    install = next(
        step["run"]
        for step in workflow["jobs"]["checks"]["steps"]
        if "pip install" in str(step.get("run", ""))
    )

    declared = {requirement for requirement in extra if requirement.startswith("ruff")}
    assert declared == {RUFF_REQUIREMENT.search(install).group(0)}


def test_the_pe_reader_the_tests_run_is_the_one_the_build_installs(workflow):
    """pefile is the Windows link check and repair, not a test helper, so the
    checks job, the dev extra and the constant the Windows driver installs from
    name one version. A drift between them is tests passing against a parser
    the build does not run.
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        extra = tomllib.load(handle)["project"]["optional-dependencies"]["dev"]
    install = next(
        step["run"]
        for step in workflow["jobs"]["checks"]["steps"]
        if "pip install" in str(step.get("run", ""))
    )

    declared = {
        requirement for requirement in extra if requirement.startswith("pefile")
    }
    assert declared == {PEFILE_REQUIREMENT}
    assert f'"{PEFILE_REQUIREMENT}"' in install, install


@pytest.mark.parametrize(
    ("name", "script", "twin"),
    [
        (
            "Smoke test in a clean virtual environment",
            "scripts/smoke-test.sh",
            "python scripts/smoke-test-windows.py",
        ),
        (
            "Launcher interoperability",
            "scripts/interop-test.sh",
            "python scripts/interop-test-windows.py --install-msmpi",
        ),
    ],
)
def test_windows_runs_the_python_twin_of_each_wheel_test(
    named_step, name, script, twin
):
    """The Windows row runs the twin, on the setup-python interpreter, and every
    other row still runs the bash script it always ran."""
    run = named_step(name)["run"]

    assert '[[ "$RUNNER_OS" == Windows ]]' in run
    assert f'{twin} "$wheel" "$config"' in run
    assert f'{script} "$wheel" "$config"' in run
    assert "python3" not in run
    assert (ROOT / twin.split()[1]).is_file()


def test_the_smoke_test_runs_before_the_interop_test_installs_msmpi(steps):
    """The smoke test is the run with no MS-MPI on the machine but the wheel's."""
    names = [step.get("name") for step in steps]

    assert names.index("Smoke test in a clean virtual environment") < names.index(
        "Launcher interoperability"
    )


def test_the_size_summary_path_is_read_from_the_environment(named_step):
    """Spliced into the Python source, a Windows path's `\\a` is a bell."""
    run = named_step("Report wheel size")["run"]

    assert "os.environ['GITHUB_STEP_SUMMARY']" in run
    assert "${GITHUB_STEP_SUMMARY}" not in run


GATE_STEPS = (
    "Fingerprint what the upstream test gate tests",
    "Look for a regression pass on these binaries",
    "Upstream test gate, unit tests",
    "Upstream test gate, unit tests on Windows",
    "Upstream test gate, regression",
    "Upstream test gate, regression on Windows",
    "Write the regression pass record",
    "Save the regression pass record",
)

#: Each sweep, as the step that runs it on Linux and macOS and the one that runs
#: it on Windows.
SWEEPS = {
    "Upstream test gate, unit tests": "Upstream test gate, unit tests on Windows",
    "Upstream test gate, regression": "Upstream test gate, regression on Windows",
}


@pytest.mark.parametrize(
    "name",
    [name for name in GATE_STEPS if name not in SWEEPS and name not in SWEEPS.values()],
)
def test_the_shared_gate_steps_run_on_every_row(named_step, name):
    """All four rows run the gate (palace-tests ticket 08)."""
    assert "runner.os" not in named_step(name).get("if", "")


@pytest.mark.parametrize(("other", "windows"), SWEEPS.items())
def test_each_sweep_runs_on_windows_under_msys2_and_elsewhere_under_bash(
    named_step, other, windows
):
    """Windows runs ctest under MSYS2's bash, as its build did."""
    assert named_step(other)["if"].startswith("runner.os != 'Windows'")
    assert named_step(windows)["if"].startswith("runner.os == 'Windows'")
    assert named_step(windows)["shell"] == "msys2 {0}"
    assert (
        named_step(other)["timeout-minutes"] == named_step(windows)["timeout-minutes"]
    )


def test_windows_judges_on_the_setup_python_interpreter(named_step):
    """MSYS2's own python names another platform than win_amd64."""
    for name in SWEEPS.values():
        assert '"$(cygpath -u "$pythonLocation")/python.exe"' in named_step(name)["run"]


def test_the_gate_runs_after_the_cache_save_and_before_the_upload(steps):
    names = [step.get("name") for step in steps]
    upload = _step_index({"steps": steps}, "actions/upload-artifact")

    assert names.index("Save build cache") < names.index(GATE_STEPS[0])
    assert names.index("Report wheel size") < names.index(GATE_STEPS[0])
    assert [names.index(name) for name in GATE_STEPS] == sorted(
        names.index(name) for name in GATE_STEPS
    )
    assert names.index(GATE_STEPS[-1]) < upload


def test_the_gate_keeps_its_ceilings(named_step):
    """Ticket 04 of the palace-tests effort. Raising one is a decision, not an edit."""
    for other, windows in SWEEPS.items():
        ceiling = 5 if "unit tests" in other else 60
        assert named_step(other)["timeout-minutes"] == ceiling
        assert named_step(windows)["timeout-minutes"] == ceiling
    script = (ROOT / "scripts" / "upstream-test-gate.sh").read_text()
    assert "unit_timeout=300" in script
    assert "timeout=1200" in script


def test_windows_cuts_a_hung_unit_entry_short():
    """The excluded symlink case hangs on two ranks under MS-MPI (palace-tests
    ticket 08); left to the 300 s entry timeout it spends the whole step."""
    script = (ROOT / "scripts" / "upstream-test-gate.sh").read_text()
    windows = script[script.index("MINGW* | MSYS*)\n    jobs=") :]

    assert "unit_timeout=150" in windows[: windows.index(";;")]


def test_the_windows_gate_runs_the_ctest_the_build_pinned():
    """The gate script finds ctest in the CMake directory the driver unpacked."""
    driver = (ROOT / "scripts" / "build-windows.sh").read_text()
    version = re.search(r"^cmake_version=(\S+)$", driver, re.MULTILINE).group(1)
    script = (ROOT / "scripts" / "upstream-test-gate.sh").read_text()

    assert f"cmake-{version}-windows-x86_64/bin" in script


def test_regression_runs_only_without_a_pass_on_these_binaries(named_step):
    lookup = named_step("Look for a regression pass on these binaries")
    save = named_step("Save the regression pass record")

    assert lookup["with"]["lookup-only"] is True
    assert lookup["with"]["key"] == save["with"]["key"]
    assert lookup["with"]["path"] == save["with"]["path"]
    assert "steps.gate.outputs.fingerprint" in save["with"]["key"]
    assert "env.PLATFORM_TAG" in save["with"]["key"]
    for name in (
        "Upstream test gate, regression",
        "Upstream test gate, regression on Windows",
        "Write the regression pass record",
        "Save the regression pass record",
    ):
        assert "steps.gate-passed.outputs.cache-hit != 'true'" in named_step(name)["if"]


@pytest.mark.parametrize("name", [*SWEEPS, *SWEEPS.values()])
def test_every_sweep_is_judged_against_the_exclusions(named_step, name):
    assert "wheelbuild.upstream_gate judge" in named_step(name)["run"]


def test_the_gate_scripts_stay_out_of_the_cache_key(build_cache):
    """They run ctest and change nothing that is cached, so iterating on them
    must not cost every row a rebuild."""
    keyed = _hashed_files(build_cache["with"]["key"])

    assert "upstream-test-gate" not in keyed
    assert "upstream_gate" not in keyed
    assert "test-exclusions" not in keyed


# --------------------------------------------------------------------------
# The wheel regression run (palace-tests tickets 09 and 11)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wheel_run(workflow):
    return workflow["jobs"]["wheel-regression"]


@pytest.fixture(scope="module")
def wheel_run_step(wheel_run):
    def find(name):
        return next(step for step in wheel_run["steps"] if step.get("name") == name)

    return find


WHEEL_RUN_DUE = "steps.wheel-passed.outputs.cache-hit != 'true'"


def test_the_wheel_run_is_a_job_per_row_after_the_wheels(workflow, wheel_run):
    """Its own job, on a fresh runner of each row the plan selected."""
    assert set(wheel_run["needs"]) == {"plan", "wheel"}
    assert (
        wheel_run["strategy"]["matrix"]
        == workflow["jobs"]["wheel"]["strategy"]["matrix"]
    )
    assert wheel_run["strategy"]["fail-fast"] is False
    assert wheel_run["runs-on"] == "${{ matrix.runner }}"


def test_the_wheel_run_tests_the_rows_own_artifact(wheel_run, upload):
    download = next(
        step
        for step in wheel_run["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact")
    )

    assert download["with"]["name"] == upload["with"]["name"]


def test_the_wheel_run_never_skips_at_job_level(wheel_run, publish):
    """A pass record skips the solve steps, not the job, so `publish` needs no
    always() to go ahead on a tag that reuses main's record."""
    assert "if" not in wheel_run
    assert "always()" not in publish["if"]


def test_the_wheel_run_solves_only_without_a_pass_on_this_payload(wheel_run_step):
    lookup = wheel_run_step("Look for a wheel regression pass on this payload")
    save = wheel_run_step("Save the wheel regression pass record")

    assert lookup["with"]["lookup-only"] is True
    assert lookup["with"]["key"] == save["with"]["key"]
    assert lookup["with"]["path"] == save["with"]["path"]
    assert save["with"]["key"].startswith("wheel-regression-passed-${{ matrix.tag }}-")
    assert "steps.payload.outputs.fingerprint" in save["with"]["key"]
    for name in (
        "Install the wheel in a clean virtual environment",
        "Fetch Palace's regression inputs",
        "Wheel regression run",
        "Write the wheel regression pass record",
        "Save the wheel regression pass record",
    ):
        assert WHEEL_RUN_DUE in wheel_run_step(name)["if"]


def test_the_wheel_run_keeps_its_ceiling(wheel_run_step):
    """Ticket 09: raising it is a scope decision, not an edit."""
    assert wheel_run_step("Wheel regression run")["timeout-minutes"] == 30


def test_the_wheel_runs_evidence_is_never_published(wheel_run_step, download):
    """The failed cases' outputs must not match the pattern `publish` collects."""
    evidence = wheel_run_step("Upload the failed cases' outputs")
    pattern = download["with"]["pattern"].rstrip("*")

    assert not evidence["with"]["name"].startswith(pattern)
    assert evidence["with"]["retention-days"] == 7
    assert evidence["if"].startswith("failure()")
