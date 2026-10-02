"""Shared helpers for the Windows twins of the wheel tests. Imported, not run.

``scripts/smoke-test-windows.py`` and ``scripts/interop-test-windows.py`` ask
of the ``win_amd64`` wheel what ``scripts/smoke-test.sh`` and
``scripts/interop-test.sh`` ask of the others, and this module is their
``_wheel_venv.sh``.

They are separate Python scripts rather than Windows branches of the bash ones
because the bash on a Windows runner is Git for Windows', an MSYS2 runtime:

- A test run from it starts every check with that runtime's ``/usr/bin`` and
  ``/mingw64/bin`` on ``PATH``, and the claim being tested is that the wheel
  needs no MSYS2 on ``PATH``. Here every check runs from a native Python with a
  ``PATH`` built from nothing (:func:`clean_environment`).
- The Windows checks are about Windows process trees: a console Ctrl-Break to
  a process group, a Ctrl-C on a console of its own, a renamed ``cmd.exe``
  standing in for a foreign launcher, ``tasklist`` for orphaned ranks. bash can
  start none of those without calling back into something native.
- The interop stages differ in substance, not spelling. The foreign launcher is
  a system MS-MPI, not the PyPI ``mpich`` wheel; the rendezvous is ``PMI_KVS``
  alone; and there is no next MPICH major series to try. Branches would be
  most of the script.

Every caller works from a temporary directory outside the repository, for the
reason ``_wheel_venv.sh`` gives: a checkout's ``palace_solver`` would shadow the
installed one.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

#: The checkout these scripts live in. ``wheelbuild`` is imported from it, as
#: ``smoke-test.sh`` does with ``PYTHONPATH``, never from the wheel under test.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Variables a check must not inherit from the job: an MPI rendezvous or
#: MS-MPI setting would change what is being measured, and the override would
#: switch off the guard the interop test exercises.
DROPPED_PREFIXES = ("PMI_", "MSMPI_")
DROPPED_NAMES = ("PALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER", "OMP_NUM_THREADS")

#: Directories under ``%SystemRoot%`` a check keeps on ``PATH``: Windows
#: itself, and nothing that could satisfy a DLL the wheel forgot to ship.
SYSTEM_PATH_DIRECTORIES = (
    "System32",
    "",
    "System32\\Wbem",
    "System32\\WindowsPowerShell\\v1.0",
)

#: What a solve leaves for the launchers to be compared on.
POSTPROCESSING_GLOB = "postpro/*.csv"

#: Relative tolerance for two launchers' postprocessing values, as
#: ``interop-test.sh`` has it.
TOLERANCE = 1e-9


def step(message: str) -> None:
    """Announce a check, as the bash scripts' ``echo "==> ..."`` lines do."""
    print(f"==> {message}", flush=True)


def fail(message: str, log: str | None = None) -> NoReturn:
    """Stop the test, showing the end of ``log`` first when there is one."""
    if log:
        print("\n".join(log.splitlines()[-30:]), file=sys.stderr)
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def clean_environment(
    environ: Mapping[str, str], scripts: Path, system_root: str
) -> dict[str, str]:
    r"""Return the environment a user who installed only the wheel would have.

    ``PATH`` is rebuilt rather than filtered: the virtual environment's
    ``Scripts`` directory, then Windows' own directories. A filter would have
    to know every toolchain a runner image might carry (MSYS2, Git's MinGW,
    Strawberry Perl's gcc runtime), and one it missed could hand the solver a
    DLL the wheel does not ship.

    Args:
        environ: The environment to start from, usually ``os.environ``.
        scripts: The virtual environment's ``Scripts`` directory.
        system_root: ``%SystemRoot%``, usually ``C:\Windows``.

    Returns:
        A new environment.
    """
    environment = {
        name: value
        for name, value in environ.items()
        if name.upper() != "PATH"
        and not name.upper().startswith(DROPPED_PREFIXES)
        and name.upper() not in DROPPED_NAMES
    }
    root = system_root.rstrip("\\")
    system = [f"{root}\\{d}" if d else root for d in SYSTEM_PATH_DIRECTORIES]
    environment["PATH"] = ";".join([str(scripts), *system])
    return environment


def make_wheel_venv(venv: Path, wheel: Path, *requirements: str | Path) -> Path:
    """Create a virtual environment holding the wheel and nothing else.

    Args:
        venv: Where to create it.
        wheel: The wheel under test.
        requirements: Anything else to install beside it.

    Returns:
        The environment's ``Scripts`` directory.
    """
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    scripts = venv / "Scripts"
    python = scripts / "python.exe"
    environment = dict(os.environ, PIP_NO_CACHE_DIR="1")
    pip = [str(python), "-m", "pip", "install", "--quiet"]
    subprocess.run([*pip, "--upgrade", "pip"], check=True, env=environment)
    subprocess.run(
        [*pip, str(wheel), *map(str, requirements)], check=True, env=environment
    )
    return scripts


def copy_example(config: Path, destination: Path) -> Path:
    """Copy the example directory holding ``config`` somewhere writable.

    Palace resolves the mesh relative to the working directory and writes its
    output beside the config, so each run needs its own copy.

    Returns:
        The copied directory.
    """
    shutil.copytree(config.parent, destination)
    for path in destination.rglob("*"):
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    return destination


def run(
    command: Sequence[str | Path] | str,
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 1800,
) -> subprocess.CompletedProcess[str]:
    """Run ``command``, capturing both streams together, and echo its tail.

    A string is a Windows command line, passed on as written, for the quoting
    ``cmd /s /c`` wants and ``list2cmdline`` cannot produce.
    """
    if isinstance(command, str):
        arguments: list[str] | str = command
        print(f"$ {command}", flush=True)
    else:
        arguments = [str(argument) for argument in command]
        print("$ " + subprocess.list2cmdline(arguments), flush=True)
    done = subprocess.run(
        arguments,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    print("\n".join(done.stdout.splitlines()[-15:]), flush=True)
    print(f"    exit status {done.returncode}", flush=True)
    return done


def count_lines(output: str, marker: str) -> int:
    """How many lines of ``output`` start with ``marker``.

    Lines are split on any ending, since a console program on Windows ends
    them with CRLF and a ``$``-anchored grep would miss every one.
    """
    return sum(line.startswith(marker) for line in output.splitlines())


def count_occurrences(output: str, marker: str) -> int:
    """How many lines of ``output`` contain ``marker`` anywhere."""
    return sum(marker in line for line in output.splitlines())


def _numbers(path: Path) -> list[float]:
    """Every float in a Palace postprocessing CSV, header row skipped."""
    rows = path.read_text().splitlines()[1:]
    return [float(field) for row in rows for field in row.split(",") if field.strip()]


def compare_reports(first: Path, second: Path, *, labels: Iterable[str]) -> list[str]:
    """Check two solves of one example produced the same postprocessing.

    Args:
        first: Directory the first solve ran in.
        second: Directory the second solve ran in.
        labels: How to name each of the two in a message.

    Returns:
        One line per report that agrees.

    Raises:
        SystemExit: If a report is missing, or differs in shape or in a value.
    """
    first_label, second_label = labels
    reports = sorted(first.glob(POSTPROCESSING_GLOB))
    if not reports:
        fail(f"no postprocessing output under {first}")
    agreed = []
    for report in reports:
        other = second / "postpro" / report.name
        if not other.is_file():
            fail(f"{report.name} is missing from the {second_label} run")
        left, right = _numbers(report), _numbers(other)
        if len(left) != len(right):
            fail(f"{report.name} has a different shape under each launcher")
        for column, (a, b) in enumerate(zip(left, right, strict=True)):
            if abs(a - b) > TOLERANCE * max(abs(a), abs(b), 1.0):
                fail(
                    f"{report.name} value {column} differs between launchers: "
                    f"{a!r} under the {first_label}, {b!r} under the {second_label}"
                )
        agreed.append(f"{report.name}: {len(left)} values agree")
    return agreed


def running_images(image: str) -> int:
    """How many processes are running ``image``, a file name like ``a.exe``."""
    listing = subprocess.run(
        ["tasklist", "/fi", f"imagename eq {image}", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    ).stdout
    return count_lines(listing.lower(), f'"{image.lower()}"')


def _record_hash(data: bytes) -> str:
    """A file's hash as a wheel's ``RECORD`` spells it."""
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def write_console_script_wheel(
    directory: Path, *, name: str, module: str, source: str
) -> Path:
    r"""Write a one-module wheel whose console script runs ``module:main``.

    pip turns the entry point into a ``Scripts\<name>.exe`` with the same
    launcher it gives ``palace.exe``, so a check that needs the console
    script's process tree, but a different answer from the end of it, can have
    one without touching the package under test.

    Args:
        directory: Where to write the wheel.
        name: Distribution and console-script name, with hyphens.
        module: Name of the single module.
        source: The module's source; it must define ``main()``.

    Returns:
        The wheel.
    """
    distribution = name.replace("-", "_")
    dist_info = f"{distribution}-0.dist-info"
    files = {
        f"{module}.py": source,
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 0\n",
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: palace-solver-tests\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": (
            f"[console_scripts]\n{name} = {module}:main\n"
        ),
    }
    wheel = directory / f"{distribution}-0-py3-none-any.whl"
    record = []
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in files.items():
            data = text.encode()
            archive.writestr(path, data)
            record.append(f"{path},{_record_hash(data)},{len(data)}")
        record.append(f"{dist_info}/RECORD,,")
        archive.writestr(f"{dist_info}/RECORD", "\n".join(record) + "\n")
    return wheel
