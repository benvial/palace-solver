"""THROWAWAY: the Python half of the Windows spike (wayfinder ticket 07).

Lives only on research/windows-spike and is never merged. Runs under the
runner's CPython from actions/setup-python, never MSYS2's python, whose
``sysconfig.get_platform()`` is ``mingw_x86_64_ucrt`` (ticket 06, decision 8).

    python scripts/spike_windows.py closure  BINARY OUT_DIR SEARCH_DIR...
    python scripts/spike_windows.py wheels   PAYLOAD_DIR OUT_DIR
    python scripts/spike_windows.py verify   WHEEL EXAMPLE_DIR [--system-mpiexec PATH]

Every check prints a ``RESULT <name>: <value>`` line, which the workflow lifts
into the job summary and ticket 07's answer reads.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pefile

#: MS-MPI redistributable files the wheel would ship beside the solver
#: (ticket 05). msmpires.dll is listed separately: whether it is needed at run
#: time is one of the questions.
MSMPI_RUNTIME = ("msmpi.dll", "mpiexec.exe", "smpd.exe")
MSMPI_RESOURCES = "msmpires.dll"
MSMPI_TEXTS = ("MicrosoftMPI_Redistributable_EULA.rtf", "MPI_Redistributables_TPN.txt")


def result(name: str, value: object) -> None:
    print(f"RESULT {name}: {value}", flush=True)


def imports(path: Path) -> list[str]:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
    )
    return sorted(
        {entry.dll.decode() for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])},
        key=str.lower,
    )


def closure(binary: Path, out_dir: Path, search: list[Path]) -> None:
    """Copy every non-system DLL ``binary`` needs, transitively, into out_dir.

    A DLL counts as vendored when it resolves in one of the search directories
    (the install prefix, MSYS2's ucrt64/bin, the extracted MS-MPI redist), and
    as system otherwise — the same split delvewheel and the link check make.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: dict[str, Path | None] = {}
    queue = [binary]
    while queue:
        current = queue.pop()
        for name in imports(current):
            key = name.lower()
            if key in seen:
                continue
            found = next(
                (d / name for d in search if (d / name).is_file()),
                None,
            )
            seen[key] = found
            if found is not None:
                queue.append(found)
    for key, found in sorted(seen.items()):
        where = "SYSTEM" if found is None else str(found)
        print(f"  {key:40s} {where}")
        if found is not None:
            shutil.copy2(found, out_dir / found.name)
    result("palace imports", ", ".join(imports(binary)))
    result(
        "vendored DLLs",
        ", ".join(sorted(p.name for p in seen.values() if p is not None)),
    )
    result("system DLLs", ", ".join(sorted(k for k, p in seen.items() if p is None)))


def _build_wheel(repo: Path, payload: Path, out_dir: Path, *, with_dlls: bool) -> Path:
    """Stage ``payload`` into a copy of the package and build a py3-none wheel."""
    work = Path(tempfile.mkdtemp(prefix="spike-wheel-"))
    project = work / "project"
    shutil.copytree(
        repo,
        project,
        ignore=shutil.ignore_patterns(".git", "build", "*.egg-info", "wheelhouse"),
    )
    bin_dir = project / "palace_solver" / "bin"
    for path in payload.iterdir():
        if with_dlls or path.suffix.lower() == ".exe":
            shutil.copy2(path, bin_dir / path.name)
    raw = work / "raw"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(raw), str(project)],
        check=True,
    )
    (built,) = raw.glob("*.whl")
    subprocess.run(
        [
            sys.executable, "-m", "wheel", "tags", "--remove",
            "--python-tag", "py3", "--abi-tag", "none", "--platform-tag", "win_amd64",
            str(built),
        ],
        check=True,
    )
    (tagged,) = raw.glob("*.whl")
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / tagged.name
    shutil.move(tagged, final)
    return final


def _size(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        unpacked = sum(info.file_size for info in archive.infolist())
        largest = max(archive.infolist(), key=lambda info: info.file_size)
    mib = 1024 * 1024
    return (
        f"{wheel.stat().st_size / mib:.1f} MiB compressed, {unpacked / mib:.1f} MiB "
        f"unpacked, largest member {largest.filename} {largest.file_size / mib:.1f} MiB"
    )


def wheels(payload: Path, out_dir: Path) -> None:
    """Build the two wheels the spike compares, and run delvewheel on one.

    ``flat``: every DLL hand-placed beside the executables — what ticket 02
    expects a Windows wheel to need, because an .exe has no RPATH.
    ``exe-only``: executables only, then ``delvewheel repair`` with and without
    ``--analyze-existing-exes``, to see what the repair tool does on its own.
    """
    repo = Path(__file__).resolve().parent.parent
    flat = _build_wheel(repo, payload, out_dir / "flat", with_dlls=True)
    result("flat wheel", f"{flat.name} — {_size(flat)}")

    exe_only = _build_wheel(repo, payload, out_dir / "exe-only-raw", with_dlls=False)
    result("exe-only raw wheel", f"{exe_only.name} — {_size(exe_only)}")
    add_path = os.pathsep.join([str(payload)])
    show = subprocess.run(
        ["delvewheel", "show", "--add-path", add_path, str(exe_only)],
        capture_output=True, text=True,
    )
    print(show.stdout, show.stderr)
    result("delvewheel show rc", show.returncode)
    for label, extra in (
        ("plain", []),
        ("analyze-existing-exes", ["--analyze-existing-exes"]),
    ):
        target = out_dir / f"delvewheel-{label}"
        repair = subprocess.run(
            [
                "delvewheel", "repair", "--add-path", add_path,
                # MS-MPI's own files are never renamed; a mangled msmpi.dll is
                # not the one a user's mpiexec would load.
                "--no-mangle", "msmpi.dll",
                *extra, "-w", str(target), str(exe_only),
            ],
            capture_output=True, text=True,
        )
        print(repair.stdout, repair.stderr)
        result(f"delvewheel repair ({label}) rc", repair.returncode)
        for repaired in target.glob("*.whl"):
            result(f"delvewheel repair ({label})", f"{repaired.name} — {_size(repaired)}")
            with zipfile.ZipFile(repaired) as archive:
                dlls = [n for n in archive.namelist() if n.lower().endswith(".dll")]
            result(f"delvewheel repair ({label}) DLL locations", ", ".join(dlls) or "none")


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None,
         timeout: int = 900) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(cmd), flush=True)
    done = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )
    tail = (done.stdout + done.stderr).splitlines()[-25:]
    print("\n".join(tail), flush=True)
    return done


def _palace_processes() -> list[str]:
    listing = subprocess.run(
        ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True
    ).stdout
    return [line for line in listing.splitlines()
            if any(n in line.lower() for n in ("palace-real", "smpd", "mpiexec"))]


def verify(wheel: Path, example: Path, system_mpiexec: Path | None) -> None:
    """Install the flat wheel into a clean venv and run the ticket's checks."""
    work = Path(tempfile.mkdtemp(prefix="spike-verify-"))
    venv = work / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "Scripts" / "python.exe"
    subprocess.run([str(python), "-m", "pip", "install", "--quiet", str(wheel)], check=True)
    package = Path(subprocess.run(
        [str(python), "-c", "import palace_solver, pathlib; print(pathlib.Path(palace_solver.__file__).parent)"],
        capture_output=True, text=True, check=True,
    ).stdout.strip())
    bin_dir = package / "bin"
    palace = bin_dir / "palace-real.exe"
    mpiexec = bin_dir / "mpiexec.exe"
    result("installed payload", ", ".join(sorted(p.name for p in bin_dir.iterdir())))

    run_dir = work / "example"
    shutil.copytree(example, run_dir)
    config = run_dir / "spheres.json"

    # The environment a user has: no MSYS2, no MS-MPI install, nothing on PATH
    # that could satisfy a DLL the wheel forgot.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MSMPI", "PMI_"))}
    env["PATH"] = os.pathsep.join(
        p for p in env.get("PATH", "").split(os.pathsep)
        if "msys64" not in p.lower() and "mingw" not in p.lower() and "git" not in p.lower()
    )
    env["MSMPI_LOCAL_ONLY"] = "1"
    env["OMP_NUM_THREADS"] = "1"

    version = _run([str(palace), "--version"], cwd=run_dir, env=env, timeout=120)
    result("palace --version", version.returncode == 0 and version.stdout.strip().splitlines()[:1])

    single = _run([str(palace), "-dry-run", config.name], cwd=run_dir, env=env)
    result("singleton dry-run (no launcher)", f"rc={single.returncode}")

    solve = _run([str(palace), config.name], cwd=run_dir, env=env, timeout=1800)
    result("singleton solve", f"rc={solve.returncode}")

    def two_ranks(launcher: Path, label: str, extra_env: dict[str, str] | None = None) -> None:
        local_env = dict(env, **(extra_env or {}))
        done = _run([str(launcher), "-n", "2", str(palace), "-dry-run", config.name],
                    cwd=run_dir, env=local_env)
        count = (done.stdout + done.stderr).count("Dry-run:")
        result(f"two ranks under {label}", f"rc={done.returncode}, Dry-run lines={count} (1 means one MPI_COMM_WORLD)")

    two_ranks(mpiexec, "vendored mpiexec")

    solve2 = _run([str(mpiexec), "-n", "2", str(palace), config.name], cwd=run_dir, env=env, timeout=1800)
    result("two-rank solve under vendored mpiexec", f"rc={solve2.returncode}")

    # msmpires.dll: take it away and run again, plus an mpiexec error path whose
    # message text would come from the resource DLL.
    resources = bin_dir / MSMPI_RESOURCES
    if resources.exists():
        hidden = resources.with_suffix(".hidden")
        resources.rename(hidden)
        two_ranks(mpiexec, "vendored mpiexec without msmpires.dll")
        bad = _run([str(mpiexec), "-n", "2", "-nonexistent-option", str(palace)], cwd=run_dir, env=env)
        result("mpiexec error text without msmpires.dll", (bad.stdout + bad.stderr).strip()[:200] or "<empty>")
        hidden.rename(resources)
        bad = _run([str(mpiexec), "-n", "2", "-nonexistent-option", str(palace)], cwd=run_dir, env=env)
        result("mpiexec error text with msmpires.dll", (bad.stdout + bad.stderr).strip()[:200] or "<empty>")
    else:
        result("msmpires.dll", "not staged")

    # The guard's regression case: PMI_RANK without PMI_KVS.
    orphan = _run([str(palace), "-dry-run", config.name], cwd=run_dir,
                  env=dict(env, PMI_RANK="1", PMI_SIZE="2"))
    result("PMI_RANK without PMI_KVS", f"rc={orphan.returncode}, Dry-run lines={(orphan.stdout + orphan.stderr).count('Dry-run:')} (1 means a silent singleton)")

    # Exit-code and Ctrl-C propagation through the subprocess.run wrapper shape
    # ticket 05 chose for _exec.py on Windows.
    wrapper = work / "wrapper.py"
    wrapper.write_text(
        "import subprocess, sys\n"
        "try:\n"
        "    sys.exit(subprocess.run(sys.argv[1:]).returncode)\n"
        "except KeyboardInterrupt:\n"
        "    sys.exit(130)\n"
    )
    missing = _run([str(python), str(wrapper), str(mpiexec), "-n", "2", str(palace), "missing.json"],
                   cwd=run_dir, env=env)
    result("exit code through wrapper (missing config)", missing.returncode)

    before = _palace_processes()
    process = subprocess.Popen(
        [str(python), str(wrapper), str(mpiexec), "-n", "2", str(palace), config.name],
        cwd=run_dir, env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    time.sleep(8)
    running = _palace_processes()
    process.send_signal(signal.CTRL_BREAK_EVENT)
    try:
        rc = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        rc = "hung, killed"
    time.sleep(5)
    after = _palace_processes()
    result("ctrl-break rc", rc)
    result("processes before/during/after", f"{len(before)}/{len(running)}/{len(after)}")
    if after:
        print("\n".join(after))
        subprocess.run(["taskkill", "/f", "/im", "palace-real.exe"], check=False)
        subprocess.run(["taskkill", "/f", "/im", "smpd.exe"], check=False)

    if system_mpiexec is not None:
        two_ranks(system_mpiexec, f"system MS-MPI ({system_mpiexec})")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("closure")
    p.add_argument("binary", type=Path)
    p.add_argument("out_dir", type=Path)
    p.add_argument("search", type=Path, nargs="+")
    p = sub.add_parser("wheels")
    p.add_argument("payload", type=Path)
    p.add_argument("out_dir", type=Path)
    p = sub.add_parser("verify")
    p.add_argument("wheel", type=Path)
    p.add_argument("example", type=Path)
    p.add_argument("--system-mpiexec", type=Path, default=None)
    args = parser.parse_args()
    if args.command == "closure":
        closure(args.binary, args.out_dir, args.search)
    elif args.command == "wheels":
        wheels(args.payload, args.out_dir)
    else:
        verify(args.wheel, args.example, args.system_mpiexec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
