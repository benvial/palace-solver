"""Refuse to start a rank that has been launched without an MPI rendezvous.

The wheel vendors its own MPICH, so a Palace rank can be started by a process
manager that did not ship with it — a site MPICH's ``mpiexec``, or the one
from the PyPI ``mpich`` wheel if that happens to be installed. Almost
everything that can go wrong there fails loudly. One thing does not.

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

On Windows the wheel vendors MS-MPI instead, and the same failure has another
trigger. MS-MPI's PMI client decides between rank and singleton from
``PMI_KVS`` alone, so a Hydra-family launcher there — Intel MPI's ``mpiexec``,
which sets ``PMI_RANK`` but no ``PMI_KVS`` — starts ranks that each become rank
0 of 1. Measured on a ``windows-2025`` runner by giving the binary ``PMI_RANK``
and ``PMI_SIZE`` without ``PMI_KVS``: it ran as a singleton and exited 0, with
no diagnostic. So on Windows the rendezvous is ``PMI_KVS`` and nothing else, and
the parent that marks a launched rank is ``smpd``, the manager MS-MPI's
``mpiexec`` starts the ranks from.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path, PureWindowsPath

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

#: The rendezvous on Windows, where the vendored MPI is MS-MPI. Its PMI client
#: reads ``PMI_KVS`` and nothing else to decide whether it is a rank, so the
#: ``PMI_RANK`` a Hydra-family launcher sets there is no rendezvous at all.
WINDOWS_RENDEZVOUS_VARIABLES = ("PMI_KVS",)

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
        # MS-MPI's mpiexec starts the ranks from an smpd manager it spawns, so
        # on Windows a rank's parent is smpd, not mpiexec.
        "smpd",
    }
)

#: Variables through which a launcher says how many ranks it started. Read only
#: to recognise a deliberate single-rank launch, which needs no rendezvous.
RANK_COUNT_VARIABLES = ("MPI_LOCALNRANKS", "SLURM_NTASKS", "OMPI_COMM_WORLD_SIZE")

#: Set to a non-empty value to launch anyway.
OVERRIDE_ENV = "PALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER"

#: How long to wait for a foreign ``mpiexec --version`` to answer.
PROBE_TIMEOUT_SECONDS = 10

#: Buffer ``proc_pidpath`` is documented to want: ``PROC_PIDPATHINFO_MAXSIZE``
#: in ``<sys/proc_info.h>``, four times ``MAXPATHLEN``. Darwin's real path
#: limit is a quarter of it.
PROC_PIDPATH_BUFFER_SIZE = 4 * 1024

#: ``PROCESS_QUERY_LIMITED_INFORMATION``: the least access
#: ``QueryFullProcessImageNameW`` needs, and one Windows grants on processes
#: where a full ``PROCESS_QUERY_INFORMATION`` request would be refused.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: ``TH32CS_SNAPPROCESS``: a Toolhelp snapshot of the process table.
_TH32CS_SNAPPROCESS = 0x2

#: Buffer for a Windows image path, in characters: the longest path the
#: extended-length form allows.
_WINDOWS_PATH_BUFFER_SIZE = 32768

#: How many of this process's own launchers may stand between it and the
#: process that started it on Windows, where nothing replaces itself: the
#: console script's ``.exe`` stub, then a virtual environment's ``python.exe``
#: redirector, each a parent waiting on its child.
_OWN_LAUNCHER_DEPTH = 2

#: Names of the launcher executable to probe inside a foreign install.
PROBE_CANDIDATES = ("mpiexec", "mpiexec.hydra", "mpirun")

_HYDRA_VERSION = re.compile(r"^\s*Version:\s*(\S+)", re.MULTILINE)


def _is_windows(system: str | None) -> bool:
    """Whether ``system``, a ``sys.platform`` value, names Windows."""
    return (system or sys.platform) == "win32"


def rendezvous_variables(*, system: str | None = None) -> tuple[str, ...]:
    """Return the variables that hand a rank its rendezvous on ``system``.

    Args:
        system: ``sys.platform`` value; defaults to the running platform.

    Returns:
        :data:`WINDOWS_RENDEZVOUS_VARIABLES` on Windows,
        :data:`RENDEZVOUS_VARIABLES` everywhere else.
    """
    if _is_windows(system):
        return WINDOWS_RENDEZVOUS_VARIABLES
    return RENDEZVOUS_VARIABLES


def has_rendezvous(environ: Mapping[str, str], *, system: str | None = None) -> bool:
    """Whether the environment carries a way for this rank to find its peers.

    Args:
        environ: Environment of this rank.
        system: ``sys.platform`` value; defaults to the running platform.

    Returns:
        ``True`` when one of :func:`rendezvous_variables` is set.
    """
    return any(name in environ for name in rendezvous_variables(system=system))


def launched_by_process_manager(
    parent_exe: Path | None, *, system: str | None = None
) -> bool:
    """Whether ``parent_exe`` is the process manager that started this rank.

    Args:
        parent_exe: Executable of the launching process, or ``None`` where it
            could not be read.
        system: ``sys.platform`` value; defaults to the running platform. On
            Windows the name is compared without its ``.exe`` and ignoring
            case, as Windows itself compares it.

    Returns:
        ``True`` when the parent is a known process manager.
    """
    if parent_exe is None:
        return False
    if not _is_windows(system):
        return parent_exe.name in PROCESS_MANAGERS
    name = PureWindowsPath(str(parent_exe)).name.lower()
    return name.removesuffix(".exe") in PROCESS_MANAGERS


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


def _proc_executable(pid: int) -> Path | None:
    """Read a process's executable out of ``/proc``, as Linux answers it."""
    try:
        return Path(f"/proc/{pid}/exe").readlink()
    except OSError:
        return None


def _libproc_executable(pid: int) -> Path | None:
    """Ask ``libproc`` for a process's executable, as macOS answers it.

    Darwin mounts no ``/proc`` at all, so the path the Linux reader takes is
    not merely empty there — it is absent. ``proc_pidpath`` is the supported
    way to ask, it lives in ``libSystem``, and it needs no subprocess, which
    matters because this runs once in every rank at startup.
    """
    try:
        import ctypes  # noqa: PLC0415

        proc_pidpath = ctypes.CDLL(None).proc_pidpath
    except (ImportError, OSError, AttributeError):
        # Not a Darwin host, a libSystem without the symbol, or a Python built
        # without ctypes. A reader that cannot answer leaves the guard open,
        # which is the failure direction this whole module prefers.
        return None
    proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(PROC_PIDPATH_BUFFER_SIZE)
    if proc_pidpath(pid, buffer, PROC_PIDPATH_BUFFER_SIZE) <= 0:
        # The documented failure report: a pid that is gone, or one this
        # process may not look at.
        return None
    return Path(os.fsdecode(buffer.value))


# The two Windows readers are defined only on Windows; anywhere else they are
# stand-ins that answer nothing, so none of the ctypes calls below is even
# defined, let alone run, on POSIX.
if sys.platform == "win32":

    def _windows_executable(pid: int) -> Path | None:
        """Ask ``QueryFullProcessImageNameW`` for a process's executable."""
        try:
            import ctypes  # noqa: PLC0415
            from ctypes import wintypes  # noqa: PLC0415

            kernel32 = ctypes.WinDLL("kernel32")
        except (ImportError, OSError):
            # As for libproc: a reader that cannot answer leaves the guard open.
            return None
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION,
            False,  # noqa: FBT003 -- bInheritHandle, positional in the C API
            pid,
        )
        if not handle:
            # A pid that is gone, or one this process may not look at.
            return None
        try:
            buffer = ctypes.create_unicode_buffer(_WINDOWS_PATH_BUFFER_SIZE)
            size = wintypes.DWORD(_WINDOWS_PATH_BUFFER_SIZE)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return None
            return Path(buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    def _windows_parent_pid(pid: int) -> int | None:
        """Return the pid of the process that started ``pid``.

        ``os.getppid`` answers this for the running process only, and a rank
        on Windows needs it a generation or two further up. A Toolhelp
        snapshot of the process table is the documented way to ask it of
        another process.
        """
        try:
            import ctypes  # noqa: PLC0415
            from ctypes import wintypes  # noqa: PLC0415

            kernel32 = ctypes.WinDLL("kernel32")
        except (ImportError, OSError):
            return None

        class ProcessEntry(ctypes.Structure):
            """``PROCESSENTRY32W``, from ``<tlhelp32.h>``."""

            _fields_ = (
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
            )

        walk_argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = walk_argtypes
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = walk_argtypes
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        # INVALID_HANDLE_VALUE is (HANDLE)-1, which a c_void_p reads unsigned.
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return None
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32ProcessID == pid:
                    return int(entry.th32ParentProcessID)
                found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return None
        finally:
            kernel32.CloseHandle(snapshot)

else:

    def _windows_executable(pid: int) -> Path | None:  # noqa: ARG001
        """Answer nothing: there is no Windows to ask."""
        return None

    def _windows_parent_pid(pid: int) -> int | None:  # noqa: ARG001
        """Answer nothing: there is no Windows to ask."""
        return None


def process_executable(pid: int, *, system: str | None = None) -> Path | None:
    """Return the executable a process is running.

    Asked of ``sys.platform`` rather than of ``platform.system()``, which the
    build tooling uses: this runs in every rank at startup and ``sys`` is
    already imported, while the ``platform`` module costs a further 2 ms of
    import to answer the same question.

    Args:
        pid: Process to ask about.
        system: ``sys.platform`` value; defaults to the running platform.

    Returns:
        Absolute path of that process's executable, or ``None`` where the
        platform will not say (a process that has gone, one this process may
        not look at, a platform neither reader knows).
    """
    if (system or sys.platform) == "darwin":
        return _libproc_executable(pid)
    if _is_windows(system):
        return _windows_executable(pid)
    return _proc_executable(pid)


def _same_file(first: Path, second: str) -> bool:
    """Whether two paths name one file, a short Windows name and its long one too."""
    try:
        return first.samefile(second)
    except (OSError, ValueError):
        return os.path.normcase(first.absolute()) == os.path.normcase(
            Path(second).absolute()
        )


def parent_executable(*, system: str | None = None) -> Path | None:
    """Return the executable of the process that started this one.

    On POSIX the console script and the interpreter replace one another with
    ``exec``, so the parent is the launcher. Windows has no ``exec``: the
    console script's ``.exe`` stub starts the interpreter as its child, and in
    a virtual environment ``python.exe`` is a redirector that starts the base
    interpreter as its own. Those are this process's own launchers, and they
    are skipped to reach the process that started them. The stub is the
    running script, ``sys.argv[0]``. The redirector is ``sys.executable`` when
    that is not the image this process runs: outside a virtual environment the
    two are one file, and a parent running it is some other Python program.

    Args:
        system: ``sys.platform`` value; defaults to the running platform.

    Returns:
        Absolute path of the parent's executable, or ``None`` where the
        platform does not answer.
    """
    pid = os.getppid()
    if not _is_windows(system):
        return process_executable(pid, system=system)
    own = [sys.argv[0]] if sys.argv and sys.argv[0] else []
    image = _windows_executable(os.getpid())
    if image is not None and not _same_file(image, sys.executable):
        own.append(sys.executable)
    for _ in range(_OWN_LAUNCHER_DEPTH):
        executable = _windows_executable(pid)
        if executable is None or not any(_same_file(executable, o) for o in own):
            return executable
        parent = _windows_parent_pid(pid)
        if parent is None:
            return None
        pid = parent
    return _windows_executable(pid)


def refusal_reason(
    environ: Mapping[str, str],
    parent_exe: Path | None,
    *,
    system: str | None = None,
) -> str | None:
    """Return why this rank must not start, if it must not.

    Args:
        environ: Environment of this rank.
        parent_exe: Executable of the launching process, from
            :func:`parent_executable`.
        system: ``sys.platform`` value; defaults to the running platform. It
            decides which variables count as a rendezvous.

    Returns:
        An operator-facing message, or ``None`` when the launch is allowed.
        A rank not started by a process manager, one holding a rendezvous, and
        one the launcher says is alone are all allowed.
    """
    if environ.get(OVERRIDE_ENV):
        return None
    if not launched_by_process_manager(parent_exe, system=system):
        return None
    if has_rendezvous(environ, system=system):
        return None
    if requested_ranks(environ) == 1:
        return None
    return (
        f"refusing to run: {parent_exe} started this rank but handed it no MPI "
        "rendezvous — none of "
        + ", ".join(rendezvous_variables(system=system))
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
    system: str | None = None,
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
        system: ``sys.platform`` value; defaults to the running platform. The
            remark compares MPICH series, and the Windows wheel vendors none:
            neither MS-MPI's ``mpiexec`` nor Intel MPI's answers Hydra's
            ``--version``, so there no launcher is probed at all.

    Returns:
        A one-line remark, or ``None`` when there is nothing to say.
    """
    if (
        _is_windows(system)
        or not launched_by_process_manager(parent_exe)
        or parent_exe is None
    ):
        return None
    if vendored_bin is None:
        from palace_solver import mpiexec_path  # noqa: PLC0415

        try:
            vendored_bin = mpiexec_path().parent
        except FileNotFoundError:
            return None
    launcher_dir = parent_exe.parent
    # Both sides are real paths already -- the platform answers with one, and
    # palace_solver._PACKAGE_DIR resolves the package's own -- so they compare
    # equal for the vendored launcher on macOS too, where /var and /private/var
    # are the same directory under two names.
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
