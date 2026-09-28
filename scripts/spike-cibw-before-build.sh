#!/usr/bin/env bash
# Spike (wayfinder ticket 06), run as cibuildwheel's `before-build`.
#
# The production pipeline stages the superbuild payload into the package
# directory and then builds the wheel in one step (wheelbuild.assemble.build).
# cibuildwheel owns the build, so the staging half has to happen here.
set -euo pipefail

build_root="${BUILD_ROOT:?BUILD_ROOT must point into /host}"

echo "==> staging the payload into $(pwd)/palace_solver"
PYTHONPATH="$(pwd)" python3 - "$build_root" <<'PYTHON'
import pathlib
import sys

from wheelbuild.assemble import stage

build_root = pathlib.Path(sys.argv[1])
notices = build_root / "THIRD-PARTY-NOTICES"
staged = stage(
    install_prefix=build_root / "install",
    package_dir=pathlib.Path.cwd() / "palace_solver",
    notices=notices if notices.exists() else None,
)
print(f"staged {staged}")
PYTHON

find palace_solver/bin palace_solver/lib -maxdepth 1 -printf '%M %10s %p\n' | head -20
