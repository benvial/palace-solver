#!/usr/bin/env bash
# Build Palace and everything the wheel vendors, on macOS arm64.
#
#   scripts/build-macos.sh [PALACE_VERSION]
#
# The macOS sibling of scripts/build-in-container.sh: there is no container
# here, so this is both the driver and the recipe. It ends where the Linux
# driver does, at a repaired wheel in OUTPUT_DIR — with delocate in place of
# auditwheel, which reads ELF and writes RPATHs and has nothing to say about a
# Mach-O.
#
# Environment:
#   OUTPUT_DIR   where the repaired wheel is written (default <repo>/wheelhouse)
#   BUILD_ROOT   scratch root for sources, the superbuild and the install
#                prefix (default ~/palace-build). Pin it and keep it: Mach-O
#                records absolute install names, so a tree that moves is a tree
#                that no longer resolves, which is a failure the Linux
#                platforms never see.
#   CCACHE_DIR   ccache directory (default $BUILD_ROOT/ccache)
#   CCACHE_MAXSIZE
#                ccache size bound (default 2G), because this directory sits
#                inside a build tree that has to fit the 10 GB GitHub Actions
#                cache budget shared with two other platforms
#   JOBS         parallel build jobs (default: the machine's core count)
#   CC, CXX, FC  override the toolchain chosen below
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: this is the macOS driver; Linux builds run scripts/build-in-container.sh" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The Palace release is __version__ minus any .postN packaging segment, exactly
# as scripts/build-wheel.sh resolves it.
palace_version="${1:-$(python3 -c 'import re,pathlib; print(re.search(r"__version__ = \"([^\"]+)\"", pathlib.Path("'"$repo_root"'/palace_solver/__init__.py").read_text()).group(1).split(".post")[0])')}"
build_root="${BUILD_ROOT:-$HOME/palace-build}"
jobs="${JOBS:-$(sysctl -n hw.logicalcpu)}"
export CCACHE_DIR="${CCACHE_DIR:-$build_root/ccache}"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-2G}"

# The floor the wheel claims, and the reason it is set at all: with no
# deployment target clang infers one from the SDK, capped at the running
# system, so Palace's own binaries would land above the tag delocate then
# computes and the wheel would claim a macOS it does not run on. Exported for
# the whole build, which is what makes it true of every sub-project.
# wheelbuild.openblas pairs it with NO_SVE=1 — see the note there.
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-$(PYTHONPATH="$repo_root" python3 -c 'from wheelbuild.platforms import MACOS_DEPLOYMENT_TARGET; print(MACOS_DEPLOYMENT_TARGET)')}"

# SDKROOT is deliberately not exported. It broke LIBXSMM's lookup of Apple's
# `as` and cost a whole spike run; the Homebrew toolchain carries its own
# sysroot and wants no help.

# Palace's CMake runs `git describe` in the source tree at configure time to
# stamp `palace --version`, and that tree comes back from a build cache that
# tar restored. See the same block in scripts/build-wheel.sh.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="*"

source_dir="$build_root/palace-$palace_version"
superbuild_dir="$build_root/superbuild"
install_prefix="$build_root/install"
venv="$build_root/venv"
toolchain_bin="$build_root/toolchain-bin"
# Outside the build root, and deliberately: the build root is what the cache
# saves, and a wheel in it would be restored by the next run and picked up as
# though this one had produced it.
output_dir="${OUTPUT_DIR:-$repo_root/wheelhouse}"

mkdir -p "$build_root" "$CCACHE_DIR" "$toolchain_bin" "$output_dir"

PYTHONPATH="$repo_root" python3 -m wheelbuild.prefix --prefix "$install_prefix"

echo "==> toolchain"
# Homebrew GCC, from what the runner image already has. Nothing is installed or
# upgraded here: Homebrew demotes macOS 15 to Tier 3 around September 2027,
# after which `brew install gcc` publishes no arm64_sequoia bottle and compiles
# from source inside an already hour-long job. It also removes a toolchain
# drift no cache key covers.
#
# One toolchain for C, C++ and Fortran, and that is the point rather than a
# convenience. gfortran is mandatory — wheelbuild/mpich.py passes
# -fallow-argument-mismatch and Palace's MUMPS, ARPACK and STRUMPACK are
# Fortran — so mixing in clang for the C++ means OpenBLAS, built with
# USE_OPENMP=1, takes clang's libomp for its C objects while its Darwin shared
# library is linked by $(FC), whose own -fopenmp re-adds -lgomp behind
# Makefile.system's FEXTRALIB substitution. That is OpenBLAS issue #5156 and it
# produces a built library rather than a failed build. wheelbuild.openblas
# refuses such an install, so this stays honest if the choice is ever revisited.
if [[ -n "${CC:-}${CXX:-}${FC:-}" ]]; then
  # All three or none: filling in the rest around a caller's one override is
  # how the C and the Fortran end up from different toolchains, which is the
  # failure this whole block is arranged to avoid.
  if [[ -z "${CC:-}" || -z "${CXX:-}" || -z "${FC:-}" ]]; then
    echo "ERROR: set all of CC, CXX and FC, or none of them" >&2
    exit 1
  fi
else
  for release in 15 14 13; do
    if command -v "gcc-$release" >/dev/null \
      && command -v "g++-$release" >/dev/null \
      && command -v "gfortran-$release" >/dev/null; then
      CC="$(command -v "gcc-$release")"
      CXX="$(command -v "g++-$release")"
      FC="$(command -v "gfortran-$release")"
      export CC CXX FC
      break
    fi
  done
  if [[ -z "${FC:-}" ]]; then
    echo "ERROR: no preinstalled Homebrew GCC found (gcc-15, gcc-14 or gcc-13)" >&2
    exit 1
  fi
fi
# The images ship gfortran only as gfortran-13/14/15 — the unversioned name is
# deleted by the image build and a test there asserts its absence, so it will
# not come back — and sub-projects that spell it bare need it on PATH.
ln -sf "$FC" "$toolchain_bin/gfortran"
export F77="$FC"
export F90="$FC"
echo "CC=$CC"
echo "CXX=$CXX"
echo "FC=$FC"

echo "==> build environment (python tooling)"
# CMake is pinned inside 3.31: upstream pins 3.31.8 on macOS for a FindMPI bug
# with GNU compilers (Palace issue #470) and PyPI has no 3.31.8. Staying in the
# series also keeps CMake 4 away, which rejects the cmake_minimum_required
# floors several sub-projects still declare.
# --clear rather than reuse: the venv lives in the cached build root and its
# interpreter is an absolute symlink into the runner's Python toolcache, which
# moves on every patch bump. `python3 -m venv` over a tree whose bin/python3 is
# a dangling symlink does not repair it — it refuses — so a cached venv
# would wedge this job on every rerun of an unchanged cache key.
python3 -m venv --clear "$venv"
"$venv/bin/pip" install --quiet --upgrade pip
# The delocate floor is wheelbuild.assemble's rather than a literal here: it is
# the version whose behaviour the repair step depends on, and one spelling of it
# is one thing to get wrong.
"$venv/bin/pip" install --quiet "cmake~=3.31.0" build wheel \
  "$(PYTHONPATH="$repo_root" python3 -c 'from wheelbuild.assemble import DELOCATE_REQUIREMENT; print(DELOCATE_REQUIREMENT)')"
export PATH="$toolchain_bin:$venv/bin:$PATH"
cmake --version | head -1

ccache_flag=()
if ! command -v ccache >/dev/null; then
  # Not fatal, and not worth a `brew install`: ccache earns its place on a
  # fallback cache restore, not on a cold build.
  echo "==> ccache is not installed; building without it"
  ccache_flag=(--no-ccache)
fi

echo "==> MPICH (vendored into the wheel)"
mpich_version="$(PYTHONPATH="$repo_root" python3 -c 'from palace_solver import MPICH_VERSION; print(MPICH_VERSION)')"
mpich_source="$build_root/mpich-$mpich_version"
if [[ ! -d "$mpich_source" ]]; then
  curl -fsSL "https://www.mpich.org/static/downloads/$mpich_version/mpich-$mpich_version.tar.gz" \
    -o "$build_root/mpich-$mpich_version.tar.gz"
  tar -xzf "$build_root/mpich-$mpich_version.tar.gz" -C "$build_root"
fi
# The install's own validator rather than a filename: it is the one that knows
# a Mach-O install carries libmpifort.12.dylib and not libmpifort.so.12.
mpich_installed=0
PYTHONPATH="$repo_root" python3 -c '
import pathlib, sys
from wheelbuild import mpich

try:
    mpich.validate(pathlib.Path(sys.argv[1]))
except FileNotFoundError as error:
    sys.exit(str(error))
' "$install_prefix" || mpich_installed=$?
if (( mpich_installed != 0 )); then
  PYTHONPATH="$repo_root" python3 -m wheelbuild.mpich \
    --source-dir "$mpich_source" \
    --build-dir "$build_root/mpich-build" \
    --prefix "$install_prefix" \
    --jobs "$jobs"
fi
export PATH="$install_prefix/bin:$PATH"

echo "==> OpenBLAS (vendored into the wheel)"
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
# What the install was compiled for, not whether it exists — the restore-keys
# can hand back a tree built before a pin changed. Verdicts in
# wheelbuild/openblas.py.
openblas_verdict=0
PYTHONPATH="$repo_root" python3 -m wheelbuild.openblas \
  --prefix "$install_prefix" --check || openblas_verdict=$?
case "$openblas_verdict" in
  0) ;;
  6) ;;
  3)
    # make does not notice a changed TARGET, so the objects in the restored
    # source tree have to go with the install.
    echo "==> discarding an OpenBLAS tree configured for another CPU baseline"
    unpack_openblas
    ;;
  *)
    # Two OpenMP runtimes in one library, or a verdict this script predates.
    # Rebuilding is not the answer to either, so stop while it is still one
    # library rather than a wheel.
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
export OPENBLAS_DIR="$install_prefix"

echo "==> Palace $palace_version sources"
if [[ ! -d "$source_dir" ]]; then
  curl -fsSL "https://github.com/awslabs/palace/archive/refs/tags/v$palace_version.tar.gz" \
    -o "$build_root/palace-$palace_version.tar.gz"
  tar -xzf "$build_root/palace-$palace_version.tar.gz" -C "$build_root"
fi
# A release tarball is not a checkout, and Palace stamps UNKNOWN without one.
if [[ ! -d "$source_dir/.git" ]]; then
  git -c init.defaultBranch=main -C "$source_dir" init --quiet
  git -C "$source_dir" add --all
  git -C "$source_dir" -c user.name=palace-solver -c user.email=palace-solver@localhost \
    commit --quiet --no-verify --message "Palace v$palace_version release tarball"
  git -C "$source_dir" tag "v$palace_version"
fi

# The tag reaches the binary only through a reconfigure, and a cache can hand
# back a tree tagged after its Palace was configured. Ask the binary.
installed_palace="$(PYTHONPATH="$repo_root" python3 -c '
import pathlib, sys
from wheelbuild.assemble import find_palace_binary

try:
    print(find_palace_binary(pathlib.Path(sys.argv[1])))
except (FileNotFoundError, OSError):
    pass
' "$install_prefix")"
# `timeout` is GNU coreutils and macOS ships none, so the guard against a hung
# binary is used where it exists and skipped where it does not.
deadline=()
command -v timeout >/dev/null && deadline=(timeout 60)
if [[ -n "$installed_palace" ]] \
  && ! ${deadline[@]+"${deadline[@]}"} "$installed_palace" --version 2>/dev/null \
    | grep -q "Palace version: v$palace_version"; then
  echo "==> installed Palace is not stamped v$palace_version, reconfiguring it"
  rm -f "$superbuild_dir"/palace-cmake/src/palace-stamp/palace-{configure,build,install,done} \
    "$superbuild_dir/CMakeFiles/palace-complete"
fi

echo "==> superbuild (dependency tree cached in $superbuild_dir)"
PYTHONPATH="$repo_root" python3 -m wheelbuild.superbuild \
  --source-dir "$source_dir" \
  --build-dir "$superbuild_dir" \
  --install-prefix "$install_prefix" \
  --prefix "$install_prefix" \
  --jobs "$jobs" \
  ${ccache_flag[@]+"${ccache_flag[@]}"}

echo "==> Palace is installed in $install_prefix"

echo "==> third-party notices"
# The same harvest the Linux driver runs, over the same three source roots.
# What it renders for the compiler runtime is the GCC note, which is correct
# here because this toolchain is GCC -- libgomp and not LLVM's libomp, which
# the audit would refuse. The release it names comes from $CC, exported above,
# rather than from the `gcc` on PATH, which on a macOS runner is Apple clang.
PYTHONPATH="$repo_root" python3 -m wheelbuild.notices \
  --source-root "$superbuild_dir" \
  --source-root "$mpich_source" \
  --source-root "$openblas_source" \
  --output "$build_root/THIRD-PARTY-NOTICES"

echo "==> wheel assembly, repair, retag"
# Run through the venv's interpreter rather than the system python3: `python -m
# build` resolves `python` from PATH, and delocate-wheel is only installed here.
# DYLD_LIBRARY_PATH is deliberately left unset -- see the comment in
# wheelbuild/assemble.py's repair_command, which is where the reason lives.
PYTHONPATH="$repo_root" "$venv/bin/python" -m wheelbuild.assemble \
  --project-dir "$repo_root" \
  --install-prefix "$install_prefix" \
  --output-dir "$output_dir" \
  --notices "$build_root/THIRD-PARTY-NOTICES"
