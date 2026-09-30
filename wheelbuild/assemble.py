"""Assemble, repair and retag the palace-solver platform wheel.

Pipeline: stage the executables into the package directory, build a wheel,
repair it to vendor every shared library they need — MPI and BLAS included,
since the wheel carries its own — and retag the result ``py3-none`` because the
payload is a binary with no Python ABI.

The repair tool is the platform's: ``auditwheel`` reads ELF headers and writes
RPATHs, ``delocate`` reads Mach-O load commands and rewrites install names, and
neither runs on the other's payload. The two differ in more than their name.
``auditwheel`` is *told* the platform tag and applies it; ``delocate`` *computes*
it from the largest ``minos`` in the payload and renames the wheel to match. So
on Linux the tag is an instruction and on Darwin it is evidence — which is why
the pipeline ends by checking the tag on the file rather than trusting the tag
it asked for. See ``wheelbuild.platforms`` for the tag itself.
"""

from __future__ import annotations

import argparse
import platform
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from palace_solver import BINARY_NAME, LAUNCHER_NAME
from wheelbuild import notices as notices_module
from wheelbuild import platforms
from wheelbuild._process import check_call

#: PyPI's default per-file upload limit. Exceeding it needs a limit request.
PYPI_SIZE_LIMIT_BYTES = 100 * 1024 * 1024

#: The delocate the macOS driver installs, pinned at the floor rather than the
#: exact version so a fix reaches the build.
#:
#: 0.13.0 is the oldest that *refuses* a payload above the requested
#: ``MACOSX_DEPLOYMENT_TARGET`` — ``_check_and_update_wheel_name`` raises
#: ``DelocationError`` and names the offending file. Given no target at all it
#: computes the tag from the payload and only logs a warning, which is how a
#: wheel comes to claim a macOS it cannot run on; ADR-0006's ``>= 0.11`` predates
#: that check. Both halves are load-bearing here, because the tag every other
#: platform is told is the one this platform is asked for.
DELOCATE_REQUIREMENT = "delocate>=0.13.0"


class PlatformTagError(RuntimeError):
    """Raised when a built wheel does not carry the tag the platform claims."""


@dataclass(frozen=True)
class SizeReport:
    """Size of a built wheel, measured against PyPI's upload limit."""

    path: Path
    size_bytes: int

    @property
    def exceeds_pypi_limit(self) -> bool:
        """Whether the wheel needs a PyPI file-size limit request."""
        return self.size_bytes > PYPI_SIZE_LIMIT_BYTES

    @property
    def text(self) -> str:
        """One-line human-readable report."""
        megabytes = self.size_bytes / (1024 * 1024)
        limit = PYPI_SIZE_LIMIT_BYTES // (1024 * 1024)
        verdict = "OVER" if self.exceeds_pypi_limit else "under"
        return (
            f"{self.path.name}: {megabytes:.1f} MB "
            f"({verdict} the {limit} MB PyPI upload limit)"
        )


def size_report(wheel: Path) -> SizeReport:
    """Measure ``wheel`` against PyPI's upload limit."""
    return SizeReport(path=wheel, size_bytes=wheel.stat().st_size)


def find_palace_binary(install_prefix: Path) -> Path:
    """Locate the real Palace executable in a superbuild install tree.

    Palace installs a small ``bin/palace`` launcher script alongside the actual
    binary (``palace-<arch>.bin``); the wheel ships the binary and provides its
    own console script. Which of the two is which is decided by the file's
    format — ELF on Linux, Mach-O on Darwin — not by its name, because the name
    carries the architecture.

    Args:
        install_prefix: Superbuild install prefix.

    Returns:
        Path of the Palace binary.

    Raises:
        FileNotFoundError: If no ``palace*`` binary is installed.
    """
    candidates = sorted(
        path
        for path in (install_prefix / "bin").glob("palace*")
        if path.is_file() and not path.is_symlink() and platforms.is_native_binary(path)
    )
    if not candidates:
        raise FileNotFoundError(f"no Palace binary under {install_prefix / 'bin'}")
    return candidates[0]


def stage(
    *, install_prefix: Path, package_dir: Path, notices: Path | None = None
) -> Path:
    """Copy the superbuild payload into the package directory.

    Args:
        install_prefix: Superbuild install prefix.
        package_dir: The ``palace_solver`` package directory to fill.
        notices: Optional THIRD-PARTY-NOTICES file to ship alongside.

    Returns:
        Path of the staged binary.
    """
    source_binary = find_palace_binary(install_prefix)
    binary_dir = package_dir / "bin"
    library_dir = package_dir / "lib"
    for directory in (binary_dir, library_dir):
        _clear_payload(directory)

    staged_binary = binary_dir / BINARY_NAME
    shutil.copy2(source_binary, staged_binary)
    staged_binary.chmod(staged_binary.stat().st_mode | 0o111)

    for launcher in _process_manager_binaries(install_prefix):
        destination = binary_dir / launcher.name
        # Wheels cannot carry symlinks, and MPICH installs mpiexec as one.
        shutil.copy2(launcher.resolve(), destination)
        destination.chmod(destination.stat().st_mode | 0o111)

    if notices is not None:
        shutil.copy2(notices, package_dir / "THIRD-PARTY-NOTICES")
    return staged_binary


def _clear_payload(directory: Path) -> None:
    """Empty a payload directory, keeping the tracked ``.gitkeep`` placeholder."""
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.name.startswith("."):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _process_manager_binaries(install_prefix: Path) -> list[Path]:
    """Return the Hydra process manager executables to ship.

    ``mpiexec`` and its ``hydra_*`` helpers are what run the solver on several
    ranks; the ``mpicc``-style compiler wrappers are build-time only shell
    scripts and stay out of the wheel.
    """
    return sorted(
        path
        for path in (install_prefix / "bin").iterdir()
        if path.is_file()
        and platforms.is_native_binary(path)
        and (path.name == LAUNCHER_NAME or path.name.startswith(("mpiexec", "hydra_")))
    )


def repair_command(
    *,
    wheel: Path,
    output_dir: Path,
    system: str | None = None,
    machine: str | None = None,
) -> list[str]:
    """Return the repair invocation for a built wheel, on one platform.

    Nothing is excluded on either platform: the vendored MPICH is part of the
    payload, so the wheel is self-contained and needs no MPI installed beside
    it. ``delocate`` needs no telling — it copies from everywhere except
    ``/usr/lib`` and ``/System``, which vendors the Homebrew runtimes and
    correctly leaves Apple's own ``libc++`` alone.

    Args:
        wheel: The freshly built, unrepaired wheel.
        output_dir: Where the repaired wheel is written.
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.

    Returns:
        The argument list to run.

    Raises:
        platforms.UnsupportedPlatformError: For a platform with no repair tool
            here, or a machine it ships no wheel for.
    """
    resolved = system or platform.system()
    if resolved == "Linux":
        return [
            "auditwheel",
            "repair",
            "--plat",
            platforms.platform_tag(system=resolved, machine=machine),
            "--wheel-dir",
            str(output_dir),
            str(wheel),
        ]
    if resolved == "Darwin":
        return [
            "delocate-wheel",
            # Deliberately no platform tag: delocate takes the incoming one as
            # a constraint on which architectures may survive and derives the
            # published one from the payload, so a value here would only be a
            # value it overrules. It is checked afterwards instead, by
            # verify_platform_tag.
            #
            # DYLD_LIBRARY_PATH is deliberately not set around this command
            # either. delocate resolves an absolute install name by searching
            # DYLD_LIBRARY_PATH for the *basename* first and only then the
            # recorded path, so pointing it at the install prefix would let a
            # same-named library shadow the one the payload was actually linked
            # against. Every library here is already at the absolute path its
            # dependents record, which is the step delocate reaches anyway.
            "--require-archs",
            # Checked rather than assumed, and it refuses an Intel Mac rather
            # than quietly building a wheel ADR-0006 rules out. delocate checks
            # the architectures of the libraries it copied, so this catches a
            # dependency that came from somewhere the build did not.
            platforms.architecture(system=resolved, machine=machine),
            "--wheel-dir",
            str(output_dir),
            # delocate reports what it copied and where from only at this
            # verbosity, and a repair nobody can read is a repair nobody can
            # debug an hour into a CI job.
            "--verbose",
            str(wheel),
        ]
    raise platforms.UnsupportedPlatformError(f"no wheel repair tool for {resolved}")


def retag_command(
    wheel: Path, *, system: str | None = None, machine: str | None = None
) -> list[str]:
    """Return the ``wheel tags`` invocation that forces the ``py3-none`` tag.

    The payload is a binary with no Python ABI, so the Python and ABI tags are
    wrong on every platform and are forced here. The platform tag is only
    forced on Linux, where this step is what applies it at all; on Darwin the
    repair already renamed the wheel from the payload, and overwriting that
    with the floor the build asked for would replace the evidence with the
    assumption.

    Args:
        wheel: The repaired wheel.
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.

    Returns:
        The argument list to run.

    Raises:
        platforms.UnsupportedPlatformError: For a platform with no platform tag
            here.
    """
    resolved = system or platform.system()
    if resolved not in {"Linux", "Darwin"}:
        raise platforms.UnsupportedPlatformError(f"no wheel is retagged for {resolved}")
    tag_arguments = (
        ["--platform-tag", platforms.platform_tag(system=resolved, machine=machine)]
        if resolved == "Linux"
        else []
    )
    return [
        "wheel",
        "tags",
        "--python-tag",
        "py3",
        "--abi-tag",
        "none",
        *tag_arguments,
        "--remove",
        str(wheel),
    ]


def wheel_platform_tags(wheel: Path) -> list[str]:
    """Return the platform tags a wheel's filename carries.

    Args:
        wheel: Any wheel; it need not exist.

    Returns:
        One entry per tag in the last component of the filename, which is a
        compressed tag *set* and may hold several.
    """
    return wheel.name.removesuffix(".whl").rsplit("-", 1)[-1].split(".")


def verify_platform_tag(wheel: Path, *, expected: str | None = None) -> None:
    """Check that a finished wheel is tagged for the platform that was built.

    On Linux this confirms the repair and retag steps ran at all, since an
    unrepaired wheel is tagged ``linux_<arch>``. On Darwin that is not what it
    buys — ``setup.py``'s platform wheel already carries a ``macosx`` tag — and
    the value is elsewhere: ``delocate`` derives the tag from the payload,
    so the filename is a measurement of what the build actually produced, and a
    value other than the one chosen means the wheel would be installable on a
    different set of machines than the metadata, the README and the release
    notes all describe. A *higher* floor is the dangerous direction and the one
    ADR-0006's ticket 06 chose against, but a lower one is not a bonus: it is
    the same disagreement, and nothing published can be corrected afterwards.

    Args:
        wheel: The finished wheel.
        expected: The tag to require; defaults to this platform's.

    Raises:
        PlatformTagError: If the filename carries anything else.
    """
    required = expected or platforms.platform_tag()
    found = wheel_platform_tags(wheel)
    if found != [required]:
        advice = (
            "the tag is computed from the largest minos in the payload, so this "
            "is what the build produced rather than a naming mistake: find the "
            "Mach-O that disagrees (`otool -l ... | grep -A3 LC_BUILD_VERSION`) "
            "rather than renaming the file"
            if required.startswith("macosx")
            else "the tag is applied by the repair and retag steps, so a wheel "
            "carrying another one means a step did nothing and its output was "
            "the wheel that went into it"
        )
        raise PlatformTagError(
            f"{wheel.name} is tagged {'.'.join(found)}, not {required}: {advice}."
        )


def build_environment(
    *, system: str | None = None, machine: str | None = None
) -> dict[str, str]:
    """Return the environment variables the wheel build needs, per platform.

    Nothing is needed on Linux: the raw wheel is tagged ``linux_<arch>`` and
    ``auditwheel repair --plat`` replaces that with the manylinux tag.

    On Darwin one variable is load-bearing. ``actions/setup-python`` installs a
    *universal2* CPython, so ``sysconfig.get_platform()`` reports
    ``macosx-<floor>-universal2`` and ``python -m build`` stamps that onto the
    raw wheel although the payload is arm64 only. ``delocate`` reads the
    incoming tag as the set of architectures the payload must have -- the same
    property that lets it derive the floor from the payload -- and fails with
    "Failed to find any binary with the required architecture: 'x86_64'".
    ``_PYTHON_HOST_PLATFORM`` is what ``sysconfig.get_platform()`` returns
    verbatim when set, so the raw wheel names the architecture that was built.

    The value is derived from :func:`wheelbuild.platforms.platform_tag` rather
    than spelled out, so the floor cannot disagree between the raw wheel and
    the finished one; the two differ only in how they punctuate it, since
    ``sysconfig`` dots the version and separates with dashes where a wheel tag
    joins everything with underscores.

    Args:
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.

    Returns:
        Extra environment variables, layered over the build's own environment.

    Raises:
        platforms.UnsupportedPlatformError: For an unsupported platform, or on
            Darwin when the build exported no ``MACOSX_DEPLOYMENT_TARGET``.
    """
    resolved = system or platform.system()
    if resolved != "Darwin":
        return {}
    prefix, major, minor, architecture = platforms.platform_tag(
        system=resolved, machine=machine
    ).split("_", 3)
    return {"_PYTHON_HOST_PLATFORM": f"{prefix}-{major}.{minor}-{architecture}"}


def build(
    *,
    project_dir: Path,
    install_prefix: Path,
    output_dir: Path,
    notices: Path | None = None,
) -> Path:
    """Run the full assemble → repair → retag pipeline.

    Args:
        project_dir: Repository root holding ``pyproject.toml``.
        install_prefix: Superbuild install prefix.
        output_dir: Where the repaired wheel is written.
        notices: Optional THIRD-PARTY-NOTICES file to ship.

    Returns:
        Path of the final wheel.

    Raises:
        UnattributedLibraryError: If the repaired wheel carries a library no
            license notice in it accounts for.
        PlatformTagError: If the finished wheel is tagged for another platform.
    """
    package_dir = project_dir / "palace_solver"
    stage(install_prefix=install_prefix, package_dir=package_dir, notices=notices)

    clean_build_tree(project_dir)
    raw_dir = output_dir / "raw"
    shutil.rmtree(raw_dir, ignore_errors=True)
    check_call(
        [
            "python",
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(raw_dir),
            str(project_dir),
        ],
        env=build_environment(),
    )
    raw_wheel = _single_wheel(raw_dir)

    # Wheels from earlier runs may still be in the output directory, so each
    # step is identified by the file it adds rather than by what is there.
    before_repair = _wheels(output_dir)
    check_call(repair_command(wheel=raw_wheel, output_dir=output_dir))
    repaired = pick_wheel(
        before=before_repair, after=_wheels(output_dir), fallback=raw_wheel
    )

    before_retag = _wheels(output_dir)
    check_call(retag_command(repaired))
    final = pick_wheel(
        before=before_retag, after=_wheels(output_dir), fallback=repaired
    )
    # What the wheel ends up claiming is asserted on the file rather than assumed
    # from the commands that were run. The two platforms get different value from
    # it. On Linux it catches a repair or retag that renamed nothing and was
    # picked up by pick_wheel's fallback, because the unrepaired wheel is tagged
    # `linux_<arch>` and the finished one must not be. On Darwin it cannot catch
    # that -- setup.py's platform wheel already carries the macosx tag, so an
    # unrepaired fallback would pass -- and instead catches the case only this
    # platform has: a payload built against a newer SDK major, which delocate
    # writes into the filename.
    verify_platform_tag(final)
    # The repair step is what pulls the compiler runtime in, after the notices
    # were harvested from source checkouts it has none of, so what the wheel
    # ended up carrying is only knowable here.
    vendored = notices_module.audit_wheel(wheel=final, install_prefix=install_prefix)
    print(f"vendored libraries: {', '.join(vendored)}", flush=True)
    print(size_report(final).text, flush=True)
    return final


def _wheels(directory: Path) -> set[Path]:
    return set(directory.glob("*.whl"))


def clean_build_tree(project_dir: Path) -> None:
    """Delete setuptools' ``build/`` directory.

    setuptools copies package data into ``build/lib.*/`` and reuses whatever is
    already there, so a payload staged by an earlier run would be shipped again
    even after the staging step stopped producing it.

    Args:
        project_dir: Repository root.
    """
    shutil.rmtree(project_dir / "build", ignore_errors=True)


def pick_wheel(*, before: set[Path], after: set[Path], fallback: Path) -> Path:
    """Return the wheel a build step produced.

    Args:
        before: Wheels present before the step.
        after: Wheels present after it.
        fallback: The wheel to assume when the step renamed nothing — retagging
            an already correctly tagged wheel leaves the filename unchanged.

    Returns:
        The wheel the step left behind.

    Raises:
        RuntimeError: If the step added more than one wheel.
    """
    added = after - before
    if len(added) > 1:
        raise RuntimeError(f"ambiguous build step output: added {sorted(added)}")
    return added.pop() if added else fallback


def _single_wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel in {directory}, found {wheels}")
    return wheels[0]


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for wheel assembly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--install-prefix", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--notices", type=Path, default=None)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    build(
        project_dir=args.project_dir,
        install_prefix=args.install_prefix,
        output_dir=args.output_dir,
        notices=args.notices,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
