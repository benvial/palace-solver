#!/usr/bin/env bash
# Build the pypalace-solver wheel. Runs *inside* a manylinux_2_28 container.
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
palace_version="${1:-$(python3 -c 'import re,pathlib; print(re.search(r"PALACE_VERSION = \"([^\"]+)\"", pathlib.Path("'"$repo_root"'/pypalace_solver/__init__.py").read_text()).group(1))')}"
build_root="${BUILD_ROOT:-/build}"
output_dir="${OUTPUT_DIR:-$repo_root/wheelhouse}"
jobs="${JOBS:-$(nproc)}"
export CCACHE_DIR="${CCACHE_DIR:-$build_root/ccache}"

source_dir="$build_root/palace-$palace_version"
superbuild_dir="$build_root/superbuild"
install_prefix="$build_root/install"
venv="$build_root/venv"

mkdir -p "$build_root" "$CCACHE_DIR" "$output_dir"

echo "==> toolchain"
dnf install -y ccache patchelf >/dev/null

echo "==> build environment (python + mpich wheel)"
python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
# The binary links against this exact mpich wheel; users get it from the
# pypalace dependency tree at install time.
"$venv/bin/pip" install --quiet "mpich<5" build auditwheel wheel
export PATH="$venv/bin:$PATH"
# auditwheel resolves libmpi here before excluding it from the wheel.
export LD_LIBRARY_PATH="$venv/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

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
  --prefix "$venv" \
  --jobs "$jobs"

echo "==> third-party notices"
PYTHONPATH="$repo_root" python3 -m wheelbuild.notices \
  --source-root "$superbuild_dir" \
  --output "$build_root/THIRD-PARTY-NOTICES"

echo "==> wheel assembly, repair, retag"
PYTHONPATH="$repo_root" python3 -m wheelbuild.assemble \
  --project-dir "$repo_root" \
  --install-prefix "$install_prefix" \
  --output-dir "$output_dir" \
  --notices "$build_root/THIRD-PARTY-NOTICES"
