#!/usr/bin/env bash
# THROWAWAY spike driver for wayfinder ticket 04: can Palace, the vendored
# MPICH and the vendored OpenBLAS be built on a free macOS arm64 runner?
#
#   scripts/spike-macos-build.sh <stage>
#
# Stages, in order: env mpich mpi-check mpich-ch3 openblas source superbuild
# verify. Each stage is a separate workflow step so the run page gives one
# status and one wall-clock time per stage, and so a late failure still leaves
# the earlier timings readable.
#
# This deliberately does not touch scripts/build-wheel.sh, which is
# manylinux-only (dnf, patchelf, auditwheel, LD_LIBRARY_PATH, .so sonames).
# It calls the same wheelbuild modules that script calls, through
# scripts/spike_darwin.py, which holds every Darwin deviation in one place.
#
# Environment:
#   TOOLCHAIN   gcc (all Homebrew GCC) or clang (Apple clang plus gfortran)
#   BUILD_ROOT  scratch root (default $HOME/spike-build)
#   JOBS        parallel jobs for MPICH and OpenBLAS (default: hw.ncpu)
#   SUPERBUILD_JOBS  parallel jobs for the superbuild (default: JOBS)
set -euo pipefail

stage="${1:?usage: spike-macos-build.sh <stage>}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_root="${BUILD_ROOT:-$HOME/spike-build}"
toolchain="${TOOLCHAIN:-gcc}"
jobs="${JOBS:-$(sysctl -n hw.ncpu)}"
superbuild_jobs="${SUPERBUILD_JOBS:-$jobs}"

install_prefix="$build_root/install"
superbuild_dir="$build_root/superbuild"
export PYTHONPATH="$repo_root"

palace_version="$(python3 -c 'import re, pathlib; print(re.search(r"__version__ = \"([^\"]+)\"", pathlib.Path("'"$repo_root"'/palace_solver/__init__.py").read_text()).group(1).split(".post")[0])')"
mpich_version="$(python3 -c 'from palace_solver import MPICH_VERSION; print(MPICH_VERSION)')"
openblas_version="$(python3 -c 'from wheelbuild.openblas import OPENBLAS_VERSION; print(OPENBLAS_VERSION)')"

mpich_source="$build_root/mpich-$mpich_version"
openblas_source="$build_root/OpenBLAS-$openblas_version"
palace_source="$build_root/palace-$palace_version"
spheres="$palace_source/examples/spheres/spheres.json"

fetch() { # fetch URL DEST_TARBALL EXTRACT_DIR
  local url="$1" tarball="$2" extracted="$3"
  [[ -d "$extracted" ]] && return 0
  curl -fsSL "$url" -o "$tarball"
  tar -xzf "$tarball" -C "$build_root"
}

case "$stage" in

env)
  # Resolve the toolchain and hand it back as $GITHUB_ENV lines on stdout.
  # Everything human-readable goes to stderr so the two do not mix.
  mkdir -p "$build_root" "$build_root/bin"
  brew_prefix="$(brew --prefix)"

  gfortran=""
  for candidate in gfortran-15 gfortran-14 gfortran-13 gfortran; do
    if command -v "$candidate" >/dev/null 2>&1; then gfortran="$(command -v "$candidate")"; break; fi
  done
  [[ -n "$gfortran" ]] || { echo "no gfortran on this image" >&2; exit 1; }
  # The runner images delete the unversioned symlink on purpose, and a test
  # asserts its absence, so put one on PATH for sub-builds that hardcode it.
  ln -sf "$gfortran" "$build_root/bin/gfortran"

  if [[ "$toolchain" == gcc ]]; then
    suffix="${gfortran##*gfortran}"
    cc="$(command -v "gcc$suffix" || true)"
    cxx="$(command -v "g++$suffix" || true)"
    [[ -n "$cc" && -n "$cxx" ]] || { echo "no gcc$suffix / g++$suffix to match $gfortran" >&2; exit 1; }
    # Homebrew GCC needs the SDK spelled out; Apple clang finds it itself.
    echo "SDKROOT=$(xcrun --show-sdk-path)"
    superbuild_extra=""
  else
    cc="$(command -v clang)"
    cxx="$(command -v clang++)"
    # Apple clang has no OpenMP runtime of its own.
    echo "OpenMP_ROOT=$brew_prefix/opt/libomp"
    superbuild_extra="-DOpenMP_ROOT=$brew_prefix/opt/libomp"
  fi

  echo "SPIKE_CC=$cc"
  echo "SPIKE_CXX=$cxx"
  echo "SPIKE_FC=$gfortran"
  echo "CC=$cc"
  echo "CXX=$cxx"
  echo "FC=$gfortran"
  echo "F77=$gfortran"
  echo "BUILD_ROOT=$build_root"
  echo "PALACE_VERSION=$palace_version"
  echo "SUPERBUILD_EXTRA=$superbuild_extra"
  echo "PATH=$build_root/bin:$install_prefix/bin:$PATH"
  # MACOSX_DEPLOYMENT_TARGET is deliberately left unset: OpenBLAS's automatic
  # NO_SVE=1 on Darwin arm64 sits inside an `ifndef MACOSX_DEPLOYMENT_TARGET`
  # guard, so setting it silently re-enables SVE kernels on hardware that has
  # none. cibuildwheel sets it as a matter of course, which is a finding for
  # ticket 07 rather than something to reproduce here.

  {
    echo "toolchain:  $toolchain"
    echo "cc:         $cc  ($("$cc" --version | head -1))"
    echo "cxx:        $cxx"
    echo "fc:         $gfortran  ($("$gfortran" --version | head -1))"
    echo "cmake:      $(cmake --version | head -1)"
    echo "cpu:        $(sysctl -n machdep.cpu.brand_string) x $(sysctl -n hw.ncpu)"
    echo "memory:     $(( $(sysctl -n hw.memsize) / 1024 / 1024 )) MB"
    echo "jobs:       $jobs (superbuild: $superbuild_jobs)"
    echo "palace:     v$palace_version"
    echo "mpich:      $mpich_version"
    echo "openblas:   $openblas_version"
    df -h "$HOME"
  } >&2
  ;;

mpich)
  fetch "https://www.mpich.org/static/downloads/$mpich_version/mpich-$mpich_version.tar.gz" \
    "$build_root/mpich-$mpich_version.tar.gz" "$mpich_source"
  # No --with-device, exactly as wheelbuild/mpich.py has it, so the spike
  # measures the production recipe: that means ch4:ofi, which is the device
  # MPICH's own CI fails on macOS.
  python3 "$repo_root/scripts/spike_darwin.py" mpich \
    --source-dir "$mpich_source" \
    --build-dir "$build_root/mpich-build" \
    --prefix "$install_prefix" \
    --jobs "$jobs"
  ;;

mpich-ch3)
  # Fallback for risk 2. Only the device changes, so a pass here attributes the
  # failure to ch4:ofi and nothing else.
  rm -rf "$build_root/mpich-build-ch3"
  python3 "$repo_root/scripts/spike_darwin.py" mpich \
    --source-dir "$mpich_source" \
    --build-dir "$build_root/mpich-build-ch3" \
    --prefix "$install_prefix" \
    --jobs "$jobs" \
    --with-device=ch3:nemesis
  ;;

mpi-check)
  # Two ranks through vendored Hydra, before spending an hour on Palace.
  cat > "$build_root/mpi-hello.c" <<'EOF'
#include <mpi.h>
#include <stdio.h>
int main(int argc, char **argv) {
  int rank, size, sum = 0;
  MPI_Init(&argc, &argv);
  MPI_Comm_rank(MPI_COMM_WORLD, &rank);
  MPI_Comm_size(MPI_COMM_WORLD, &size);
  MPI_Allreduce(&rank, &sum, 1, MPI_INT, MPI_SUM, MPI_COMM_WORLD);
  printf("rank %d of %d, allreduce %d\n", rank, size, sum);
  MPI_Finalize();
  return 0;
}
EOF
  "$install_prefix/bin/mpicc" "$build_root/mpi-hello.c" -o "$build_root/mpi-hello"
  echo "== mpicc -show"; "$install_prefix/bin/mpicc" -show
  echo "== otool -L libmpi"; otool -L "$install_prefix/lib/libmpi.12.dylib" | head -20
  echo "== 2-rank run under vendored Hydra"
  timeout 300 "$install_prefix/bin/mpiexec" -n 2 "$build_root/mpi-hello"
  echo "== the PMI_* variables the launcher guard keys off"
  timeout 300 "$install_prefix/bin/mpiexec" -n 2 /usr/bin/env \
    | grep -E '^(PMI|PMIX|HYDRA)' | sort -u || echo "no PMI_*/PMIX_* in the rank environment"
  ;;

openblas)
  fetch "https://github.com/OpenMathLib/OpenBLAS/releases/download/v$openblas_version/OpenBLAS-$openblas_version.tar.gz" \
    "$build_root/OpenBLAS-$openblas_version.tar.gz" "$openblas_source"
  python3 "$repo_root/scripts/spike_darwin.py" openblas \
    --source-dir "$openblas_source" \
    --prefix "$install_prefix" \
    --jobs "$jobs"
  echo "== kernels the DYNAMIC_ARCH build selected from"
  "$install_prefix/bin/openblas_get_config" 2>/dev/null || true
  otool -L "$install_prefix/lib/libopenblas.dylib" | head -20
  ;;

source)
  fetch "https://github.com/awslabs/palace/archive/refs/tags/v$palace_version.tar.gz" \
    "$build_root/palace-$palace_version.tar.gz" "$palace_source"
  # Palace stamps `palace --version` from `git describe`, and a release tarball
  # is not a checkout, so recreate the tag the way build-wheel.sh does.
  if [[ ! -d "$palace_source/.git" ]]; then
    git -c init.defaultBranch=main -C "$palace_source" init --quiet
    git -C "$palace_source" add --all
    git -C "$palace_source" -c user.name=spike -c user.email=spike@localhost \
      commit --quiet --no-verify --message "Palace v$palace_version release tarball"
    git -C "$palace_source" tag "v$palace_version"
  fi
  # Same lib/lib64 unification the production build does, unchanged.
  python3 -m wheelbuild.prefix --prefix "$install_prefix"
  ;;

superbuild)
  # OPENBLAS_DIR is what Palace's ExternalBLASLAPACK.cmake keys off to pick the
  # OpenBLAS vendor instead of Accelerate.
  export OPENBLAS_DIR="$install_prefix"
  # shellcheck disable=SC2086
  python3 "$repo_root/scripts/spike_darwin.py" superbuild \
    --source-dir "$palace_source" \
    --build-dir "$superbuild_dir" \
    --install-prefix "$install_prefix" \
    --prefix "$install_prefix" \
    --jobs "$superbuild_jobs" \
    ${SUPERBUILD_EXTRA:-}
  ;;

verify)
  palace_binary="$(python3 "$repo_root/scripts/spike_darwin.py" palace-binary --install-prefix "$install_prefix")"
  echo "== binary: $palace_binary"
  file "$palace_binary"
  echo "== otool -L"
  otool -L "$palace_binary"
  echo "== palace --version"
  timeout 120 "$palace_binary" --version
  echo "== 1 rank, dry run"
  timeout 600 "$palace_binary" --dry-run "$spheres"
  echo "== 2 ranks, real solve"
  cd "$palace_source/examples/spheres"
  timeout 1800 "$install_prefix/bin/mpiexec" -n 2 "$palace_binary" "$spheres" | tail -40
  echo "== installed tree size"
  du -sh "$install_prefix"
  du -sh "$build_root"
  ;;

*)
  echo "unknown stage: $stage" >&2
  exit 2
  ;;
esac
