"""Prove the Windows wheel interoperates with a system MS-MPI, and fails safe.

    python scripts/interop-test-windows.py [--install-msmpi] WHEEL PALACE_CONFIG

The Windows twin of scripts/interop-test.sh; why it is a twin rather than a
branch of that script is in scripts/_windows_wheel.py. The wheel vendors MS-MPI,
and the foreign launcher a Windows user is likely to have is the ``mpiexec`` of
a system MS-MPI 10.1.x, installed from ``msmpisetup.exe`` (what mpi4py users
have). ``--install-msmpi`` installs the pinned 10.1.3 from Microsoft, checked
against the hash in wheelbuild/msmpi.py, if none is installed; it needs an
administrator, and is meant for a disposable CI runner.

Stages, every solve a real one on two ranks:

1. under the vendored palace-mpiexec, one MPI_COMM_WORLD;
2. under the system MS-MPI's mpiexec launching the console script, one
   MPI_COMM_WORLD, with results equal to stage 1's;
3. the launcher guard's parent read, through the console script's real
   process tree (launcher stub, venv redirector, interpreter), names smpd.exe
   under both launchers;
4. a foreign launcher handing a rank PMI_RANK but no PMI_KVS -- Intel MPI's
   shape, played by a copy of cmd.exe named hydra_pmi_proxy.exe -- is refused,
   where without the guard the rank would run alone and exit 0;
5. a failing run's exit status passes through both console scripts unchanged;
6. Ctrl-Break to a running two-rank solve ends it with 0xC000013A, and Ctrl-C
   on its console ends it too, each leaving no palace-real.exe behind.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _windows_wheel import (
    REPO_ROOT,
    clean_environment,
    compare_reports,
    copy_example,
    count_lines,
    count_occurrences,
    fail,
    make_wheel_venv,
    run,
    running_images,
    step,
    write_console_script_wheel,
)

sys.path.insert(0, str(REPO_ROOT))

from wheelbuild import msmpi
from wheelbuild.pin_check import INTEROP_MSMPI_REQUIREMENT

#: The rank-0 banner of a two-rank solve. Seen once, the ranks formed one
#: MPI_COMM_WORLD; seen twice, or with another count, they did not.
BANNER = "Running with 2 MPI processes"

#: The process every rank of a two-rank solve runs, for counting them.
RANK_IMAGE = "palace-real.exe"

#: ``STATUS_CONTROL_C_EXIT``: how Windows reports a process a console control
#: event ended, as an unsigned 32-bit exit status.
STATUS_CONTROL_C_EXIT = 0xC000013A

#: How long a two-rank solve may take to start both ranks, and how long after
#: an interrupt the ranks may take to go.
START_TIMEOUT = 300
EXIT_TIMEOUT = 120

#: A console script that reports the guard's parent read and nothing else.
PROBE_NAME = "palace-parent-probe"
PROBE_SOURCE = """\
from palace_solver import _launcher


def main():
    print(f"parent: {_launcher.parent_executable()}", flush=True)
"""

#: Run with a console of its own, so its Ctrl-C reaches only the solve it
#: started. A process created into a new process group -- as a CI step may be
#: -- starts with Ctrl-C ignored, and passes that on, so the helper turns it
#: back on first. It reports as JSON to the path in its third argument.
CTRL_C_HELPER = """\
import ctypes, json, signal, subprocess, sys, time

command, log, result, image, timeout = sys.argv[1:6]
kernel32 = ctypes.WinDLL("kernel32")
kernel32.SetConsoleCtrlHandler(None, False)


def ranks():
    listing = subprocess.run(
        ["tasklist", "/fi", f"imagename eq {image}", "/fo", "csv", "/nh"],
        capture_output=True, text=True, errors="replace",
    ).stdout.lower()
    return listing.count(f'"{image}"')


seen = 0
with open(log, "w") as stream:
    child = subprocess.Popen(json.loads(command), stdout=stream,
                             stderr=subprocess.STDOUT)
    deadline = time.monotonic() + float(timeout)
    while child.poll() is None and time.monotonic() < deadline:
        seen = ranks()
        if seen >= 2:
            break
        time.sleep(0.5)
    time.sleep(3)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sent = bool(kernel32.GenerateConsoleCtrlEvent(0, 0))
    try:
        returncode = child.wait(timeout=float(timeout))
    except subprocess.TimeoutExpired:
        child.kill()
        returncode = None
with open(result, "w") as stream:
    json.dump({"ranks": seen, "sent": sent, "returncode": returncode}, stream)
"""


def system_mpiexec_path() -> Path:
    """Where ``msmpisetup.exe`` installs the system ``mpiexec``."""
    program_files = os.environ.get("PROGRAMFILES", "C:\\Program Files")
    return Path(program_files) / "Microsoft MPI" / "Bin" / "mpiexec.exe"


def install_system_msmpi(workdir: Path, mpiexec: Path) -> None:
    """Install the pinned MS-MPI from Microsoft's own installer, unattended."""
    installer = msmpi.fetch(workdir / "msmpisetup.exe")
    subprocess.run([str(installer), "-unattend", "-force"], check=True)
    deadline = time.monotonic() + 120
    while not mpiexec.is_file():
        if time.monotonic() > deadline:
            fail(f"msmpisetup.exe finished, but there is no {mpiexec}")
        time.sleep(2)


def in_proved_series(version: str) -> bool:
    """Whether an MS-MPI file version is in the series the pin check records.

    The files carry the build number (``10.1.12498.52``), the requirement the
    release series (``msmpi==10.1.*``), so the comparison is on the prefix.
    """
    series = INTEROP_MSMPI_REQUIREMENT.split("==", 1)[1].removesuffix("*")
    return version.startswith(series)


def check_system_msmpi(mpiexec: Path, workdir: Path) -> str:
    """Return the system MS-MPI's version, failing outside the proved series."""
    done = run([mpiexec, "-help"], cwd=workdir, timeout=60)
    match = re.search(r"Version\s+(\d+(?:\.\d+)+)", done.stdout)
    if match is None:
        fail(f"{mpiexec} did not report a version", done.stdout)
    version = match.group(1)
    if not in_proved_series(version):
        fail(
            f"the system MS-MPI is {version}, outside {INTEROP_MSMPI_REQUIREMENT!r}, "
            "the series this test is recorded for"
        )
    return version


def solve(
    name: str, launcher: Path, palace: Path, config: Path, *, workdir: Path, env
) -> Path:
    """Solve ``config`` on two ranks under ``launcher``; demand one world."""
    directory = copy_example(config, workdir / name)
    done = run([launcher, "-n", "2", palace, config.name], cwd=directory, env=env)
    banner = count_occurrences(done.stdout, BANNER)
    if done.returncode != 0 or banner != 1:
        fail(
            f"{name} did not form one MPI_COMM_WORLD of 2 ranks "
            f"(exit status {done.returncode}, rank-0 banner seen {banner} times, "
            "expected once)",
            done.stdout,
        )
    return directory


def check_parent_read(
    label: str, launcher: Path, probe: Path, smpd: Path, *, workdir: Path, env
) -> None:
    """Demand that both ranks' guard reads ``smpd`` as the process that started them."""
    done = run([launcher, "-n", "2", probe], cwd=workdir, env=env, timeout=300)
    parents = [
        line.removeprefix("parent: ").strip()
        for line in done.stdout.splitlines()
        if line.startswith("parent: ")
    ]
    if done.returncode != 0 or len(parents) != 2:
        fail(f"the parent probe did not run on two ranks under {label}", done.stdout)
    for parent in parents:
        if parent == "None" or not Path(parent).samefile(smpd):
            fail(f"under {label} a rank read its parent as {parent}, not {smpd}")
    print(f"    both ranks read {smpd}")


def wait_for_ranks(process: subprocess.Popen[bytes], log: Path) -> None:
    """Wait until both ranks of ``process``'s solve are running."""
    deadline = time.monotonic() + START_TIMEOUT
    while running_images(RANK_IMAGE) < 2:
        if process.poll() is not None:
            fail(
                "the solve ended before it could be interrupted",
                log.read_text(errors="replace"),
            )
        if time.monotonic() > deadline:
            process.kill()
            fail("the solve never started two ranks", log.read_text(errors="replace"))
        time.sleep(0.5)
    # Into the solve proper, past MPI_Init and the mesh read.
    time.sleep(3)


def check_no_orphans(label: str) -> None:
    """Demand that no rank outlives the interrupted solve, cleaning up if one does."""
    deadline = time.monotonic() + EXIT_TIMEOUT
    while (left := running_images(RANK_IMAGE)) > 0:
        if time.monotonic() > deadline:
            subprocess.run(["taskkill", "/f", "/im", RANK_IMAGE], check=False)
            subprocess.run(["taskkill", "/f", "/im", "smpd.exe"], check=False)
            fail(f"{left} {RANK_IMAGE} still running after {label}")
        time.sleep(1)
    print(f"    no {RANK_IMAGE} left after {label}")


def ctrl_break(palace: Path, config: Path, workdir: Path, env) -> None:
    """Ctrl-Break a running ``palace --np 2`` and check what it leaves."""
    directory = copy_example(config, workdir / "ctrl-break")
    log = workdir / "ctrl-break.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(
            [str(palace), "--np", "2", config.name],
            cwd=directory,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        wait_for_ranks(process, log)
        process.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            returncode = process.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            fail(
                "palace --np 2 did not end after Ctrl-Break",
                log.read_text(errors="replace"),
            )
    print(f"    exit status {returncode:#x}")
    if returncode & 0xFFFFFFFF != STATUS_CONTROL_C_EXIT:
        fail(
            f"Ctrl-Break ended palace --np 2 with {returncode:#x}, "
            f"not {STATUS_CONTROL_C_EXIT:#x}",
            log.read_text(errors="replace"),
        )
    check_no_orphans("Ctrl-Break")


def ctrl_c(palace: Path, config: Path, workdir: Path, env) -> None:
    """Ctrl-C a running ``palace --np 2`` on its own console."""
    directory = copy_example(config, workdir / "ctrl-c")
    log = workdir / "ctrl-c.log"
    result = workdir / "ctrl-c.json"
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE
    subprocess.run(
        [
            sys.executable,
            "-c",
            CTRL_C_HELPER,
            json.dumps([str(palace), "--np", "2", config.name]),
            str(log),
            str(result),
            RANK_IMAGE,
            str(START_TIMEOUT),
        ],
        cwd=directory,
        env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=startup,
        check=True,
        timeout=START_TIMEOUT + EXIT_TIMEOUT + 60,
    )
    report = json.loads(result.read_text())
    output = log.read_text(errors="replace")
    print("\n".join(output.splitlines()[-10:]))
    print(f"    {report}")
    if report["ranks"] < 2 or not report["sent"]:
        fail("Ctrl-C was not delivered to a running two-rank solve", output)
    returncode = report["returncode"]
    if returncode is None:
        fail("palace --np 2 did not end after Ctrl-C", output)
    print(f"    exit status {returncode & 0xFFFFFFFF:#x}")
    if returncode == 0:
        fail("Ctrl-C left palace --np 2 to exit 0", output)
    check_no_orphans("Ctrl-C")


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915
    """Run the interop test; exit non-zero on the first check that fails."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--install-msmpi",
        action="store_true",
        help="install the pinned system MS-MPI if none is installed",
    )
    args = parser.parse_args(argv)
    wheel = args.wheel.resolve()
    config = args.config.resolve()
    workdir = Path(tempfile.mkdtemp(prefix="palace-interop-"))
    system_root = os.environ["SYSTEMROOT"]

    system_mpiexec = system_mpiexec_path()
    if not system_mpiexec.is_file():
        if not args.install_msmpi:
            fail(f"no system MS-MPI at {system_mpiexec}; pass --install-msmpi")
        step(f"installing the system MS-MPI {msmpi.MSMPI_VERSION}")
        install_system_msmpi(workdir, system_mpiexec)
    version = check_system_msmpi(system_mpiexec, workdir)
    print(f"==> system MS-MPI: {version} at {system_mpiexec}")

    probe = write_console_script_wheel(
        workdir,
        name=PROBE_NAME,
        module="palace_parent_probe",
        source=PROBE_SOURCE,
    )
    scripts = make_wheel_venv(workdir / "venv", wheel, probe)
    env = clean_environment(os.environ, scripts, system_root)
    palace = scripts / "palace.exe"
    palace_mpiexec = scripts / "palace-mpiexec.exe"
    query = "import palace_solver as p; print(p.lib_dir())"
    done = run([scripts / "python.exe", "-c", query], cwd=workdir, timeout=120)
    if done.returncode != 0:
        fail("the installed package could not be imported", done.stdout)
    payload = Path(done.stdout.strip().splitlines()[-1])

    step("stage 1: two-rank solve under the vendored launcher")
    vendored = solve(
        "vendored", palace_mpiexec, palace, config, workdir=workdir, env=env
    )

    step("stage 2: two-rank solve under the system MS-MPI's mpiexec")
    system = solve("system", system_mpiexec, palace, config, workdir=workdir, env=env)

    step("the two launchers produce the same results")
    for line in compare_reports(
        vendored, system, labels=("vendored launcher", "system MS-MPI's")
    ):
        print(f"    {line}")

    step("stage 3: each rank's guard reads smpd as its parent")
    probe_exe = scripts / f"{PROBE_NAME}.exe"
    check_parent_read(
        "the vendored launcher",
        palace_mpiexec,
        probe_exe,
        payload / "smpd.exe",
        workdir=workdir,
        env=env,
    )
    check_parent_read(
        "the system MS-MPI",
        system_mpiexec,
        probe_exe,
        system_mpiexec.with_name("smpd.exe"),
        workdir=workdir,
        env=env,
    )

    step("stage 4: a rank with PMI_RANK and no PMI_KVS is refused")
    # The guard lets a shell parent through, so the foreign launcher has to
    # carry a process manager's name: a copy of cmd.exe as Intel MPI's proxy.
    foreign = workdir / "foreign" / "hydra_pmi_proxy.exe"
    foreign.parent.mkdir()
    shutil.copy2(Path(system_root) / "System32" / "cmd.exe", foreign)
    directory = copy_example(config, workdir / "foreign-run")
    # /s: cmd strips the outer pair of quotes and runs what is inside as is.
    command = f'"{foreign}" /d /s /c ""{palace}" --dry-run {config.name}"'
    foreign_env = dict(env, PMI_RANK="1", PMI_SIZE="2")
    done = run(command, cwd=directory, env=foreign_env, timeout=300)
    if done.returncode != 1 or "PMI_KVS" not in done.stdout:
        fail("a rank without PMI_KVS was not refused naming it", done.stdout)
    if "hydra_pmi_proxy.exe" not in done.stdout:
        fail("the refusal did not name the launcher it saw", done.stdout)
    print("    refused, naming PMI_KVS and hydra_pmi_proxy.exe")
    # What the guard is for: let through, the same rank runs as a singleton
    # and exits 0, as though nothing were wrong.
    done = run(
        command,
        cwd=directory,
        env=dict(foreign_env, PALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER="1"),
        timeout=300,
    )
    if done.returncode != 0 or count_lines(done.stdout, "Dry-run:") != 1:
        fail("with the override the rank did not run as a singleton", done.stdout)
    print("    with the override, the rank runs alone and exits 0")

    step("stage 5: a failing run's exit status passes through the console scripts")
    missing = "missing.json"
    single = run([payload / "palace-real.exe", missing], cwd=workdir, env=env)
    wrapped = run([palace, missing], cwd=workdir, env=env)
    if single.returncode == 0 or wrapped.returncode != single.returncode:
        fail(
            f"palace exited {wrapped.returncode} where palace-real.exe exited "
            f"{single.returncode}"
        )
    pair = run(
        [payload / "mpiexec.exe", "-n", "2", palace, missing], cwd=workdir, env=env
    )
    wrapped = run([palace, "--np", "2", missing], cwd=workdir, env=env)
    if pair.returncode == 0 or wrapped.returncode != pair.returncode:
        fail(
            f"palace --np 2 exited {wrapped.returncode} where mpiexec.exe exited "
            f"{pair.returncode}"
        )
    print(
        f"    {single.returncode} on one rank, as palace-real.exe; "
        f"{pair.returncode} on two, as mpiexec.exe"
    )

    step("stage 6: an interrupted two-rank solve leaves nothing running")
    ctrl_break(palace, config, workdir, env)
    ctrl_c(palace, config, workdir, env)

    step("interop test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
