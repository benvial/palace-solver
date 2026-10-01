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

# DEVIATION: METIS, ParMETIS, ScaLAPACK and others are configured by a bare
# `${CMAKE_COMMAND} <SOURCE_DIR>` with no -G, so they take CMake's Windows
# default (NMake) instead of the superbuild's generator. CMake reads this
# variable whenever no -G is given (run 36917961846: "Running 'nmake' '-?'
# failed").
export CMAKE_GENERATOR="MSYS Makefiles"

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
  # Run 36907972682 compiled OpenBLAS about 5x slower than Linux at the same
  # -j4, which is per-object rather than per-file-size. Split the cost: an
  # MSYS2 fork, a native process start, and gcc compiling an empty file.
  probe="$(mktemp -d)"
  : >"$probe/empty.c"
  time (for _ in $(seq 50); do sh -c true; done)
  time (for _ in $(seq 50); do cmd //c "exit 0"; done)
  time (for _ in $(seq 50); do gcc -c -O2 "$probe/empty.c" -o "$probe/empty.o"; done)
  # Run 36917961846: those three cost 26, 50 and 58 ms each, so process
  # overhead is not the 1.3 s per object. OpenBLAS's common.h includes
  # <windows.h> on Windows; time a translation unit that does only that.
  printf '#include <windows.h>\n' >"$probe/windows.c"
  time (for _ in $(seq 10); do gcc -c -O2 "$probe/windows.c" -o "$probe/windows.o"; done)
  powershell -NoProfile -Command \
    'Get-MpComputerStatus | Select-Object RealTimeProtectionEnabled, AntivirusEnabled | Format-List' || true
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
  # PATCH: Palace's own sources. palace-memoryreporting is the one POSIX-only
  # file ticket 02 found; palace-<dep>-patch-step wires a dependency patch
  # (<dep>-*.diff, copied into extern/patch/<dep>/) into that dependency's
  # ExternalProject, the way upstream already patches MFEM or MUMPS.
  for diff in "$repo_root"/scripts/spike-windows-patches/libxsmm-*.diff; do
    mkdir -p "$source_dir/extern/patch/libxsmm"
    cp "$diff" "$source_dir/extern/patch/libxsmm/patch_${diff##*/libxsmm-}"
  done
  for patch in "$repo_root"/scripts/spike-windows-patches/palace-*.patch; do
    if git -C "$source_dir" apply --reverse --check "$patch" 2>/dev/null; then
      echo "already applied: $(basename "$patch")"
    elif git -C "$source_dir" apply --check "$patch"; then
      git -C "$source_dir" apply "$patch"
      echo "applied $(basename "$patch")"
      # A cached checkout of the dependency predates its new patch step.
      dep="${patch##*/palace-}"
      dep="${dep%-patch-step.patch}"
      if [[ "$dep" != "${patch##*/palace-}" ]]; then
        rm -rf "$superbuild_dir/extern/$dep" "$superbuild_dir/extern/$dep-cmake"
        echo "discarded the cached $dep checkout"
      fi
    else
      echo "ERROR: $(basename "$patch") neither applies nor is applied" >&2
      exit 1
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
  # A sub-project configured before CMAKE_GENERATOR was exported cached NMake
  # as its generator, and CMake refuses to switch an existing build tree.
  grep -l 'CMAKE_GENERATOR:INTERNAL=NMake' -r "$superbuild_dir/extern" --include=CMakeCache.txt 2>/dev/null \
    | while read -r cache; do
        echo "discarding NMake-configured $(dirname "$cache")"
        rm -rf "$cache" "$(dirname "$cache")/CMakeFiles"
      done
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
