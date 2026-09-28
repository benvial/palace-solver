#!/usr/bin/env bash
# Spike (wayfinder ticket 06), run as cibuildwheel's `repair-wheel-command`.
#
#   scripts/spike-cibw-repair.sh WHEEL DEST_DIR
#
# Ticket 01 claimed wheelbuild/assemble.py's repair and retag steps drop into
# cibuildwheel unchanged. This calls the module's own command builders rather
# than re-spelling auditwheel by hand, so that claim is what is being tested.
set -euo pipefail

wheel="${1:?usage: spike-cibw-repair.sh WHEEL DEST_DIR}"
dest_dir="${2:?usage: spike-cibw-repair.sh WHEEL DEST_DIR}"
build_root="${BUILD_ROOT:?BUILD_ROOT must point into /host}"

# The venv scripts/build-wheel.sh made lives in the cached tree and hard-codes
# the container's interpreter path, so the tooling is installed fresh here
# instead of trusting a path restored from a previous run's image.
python3 -m pip install --quiet --upgrade auditwheel wheel

export LD_LIBRARY_PATH="$build_root/install/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

PYTHONPATH="$(pwd)" python3 - "$wheel" "$dest_dir" <<'PYTHON'
import pathlib
import sys

from wheelbuild._process import check_call
from wheelbuild.assemble import repair_command, retag_command, size_report

wheel = pathlib.Path(sys.argv[1])
dest_dir = pathlib.Path(sys.argv[2])
dest_dir.mkdir(parents=True, exist_ok=True)

check_call(repair_command(wheel=wheel, output_dir=dest_dir))
repaired = next(iter(dest_dir.glob("*.whl")))
check_call(retag_command(repaired))
final = next(iter(dest_dir.glob("*.whl")))
print(size_report(final).text, flush=True)
PYTHON

ls -la "$dest_dir"
