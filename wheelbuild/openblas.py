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
import platform
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

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

#: Where an install records the ``make`` arguments that produced it, relative
#: to the prefix. The core name in ``openblas_config.h`` answers what the
#: compiler was told about the CPU and nothing else; every other build
#: argument -- ``NO_SVE``, ``USE_OPENMP``, ``INTERFACE64`` -- leaves no trace
#: in the install that can be read back. A restored tree is exactly where that
#: matters, because the fallback ``restore-keys`` exist to hand back a tree
#: built under older arguments and ``make`` does not notice that they changed.
#: A dotfile at the prefix root, which is shared with MPICH and Palace, so it
#: names the project it belongs to.
BUILD_STAMP = Path(".openblas-build-arguments")

#: ``--check`` verdicts, for a caller that has to do something about them.
#: There is nothing usable installed, so build one; or there is, and it is
#: built for the wrong CPU, which also means the source tree's objects carry
#: the wrong ``-march`` and have to go.
#:
#: 1 and 2 are not available to a verdict, for the same reason: something else
#: already exits with them. argparse exits 2 on a usage error, and an unhandled
#: exception exits 1 -- and a driver branching on the number cannot tell either
#: from a verdict it was told to act on. That is not hypothetical: this is
#: where ``CHECK_NO_INSTALL`` used to be, so any exception escaping
#: :func:`validate` read as "nothing is installed" and bought a rebuild in
#: place on the strength of a question that was never answered.
CHECK_NO_INSTALL = 6
CHECK_WRONG_CPU = 3
#: A third verdict, and the one no rebuild fixes: the library links both OpenMP
#: runtimes and only a toolchain change can make it link one.
CHECK_MIXED_OPENMP = 4
#: And the verdict for a question that could not be asked at all. Distinct from
#: every other because it is about this machine rather than about the install:
#: reported as CHECK_NO_INSTALL it would buy a full rebuild of a library that
#: is already there and then fail anyway.
CHECK_CANNOT_INSPECT = 5

#: The two OpenMP runtimes a Darwin build can end up linking at once — LLVM's
#: and GCC's. Matched against the basenames ``otool -L`` reports, so the names
#: carry no version.
OPENMP_RUNTIMES = ("libomp", "libgomp")


class CpuBaselineError(RuntimeError):
    """Raised when an OpenBLAS install is not built for the intended CPU baseline."""


class BuildRecipeError(RuntimeError):
    """Raised when an install was built by different arguments than today's."""


class InstallInspectionError(RuntimeError):
    """Raised when the tools that inspect a built install are not available."""


class MixedOpenMPRuntimeError(RuntimeError):
    """Raised when one library links both OpenMP runtimes.

    OpenBLAS is where the two toolchains meet: with ``USE_OPENMP=1`` the C
    objects take the C compiler's runtime while the Darwin shared library is
    linked by ``$(FC)``, whose own ``-fopenmp`` re-adds ``-lgomp`` after
    ``Makefile.system``'s ``FEXTRALIB`` substitution has already rewritten it.
    Both runtimes in one process is OpenBLAS issue #5156 — "That won't work at
    all" — and it produces a working build of a broken library, so nothing
    downstream fails on it.
    """


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
    resolved_system = system or platform.system()
    target = baseline_target(system=resolved_system, machine=machine)
    return [
        "make",
        f"-j{jobs}",
        # The oldest CPU the library is allowed to require. Absent on x86_64,
        # whose makefile adds no -march under DYNAMIC_ARCH anyway.
        *([f"TARGET={target}"] if target else []),
        # Apple Silicon has no SVE, and OpenBLAS knows it: Makefile.system sets
        # NO_SVE itself on Darwin arm64 — but only inside
        # `ifndef MACOSX_DEPLOYMENT_TARGET`, and the macOS build has to set a
        # deployment target so the payload cannot exceed the floor the wheel
        # claims. Setting one therefore re-enables the SVE kernels unless this
        # says otherwise. Darwin only: Linux aarch64 runs on hardware that has
        # SVE and DYNAMIC_ARCH dispatches to it.
        *(["NO_SVE=1"] if resolved_system == "Darwin" else []),
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


def recipe(arguments: Sequence[str]) -> tuple[str, ...]:
    """Return the part of a ``make`` command that describes the library.

    Args:
        arguments: A :func:`build_arguments` vector.

    Returns:
        The same arguments without ``make`` itself or the job count, which
        says how fast the build ran and nothing about what it produced.
    """
    return tuple(
        argument for argument in arguments[1:] if not argument.startswith("-j")
    )


def write_build_stamp(prefix: Path, arguments: Sequence[str]) -> Path:
    """Record in the install which ``make`` arguments built it.

    Args:
        prefix: OpenBLAS install prefix.
        arguments: The ``make`` command that was run.

    Returns:
        Path of the stamp file.
    """
    stamp = prefix / BUILD_STAMP
    stamp.write_text("\n".join(recipe(arguments)) + "\n")
    return stamp


def stamped_recipe(prefix: Path) -> tuple[str, ...] | None:
    """Return the arguments an install records, or None if it records none.

    Args:
        prefix: OpenBLAS install prefix.

    Returns:
        The stamped arguments, or None for an install built before the stamp
        existed — which is a build this code cannot vouch for rather than one
        it can pass.
    """
    try:
        text = (prefix / BUILD_STAMP).read_text()
    except OSError:
        return None
    return tuple(line for line in text.splitlines() if line)


def parse_linked_libraries(output: str) -> tuple[str, ...]:
    """Return the library basenames in ``otool -L`` output.

    Args:
        output: What ``otool -L <binary>`` printed. Every dependency is an
            indented line — a path, or a bare install name — followed by its
            compatibility version in brackets. The unindented lines name the
            file itself, once per architecture slice.

    Returns:
        One basename per linked library, in the order Mach-O records them.
    """
    names = []
    for line in output.splitlines():
        if not line.startswith(("\t", " ")):
            continue
        entry = line.strip()
        if not entry:
            continue
        names.append(PurePosixPath(entry.split(" (")[0]).name)
    return tuple(names)


def linked_libraries(binary: Path) -> tuple[str, ...]:
    """Return the basenames of the libraries a Mach-O binary links.

    Darwin only: ``otool`` ships with the command line tools, so it is on any
    machine that can build this at all.

    Args:
        binary: Mach-O executable or shared library.

    Returns:
        One basename per ``LC_LOAD_DYLIB`` entry.
    """
    try:
        completed = subprocess.run(
            ["otool", "-L", str(binary)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        # Not the FileNotFoundError :func:`validate` documents, and given its
        # own type for that reason: read as "nothing is installed" it would buy
        # a rebuild of a library that is already there.
        raise InstallInspectionError(
            f"otool is not installed, so nothing can say what {binary} links"
        ) from error
    except subprocess.CalledProcessError as error:
        # otool rejects the file -- truncated, not Mach-O, or unreadable. The
        # same verdict as a missing otool rather than a rebuild, because what
        # this reports is that the question could not be answered, and a
        # rebuild decided on an unanswered question is 40 minutes spent before
        # failing on whatever is really wrong.
        raise InstallInspectionError(
            f"otool could not read {binary}: {error.stderr or error}"
        ) from error
    return parse_linked_libraries(completed.stdout)


def linked_openmp_runtimes(binary: Path) -> set[str]:
    """Return which OpenMP runtimes a Mach-O binary links.

    Args:
        binary: Mach-O executable or shared library.

    Returns:
        A subset of :data:`OPENMP_RUNTIMES`; more than one member is the
        failure :class:`MixedOpenMPRuntimeError` describes.
    """
    return {
        runtime
        for name in linked_libraries(binary)
        for runtime in OPENMP_RUNTIMES
        if name.startswith(f"{runtime}.")
    }


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
        BuildRecipeError: If the install does not record being built by the
            arguments :func:`build_arguments` produces today.
        InstallInspectionError: On Darwin, if ``otool`` is not installed.
        CpuBaselineError: If the architecture names a baseline in
            :data:`BASELINE_TARGETS` and the install was built for a different
            core. That install runs on the machine that produced it and faults
            on the older CPUs the wheel's platform tag admits, so it fails the
            build rather than the user's.
        MixedOpenMPRuntimeError: On Darwin, if the installed library links both
            OpenMP runtimes.
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
    expected_recipe = recipe(build_arguments(jobs=1, system=system, machine=machine))
    stamped = stamped_recipe(prefix)
    if stamped != expected_recipe:
        raise BuildRecipeError(
            f"the OpenBLAS at {prefix} was built by "
            + ("arguments it does not record" if stamped is None else " ".join(stamped))
            + ", not by "
            + " ".join(expected_recipe)
            + ": make does not notice a changed argument, so the objects in "
            "the source tree carry the old ones too and both have to go."
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
    _check_openmp_runtime(prefix, system=system)
    return prefix


def _check_openmp_runtime(prefix: Path, *, system: str | None) -> None:
    """Fail an install whose library links both OpenMP runtimes.

    Gated on actually running on Darwin rather than on the ``system`` argument
    alone: unlike every other check here this one reads the compiled library
    through ``otool`` instead of asking what a filename would be, so a Linux
    bench describing a Darwin tree has nothing to read.
    """
    if (system or platform.system()) != "Darwin" or platform.system() != "Darwin":
        return
    library = prefix / platforms.library_path("libopenblas", system="Darwin")
    runtimes = linked_openmp_runtimes(library)
    if len(runtimes) > 1:
        raise MixedOpenMPRuntimeError(
            f"{library} links {' and '.join(sorted(runtimes))}: one process "
            "cannot host two OpenMP runtimes, and what comes out is a built "
            "library rather than a failed build, so nothing later here would "
            "notice. Compile the C and the Fortran with the same toolchain."
        )


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
    arguments = build_arguments(jobs=jobs)
    check_call(arguments, cwd=source_dir)
    check_call(install_arguments(prefix=prefix), cwd=source_dir)
    write_build_stamp(prefix, arguments)
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
        f"{CHECK_NO_INSTALL} if there is nothing usable installed, "
        f"{CHECK_WRONG_CPU} if what is installed is built for the wrong CPU, "
        f"{CHECK_MIXED_OPENMP} if it links both OpenMP runtimes and "
        f"{CHECK_CANNOT_INSPECT} if the question could not be asked at all, "
        "with the reason on stdout",
    )
    args = parser.parse_args(argv)

    if args.check:
        try:
            validate(args.prefix)
        except InstallInspectionError as error:
            print(f"OpenBLAS at {args.prefix} cannot be checked: {error}")
            return CHECK_CANNOT_INSPECT
        except MixedOpenMPRuntimeError as error:
            print(f"OpenBLAS at {args.prefix} cannot be used: {error}")
            return CHECK_MIXED_OPENMP
        except FileNotFoundError as error:
            print(f"OpenBLAS at {args.prefix} has to be built: {error}")
            return CHECK_NO_INSTALL
        except (CpuBaselineError, BuildRecipeError) as error:
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
