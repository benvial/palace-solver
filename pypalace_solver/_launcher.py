"""Refuse to start the packaged solver under an incompatible MPI launcher.

The wheel vendors its own MPICH (ADR-0002), so a Palace rank can be started by
an ``mpiexec`` that did not ship with it — most often the one from the PyPI
``mpich`` wheel, which pypalace pulls in for mpi4py. That works only while both
sides belong to the same MPICH major series, which is what pypalace's
``mpich<5`` pin encodes. Across a major bump the PMI handshake between the
launcher and the rank is not guaranteed, and its failure mode is silent: every
rank initialises as its own ``MPI_COMM_WORLD``, the solve runs to completion
and the answer is wrong.

``palace-mpiexec``, the launcher vendored in this wheel, is the supported way
to start multi-rank runs. When another one is used, this module identifies it
and refuses the launch if its major version differs from the vendored MPICH.
The check proves a mismatch; it cannot prove a match, so a launcher whose
version cannot be read — Slurm's ``srun``, a foreign process manager with
different ``--version`` output — is allowed through.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from pypalace_solver import MPICH_VERSION, mpiexec_path

#: Environment variables MPICH's Hydra sets in every rank it starts. Their
#: presence is what separates a launched rank from a direct single-rank run.
PROCESS_MANAGER_MARKERS = ("PMI_RANK", "PMI_SIZE", "PMI_FD")

#: Set to a non-empty value to run under a mismatching launcher regardless.
OVERRIDE_ENV = "PYPALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER"

#: How long to wait for a foreign ``mpiexec --version`` to answer.
PROBE_TIMEOUT_SECONDS = 10

#: Names of the launcher executable to probe inside a foreign install.
PROBE_CANDIDATES = ("mpiexec", "mpiexec.hydra", "mpirun")

_HYDRA_VERSION = re.compile(r"^\s*Version:\s*(\S+)", re.MULTILINE)


def launched_by_process_manager(environ: Mapping[str, str]) -> bool:
    """Whether this process was started by an MPI process manager."""
    return any(marker in environ for marker in PROCESS_MANAGER_MARKERS)


def parse_hydra_version(output: str) -> str | None:
    """Extract the MPICH release from ``mpiexec --version`` output.

    Args:
        output: Whatever the launcher printed.

    Returns:
        The version string, or ``None`` if the output is not Hydra's.
    """
    match = _HYDRA_VERSION.search(output)
    return None if match is None else match.group(1)


def incompatibility(vendored: str, launcher: str) -> str | None:
    """Describe why a launcher cannot be trusted with the vendored MPICH.

    MPICH keeps its ABI and its PMI wire protocol stable within a major
    series, so only a differing major version is treated as a conflict.

    Args:
        vendored: MPICH release built into this wheel.
        launcher: MPICH release of the process manager that started this rank.

    Returns:
        An operator-facing message, or ``None`` if the two are compatible.
    """
    if vendored.split(".", 1)[0] == launcher.split(".", 1)[0]:
        return None
    return (
        f"refusing to run: this rank was started by an MPICH {launcher} process "
        f"manager, but the solver is linked against the MPICH {vendored} vendored "
        "in pypalace-solver. Across a major version the PMI handshake is not "
        "guaranteed, and when it fails every rank runs as its own MPI_COMM_WORLD "
        "and the results are silently wrong. Use the launcher shipped with this "
        f"wheel:\n    palace-mpiexec -n <ranks> palace <config>\nSet "
        f"{OVERRIDE_ENV}=1 to run under this launcher anyway."
    )


def parent_executable() -> Path | None:
    """Return the executable of the process that started this one.

    Returns:
        Absolute path of the parent's executable, or ``None`` where ``/proc``
        does not answer (a non-Linux host, a vanished parent).
    """
    try:
        return Path(f"/proc/{os.getppid()}/exe").readlink()
    except OSError:
        return None


def foreign_launcher_dir(*, vendored_bin: Path, parent_exe: Path | None) -> Path | None:
    """Return the ``bin`` directory of a launcher that is not this wheel's.

    Args:
        vendored_bin: Directory holding the wheel's own process manager.
        parent_exe: Executable of the launching process, as resolved by
            :func:`parent_executable`.

    Returns:
        The foreign launcher's directory, or ``None`` when the launch came
        from the vendored process manager or cannot be attributed.
    """
    if parent_exe is None:
        return None
    launcher_dir = parent_exe.parent
    if launcher_dir == vendored_bin:
        return None
    return launcher_dir


def probe_launcher_version(launcher_dir: Path) -> str | None:
    """Ask the process manager in ``launcher_dir`` which MPICH it is.

    Args:
        launcher_dir: Directory holding the foreign launcher.

    Returns:
        Its MPICH release, or ``None`` if no launcher there answers with one.
    """
    for name in PROBE_CANDIDATES:
        candidate = launcher_dir / name
        if not candidate.is_file():
            continue
        try:
            completed = subprocess.run(
                [str(candidate), "--version"],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        version = parse_hydra_version(completed.stdout + completed.stderr)
        if version is not None:
            return version
    return None


def refusal_reason(
    environ: Mapping[str, str] | None = None,
    *,
    vendored_version: str = MPICH_VERSION,
    vendored_bin: Path | None = None,
    parent_exe: Path | None = None,
    probe: Callable[[Path], str | None] | None = None,
) -> str | None:
    """Return the reason this rank must not start, if there is one.

    Args:
        environ: Environment to inspect. Defaults to this process's.
        vendored_version: MPICH release built into this wheel.
        vendored_bin: Directory holding the vendored process manager.
            Defaults to the one in the installed package.
        parent_exe: Executable of the launching process. Defaults to the one
            ``/proc`` reports.
        probe: How to read a foreign launcher's version. Defaults to running
            its ``mpiexec --version``.

    Returns:
        An operator-facing message, or ``None`` when the launch is allowed.
    """
    environ = os.environ if environ is None else environ
    if not launched_by_process_manager(environ) or environ.get(OVERRIDE_ENV):
        return None
    if vendored_bin is None:
        try:
            vendored_bin = mpiexec_path().parent
        except FileNotFoundError:
            return None
    launcher_dir = foreign_launcher_dir(
        vendored_bin=vendored_bin,
        parent_exe=parent_executable() if parent_exe is None else parent_exe,
    )
    if launcher_dir is None:
        return None
    read_version = probe_launcher_version if probe is None else probe
    launcher_version = read_version(launcher_dir)
    if launcher_version is None:
        return None
    return incompatibility(vendored_version, launcher_version)
