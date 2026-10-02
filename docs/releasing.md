# Releasing

`palace_solver.__version__` is the only place the version is written down;
`pyproject.toml`, the build scripts and CI all read it from there. It mirrors
the Palace release the wheel ships, with a `.postN` segment for a
packaging-only fix that ships the same Palace — `0.17.0`, then `0.17.0.post1`.
Palace's own release is what decides the first three numbers; nothing here
gets to choose them. The build scripts and `PALACE_VERSION` drop the `.postN`
segment, since upstream has no tag for it — a `.post` release rebuilds the
same Palace.

The release tag is `v` plus that version, spelled identically: `v0.17.0`,
`v0.17.0.post1`. `python -m wheelbuild.tag_check <tag>` enforces the match, and
CI runs it on tag pushes before the build, because a tag that disagrees with
the packaged version publishes a release under a number nobody chose and PyPI
never lets a filename be reused.

Per release:

1. Bump `palace_solver.__version__`, and `MPICH_VERSION` if the vendored MPICH
   moved. Commit.
2. Rehearse the upload: run the `wheels` workflow manually with the
   **Upload the four wheels to TestPyPI as a release dry run** input ticked —
   `gh workflow run wheels.yml --ref main -f testpypi=true`. The
   `publish-testpypi` job waits for all four rows, collects the same four
   wheels the release would, and uploads them to TestPyPI through its own
   trusted publisher. Nothing is uploaded from a workstation here either.
3. Tag and push the tag. The tag runs the checks, builds every platform's wheel
   and, on its own runner, smoke-tests it and solves a real example under both
   launchers, then publishes to PyPI.

A tag lands best on a commit `main` has already built: the four rows then
restore a warm superbuild cache and finish in minutes, where a tag on an
unbuilt commit pays a cold build before it can publish — an hour or more on
Linux and macOS, about two hours on Windows.

## The dry run

The dry run is the only thing that exercises what a tag cannot be asked to
retry: a platform tag the index refuses, a file over the per-file size limit,
and metadata PyPI validates more strictly than the `wheel` module that wrote
it. `twine check --strict` runs on every row of every pull request and covers
the metadata half without an index, so the dry run is there for the half that
needs a real one.

The verdict is "TestPyPI accepted the four files", and nothing is installed
back out of it. That is a deliberate limit, not an omission: installing a
wheel is only meaningful on a machine of its own platform, and each platform
already installs its own wheel into a clean virtual environment and solves a
real example on the row that built it. What the dry run adds is the index's
opinion, which is the only thing that row cannot ask for. It is also only
*its own* trusted publisher that it proves: the `testpypi` environment and the
TestPyPI publisher, not the `release` pair the tag will use. Those two are
registered the same way at the same time, so a working rehearsal is evidence
about the procedure rather than about the credentials.

TestPyPI reuses a filename no more than PyPI does, so each dry run spends the
four filenames of the version it carries, and the job carries no
`skip-existing`: a green run over a version TestPyPI already holds would say
nothing about the wheels in the directory. If a dry run has to be repeated —
including after one that uploaded some of the four and then failed — bump
`palace_solver.__version__` to the next `.postN` and run it again. That is free
before a tag exists, and it is the same move a half-published release on PyPI
would force.

## All or nothing

A release is all or nothing across the platforms. The `publish` job waits for
every matrix row, collects each row's artifact by pattern into one directory,
and `python -m wheelbuild.release_check dist` refuses the upload unless that
directory holds exactly one wheel per supported platform and nothing else. A
row that failed skips the job rather than publishing a subset, because a
release missing a platform cannot be repaired — PyPI never lets a filename be
reused, so the only fix is another version number.

Publishing is entirely CI's: the `publish` job runs only for `refs/tags/v*`,
in the `release` environment, and uploads through PyPI's trusted publishing —
no API token lives in this repository, and nothing is uploaded from a
workstation.

Two things must exist outside this repository for that job to work, and are
the first place to look if a release fails at the upload step:

- A trusted publisher registered on PyPI for `palace-solver`, naming this
  repository, the `wheels.yml` workflow and the `release` environment.
- The `release` environment on GitHub. Adding required reviewers to it is how a
  release is made to wait for a human before uploading.

The dry run needs the same two things on the other side, registered once and
then never touched again — a trusted publisher on **TestPyPI** for
`palace-solver` naming this repository, the `wheels.yml` workflow and the
`testpypi` environment, and that `testpypi` environment on GitHub. They are
separate from the release pair on purpose: a required reviewer standing in
front of the real upload should not also stand in front of a rehearsal.

## What a release spends

A release spends two separate PyPI limits. *Per file*, 100 MiB — mebibytes,
not 100 million. At `0.18.1.post1` the three wheels are 68.7 MiB (x86_64),
57.5 MiB (aarch64) and 55.5 MiB (macOS arm64), so the largest uses 69% of that
limit, and the Windows wheel is the smallest, at 47.1 MiB when first built.
The build refuses an oversized wheel rather than reporting one, and CI puts the
size in the job summary; if a later Palace release pushes a wheel over, the
per-file limit is raisable to 1 GiB by a file-size limit
request on PyPI, and that request has to name the concrete wheel — a built
file, not an estimate. *Per project*, a 10 GiB total-storage quota that every
file of every version counts against and that nothing in a build can see: PyPI
held 273.5 MiB on 2026-09-30, which leaves room for roughly 43 further
four-wheel releases, so it is a figure to re-measure every year or so rather
than a constraint to design around.
