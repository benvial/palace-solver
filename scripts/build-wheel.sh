#!/usr/bin/env bash
# Build the palace-solver wheel. Runs *inside* a manylinux_2_28 container.
#
#   scripts/build-wheel.sh [PALACE_VERSION]
#
# Environment:
#   BUILD_ROOT   scratch root for sources and the superbuild (default /build);
#                keep it on a cached volume to reuse the dependency tree
#   CCACHE_DIR   ccache directory (default /build/ccache)
#   CCACHE_MAXSIZE
#                ccache size bound (default 2G); ccache's own default is
#                unbounded growth, and this directory sits inside a build tree
#                that has to fit the 10 GB GitHub Actions cache budget
#   JOBS         parallel build jobs (default: nproc)
#   OUTPUT_DIR   where the repaired wheel is written (default <repo>/wheelhouse)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The Palace release is __version__ — the one place the version is written
# down — minus any .postN packaging segment: a .post release ships the same
# Palace, and upstream has no tag for the suffixed number.
palace_version="${1:-$(python3 -c 'import re,pathlib; print(re.search(r"__version__ = \"([^\"]+)\"", pathlib.Path("'"$repo_root"'/palace_solver/__init__.py").read_text()).group(1).split(".post")[0])')}"
build_root="${BUILD_ROOT:-/build}"
output_dir="${OUTPUT_DIR:-$repo_root/wheelhouse}"
jobs="${JOBS:-$(nproc)}"
export CCACHE_DIR="${CCACHE_DIR:-$build_root/ccache}"
# ccache earns its place on the fallback restore, where a changed cache key
# lands on an older tree and ccache is what keeps the partial rebuild cheap.
# Bounded so the cached tree is a size chosen rather than observed.
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-2G}"
# The Palace source tree below is a git repository, and Palace's CMake runs
# `git describe` in it at configure time to stamp `palace --version`. It is
# created once and then restored from a cache — or, locally, from a directory
# the developer owns — while this script runs as root inside the container, so
# git sees a repository someone else owns and refuses it with "detected dubious
# ownership". The failure is quiet in the worst way: `palace --version` reports
# UNKNOWN, the stamp check below then finds a mismatch and reconfigures the
# superbuild on every single run. Set through the environment rather than
# `git config --global` so it applies to the git CMake spawns as well and
# touches no file.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="*"

source_dir="$build_root/palace-$palace_version"
superbuild_dir="$build_root/superbuild"
install_prefix="$build_root/install"
venv="$build_root/venv"

mkdir -p "$build_root" "$CCACHE_DIR" "$output_dir"

# ZFP and friends install into lib64 on RHEL-family systems while Palace links
# <prefix>/lib, so the two spellings are made the same directory up front.
PYTHONPATH="$repo_root" python3 -m wheelbuild.prefix --prefix "$install_prefix"

echo "==> toolchain"
dnf install -y ccache patchelf >/dev/null

echo "==> build environment (python tooling)"
python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
"$venv/bin/pip" install --quiet build auditwheel wheel
export PATH="$venv/bin:$PATH"
# The vendored MPICH and Palace share one install prefix; auditwheel resolves
# the payload's libraries from there.
export LD_LIBRARY_PATH="$install_prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

echo "==> MPICH (vendored into the wheel)"
# The PyPI mpich wheel is C-only, and Palace needs Fortran MPI for MUMPS,
# ARPACK and STRUMPACK, so MPICH is built here and shipped in the wheel.
mpich_version="$(PYTHONPATH="$repo_root" python3 -c 'from palace_solver import MPICH_VERSION; print(MPICH_VERSION)')"
mpich_source="$build_root/mpich-$mpich_version"
if [[ ! -d "$mpich_source" ]]; then
  curl -fsSL "https://www.mpich.org/static/downloads/$mpich_version/mpich-$mpich_version.tar.gz" \
    -o "$build_root/mpich-$mpich_version.tar.gz"
  tar -xzf "$build_root/mpich-$mpich_version.tar.gz" -C "$build_root"
fi
if [[ ! -f "$install_prefix/lib/libmpifort.so.12" ]]; then
  PYTHONPATH="$repo_root" python3 -m wheelbuild.mpich \
    --source-dir "$mpich_source" \
    --build-dir "$build_root/mpich-build" \
    --prefix "$install_prefix" \
    --jobs "$jobs"
fi
export PATH="$install_prefix/bin:$PATH"

echo "==> OpenBLAS (vendored into the wheel)"
# Palace requires a system BLAS/LAPACK and the manylinux image has none, so
# OpenBLAS is built here: DYNAMIC_ARCH so one binary picks its kernels at run
# time, and, on arm, a named TARGET so the common objects around those kernels
# are compiled for the oldest CPU the wheel claims instead of for the runner.
# See BASELINE_TARGETS in wheelbuild/openblas.py.
openblas_version="$(PYTHONPATH="$repo_root" python3 -c 'from wheelbuild.openblas import OPENBLAS_VERSION; print(OPENBLAS_VERSION)')"
openblas_source="$build_root/OpenBLAS-$openblas_version"
openblas_tarball="$build_root/OpenBLAS-$openblas_version.tar.gz"
unpack_openblas() {
  if [[ ! -f "$openblas_tarball" ]]; then
    curl -fsSL "$(PYTHONPATH="$repo_root" python3 -c 'from wheelbuild.openblas import source_url; print(source_url())')" \
      -o "$openblas_tarball"
  fi
  rm -rf "$openblas_source"
  tar -xzf "$openblas_tarball" -C "$build_root"
}
[[ -d "$openblas_source" ]] || unpack_openblas
# The question is not whether libopenblas is installed, it is what it was
# compiled for -- the same shape as the Palace stamp check below, and for the
# same reason. A restored tree can carry an OpenBLAS built before the CPU
# baseline was pinned; the exact cache key changing is no protection, because
# the fallback restore-keys are what hand that tree back. The verdicts are
# CHECK_NO_INSTALL and CHECK_WRONG_CPU in wheelbuild/openblas.py.
openblas_verdict=0
PYTHONPATH="$repo_root" python3 -m wheelbuild.openblas \
  --prefix "$install_prefix" --check || openblas_verdict=$?
# Enumerated rather than "not zero", and the same case the macOS driver uses:
# an unhandled exception exits 1 and a verdict this script predates could be
# anything, and neither is an instruction to spend 40 minutes rebuilding a
# library whose state was never established. tests/test_openblas.py ties these
# numbers to the constants.
case "$openblas_verdict" in
  0) ;;
  6) ;;
  3)
    # `make` does not notice a changed TARGET: the objects in a restored source
    # tree keep the -march they were compiled with, so building in place would
    # reinstall the same wrong code. Re-extracting is the only clean that cannot
    # leave one behind. The tarball stays, so this costs an unpack, not a
    # download -- and only this verdict pays it, because a merely absent install
    # has no wrongly compiled objects to discard.
    echo "==> discarding an OpenBLAS tree configured for another CPU baseline"
    unpack_openblas
    ;;
  *)
    echo "::error::OpenBLAS check returned $openblas_verdict; not rebuilding" >&2
    exit 1
    ;;
esac
if (( openblas_verdict != 0 )); then
  PYTHONPATH="$repo_root" python3 -m wheelbuild.openblas \
    --source-dir "$openblas_source" \
    --prefix "$install_prefix" \
    --jobs "$jobs"
fi
# Palace's ExternalBLASLAPACK.cmake keys off this to select the OpenBLAS vendor.
export OPENBLAS_DIR="$install_prefix"

echo "==> Palace $palace_version sources"
if [[ ! -d "$source_dir" ]]; then
  curl -fsSL "https://github.com/awslabs/palace/archive/refs/tags/v$palace_version.tar.gz" \
    -o "$build_root/palace-$palace_version.tar.gz"
  tar -xzf "$build_root/palace-$palace_version.tar.gz" -C "$build_root"
fi
# Palace stamps `palace --version` from `git describe` at configure time and
# prints UNKNOWN when the source is not a checkout, which a release tarball is
# not. Recreating the release tag locally makes it report v$palace_version.
if [[ ! -d "$source_dir/.git" ]]; then
  git -c init.defaultBranch=main -C "$source_dir" init --quiet
  git -C "$source_dir" add --all
  git -C "$source_dir" -c user.name=palace-solver -c user.email=palace-solver@localhost \
    commit --quiet --no-verify --message "Palace v$palace_version release tarball"
  git -C "$source_dir" tag "v$palace_version"
fi

# The tag only reaches the binary through a Palace reconfigure, and the cache
# can hand back a tree that is tagged but whose Palace was configured before
# the tag existed — 0.18.1 shipped exactly that. So the question is not whether
# the repository is there, it is what the installed binary reports; when that
# disagrees, the Palace stamps go and the superbuild configures it again.
installed_palace="$(PYTHONPATH="$repo_root" python3 -c '
import pathlib, sys
from wheelbuild.assemble import find_palace_binary

try:
    print(find_palace_binary(pathlib.Path(sys.argv[1])))
except (FileNotFoundError, OSError):
    pass
' "$install_prefix")"
if [[ -n "$installed_palace" ]] \
  && ! timeout 60 "$installed_palace" --version 2>/dev/null | grep -q "Palace version: v$palace_version"; then
  echo "==> installed Palace is not stamped v$palace_version, reconfiguring it"
  rm -f "$superbuild_dir"/palace-cmake/src/palace-stamp/palace-{configure,build,install,done} \
    "$superbuild_dir/CMakeFiles/palace-complete"
fi

echo "==> superbuild (dependency tree cached in $superbuild_dir)"
# Palace's top-level CMake project is the superbuild: building it also installs
# Palace and its dependencies into CMAKE_INSTALL_PREFIX.
PYTHONPATH="$repo_root" python3 -m wheelbuild.superbuild \
  --source-dir "$source_dir" \
  --build-dir "$superbuild_dir" \
  --install-prefix "$install_prefix" \
  --prefix "$install_prefix" \
  --jobs "$jobs"

echo "==> third-party notices"
PYTHONPATH="$repo_root" python3 -m wheelbuild.notices \
  --source-root "$superbuild_dir" \
  --source-root "$mpich_source" \
  --source-root "$openblas_source" \
  --output "$build_root/THIRD-PARTY-NOTICES"

echo "==> wheel assembly, repair, retag"
PYTHONPATH="$repo_root" python3 -m wheelbuild.assemble \
  --project-dir "$repo_root" \
  --install-prefix "$install_prefix" \
  --output-dir "$output_dir" \
  --notices "$build_root/THIRD-PARTY-NOTICES"
