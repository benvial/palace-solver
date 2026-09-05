"""Refuse to start a rank that has been launched without an MPI rendezvous.

The wheel vendors its own MPICH, so a Palace rank can be started by a process
manager that did not ship with it — most often the ``mpiexec`` from the PyPI
``mpich`` wheel that palais pulls in for mpi4py. Almost everything that can go
wrong there fails loudly. One thing does not.

When a process manager starts the ranks but gives them no way to find each
other — no PMI or PMIx rendezvous in their environment — MPICH does not fail.
Each rank initialises as a singleton, becomes its own ``MPI_COMM_WORLD``,
solves the whole problem alone, writes over the other ranks' output and exits
0. Measured on the 0.17.0 wheel by stripping ``PMI_*`` from a normal ``-n 2``
launch: Palace's rank-0 line appeared twice instead of once, exit status 0, no
diagnostic anywhere. That is what :func:`refusal_reason` catches, and it is
worth a hard error because nothing downstream will notice.

The check is deliberately not a version comparison. MPICH 5.0.1's Hydra
launching this 4.3.2-linked binary was measured to produce results identical to
the vendored launcher's, so refusing it would break a working setup while
missing the failure above, which no version tells you about.
:func:`version_note` reports a differing major series as a remark on stderr, not
as a refusal.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from palace_solver import MPICH_VERSION

#: Environment variables through which a process manager tells a rank how to
#: reach its peers. Any one of them means the rendezvous was handed over.
#:
#: Named individually rather than matched by prefix: Hydra also exports
#: ``PMI_HOSTNAME``, which is informational and present even when the
#: rendezvous proper is not, so a ``PMI_`` prefix match would wave through the
#: exact case this module exists to catch.
RENDEZVOUS_VARIABLES = (
    "PMI_FD",
    "PMI_PORT",
    "PMI_RANK",
    "PMIX_RANK",
    "PMIX_NAMESPACE",
    "PMIX_SERVER_URI",
    "OMPI_COMM_WORLD_RANK",
)

#: Executables that, as our parent, mean a process manager started this rank.
PROCESS_MANAGERS = frozenset(
    {
        "hydra_pmi_proxy",
        "hydra_bstrap_proxy",
        "mpiexec",
        "mpiexec.hydra",
        "mpiexec.gforker",
        "mpirun",
        "prterun",
        "orted",
        "orterun",
        "srun",
        "slurmstepd",
    }
)

#: Variables through which a launcher says how many ranks it started. Read only
#: to recognise a deliberate single-rank launch, which needs no rendezvous.
RANK_COUNT_VARIABLES = ("MPI_LOCALNRANKS", "SLURM_NTASKS", "OMPI_COMM_WORLD_SIZE")

#: Set to a non-empty value to launch anyway.
OVERRIDE_ENV = "PALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER"

#: How long to wait for a foreign ``mpiexec --version`` to answer.
PROBE_TIMEOUT_SECONDS = 10

#: Names of the launcher executable to probe inside a foreign install.
PROBE_CANDIDATES = ("mpiexec", "mpiexec.hydra", "mpirun")

_HYDRA_VERSION = re.compile(r"^\s*Version:\s*(\S+)", re.MULTILINE)


def has_rendezvous(environ: Mapping[str, str]) -> bool:
    """Whether the environment carries a way for this rank to find its peers."""
    return any(name in environ for name in RENDEZVOUS_VARIABLES)


def launched_by_process_manager(parent_exe: Path | None) -> bool:
    """Whether ``parent_exe`` is the process manager that started this rank.

    Args:
        parent_exe: Executable of the launching process, or ``None`` where it
            could not be read.

    Returns:
        ``True`` when the parent is a known process manager.
    """
    return parent_exe is not None and parent_exe.name in PROCESS_MANAGERS


def requested_ranks(environ: Mapping[str, str]) -> int | None:
    """Return how many ranks the launcher says it started, if it says.

    Args:
        environ: Environment of this rank.

    Returns:
        The rank count, or ``None`` when no launcher variable gives one.
    """
    for name in RANK_COUNT_VARIABLES:
        value = environ.get(name, "")
        if value.isdigit():
            return int(value)
    return None


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


def refusal_reason(environ: Mapping[str, str], parent_exe: Path | None) -> str | None:
    """Return why this rank must not start, if it must not.

    Args:
        environ: Environment of this rank.
        parent_exe: Executable of the launching process, from
            :func:`parent_executable`.

    Returns:
        An operator-facing message, or ``None`` when the launch is allowed.
        A rank not started by a process manager, one holding a rendezvous, and
        one the launcher says is alone are all allowed.
    """
    if environ.get(OVERRIDE_ENV):
        return None
    if not launched_by_process_manager(parent_exe) or has_rendezvous(environ):
        return None
    if requested_ranks(environ) == 1:
        return None
    return (
        f"refusing to run: {parent_exe} started this rank but handed it no MPI "
        "rendezvous — none of "
        + ", ".join(RENDEZVOUS_VARIABLES)
        + " is set. Every rank would "
        "initialise as its own MPI_COMM_WORLD, solve the whole problem alone, "
        "write over the other ranks' output and exit 0, with no error anywhere "
        "and results that look plausible. Use the launcher shipped with this "
        f"wheel:\n    palace-mpiexec -n <ranks> palace <config>\nSet "
        f"{OVERRIDE_ENV}=1 to launch anyway."
    )


def parse_hydra_version(output: str) -> str | None:
    """Extract the MPICH release from ``mpiexec --version`` output.

    Args:
        output: Whatever the launcher printed.

    Returns:
        The version string, or ``None`` if the output is not Hydra's.
    """
    match = _HYDRA_VERSION.search(output)
    return None if match is None else match.group(1)


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


def version_note(
    parent_exe: Path | None,
    *,
    vendored_version: str = MPICH_VERSION,
    vendored_bin: Path | None = None,
    probe: Callable[[Path], str | None] | None = None,
) -> str | None:
    """Return a remark about a launcher from another MPICH major series.

    This is diagnosis, not a verdict: such a pairing was measured to work, and
    the note exists so that a log carries the fact if something else later
    looks wrong.

    Args:
        parent_exe: Executable of the launching process.
        vendored_version: MPICH release built into this wheel.
        vendored_bin: Directory holding the vendored process manager. Defaults
            to the one in the installed package.
        probe: How to read a foreign launcher's version. Defaults to running
            its ``mpiexec --version``.

    Returns:
        A one-line remark, or ``None`` when there is nothing to say.
    """
    if not launched_by_process_manager(parent_exe) or parent_exe is None:
        return None
    if vendored_bin is None:
        from palace_solver import mpiexec_path  # noqa: PLC0415

        try:
            vendored_bin = mpiexec_path().parent
        except FileNotFoundError:
            return None
    launcher_dir = parent_exe.parent
    if launcher_dir == vendored_bin:
        return None
    read_version = probe_launcher_version if probe is None else probe
    launcher_version = read_version(launcher_dir)
    if launcher_version is None:
        return None
    if launcher_version.split(".", 1)[0] == vendored_version.split(".", 1)[0]:
        return None
    return (
        f"note: started by an MPICH {launcher_version} process manager while "
        f"this wheel vendors MPICH {vendored_version}. That pairing is not the "
        "supported one (palace-mpiexec is), though it has been measured to "
        "work within the MPI features Palace uses."
    )
