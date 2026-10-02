"""The conventions that differ between the platforms the wheel is built for.

The build always runs natively: inside the matching manylinux image on Linux,
on an Apple Silicon runner on macOS, on an x86-64 runner on Windows. "Which
platform" is therefore the one this interpreter runs on, and the three things
that differ between them live here rather than being spelled out again in each
validator that consults them.

**Binary format.** Executables and shared libraries are ELF on Linux and Mach-O
on Darwin, so the magic-number test that picks the real Palace binary out of an
install tree — past the wrapper script Palace installs beside it — has to know
both.

**Shared-library naming.** ELF appends the soname version
(``libmpi.so.12``); Mach-O puts it before the suffix (``libmpi.12.dylib``). The
MPICH and OpenBLAS install validators both check for named libraries, so the
convention is a function they share.

**The wheel's platform tag.** It carries an architecture, and on Darwin also an
operating-system floor. On Linux the tag is final: ``auditwheel repair --plat``
and ``wheel tags --platform-tag`` are both given it verbatim. On Darwin it is
only a floor — ``delocate`` walks every Mach-O in the wheel and computes the
tag from the largest ``minos`` it finds, so the value here constrains which
architectures survive the repair and namespaces the build cache, and the
published filename comes from the payload. See
``docs/adr/0006-parameterised-raw-jobs-not-cibuildwheel.md``. On Windows the
tag is final as on Linux, and has no floor: ``win_amd64``, applied by
``wheel tags --platform-tag``. See ``docs/adr/0007-ship-a-windows-wheel.md``.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

#: The manylinux profile every Linux wheel claims, per ADR-0006.
MANYLINUX_VERSION = "manylinux_2_28"

#: The oldest macOS the wheel is compiled for, and the value the build exports
#: as ``MACOSX_DEPLOYMENT_TARGET``.
#:
#: It is a property of the runner image rather than a free choice. Every
#: Homebrew runtime the wheel vendors — ``libgfortran``, ``libgomp``,
#: ``libstdc++``, ``libquadmath`` — is built by Homebrew with no deployment
#: target of its own, so each bottle carries the ``minos`` of the machine that
#: built it: exactly ``15.0.0`` for ``arm64_sequoia``, which is what
#: ``macos-15`` installs. A lower floor would be a claim the payload cannot
#: keep, and ``delocate`` refuses it rather than honouring it. Raising it above
#: the payload only excludes users: a 15.0 floor excludes about 13.8% of Macs
#: against 36.3% for 26.0.
#:
#: This is a floor and not the published tag. ``delocate`` computes the tag
#: from the largest ``minos`` in the payload, so setting this is what stops
#: Palace's own binaries — which clang would otherwise compile against the
#: running system's SDK, at 15.5 or 15.7 — from quietly exceeding it.
#: Ticket 06 of the platform-expansion effort has the measurements.
MACOS_DEPLOYMENT_TARGET = "15.0"

_ELF_MAGIC = b"\x7fELF"

#: Mach-O headers, thin and universal, in both byte orders. Mach-O spells its
#: magic as a 32-bit word, so a little-endian host writes it reversed; a
#: universal ("fat") binary carries an archive header instead and a per-slice
#: Mach-O header further in.
_MACH_O_MAGICS = frozenset(
    {
        b"\xfe\xed\xfa\xce",  # MH_MAGIC, 32-bit
        b"\xce\xfa\xed\xfe",  # MH_CIGAM
        b"\xfe\xed\xfa\xcf",  # MH_MAGIC_64
        b"\xcf\xfa\xed\xfe",  # MH_CIGAM_64 — what arm64 macOS writes
        b"\xca\xfe\xba\xbe",  # FAT_MAGIC
        b"\xbe\xba\xfe\xca",  # FAT_CIGAM
        b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
        b"\xbf\xba\xfe\xca",  # FAT_CIGAM_64
    }
)

#: Machine names per platform, mapped to the spelling the wheel tag uses.
#: Deliberately not a single table: ``arm64`` means the aarch64 manylinux wheel
#: on Linux and the ``arm64`` macOS wheel on Darwin, and macOS x86_64 is ruled
#: out by ADR-0006 rather than merely unbuilt, so naming it nowhere is the
#: check. Windows arm64 is likewise out of scope per ADR-0007. Windows reports
#: x86-64 as ``AMD64``, which :func:`architecture` lowercases before the lookup.
_ARCHITECTURES = {
    "Linux": {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    },
    "Darwin": {"arm64": "arm64", "aarch64": "arm64"},
    "Windows": {"amd64": "amd64"},
}


class UnsupportedPlatformError(RuntimeError):
    """Raised for a platform this package does not build a wheel for."""


def is_native_binary(path: Path) -> bool:
    """Whether ``path`` is a compiled executable or shared library.

    Recognises ELF and Mach-O, including universal Mach-O binaries, from the
    first four bytes — the cheapest possible read, and the reason this is not a
    call out to ``file``. Deliberately not restricted to the running platform's
    format: the callers are asking whether a file is the compiled artefact or
    the wrapper script beside it, which is one question on both platforms.

    Args:
        path: File to inspect.

    Returns:
        True for an ELF or Mach-O file, False for anything else, a wrapper
        script included.
    """
    with path.open("rb") as handle:
        magic = handle.read(4)
    return magic == _ELF_MAGIC or magic in _MACH_O_MAGICS


def _shared_library_name(
    stem: str, *, version: str | None = None, system: str | None = None
) -> str:
    """Return a shared library's filename on one platform.

    Args:
        stem: Library name including the ``lib`` prefix, such as ``libmpi``.
        version: Soname major version, if the install carries a versioned name.
        system: ``platform.system()`` value; defaults to the running platform.

    Returns:
        ``libmpi.so.12`` on Linux, ``libmpi.12.dylib`` on Darwin.

    Raises:
        UnsupportedPlatformError: For any other platform.
    """
    resolved = system or platform.system()
    if resolved == "Linux":
        return f"{stem}.so" if version is None else f"{stem}.so.{version}"
    if resolved == "Darwin":
        return f"{stem}.dylib" if version is None else f"{stem}.{version}.dylib"
    raise UnsupportedPlatformError(
        f"no shared library naming convention for {resolved}"
    )


def library_path(
    stem: str, *, version: str | None = None, system: str | None = None
) -> Path:
    """Return a shared library's path relative to an install prefix.

    Args:
        stem: Library name including the ``lib`` prefix.
        version: Soname major version, if any.
        system: ``platform.system()`` value; defaults to the running platform.

    Returns:
        ``lib/libmpi.so.12`` on Linux, ``lib/libmpi.12.dylib`` on Darwin. The
        directory is ``lib`` on both, which is what :mod:`wheelbuild.prefix`
        exists to guarantee.

    Raises:
        UnsupportedPlatformError: For a platform with no naming convention here.
    """
    return Path("lib") / _shared_library_name(stem, version=version, system=system)


def architecture(*, system: str | None = None, machine: str | None = None) -> str:
    """Return the architecture component of the wheel's platform tag.

    Args:
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.

    Returns:
        ``x86_64`` or ``aarch64`` on Linux, ``arm64`` on Darwin, ``amd64`` on
        Windows.

    Raises:
        UnsupportedPlatformError: For a platform or machine this package does
            not ship a wheel for — macOS x86_64 included, which ADR-0006 rules
            out rather than defers, and Windows arm64, which ADR-0007 leaves
            out of scope.
    """
    resolved_system = system or platform.system()
    resolved_machine = (machine or platform.machine()).lower()
    known = _ARCHITECTURES.get(resolved_system)
    if known is None:
        raise UnsupportedPlatformError(f"no wheel is built for {resolved_system}")
    if resolved_machine not in known:
        raise UnsupportedPlatformError(
            f"no {resolved_system} wheel is built for {resolved_machine}"
        )
    return known[resolved_machine]


def supported_platform_tags() -> tuple[str, ...]:
    """Return the platform tag of every platform this package builds a wheel for.

    Derived from the same architecture table :func:`architecture` reads, so the
    set cannot drift from what the build accepts: a platform this returns a tag
    for is a platform the validators, the repair step and the cache namespace
    all already know. The macOS entry is resolved against
    :data:`MACOS_DEPLOYMENT_TARGET` rather than the environment, because the
    question here is which platforms a *release* carries and that is the same
    answer off Darwin as on it.

    A matrix row is still the thing that builds one, so
    ``tests/test_wheels_workflow.py`` asserts these are exactly the rows'
    tags — a platform added to one and not the other fails the unit suite
    rather than a release.

    Returns:
        One tag per supported platform, Linux platforms first, then macOS, then
        Windows.
    """
    return tuple(
        platform_tag(
            system=system, machine=machine, macos_version=MACOS_DEPLOYMENT_TARGET
        )
        for system, machines in _ARCHITECTURES.items()
        # The table maps several spellings onto each architecture, so the
        # canonical values are the platforms and the keys are only aliases.
        for machine in dict.fromkeys(machines.values())
    )


def platform_tag(
    *,
    system: str | None = None,
    machine: str | None = None,
    macos_version: str | None = None,
) -> str:
    """Return the wheel platform tag for one platform.

    Args:
        system: ``platform.system()`` value; defaults to the running platform.
        machine: ``platform.machine()`` value; defaults to the running machine.
        macos_version: The macOS floor the wheel claims. Defaults to
            ``MACOSX_DEPLOYMENT_TARGET``, and is ignored off Darwin.

    Returns:
        ``manylinux_2_28_x86_64``, ``manylinux_2_28_aarch64``,
        ``macosx_<major>_<minor>_arm64`` or ``win_amd64``.

    Raises:
        UnsupportedPlatformError: For an unsupported platform, or on Darwin
            when no deployment target is set — the runner's own macOS version
            is not a safe default, since it would tag the wheel for the machine
            that built it rather than for the floor the build was compiled to.
    """
    resolved_system = system or platform.system()
    architecture_name = architecture(system=resolved_system, machine=machine)
    if resolved_system == "Linux":
        return f"{MANYLINUX_VERSION}_{architecture_name}"
    if resolved_system == "Windows":
        return f"win_{architecture_name}"

    target = macos_version or os.environ.get("MACOSX_DEPLOYMENT_TARGET")
    if not target:
        raise UnsupportedPlatformError(
            "MACOSX_DEPLOYMENT_TARGET is unset: the macOS wheel's floor has to "
            "be chosen by the build, not read off the runner"
        )
    components = target.split(".")
    if len(components) < 2:
        raise UnsupportedPlatformError(
            f"MACOSX_DEPLOYMENT_TARGET={target!r} names no minor version; "
            "a wheel tag needs both major and minor"
        )
    major, minor = components[0], components[1]
    # Since macOS 11 the minor component is not part of the release version and
    # a wheel tag has to carry zero: pip reads macosx_15_0 as "macOS 15 and
    # later", and there is no tag that expresses 15.4. delocate applies the same
    # rule when it renames the wheel from the payload
    # (``_calculate_minimum_wheel_name``: ``minor = 0 if version.major >= 11``),
    # so a floor of 15.4 here without this would derive a tag delocate never
    # produces and the check in wheelbuild.assemble would fail every build.
    if int(major) >= 11:
        minor = "0"
    return f"macosx_{major}_{minor}_{architecture_name}"
