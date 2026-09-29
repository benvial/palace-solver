"""Build the OpenBLAS that Palace links and the wheel vendors.

Palace does not build BLAS/LAPACK itself: ``cmake/ExternalBLASLAPACK.cmake``
requires one already on the system (OpenBLAS, MKL, AOCL or ARMPL), and the
manylinux image ships none. Building it here rather than installing the distro
package buys three things the wheel needs: ``DYNAMIC_ARCH``, so one binary
picks its kernels at run time on whatever CPU a user has; a CPU baseline the
code around those kernels is held to, so the wheel runs on the oldest machine
its platform tag admits rather than on the one that built it; and an
OpenMP-threaded build that matches Palace's own OpenMP.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Sequence
from pathlib import Path

from wheelbuild import platforms
from wheelbuild._process import check_call

#: OpenBLAS release vendored into the wheel.
OPENBLAS_VERSION = "0.3.34"

#: The oldest CPU each architecture's build is compiled for, as an OpenBLAS
#: ``TARGET`` from its ``TargetList.txt``.
#:
#: ``DYNAMIC_ARCH`` alone does not settle this. It governs the dispatched
#: kernels; the roughly 6,700 common objects around them are compiled once,
#: with whatever ``-march`` the platform makefile chooses. ``Makefile.x86_64``
#: adds none of its own under ``DYNAMIC_ARCH=1`` (its ``ADD_CPUFLAGS`` is set
#: only in the ``ifneq ($(DYNAMIC_ARCH),1)`` branch), so an x86_64 build is
#: portable without a ``TARGET`` and is left alone here. ``Makefile.arm64`` has
#: no such gate: it appends ``-march`` per detected ``CORE`` unconditionally,
#: so a build on a Neoverse-N2 host compiles the common objects
#: ``-march=armv8.5-a+sve+sve2+bf16`` and the wheel faults on Graviton2 or any
#: pre-SVE core. Naming ``ARMV8`` is what makes the arm wheel mean what
#: ``manylinux_2_28_aarch64`` implies, which says nothing about the CPU.
#:
#: Keyed by :func:`wheelbuild.platforms.architecture`, so Darwin ``arm64`` gets
#: the same baseline: it reads the same ``Makefile.arm64``, and a macOS wheel
#: tuned to whichever Apple core the runner happens to have would exclude the
#: older Apple Silicon its platform tag admits.
#:
#: These names are OpenBLAS's own and are matched exactly. ``TARGET=X`` reaches
#: the build as ``-DFORCE_X`` (``Makefile.system``), which selects the block in
#: ``getarch.c`` that defines ``CORENAME``; for ``ARMV8`` that is the literal
#: ``"ARMV8"`` (``getarch.c:1346`` at 0.3.34), and every arm64 core name there
#: is upper case. ``Makefile.arm64`` then keys its ``-march`` off that same
#: ``CORE``, in one ``ifeq ($(CORE), ARMV8)``, which is why checking the core
#: name is checking the flags. Pinned by :data:`OPENBLAS_VERSION`, so the
#: mapping cannot move without a version bump.
BASELINE_TARGETS = {
    "aarch64": "ARMV8",
    "arm64": "ARMV8",
}

#: The header an OpenBLAS install carries, which records the core the build was
#: configured for. Not in :func:`required_artefacts` because Palace configures
#: against ``cblas.h`` and never reads this one; it is evidence about the
#: build, not a dependency of it.
CONFIG_HEADER = Path("include/openblas_config.h")

_CORENAME_PATTERN = re.compile(r'#\s*define\s+OPENBLAS_CHAR_CORENAME\s+"([^"]+)"')

#: ``--check`` verdicts, for a caller that has to do something about them.
#: There is nothing usable installed, so build one; or there is, and it is
#: built for the wrong CPU, which also means the source tree's objects carry
#: the wrong ``-march`` and have to go. 2 is skipped because argparse exits
#: with it on a usage error.
CHECK_NO_INSTALL = 1
CHECK_WRONG_CPU = 3


class CpuBaselineError(RuntimeError):
    """Raised when an OpenBLAS install is not built for the intended CPU baseline."""


def baseline_target(
    *, system: str | None = None, machine: str | None = None
) -> str | None:
    """Return the OpenBLAS ``TARGET`` this architecture's build must name.

    Args:
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.

    Returns:
        The baseline core name, or None for an architecture whose platform
        makefile already leaves the common objects portable under
        ``DYNAMIC_ARCH``. See :data:`BASELINE_TARGETS`.

    Raises:
        UnsupportedPlatformError: For a platform this package builds no wheel
            for.
    """
    return BASELINE_TARGETS.get(platforms.architecture(system=system, machine=machine))


def installed_core(prefix: Path) -> str:
    """Return the CPU core an installed OpenBLAS was configured for.

    Reads the install's own ``openblas_config.h`` rather than the build tree's
    ``Makefile.conf``, so the answer is about the library that will be
    vendored, including one handed back by a build cache.

    Args:
        prefix: OpenBLAS install prefix.

    Returns:
        The core name, such as ``ARMV8`` or ``NEOVERSEN2``.

    Raises:
        CpuBaselineError: If the header is missing or names no core.
    """
    header = prefix / CONFIG_HEADER
    try:
        text = header.read_text()
    except OSError as error:
        raise CpuBaselineError(
            f"cannot tell what CPU the OpenBLAS at {prefix} was built for: "
            f"{header} is unreadable ({error})"
        ) from error
    found = _CORENAME_PATTERN.search(text)
    if found is None:
        raise CpuBaselineError(
            f"cannot tell what CPU the OpenBLAS at {prefix} was built for: "
            f"{header} names no OPENBLAS_CHAR_CORENAME"
        )
    return found.group(1)


def required_artefacts(*, system: str | None = None) -> tuple[Path, ...]:
    """Files an OpenBLAS install must have for Palace to configure against it.

    Args:
        system: ``platform.system()`` value; defaults to the running platform.
            OpenBLAS installs an unversioned ``lib/libopenblas.so`` on Linux and
            ``lib/libopenblas.dylib`` on Darwin.

    Returns:
        Paths relative to the install prefix.
    """
    return (
        Path("include/cblas.h"),
        platforms.library_path("libopenblas", system=system),
    )


def source_url(version: str = OPENBLAS_VERSION) -> str:
    """Return the download URL of the pinned OpenBLAS source tarball."""
    return (
        "https://github.com/OpenMathLib/OpenBLAS/releases/download/"
        f"v{version}/OpenBLAS-{version}.tar.gz"
    )


def build_arguments(
    *, jobs: int, system: str | None = None, machine: str | None = None
) -> list[str]:
    """Return the ``make`` command that builds OpenBLAS.

    Args:
        jobs: Parallel build jobs.
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.

    Returns:
        The ``make`` argument vector.
    """
    target = baseline_target(system=system, machine=machine)
    return [
        "make",
        f"-j{jobs}",
        # The oldest CPU the library is allowed to require. Absent on x86_64,
        # whose makefile adds no -march under DYNAMIC_ARCH anyway.
        *([f"TARGET={target}"] if target else []),
        # One wheel runs on many CPUs, so build every kernel and dispatch at
        # run time.
        "DYNAMIC_ARCH=1",
        # Match Palace's threading model rather than mixing pthreads with it.
        "USE_OPENMP=1",
        # Palace is built with PALACE_WITH_64BIT_INT=OFF, so LP64 it is.
        "INTERFACE64=0",
        "NO_STATIC=1",
    ]


def install_arguments(*, prefix: Path) -> list[str]:
    """Return the ``make install`` command for an OpenBLAS build."""
    return ["make", "install", f"PREFIX={prefix}", "NO_STATIC=1"]


def validate(
    prefix: Path, *, system: str | None = None, machine: str | None = None
) -> Path:
    """Check an OpenBLAS install is complete and built for the intended CPU.

    Args:
        prefix: OpenBLAS install prefix.
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.

    Returns:
        The validated prefix.

    Raises:
        FileNotFoundError: If any required artefact is missing.
        CpuBaselineError: If the architecture names a baseline in
            :data:`BASELINE_TARGETS` and the install was built for a different
            core. That install runs on the machine that produced it and faults
            on the older CPUs the wheel's platform tag admits, so it fails the
            build rather than the user's.
    """
    missing = [
        relative
        for relative in required_artefacts(system=system)
        if not (prefix / relative).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"incomplete OpenBLAS install at {prefix}: missing "
            + ", ".join(str(relative) for relative in missing)
        )
    expected = baseline_target(system=system, machine=machine)
    if expected is not None:
        core = installed_core(prefix)
        if core != expected:
            raise CpuBaselineError(
                f"the OpenBLAS at {prefix} is built for {core}, not the "
                f"{expected} baseline this wheel claims: its common objects "
                "carry that core's -march and the wheel would fault on older "
                "CPUs. Rebuild it with "
                f"TARGET={expected}."
            )
    return prefix


def run(*, source_dir: Path, prefix: Path, jobs: int) -> Path:
    """Build and install OpenBLAS.

    OpenBLAS builds in its source tree, so ``source_dir`` doubles as the build
    directory; cache it to skip a rebuild.

    Args:
        source_dir: Unpacked OpenBLAS source tree.
        prefix: Install prefix.
        jobs: Parallel build jobs.

    Returns:
        The validated install prefix.
    """
    check_call(build_arguments(jobs=jobs), cwd=source_dir)
    check_call(install_arguments(prefix=prefix), cwd=source_dir)
    return validate(prefix)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the OpenBLAS build.

    With ``--check`` it builds nothing and reports on an existing install
    instead, as an exit code the build script can branch on: a cache can hand
    back an OpenBLAS compiled before the baseline was pinned, and "the library
    is there" is not the question worth asking about it. The two failing
    verdicts are distinguished because they need different things done about
    them -- see :data:`CHECK_NO_INSTALL` and :data:`CHECK_WRONG_CPU`.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report on the install at --prefix instead of building one; exit "
        f"{CHECK_NO_INSTALL} if there is nothing usable installed and "
        f"{CHECK_WRONG_CPU} if what is installed is built for the wrong CPU, "
        "with the reason on stdout",
    )
    args = parser.parse_args(argv)

    if args.check:
        try:
            validate(args.prefix)
        except FileNotFoundError as error:
            print(f"OpenBLAS at {args.prefix} has to be built: {error}")
            return CHECK_NO_INSTALL
        except CpuBaselineError as error:
            print(f"OpenBLAS at {args.prefix} has to be rebuilt: {error}")
            return CHECK_WRONG_CPU
        expected = baseline_target()
        if expected is None:
            print(
                f"OpenBLAS at {args.prefix} is complete; "
                f"{platforms.architecture()} pins no CPU baseline"
            )
        else:
            print(f"OpenBLAS at {args.prefix} is built for the {expected} baseline")
        return 0

    if args.source_dir is None:
        parser.error("--source-dir is required unless --check is given")
    prefix = run(source_dir=args.source_dir, prefix=args.prefix, jobs=args.jobs)
    print(f"OpenBLAS {OPENBLAS_VERSION} installed into {prefix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
