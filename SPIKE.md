# Spike: the build cache under cibuildwheel (wayfinder ticket 06)

Throwaway branch. Never merged. Delete once ticket 06 records its answer.

## What is being measured

Property 1 of the cibuildwheel map says the `/build` GitHub Actions cache must
survive any migration: a cold build is 30-60+ minutes and every pull request
would otherwise pay it. Ticket 01 found that cibuildwheel's `before-all` runs
once per container but does not persist across CI runs, so the only route to a
cross-run cache is the `/host` mount (`--volume=/:/host`, confirmed in
`cibuildwheel/oci_container.py`). Upstream has no first-class support for this;
issues #1285 and #1030 are open requests for it.

Two things ticket 01 left unconfirmed, both load-bearing:

1. **Do writes into `/host` survive back to the runner, and can
   `actions/cache/save` pick them up?** The container runs as root; the rest of
   the workflow does not. The only upstream evidence is an anecdote.
2. **What happens when `before-all` takes 30-60 minutes?** Nothing upstream
   documents a `before-all` of that length.

## The two jobs

`.github/workflows/spike-cibw-cache.yml`, dispatched by hand.

- **`probe`** — minutes, not hours. `before-all` writes two trees into the
  `/host` mount: a world-readable one with a 256 MB payload, a symlink and an
  executable, and a deliberately hostile one (a root-owned `0600` file inside a
  `0700` directory). Each gets its own cache entry so one failing does not hide
  the other. The job then reports ownership and modes as the runner user sees
  them, tries to read both trees, and tars each one the way `actions/cache`
  does. Run it twice: the second run's restore is the thing under test.
- **`full`** — the production recipe driven through cibuildwheel with
  `BUILD_ROOT=/host/build`, one interpreter (`cp313-manylinux_x86_64`), the
  runner's `/build` under `actions/cache`, and the wheel proved afterwards by
  the existing smoke and launcher-interoperability scripts. Run it twice for
  the same reason.

## Deviations from production, and why each one exists

1. **`SPIKE_STOP_AFTER_NOTICES` in `scripts/build-wheel.sh`.** Under
   cibuildwheel the wheel build, the repair and the retag belong to
   cibuildwheel, so the production script has to stop after the notices step
   and hand over. A real migration would split the script rather than gate it;
   the gate keeps the spike's diff to one block.

2. **`--plat-name` passed to the build frontend.** This project's backend is
   plain setuptools with a binary payload in `package-data`, so it emits
   `py3-none-any`, and `cibuildwheel/platforms/linux.py:378` rejects a
   `none-any` wheel *before* `repair-wheel-command` can retag it. The spike
   passes `--config-setting=--build-option=--plat-name=manylinux_2_28_x86_64`,
   which yields `cp313-cp313-manylinux_2_28_x86_64`; the repair step retags it
   to `py3-none` as production does.

3. **Staging split out into `before-build`.** `wheelbuild.assemble.build()`
   stages the payload into the package directory and builds the wheel in one
   call. cibuildwheel owns the build, so the staging half runs as `before-build`
   and only `repair_command()` and `retag_command()` remain in
   `repair-wheel-command` — which is exactly ticket 01's claim that
   `assemble.py` drops in unchanged, tested rather than assumed.

4. **`PATH` fixed up in `before-all`.** cibuildwheel puts
   `/opt/python/cp39-cp39/bin` first for `before-all`, so `python3` there is 3.9
   rather than the container interpreter a plain `container:` job gets. The
   script puts `/usr/local/bin` back in front.

5. **A spike-only cache key.** The repository is at GitHub's 10 GB cache cap
   (ticket 09), so this entry competes with the production superbuild cache for
   the budget and is deleted once the ticket has its answer.

6. **No `container:` job.** cibuildwheel runs on the runner and drives the
   container itself, which is the migration side effect ticket 01 noted: it
   moves `actions/cache` back onto the host.
