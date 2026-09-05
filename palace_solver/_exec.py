"""Console-script wrapper that hands control to the packaged Palace binary.

Upstream Palace ships ``palace`` as a bash wrapper that parses ``--np`` and
friends and re-invokes ``mpirun -n N palace-<arch>.bin``. This module is that
wrapper's replacement, and keeps its command line so that callers written
against upstream — palais's runner among them, which passes ``--np`` — work
against the packaged solver unchanged.

Two roles share one entry point. Asked for ranks, the script is the *driver*:
it execs a process manager, which starts the ranks by invoking this same
script again, so every rank still passes the guard in :mod:`._launcher`. Asked
for nothing but a config, it is a *rank*: it checks the guard and execs the
binary. The difference is only whether ``--np`` was given, which is why a rank
started by ``palace-mpiexec -n 2 palace config.json`` — where the launcher, not
the caller, decides the rank count — takes the direct path.

The default launcher is the ``mpiexec`` vendored in this wheel, not upstream's
``mpirun`` from ``PATH``: the wheel carries its own MPICH, and a ``mpirun``
found on ``PATH`` belongs to some other MPI install.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from palace_solver import _launcher, binary_path, console_script_path, mpiexec_path

#: Help text, mirroring the options of upstream's wrapper script so that the
#: two answer ``--help`` with the same vocabulary.
USAGE = """Usage: palace [OPTIONS] CONFIG_FILE

Wrapper for launching the Palace binary packaged in this wheel

Options:
  -h, --help                       Show this help message and exit
  -dry-run, --dry-run              Parse configuration file for errors and exit
  -serial, --serial                Call Palace without MPI launcher, default is false
  -np, --np NUM_PROCS              How many MPI processes to use, default is 1
  -nt, --nt NUM_THREADS            Number of OpenMP threads to use, default is 1 or
                                   the value of OMP_NUM_THREADS in the environment
  -launcher, --launcher LAUNCHER   MPI launcher, default is the palace-mpiexec
                                   vendored in this wheel
  -launcher-args,
    --launcher-args ARGS           Extra arguments for the MPI launcher (quoted)
"""


@dataclass(frozen=True)
class Invocation:
    """What a ``palace`` command line asks for.

    Attributes:
        palace_args: Arguments for the Palace binary itself, config file
            included, in the order they were given.
        num_procs: Rank count from ``--np``, or ``None`` when unasked.
        num_threads: OpenMP thread count from ``--nt``, or ``None``.
        serial: Whether ``--serial`` forbade the MPI launcher.
        launcher: Launcher named by ``--launcher``, or ``None`` for the
            vendored one.
        launcher_args: Extra launcher arguments, already word-split.
        help_requested: Whether ``--help`` was given.
    """

    palace_args: list[str] = field(default_factory=list)
    num_procs: int | None = None
    num_threads: int | None = None
    serial: bool = False
    launcher: str | None = None
    launcher_args: list[str] = field(default_factory=list)
    help_requested: bool = False


def parse_arguments(arguments: Sequence[str]) -> Invocation:
    """Split a ``palace`` command line into wrapper options and Palace arguments.

    Both spellings of every option are accepted (``-np`` and ``--np``), as
    upstream's wrapper accepts them. Anything unrecognised belongs to Palace
    and keeps its position; a bare ``-`` or ``--`` ends option parsing.

    Args:
        arguments: Arguments the console script was invoked with.

    Returns:
        The parsed invocation.

    Raises:
        SystemExit: If an option that takes a value was given none, or ``--np``
            or ``--nt`` was given something that is not a positive integer.
    """
    palace_args: list[str] = []
    values: dict[str, str] = {}
    serial = False
    help_requested = False
    remaining = list(arguments)
    while remaining:
        argument = remaining.pop(0)
        if argument in ("-h", "--help"):
            help_requested = True
        elif argument in ("-serial", "--serial", "-sequential", "--sequential"):
            serial = True
        elif argument in ("-np", "--np", "-nt", "--nt"):
            values[argument.lstrip("-")] = _take_value(argument, remaining)
        elif argument in ("-launcher", "--launcher"):
            values["launcher"] = _take_value(argument, remaining)
        elif argument in ("-launcher-args", "--launcher-args"):
            values["launcher-args"] = _take_value(argument, remaining)
        elif argument in ("-", "--"):
            palace_args.extend(remaining)
            break
        else:
            palace_args.append(argument)
    return Invocation(
        palace_args=palace_args,
        num_procs=_positive_integer("--np", values.get("np")),
        num_threads=_positive_integer("--nt", values.get("nt")),
        serial=serial,
        launcher=values.get("launcher"),
        launcher_args=shlex.split(values.get("launcher-args", "")),
        help_requested=help_requested,
    )


def _take_value(option: str, remaining: list[str]) -> str:
    """Pop the value of ``option`` off the front of ``remaining``."""
    if not remaining:
        print(f"palace: {option} needs a value", file=sys.stderr)
        raise SystemExit(1)
    return remaining.pop(0)


def _positive_integer(option: str, value: str | None) -> int | None:
    """Return ``value`` as a positive integer, or ``None`` when unset."""
    if value is None:
        return None
    if not value.isdigit() or int(value) < 1:
        print(
            f"palace: {option} needs a positive integer, not {value!r}", file=sys.stderr
        )
        raise SystemExit(1)
    return int(value)


def mpiexec(argv: Sequence[str] | None = None) -> None:
    """Replace this process with the MPICH launcher vendored in the wheel.

    Args:
        argv: Arguments to forward to ``mpiexec``. Defaults to the arguments
            this console script was invoked with.

    Raises:
        SystemExit: If the wheel carries no process manager.
    """
    _exec_payload(mpiexec_path, argv, name="palace-mpiexec")


def main(argv: Sequence[str] | None = None) -> None:
    """Run the packaged Palace binary, under a process manager when asked.

    With ``--np`` this process becomes the launcher, which starts each rank by
    invoking this script again. Without it, this process is the rank: a rank a
    process manager started without an MPI rendezvous is refused here rather
    than left to solve the whole problem alone and exit 0; see
    :mod:`._launcher`.

    Args:
        argv: Arguments to forward to Palace. Defaults to the arguments this
            console script was invoked with.

    Raises:
        SystemExit: If the wheel carries no Palace binary, this rank was
            launched without a rendezvous, or the requested launcher is not
            an executable.
    """
    invocation = parse_arguments(list(sys.argv[1:] if argv is None else argv))
    if invocation.help_requested:
        print(USAGE, end="")
        raise SystemExit(0)
    _apply_thread_count(invocation.num_threads)
    if invocation.serial or invocation.num_procs is None or invocation.num_procs == 1:
        _run_rank(invocation.palace_args)
    else:
        _run_under_launcher(invocation)


def _apply_thread_count(num_threads: int | None) -> None:
    """Fix ``OMP_NUM_THREADS`` for the run, as upstream's wrapper does.

    Palace's OpenMP build otherwise takes a thread per core in every rank,
    which oversubscribes a multi-rank run, so an unset variable means one
    thread rather than "as many as there are".

    Args:
        num_threads: Thread count from ``--nt``, or ``None`` to keep whatever
            the environment already says.
    """
    if num_threads is not None:
        os.environ["OMP_NUM_THREADS"] = str(num_threads)
    else:
        os.environ.setdefault("OMP_NUM_THREADS", "1")


def _run_rank(palace_args: list[str]) -> None:
    """Check the launcher guard, then become the Palace binary.

    Args:
        palace_args: Arguments for the binary, config file included.
    """
    parent_exe = _launcher.parent_executable()
    note = _launcher.version_note(parent_exe)
    if note is not None:
        print(f"palace: {note}", file=sys.stderr)
    reason = _launcher.refusal_reason(os.environ, parent_exe)
    if reason is not None:
        print(f"palace: {reason}", file=sys.stderr)
        raise SystemExit(1)
    _exec_payload(binary_path, palace_args, name="palace")


def _run_under_launcher(invocation: Invocation) -> None:
    """Become the process manager that starts the ranks.

    The ranks are started as this console script rather than as the raw
    binary, so each of them runs the guard in :mod:`._launcher` — the check
    that catches a launcher which starts ranks without letting them find each
    other.

    Args:
        invocation: The parsed command line, with a rank count.

    Raises:
        SystemExit: If the named launcher cannot be found, or the wheel
            carries no binary for the ranks to run.
    """
    launcher = _resolve_launcher(invocation.launcher)
    try:
        target = console_script_path() or binary_path()
    except FileNotFoundError as error:
        print(f"palace: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    command = [
        str(launcher),
        "-n",
        str(invocation.num_procs),
        *invocation.launcher_args,
        str(target),
        *invocation.palace_args,
    ]
    os.execv(str(launcher), command)


def _resolve_launcher(requested: str | None) -> Path:
    """Return the process manager to start the ranks with.

    Args:
        requested: Launcher named by ``--launcher``, or ``None`` for the one
            vendored in this wheel.

    Returns:
        Path of the launcher executable.

    Raises:
        SystemExit: If ``requested`` is neither a path to an executable nor a
            command on ``PATH``, or the wheel carries no process manager.
    """
    if requested is None:
        try:
            return mpiexec_path()
        except FileNotFoundError as error:
            print(f"palace: {error}", file=sys.stderr)
            raise SystemExit(1) from error
    resolved = shutil.which(requested)
    if resolved is None:
        print(
            f"palace: --launcher {requested!r} is not an executable file or a "
            "command on PATH",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return Path(resolved)


def _exec_payload(
    resolve: Callable[[], Path], argv: Sequence[str] | None, *, name: str
) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        executable = resolve()
    except FileNotFoundError as error:
        print(f"{name}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    os.execv(str(executable), [str(executable), *arguments])
