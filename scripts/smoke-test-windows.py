r"""Install the built Windows wheel into a clean venv and prove the binary runs.

    python scripts/smoke-test-windows.py WHEEL PALACE_CONFIG

The Windows twin of scripts/smoke-test.sh; why it is a twin rather than a
branch of that script is in scripts/_windows_wheel.py. Run it on a machine with
no MS-MPI installed: what it proves is that the wheel needs nothing else.

Checks: the machine has no MS-MPI installed; the wheel vendors it; it
installs into a venv with nothing else in it; palace_solver finds its payload,
its DLL directory and its own console script (Scripts\palace.exe -- a None
there would send `palace --np` to the unguarded binary); every PE file in the
payload imports only what sits beside it or ships with Windows; and, with a
PATH holding nothing but the venv and Windows, Palace reports its version and
runs PALACE_CONFIG as a dry run and a real solve on one rank, then as a dry
run on two ranks under the vendored palace-mpiexec and through the wrapper's
own --np. Last, one adaptive refinement on one rank must save its first
iteration, where Palace leaves symlinks on other platforms.

scripts/interop-test-windows.py carries this further, into real two-rank solves
under a launcher that did not ship with the wheel.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from _windows_wheel import (
    REPO_ROOT,
    clean_environment,
    copy_example,
    count_lines,
    fail,
    make_wheel_venv,
    run,
    step,
    write_adaptive_config,
)

#: What the wheel has to carry for nothing else to be needed: the solver, MS-MPI
#: (its runtime, its launcher and the manager the launcher starts the ranks
#: from) and the BLAS, all flat in ``bin``.
REQUIRED_MEMBERS = (
    "palace_solver/bin/palace-real.exe",
    "palace_solver/bin/mpiexec.exe",
    "palace_solver/bin/smpd.exe",
    "palace_solver/bin/msmpi.dll",
    "palace_solver/bin/libopenblas.dll",
)

#: Asked of the installed package, from a directory outside the checkout.
QUERY = """
import json, sys
import palace_solver as p
print(json.dumps({
    "binary": str(p.binary_path()),
    "mpiexec": str(p.mpiexec_path()),
    "lib_dir": str(p.lib_dir()),
    "console_script": str(p.console_script_path()),
    "palace_version": p.PALACE_VERSION,
    "windows": list(sys.getwindowsversion()[:3]),
}))
"""


def install(wheel: Path, workdir: Path) -> tuple[Path, dict]:
    """Install the wheel alone and ask the package where everything is.

    Returns:
        The venv's ``Scripts`` directory, and the package's answers.
    """
    step("no MS-MPI is installed on this machine")
    # Where msmpisetup.exe puts the runtime. With it there, a DLL the wheel
    # forgot could still be found, and the test would prove nothing.
    system_msmpi = Path(os.environ["SYSTEMROOT"]) / "System32" / "msmpi.dll"
    if system_msmpi.exists():
        fail(f"{system_msmpi} exists: run this where MS-MPI is not installed")

    step("the wheel vendors MS-MPI")
    names = set(zipfile.ZipFile(wheel).namelist())
    for member in REQUIRED_MEMBERS:
        if member not in names:
            fail(f"{member} is missing from the wheel")

    # Nothing but the wheel: it must bring its own MPI.
    scripts = make_wheel_venv(workdir / "venv", wheel)
    query = run([scripts / "python.exe", "-c", QUERY], cwd=workdir, timeout=120)
    if query.returncode != 0:
        fail("the installed package could not be imported", query.stdout)
    installed = json.loads(query.stdout.strip().splitlines()[-1])
    print(f"==> packaged binary: {installed['binary']}")
    print(f"==> Windows {'.'.join(map(str, installed['windows']))}")

    step("the package finds its console script")
    console_script = scripts / "palace.exe"
    found = installed["console_script"]
    if found == "None" or not Path(found).samefile(console_script):
        fail(
            f"console_script_path() is {found}, expected {console_script}: "
            "`palace --np` would start unguarded ranks"
        )

    step("the vendored DLLs sit beside the executables")
    payload = Path(installed["binary"]).parent
    if not Path(installed["lib_dir"]).samefile(payload):
        fail(f"lib_dir() is {installed['lib_dir']}, not {payload}")
    return scripts, installed


def check_links(binary: Path, workdir: Path) -> None:
    """Run the link check on the installed payload.

    The same two questions smoke-test.sh asks: the solver links MPI at all, and
    every PE file in the payload -- solver, mpiexec, smpd and each DLL -- finds
    each import beside it or in Windows.
    """
    step("PE import resolution")
    link_env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    for arguments in (["--require-mpi", binary], [binary.parent]):
        done = run(
            [sys.executable, "-m", "wheelbuild.link_check", *arguments],
            cwd=workdir,
            env=link_env,
            timeout=300,
        )
        if done.returncode != 0:
            fail("the link check failed on the installed wheel", done.stdout)


def expect(
    done: subprocess.CompletedProcess[str],
    message: str,
    *,
    dry_runs: int | None = None,
) -> None:
    """Fail with ``message`` unless ``done`` exited 0, with the dry-run lines asked.

    Rank 0 alone prints the dry-run line; twice would mean the ranks never
    found each other and each ran as its own MPI_COMM_WORLD.
    """
    if done.returncode != 0:
        fail(message, done.stdout)
    if dry_runs is not None and count_lines(done.stdout, "Dry-run:") != dry_runs:
        fail(message, done.stdout)


def check_runs(scripts: Path, palace_version: str, config: Path, workdir: Path) -> None:
    """Run Palace from the venv with nothing but it and Windows on ``PATH``."""
    env = clean_environment(os.environ, scripts, os.environ["SYSTEMROOT"])
    palace = scripts / "palace.exe"
    palace_mpiexec = scripts / "palace-mpiexec.exe"

    step("version")
    # Also the floor check: the console script refuses to start anything on a
    # Windows older than its floor, so a version from it means it passed.
    done = run([palace, "--version"], cwd=workdir, env=env, timeout=120)
    expect(done, "`palace --version` failed")
    expected = f"Palace version: v{palace_version}"
    if expected not in done.stdout.splitlines():
        fail(f"`palace --version` did not print {expected!r}", done.stdout)

    step("single rank, dry run")
    directory = copy_example(config, workdir / "dry-run")
    done = run([palace, "--dry-run", config.name], cwd=directory, env=env)
    expect(done, "the single-rank dry run failed", dry_runs=1)

    step("single rank, solve")
    directory = copy_example(config, workdir / "solve")
    done = run([palace, config.name], cwd=directory, env=env)
    expect(done, "the single-rank solve failed")
    if not list(directory.glob("postpro/*.csv")):
        fail("the single-rank solve wrote no postprocessing output", done.stdout)

    step("two ranks, under the vendored process manager")
    directory = copy_example(config, workdir / "mpiexec")
    done = run(
        [palace_mpiexec, "-n", "2", palace, "--dry-run", config.name],
        cwd=directory,
        env=env,
    )
    expect(done, "palace-mpiexec -n 2 did not form one MPI_COMM_WORLD", dry_runs=1)

    step("two ranks through the wrapper's own --np")
    directory = copy_example(config, workdir / "np")
    done = run([palace, "--np", "2", "--dry-run", config.name], cwd=directory, env=env)
    expect(done, "--np 2 did not form one MPI_COMM_WORLD of 2 ranks", dry_runs=1)

    step("single rank, adaptive refinement that saves its iterations")
    # Saving an iteration moves each output into iteration1/ and, on other
    # platforms, leaves a symlink in its place, which MinGW's libstdc++ cannot
    # make: without the carried copy fallback, Palace aborts here.
    directory = copy_example(config, workdir / "adaptive")
    adaptive = write_adaptive_config(directory / config.name, max_iterations=1)
    done = run([palace, adaptive.name], cwd=directory, env=env)
    expect(done, "the adaptive solve failed")
    if not list(directory.glob("postpro/iteration1/*.csv")):
        fail("the adaptive solve archived nothing in iteration1", done.stdout)
    if not list(directory.glob("postpro/*.csv")):
        fail("the adaptive solve left no output after its iteration", done.stdout)


def main(argv: list[str] | None = None) -> int:
    """Run the smoke test; exit non-zero on the first check that fails."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel", type=Path)
    parser.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    workdir = Path(tempfile.mkdtemp(prefix="palace-smoke-"))

    scripts, installed = install(args.wheel.resolve(), workdir)
    check_links(Path(installed["binary"]), workdir)
    check_runs(scripts, installed["palace_version"], args.config.resolve(), workdir)

    step("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
