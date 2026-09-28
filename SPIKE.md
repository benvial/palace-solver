# Spike: manylinux aarch64 (wayfinder ticket 05)

Throwaway branch. Never merged. Delete once ticket 05 records its answer.

## What is being measured

Whether `scripts/build-wheel.sh` — the production recipe, unchanged — produces
a working `py3-none-manylinux_2_28_aarch64` wheel inside
`quay.io/pypa/manylinux_2_28_aarch64` on a free `ubuntu-24.04-arm` runner, and
what a cold build costs there in wall clock.

This is the evidence option (iii) in the cibuildwheel map rests on. cibuildwheel
has no Linux cross-compile support, so it buys nothing for aarch64 that a
native arm runner does not already give. If the existing script works on an arm
runner with little more than an image swap, the cibuildwheel case has to be
carried by macOS alone.

## Deviations from production, and why each one exists

1. **`SPIKE_PLATFORM_TAG` in `wheelbuild/assemble.py`.** `PLATFORM_TAG` is the
   bare string `"manylinux_2_28_x86_64"` with no architecture component, so
   `auditwheel repair --plat` and `wheel tags --platform-tag` would both be
   handed x86_64 for an aarch64 payload. The spike reads an environment
   override instead of deciding the production spelling; whether that becomes
   `platform.machine()` or a threaded-through parameter is the migration's
   call, not this measurement's.

2. **No `actions/cache`.** The repository is already at 10.35 GB against
   GitHub's hard 10 GB cap (ticket 09), and the production cache key in
   `wheels.yml` carries no architecture component, so a cached spike would
   collide with the x86_64 entries and make real pull requests pay a cold
   30-60 minute build. Cold is also the number ticket 05 asks for.

Nothing else is patched. Anything further the build turns out to need is a
finding to record, not a fix to apply here.

## Jobs

- **`openblas-target`** (~10 minutes) answers ticket 03's second flag on its
  own, without waiting for the superbuild: `wheelbuild/openblas.py` passes
  `DYNAMIC_ARCH=1` with no `TARGET`, and on aarch64 OpenBLAS detects the build
  host. `DYNAMIC_ARCH` only dispatches the kernels; the common code is compiled
  for `TARGET`, so if that comes out above `ARMV8` the "one wheel runs on any
  CPU" invariant in `CONTEXT.md` does not hold on aarch64. The job builds the
  production recipe and a `TARGET=ARMV8` variant and prints
  `OPENBLAS_CORE`/`OPENBLAS_CONFIG` from each install's `openblas_config.h`.

- **`wheel`** runs `scripts/build-wheel.sh` with every output line stamped with
  seconds since the start, so the `==>` banners the script already prints turn
  into per-stage wall clock without editing it. Then the production smoke and
  launcher-interoperability tests, the wheel tag and size, and the logs.

## What ticket 03 said to watch

- **STRUMPACK is the weakest cell** — no aarch64 job in its own CI, no
  conda-forge feedstock, not in Debian, and upstream Palace's arm superbuild is
  static and serial and built without it.
- **`ubuntu-24.04-arm`** is what upstream Palace avoids, pinning its arm job to
  `ubuntu-22.04-arm` over "flaky arm builds with ubuntu 24" with no linked
  issue. Different configuration, but it is the first lead if this behaves
  oddly.
- **PyPI `mpich`** publishes `manylinux_2_28_aarch64` for both the `<5` and
  `>=5` series, so every stage of `scripts/interop-test.sh` has a foreign
  launcher to run against here — checked before the run, not assumed.
