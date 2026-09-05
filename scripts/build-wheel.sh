#!/usr/bin/env bash
# Build the palace-solver wheel. Runs *inside* a manylinux_2_28 container.
#
#   scripts/build-wheel.sh [PALACE_VERSION]
#
# Environment:
#   BUILD_ROOT   scratch root for sources and the superbuild (default /build);
#                keep it on a cached volume to reuse the dependency tree
#   CCACHE_DIR   ccache directory (default /build/ccache)
#   JOBS         parallel build jobs (default: nproc)
#   OUTPUT_DIR   where the repaired wheel is written (default <repo>/wheelhouse)
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
palace_version="${1:-$(python3 -c 'import re,pathlib; print(re.search(r"PALACE_VERSION = \"([^\"]+)\"", pathlib.Path("'"$repo_root"'/palace_solver/__init__.py").read_text()).group(1))')}"
build_root="${BUILD_ROOT:-/build}"
output_dir="${OUTPUT_DIR:-$repo_root/wheelhouse}"
jobs="${JOBS:-$(nproc)}"
export CCACHE_DIR="${CCACHE_DIR:-$build_root/ccache}"

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
# OpenBLAS is built here with DYNAMIC_ARCH so one wheel runs on any x86-64 CPU.
openblas_version="$(PYTHONPATH="$repo_root" python3 -c 'from wheelbuild.openblas import OPENBLAS_VERSION; print(OPENBLAS_VERSION)')"
openblas_source="$build_root/OpenBLAS-$openblas_version"
if [[ ! -d "$openblas_source" ]]; then
  curl -fsSL "https://github.com/OpenMathLib/OpenBLAS/releases/download/v$openblas_version/OpenBLAS-$openblas_version.tar.gz" \
    -o "$build_root/OpenBLAS-$openblas_version.tar.gz"
  tar -xzf "$build_root/OpenBLAS-$openblas_version.tar.gz" -C "$build_root"
fi
if [[ ! -f "$install_prefix/lib/libopenblas.so" ]]; then
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
