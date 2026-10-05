#!/usr/bin/env bash
# Build Palace and everything the wheel vendors, on Windows x86-64.
#
#   scripts/build-windows.sh [PALACE_VERSION]
#
# The Windows sibling of scripts/build-macos.sh, and like it both the driver and
# the recipe: no container, a sequence of wheelbuild invocations. It runs under
# MSYS2's UCRT64 bash, which supplies the toolchain (gcc, g++ and gfortran),
# GNU make and the POSIX tools Palace's superbuild drives, and MS-MPI's import
# library and gfortran bridge (mingw-w64-msmpi). It has only ever run on a
# GitHub windows-2025 runner; a Windows owner with MSYS2 can run it the same
# way, with the packages the workflow installs.
#
# Every wheelbuild step runs under a native Windows CPython, never MSYS2's: its
# own python reports its platform as mingw_x86_64_ucrt (or msys) and would
# mis-tag the wheel, and the wheelbuild modules ask platform.system() which
# arm of the recipe they are on.
#
# Environment:
#   PYTHON       a native Windows CPython (default: the one actions/setup-python
#                exported as pythonLocation)
#   BUILD_ROOT   scratch root for sources, the superbuild and the install
#                prefix (default D:\b). Short, and on D: on a runner: tools
#                without a long-path manifest still stop at MAX_PATH, and the
#                runner's C: is 40-50% slower.
#   MSMPI_DIR    where the MS-MPI redistributable is fetched to (default
#                <BUILD_ROOT>-msmpi). Outside the build root on purpose: it is
#                fetched and hash-checked on every run, never cached.
#   PAYLOAD_DIR  where the repair copies the wheel's DLLs (default
#                <BUILD_ROOT>-payload). Outside the build root for the same
#                reason: it is rebuilt from the closure on every run.
#   OUTPUT_DIR   wheel output directory (default: <repo>/wheelhouse)
#   JOBS         parallel build jobs (default: the machine's core count)
set -euo pipefail

if [[ "${MSYSTEM:-}" != "UCRT64" ]]; then
  echo "ERROR: this is the Windows driver; run it under MSYS2 UCRT64 bash, not ${MSYSTEM:-a non-MSYS2 shell}" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -z "${pythonLocation:-}" ]]; then
    echo "ERROR: set PYTHON to a native Windows CPython (MSYS2's python cannot run wheelbuild)" >&2
    exit 1
  fi
  PYTHON="$(cygpath -u "$pythonLocation")/python.exe"
fi
# sysconfig rather than sys.platform: MSYS2's MinGW python also says win32.
if [[ "$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_platform())' | tr -d '\r')" != "win-amd64" ]]; then
  echo "ERROR: $PYTHON is not a native Windows x64 CPython" >&2
  exit 1
fi

# Native programs take Windows paths; forward slashes are the form both they
# and the MSYS2 tools read.
win() { cygpath -m "$1"; }
# Native python writes CRLF, and bash keeps the CR of a captured line.
wheelbuild() { PYTHONPATH="$(win "$repo_root")" "$PYTHON" "$@"; }
wheelbuild_value() { wheelbuild "$@" | tr -d '\r'; }

# The Palace release is __version__ minus any .postN packaging segment, exactly
# as scripts/build-wheel.sh resolves it.
palace_version="${1:-$(wheelbuild_value -c 'import re,pathlib,sys; print(re.search(r"__version__ = \"([^\"]+)\"", pathlib.Path(sys.argv[1]).read_text()).group(1).split(".post")[0])' "$(win "$repo_root/palace_solver/__init__.py")")}"
build_root="$(cygpath -u "${BUILD_ROOT:-D:\\b}")"
msmpi_dir="$(cygpath -u "${MSMPI_DIR:-$(cygpath -w "$build_root")-msmpi}")"
payload_dir="$(cygpath -u "${PAYLOAD_DIR:-$(cygpath -w "$build_root")-payload}")"
output_dir="$(cygpath -u "${OUTPUT_DIR:-$(cygpath -w "$repo_root")/wheelhouse}")"
jobs="${JOBS:-$(nproc)}"

# Palace's CMake runs `git describe` in the source tree at configure time to
# stamp `palace --version`, and that tree comes back from a build cache that
# tar restored. See the same block in scripts/build-wheel.sh.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="*"

source_dir="$build_root/palace-$palace_version"
superbuild_dir="$build_root/superbuild"
install_prefix="$build_root/install"

mkdir -p "$build_root" "$msmpi_dir"

# No wheelbuild.prefix here: GNUInstallDirs installs into lib on Windows, so
# there is no lib64 to alias, and a directory symlink needs a privilege a
# developer machine may not grant.

echo "==> toolchain"
gcc --version | head -1
gfortran --version | head -1
"$PYTHON" --version
# MSYS2's packages roll and are not pinned (ADR-0007), so the run records what
# it got.
pacman -Q

# Kitware's CMake inside 3.31, not MSYS2's rolling 4.x, which rejects the
# cmake_minimum_required floors several sub-projects still declare. 3.31.8 is
# upstream Palace's own pin on macOS, for a FindMPI bug with GNU compilers
# (Palace issue #470), and the build the spike proved. Downloaded into the
# build root, so the cache holds it, and checked against Kitware's published
# SHA-256.
cmake_version=3.31.8
cmake_sha256=81aa9964dbabd71fe02e7ec50472fd3ad56138c49944515ece9001efbff8d719
cmake_dir="$build_root/cmake-$cmake_version-windows-x86_64"
if [[ ! -x "$cmake_dir/bin/cmake.exe" ]]; then
  echo "==> CMake $cmake_version"
  cmake_zip="$build_root/cmake-$cmake_version-windows-x86_64.zip"
  curl -fsSL -o "$cmake_zip" \
    "https://github.com/Kitware/CMake/releases/download/v$cmake_version/cmake-$cmake_version-windows-x86_64.zip"
  echo "$cmake_sha256  $cmake_zip" | sha256sum --check --quiet
  rm -rf "$cmake_dir"
  unzip -q "$cmake_zip" -d "$build_root"
  rm -f "$cmake_zip"
fi
export PATH="$cmake_dir/bin:$PATH"
cmake --version | head -1

# Several sub-projects (METIS, ParMETIS, ScaLAPACK, ...) are configured by a
# bare `${CMAKE_COMMAND} <SOURCE_DIR>` with no -G, which on Windows means
# NMake. CMake reads this whenever no generator is given.
export CMAKE_GENERATOR="MSYS Makefiles"

# PETSc's ./configure is `#!/usr/bin/env python3` and refuses a Windows python
# ("Windows python detected. Please rerun ./configure with cygwin-python"),
# which MinGW's python is. MSYS2's own /usr/bin/python3 is the one it wants,
# and this shim, first on PATH, hands it to every `env python3`. That python
# reads only POSIX paths -- given `--with-cc=C:/msys64/...` it searches PATH
# for a program of that name -- so the shim rewrites every drive-letter path
# in its arguments, and in PETSC_DIR and SLEPC_DIR, to its /X/... form. The
# letter keeps its case: make reports the working directory as /D/b/..., and
# SLEPc compares SLEPC_DIR against it case-sensitively.
shim_dir="$build_root/posix-python"
mkdir -p "$shim_dir"
cat >"$shim_dir/python3" <<'SHIM'
#!/usr/bin/bash
posix() { sed -E 's#(^|[=,;[:space:]]|\[)([A-Za-z]):/#\1/\2/#g' <<<"$1"; }
args=()
for arg in "$@"; do args+=("$(posix "$arg")"); done
for var in PETSC_DIR SLEPC_DIR; do
  [[ -n "${!var:-}" ]] && export "$var=$(posix "${!var}")"
done
exec /usr/bin/python3 "${args[@]}"
SHIM
chmod +x "$shim_dir/python3"
export PATH="$shim_dir:$PATH"

echo "==> MS-MPI runtime (fetched and verified, never cached)"
# MSYS2's mingw-w64-msmpi is the SDK only. Palace's configure try_run()s a
# PETSc test program, which cannot start without msmpi.dll, and the runner has
# no MS-MPI installed -- nor should a build machine need one.
seven_zip="$(command -v 7z || true)"
if [[ -z "$seven_zip" && -x "/c/Program Files/7-Zip/7z.exe" ]]; then
  seven_zip="/c/Program Files/7-Zip/7z.exe"
fi
if [[ -z "$seven_zip" ]]; then
  echo "ERROR: 7-Zip is needed to unpack msmpisetup.exe; install it or put 7z on PATH" >&2
  exit 1
fi
wheelbuild -m wheelbuild.msmpi "$(win "$msmpi_dir")" --seven-zip "$(cygpath -w "$seven_zip")"

# The DLLs a try_run program imports: the vendored OpenBLAS from bin, libCEED
# and LIBXSMM from lib (Palace installs them there), and MS-MPI.
export PATH="$install_prefix/bin:$install_prefix/lib:$msmpi_dir:$PATH"

echo "==> OpenBLAS (vendored into the wheel)"
openblas_version="$(wheelbuild_value -c 'from wheelbuild.openblas import OPENBLAS_VERSION; print(OPENBLAS_VERSION)')"
openblas_source="$build_root/OpenBLAS-$openblas_version"
openblas_tarball="$build_root/OpenBLAS-$openblas_version.tar.gz"
unpack_openblas() {
  if [[ ! -f "$openblas_tarball" ]]; then
    curl -fsSL "$(wheelbuild_value -c 'from wheelbuild.openblas import source_url; print(source_url())')" \
      -o "$openblas_tarball"
  fi
  rm -rf "$openblas_source"
  tar -xzf "$openblas_tarball" -C "$build_root"
}
[[ -d "$openblas_source" ]] || unpack_openblas
# Verdicts in wheelbuild/openblas.py, handled as scripts/build-macos.sh does.
# The recipe is the Linux x86_64 one byte for byte: Makefile.x86_64 is the same
# file, and DYNAMIC_ARCH needs no baseline. It compiles 5-8x slower per object
# than on Linux, because OpenBLAS's common.h includes <windows.h> in every
# translation unit, which is why the cache matters most here.
openblas_verdict=0
wheelbuild -m wheelbuild.openblas --prefix "$(win "$install_prefix")" --check || openblas_verdict=$?
case "$openblas_verdict" in
  0) ;;
  6) ;;
  3)
    echo "==> discarding an OpenBLAS tree built by other arguments"
    unpack_openblas
    ;;
  *)
    echo "::error::OpenBLAS check returned $openblas_verdict; not rebuilding" >&2
    exit 1
    ;;
esac
if (( openblas_verdict != 0 )); then
  wheelbuild -m wheelbuild.openblas \
    --source-dir "$(win "$openblas_source")" \
    --prefix "$(win "$install_prefix")" \
    --jobs "$jobs"
fi
OPENBLAS_DIR="$(win "$install_prefix")"
export OPENBLAS_DIR

echo "==> Palace $palace_version sources"
if [[ ! -d "$source_dir" ]]; then
  curl -fsSL "https://github.com/awslabs/palace/archive/refs/tags/v$palace_version.tar.gz" \
    -o "$build_root/palace-$palace_version.tar.gz"
  tar -xzf "$build_root/palace-$palace_version.tar.gz" -C "$build_root"
  rm -f "$build_root/palace-$palace_version.tar.gz"
fi
# A release tarball is not a checkout, and Palace stamps UNKNOWN without one.
# The tarball commit is tagged upstream-v<version> rather than v<version>:
# wheelbuild.patches commits the carried patches on top of it and moves the
# release tag there, so `palace --version` names the release, as it does on
# the other platforms, rather than a -dirty tree.
if [[ ! -d "$source_dir/.git" ]]; then
  git -c init.defaultBranch=main -C "$source_dir" init --quiet
  git -C "$source_dir" add --all
  git -C "$source_dir" -c user.name=palace-solver -c user.email=palace-solver@localhost \
    commit --quiet --no-verify --message "Palace v$palace_version release tarball"
  git -C "$source_dir" tag "upstream-v$palace_version"
fi

# The installed binary, by the name wheelbuild.assemble.find_palace_binary
# matches too: palace-<arch>.bin, which palace-unit-tests.exe beside it in the
# same bin does not.
installed_palace() {
  find "$install_prefix/bin" -maxdepth 1 -name 'palace-*.bin' 2>/dev/null | head -1
}
palace_reports() {
  timeout 60 "$1" --version 2>/dev/null | tr -d '\r' | grep -x "Palace version: v$palace_version"
}

# The tag reaches the binary only through a reconfigure, and a cache can hand
# back a tree tagged after its Palace was configured. Ask the binary.
binary="$(installed_palace)"
if [[ -n "$binary" ]] && ! palace_reports "$binary" >/dev/null; then
  echo "==> installed Palace is not stamped v$palace_version, reconfiguring it"
  rm -f "$superbuild_dir"/palace-cmake/src/palace-stamp/palace-{configure,build,install,done} \
    "$superbuild_dir/CMakeFiles/palace-complete"
fi

echo "==> superbuild (dependency tree cached in $superbuild_dir)"
# The carried patches go in inside this step: Palace's are committed onto the
# checkout, every dependency is fetched before anything compiles, its patches
# are applied, and after the build every one is proved still there. Palace's
# tests are built last, for the upstream test gate. See
# wheelbuild/patches.py.
wheelbuild -m wheelbuild.patches list
wheelbuild -m wheelbuild.superbuild \
  --source-dir "$(win "$source_dir")" \
  --build-dir "$(win "$superbuild_dir")" \
  --install-prefix "$(win "$install_prefix")" \
  --jobs "$jobs"

binary="$(installed_palace)"
if [[ -z "$binary" ]]; then
  echo "ERROR: the superbuild installed no palace-*.bin under $install_prefix/bin" >&2
  exit 1
fi
if ! version="$(palace_reports "$binary")"; then
  echo "ERROR: $binary does not report Palace v$palace_version:" >&2
  "$binary" --version >&2 || true
  exit 1
fi
echo "$version ($binary, $(stat -c %s "$binary") bytes)"
echo "==> Palace is installed in $install_prefix"

# MS-MPI's gfortran bridge is vendored but comes from MSYS2's rolling
# mingw-w64-msmpi, unhashed (ADR-0007 §2), so the run names the package that
# supplied it next to the full `pacman -Q` above.
pacman -Qo /ucrt64/bin/libmsmpifec.dll

echo "==> wheel tools"
# Into the native CPython itself, not a venv: wheelbuild.assemble runs
# `python -m build` and `wheel tags` by name, and a native Windows process
# finds `python` in its own directory before anything on PATH. Its Scripts
# directory goes on PATH for `wheel`, which MSYS2's minimal PATH leaves off.
wheelbuild -m pip install --upgrade build wheel \
  "$(wheelbuild_value -c 'from wheelbuild.link_check import PEFILE_REQUIREMENT; print(PEFILE_REQUIREMENT)')"
python_dir="$(dirname "$PYTHON")"
export PATH="$python_dir:$python_dir/Scripts:$PATH"

echo "==> repair: the DLL closure, flat"
# Before the wheel exists, unlike auditwheel and delocate: the notices below
# are rendered from what this copies. See wheelbuild/pe_repair.py.
wheelbuild -m wheelbuild.pe_repair \
  --install-prefix "$(win "$install_prefix")" \
  --msmpi-dir "$(win "$msmpi_dir")" \
  --toolchain-dir "$(cygpath -m /ucrt64/bin)" \
  --output-dir "$(win "$payload_dir")"

echo "==> third-party notices"
# The Linux driver's harvest less MPICH, which this wheel does not carry; the
# Windows sections are chosen by the DLLs the repair copied.
wheelbuild -m wheelbuild.notices \
  --source-root "$(win "$superbuild_dir")" \
  --source-root "$(win "$openblas_source")" \
  --system Windows \
  --payload-dir "$(win "$payload_dir")" \
  --output "$(win "$build_root/THIRD-PARTY-NOTICES")"

echo "==> wheel assembly, link check, retag"
wheelbuild -m wheelbuild.assemble \
  --project-dir "$(win "$repo_root")" \
  --install-prefix "$(win "$install_prefix")" \
  --output-dir "$(win "$output_dir")" \
  --notices "$(win "$build_root/THIRD-PARTY-NOTICES")" \
  --msmpi-dir "$(win "$msmpi_dir")" \
  --payload-dir "$(win "$payload_dir")"
