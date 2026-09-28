# Throwaway spike: Palace on a macOS arm64 runner

This branch is disposable. **Do not merge it.** It exists to answer one
question: can the Palace superbuild, the vendored MPICH and the vendored
OpenBLAS be built on a *free* macOS arm64 GitHub Actions runner, and how far
from the manylinux recipe does the build have to move?

Three files, all of them spike-only:

- `.github/workflows/spike-macos.yml` — one job per toolchain (all-Homebrew-GCC
  and Apple-clang-plus-gfortran), staged so each stage reports its own status
  and wall-clock time. No `actions/cache`, on purpose: the repository is
  already over GitHub's 10 GB cache cap, and a spike must not evict the
  x86_64 `/build` caches that real pull requests depend on.
- `scripts/spike-macos-build.sh` — the stage driver. It does not touch
  `scripts/build-wheel.sh`, which is manylinux-only, but it calls the same
  `wheelbuild` modules in the same order.
- `scripts/spike_darwin.py` — every Darwin deviation, in one place, so the
  count of them is itself a finding: ELF sonames in the MPICH and OpenBLAS
  validators, `is_elf` in `find_palace_binary`, and the absence of any hook for
  an extra MPICH device or CMake argument.

Two risks the spike is built to attribute rather than merely trip over:

1. **Shared libraries plus OpenMP.** Upstream Palace's macOS matrix covers one
   or the other and never both; the five rows commented out in February 2025
   are exactly the shared-plus-OpenMP ones. `wheelbuild/superbuild.py` sets
   both, and `BUILD_SHARED_LIBS=ON` is not negotiable for a wheel.
2. **The vendored MPICH's device.** `wheelbuild/mpich.py` passes no
   `--with-device`, so it gets `ch4:ofi`, which is the only configuration
   failing in MPICH's own macOS CI. The spike builds the production recipe
   first and only then retries with `ch3:nemesis`, changing nothing else, so a
   pass on the retry attributes the failure to the device alone.

`MACOSX_DEPLOYMENT_TARGET` is left unset throughout: OpenBLAS's automatic
`NO_SVE=1` on Darwin arm64 sits inside an `ifndef` guard on that variable, so
setting it re-enables SVE kernels for hardware that has none.
