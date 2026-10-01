#!/usr/bin/env bash
# THROWAWAY stage driver for wayfinder ticket 07 (windows-support). Lives only
# on research/windows-spike and is never merged.
#
#   scripts/spike-windows-build.sh STAGE
#
# Runs under MSYS2 UCRT64 bash (`shell: msys2 {0}`): the compile half of the
# recipe ticket 06 fixed. Each stage is one workflow step, so the run's summary
# says which stage failed and how long each one took. Stages:
#
#   env        log the toolchain, the package set and D:'s free space
#   openblas   the vendored OpenBLAS, with the Linux x86_64 recipe verbatim
#   source     Palace's release tarball, as a git checkout, with spike patches
#   configure  configure the superbuild (static, MSYS Makefiles, MS-MPI)
#   target T   build one superbuild target (libCEED, gslib, strumpack, ...)
#   superbuild build everything that is left, Palace included
#   inspect    the installed binary: PE imports, size, the runtime DLL set
#
# Every deviation from the Linux recipe is marked "DEVIATION:" so that
# `grep -c DEVIATION:` over this file is the count ticket 08 reads.
set -euo pipefail

if [[ "${MSYSTEM:-}" != "UCRT64" ]]; then
  echo "ERROR: run under MSYS2 UCRT64 bash, not ${MSYSTEM:-a non-MSYS2 shell}" >&2
  exit 1
fi

stage="${1:?usage: spike-windows-build.sh STAGE [TARGET]}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# DEVIATION: ticket 03 — the build root must sit on D: and be short, because
# unmanifested tools still bind at MAX_PATH and C: is 40-50% slower.
build_root="${BUILD_ROOT:-/d/b}"
jobs="${JOBS:-$(nproc)}"
palace_version="$(python -c 'import re,sys; print(re.search(r"__version__ = \"([^\"]+)\"", open(sys.argv[1]).read()).group(1).split(".post")[0])' "$(cygpath -w "$repo_root/palace_solver/__init__.py")" | tr -d '\r')"
source_dir="$build_root/palace-$palace_version"
superbuild_dir="$build_root/superbuild"
install_prefix="$build_root/install"
export CCACHE_DIR="${CCACHE_DIR:-$build_root/ccache}"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-2G}"
# Native tools (cmake, gcc, python) get Windows paths with forward slashes.
win() { cygpath -m "$1"; }
# Native python on Windows writes CRLF; bash keeps the CR unless told not to.
wheelbuild() { PYTHONPATH="$(win "$repo_root")" python "$@" | tr -d '\r'; }

# DEVIATION: CMake 3.31.8 from Kitware's Windows zip, not MSYS2's rolling CMake
# 4.x (which rejects the cmake_minimum_required floors several sub-projects
# declare). 3.31.8 is upstream Palace's own macOS pin (FindMPI with GNU, #470).
cmake_dir="$build_root/cmake-3.31.8-windows-x86_64"
export PATH="$cmake_dir/bin:$PATH"

mkdir -p "$build_root" "$CCACHE_DIR"

case "$stage" in
env)
  echo "BUILD_ROOT=$build_root"
  echo "PALACE_VERSION=$palace_version"
  echo "JOBS=$jobs"
  df -h /d /c || true
  if [[ ! -x "$cmake_dir/bin/cmake.exe" ]]; then
    curl -fsSL -o "$build_root/cmake.zip" \
      https://github.com/Kitware/CMake/releases/download/v3.31.8/cmake-3.31.8-windows-x86_64.zip
    unzip -q -o "$build_root/cmake.zip" -d "$build_root"
  fi
  for tool in gcc g++ gfortran make cmake python ccache mpicc; do
    printf '%-9s %s\n' "$tool" "$(command -v "$tool" || echo MISSING)"
  done
  gcc --version | head -1
  gfortran --version | head -1
  cmake --version | head -1
  pacman -Q
  ;;

openblas)
  version="$(wheelbuild -c 'from wheelbuild.openblas import OPENBLAS_VERSION; print(OPENBLAS_VERSION)')"
  source="$build_root/OpenBLAS-$version"
  if [[ ! -f "$install_prefix/bin/libopenblas.dll" ]]; then
    if [[ ! -d "$source" ]]; then
      curl -fsSL "$(wheelbuild -c 'from wheelbuild.openblas import source_url; print(source_url())')" \
        -o "$build_root/OpenBLAS-$version.tar.gz"
      tar -xzf "$build_root/OpenBLAS-$version.tar.gz" -C "$build_root"
    fi
    # DEVIATION: wheelbuild.platforms has no Windows entry, so the recipe is
    # asked for as Linux x86_64 — which is the point: the argument vector is
    # the Linux one, byte for byte (ticket 02: Makefile.x86_64 is identical).
    mapfile -t make_args < <(wheelbuild -c '
from wheelbuild.openblas import build_arguments
print("\n".join(build_arguments(jobs='"$jobs"', system="Linux", machine="x86_64")))')
    echo "${make_args[*]}"
    (cd "$source" && "${make_args[@]}")
    (cd "$source" && make install PREFIX="$(win "$install_prefix")" NO_STATIC=1)
  fi
  # DEVIATION: the DLL lands in bin/, beside an import library in lib/, so
  # wheelbuild.openblas.required_artefacts needs a PE branch.
  ls -la "$install_prefix/bin" "$install_prefix/lib" | grep -i openblas
  objdump -p "$install_prefix/bin/libopenblas.dll" | grep 'DLL Name' | sort -u
  ;;

source)
  if [[ ! -d "$source_dir" ]]; then
    curl -fsSL "https://github.com/awslabs/palace/archive/refs/tags/v$palace_version.tar.gz" \
      -o "$build_root/palace-$palace_version.tar.gz"
    tar -xzf "$build_root/palace-$palace_version.tar.gz" -C "$build_root"
  fi
  if [[ ! -d "$source_dir/.git" ]]; then
    git -c init.defaultBranch=main -C "$source_dir" init --quiet
    git -C "$source_dir" add --all
    git -C "$source_dir" -c user.name=palace-solver -c user.email=palace-solver@localhost \
      commit --quiet --no-verify --message "Palace v$palace_version release tarball"
    git -C "$source_dir" tag "v$palace_version"
  fi
  # PATCH: Palace's own C++ — the one POSIX-only file ticket 02 found.
  for patch in "$repo_root"/scripts/spike-windows-patches/palace-*.patch; do
    if git -C "$source_dir" apply --check "$patch" 2>/dev/null; then
      git -C "$source_dir" apply "$patch"
      echo "applied $(basename "$patch")"
    else
      echo "already applied (or stale): $(basename "$patch")"
    fi
  done
  ;;

configure)
  mapfile -t features < <(wheelbuild -c '
from wheelbuild.superbuild import FEATURE_FLAGS
print("\n".join(FEATURE_FLAGS))')
  # DEVIATION: ticket 06 decision 6 — static on Windows. Every other flag is
  # the production FEATURE_FLAGS tuple unchanged.
  features=("${features[@]/-DBUILD_SHARED_LIBS=ON/-DBUILD_SHARED_LIBS=OFF}")
  export OPENBLAS_DIR="$(win "$install_prefix")"
  mkdir -p "$superbuild_dir"
  # DEVIATION: the generator. Palace runs `${CMAKE_MAKE_PROGRAM} VAR=value
  # install` for libCEED, GSLIB and LIBXSMM, which needs GNU make and sh.
  # No MPI_HOME: MS-MPI comes from MSYS2's mingw-w64-msmpi (headers, import
  # library, mpicc wrappers) and FindMPI is left to find it — what it picks is
  # in the configure log.
  (cd "$superbuild_dir" && cmake -G "MSYS Makefiles" \
    -DCMAKE_INSTALL_PREFIX="$(win "$install_prefix")" \
    -DCMAKE_PREFIX_PATH="$(win "$install_prefix")" \
    "${features[@]}" \
    -DCMAKE_C_COMPILER_LAUNCHER=ccache \
    -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
    -DCMAKE_Fortran_COMPILER_LAUNCHER=ccache \
    "$(win "$source_dir")")
  grep -E '^MPI_|^BLAS_LAPACK|^CMAKE_(C|CXX|Fortran)_COMPILER:' "$superbuild_dir/CMakeCache.txt" || true
  ;;

target)
  target="${2:?usage: spike-windows-build.sh target TARGET}"
  (cd "$superbuild_dir" && make -j"$jobs" "$target")
  ccache --show-stats || true
  ;;

superbuild)
  (cd "$superbuild_dir" && make -j"$jobs")
  ccache --show-stats || true
  ;;

inspect)
  ls -la "$install_prefix/bin"
  binary="$(find "$install_prefix/bin" -maxdepth 1 -name 'palace-*.bin' | head -1)"
  [[ -n "$binary" ]] || { echo "ERROR: no palace-*.bin installed" >&2; exit 1; }
  echo "binary: $binary ($(stat -c %s "$binary") bytes)"
  echo "PE imports:"
  objdump -p "$binary" | grep 'DLL Name' | sort -u
  ;;

*)
  echo "ERROR: unknown stage $stage" >&2
  exit 1
  ;;
esac
