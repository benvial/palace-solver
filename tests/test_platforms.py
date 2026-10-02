from pathlib import Path

import pytest

from wheelbuild import platforms

ELF_MAGIC = b"\x7fELF\x02\x01\x01\x00"
MACH_O_ARM64_MAGIC = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"
MACH_O_UNIVERSAL_MAGIC = b"\xca\xfe\xba\xbe\x00\x00\x00\x02"


def test_is_native_binary_accepts_an_elf_binary(tmp_path):
    path = tmp_path / "palace-x86_64.bin"
    path.write_bytes(ELF_MAGIC + b"binary")

    assert platforms.is_native_binary(path)


def test_is_native_binary_accepts_a_mach_o_binary(tmp_path):
    path = tmp_path / "palace-arm64.bin"
    path.write_bytes(MACH_O_ARM64_MAGIC + b"binary")

    assert platforms.is_native_binary(path)


def test_is_native_binary_accepts_a_universal_mach_o_binary(tmp_path):
    """Homebrew's and Apple's own tools ship fat binaries; one arch is still ours."""
    path = tmp_path / "hydra_pmi_proxy"
    path.write_bytes(MACH_O_UNIVERSAL_MAGIC + b"binary")

    assert platforms.is_native_binary(path)


def test_is_native_binary_rejects_a_wrapper_script(tmp_path):
    path = tmp_path / "palace"
    path.write_text('#!/bin/sh\nexec palace-arm64.bin "$@"\n')

    assert not platforms.is_native_binary(path)


def test_is_native_binary_rejects_a_file_too_short_to_have_magic(tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")

    assert not platforms.is_native_binary(path)


@pytest.mark.parametrize(
    ("stem", "version", "system", "expected"),
    [
        ("libmpi", "12", "Linux", "lib/libmpi.so.12"),
        ("libmpi", "12", "Darwin", "lib/libmpi.12.dylib"),
        ("libmpifort", "12", "Linux", "lib/libmpifort.so.12"),
        ("libmpifort", "12", "Darwin", "lib/libmpifort.12.dylib"),
        ("libopenblas", None, "Linux", "lib/libopenblas.so"),
        ("libopenblas", None, "Darwin", "lib/libopenblas.dylib"),
    ],
)
def test_library_path_places_the_version_per_platform(stem, version, system, expected):
    """ELF appends the soname version, Mach-O puts it before the suffix."""
    assert platforms.library_path(stem, version=version, system=system) == Path(
        expected
    )


def test_library_path_defaults_to_the_running_platform():
    path = platforms.library_path("libopenblas")

    assert path in {Path("lib/libopenblas.so"), Path("lib/libopenblas.dylib")}


def test_library_path_rejects_an_unsupported_platform():
    with pytest.raises(platforms.UnsupportedPlatformError, match="Windows"):
        platforms.library_path("libmpi", version="12", system="Windows")


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", "x86_64"),
        ("Linux", "aarch64", "aarch64"),
        # Some container runtimes report the arm64 spelling on the machine
        # manylinux calls aarch64; the wheel tag is the manylinux one.
        ("Linux", "arm64", "aarch64"),
        ("Darwin", "arm64", "arm64"),
        # platform.machine() on Windows reports the uppercase AMD64; the wheel
        # tag spells it lowercase.
        ("Windows", "AMD64", "amd64"),
    ],
)
def test_architecture_normalises_the_machine_name(system, machine, expected):
    assert platforms.architecture(system=system, machine=machine) == expected


def test_architecture_rejects_a_machine_no_platform_targets():
    with pytest.raises(platforms.UnsupportedPlatformError, match="i686"):
        platforms.architecture(system="Linux", machine="i686")


def test_architecture_rejects_macos_x86_64():
    """Ruled out by ADR-0006: upstream Palace stopped building it in 2024."""
    with pytest.raises(platforms.UnsupportedPlatformError, match="x86_64"):
        platforms.architecture(system="Darwin", machine="x86_64")


def test_architecture_rejects_windows_arm64():
    """Out of scope per ADR-0007: only x86-64 Windows ships a wheel."""
    with pytest.raises(platforms.UnsupportedPlatformError, match="arm64"):
        platforms.architecture(system="Windows", machine="ARM64")


def test_architecture_rejects_a_system_no_wheel_is_built_for():
    with pytest.raises(platforms.UnsupportedPlatformError, match="FreeBSD"):
        platforms.architecture(system="FreeBSD", machine="amd64")


def test_platform_tag_for_x86_64_linux_is_what_the_shipping_wheel_carries():
    """The published filename and every cache key namespace depend on this string."""
    assert (
        platforms.platform_tag(system="Linux", machine="x86_64")
        == "manylinux_2_28_x86_64"
    )


def test_platform_tag_for_aarch64_linux_names_the_same_manylinux_version():
    assert (
        platforms.platform_tag(system="Linux", machine="aarch64")
        == "manylinux_2_28_aarch64"
    )


def test_platform_tag_for_x86_64_windows_is_win_amd64():
    assert platforms.platform_tag(system="Windows", machine="AMD64") == "win_amd64"


def test_platform_tag_on_windows_ignores_the_macos_floor(monkeypatch):
    """The deployment target is a Darwin concept; a Windows tag has no floor."""
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "15.0")

    assert (
        platforms.platform_tag(system="Windows", machine="AMD64", macos_version="15.0")
        == "win_amd64"
    )


def test_platform_tag_rejects_windows_arm64():
    with pytest.raises(platforms.UnsupportedPlatformError, match="arm64"):
        platforms.platform_tag(system="Windows", machine="ARM64")


def test_platform_tag_on_macos_comes_from_the_deployment_target():
    """delocate recomputes the real tag from the payload; this is the floor."""
    assert (
        platforms.platform_tag(system="Darwin", machine="arm64", macos_version="15.0")
        == "macosx_15_0_arm64"
    )


def test_platform_tag_on_macos_ignores_a_patch_component():
    """Wheel tags carry major and minor only, and on macOS 11 and later the
    minor is always zero — so a patch component cannot survive by either route.
    """
    assert (
        platforms.platform_tag(system="Darwin", machine="arm64", macos_version="15.4.1")
        == "macosx_15_0_arm64"
    )


def test_platform_tag_on_macos_reads_the_deployment_target_from_the_environment(
    monkeypatch,
):
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "15.0")

    assert (
        platforms.platform_tag(system="Darwin", machine="arm64") == "macosx_15_0_arm64"
    )


def test_platform_tag_on_macos_refuses_to_guess_the_deployment_target(monkeypatch):
    """Falling back to the runner's own macOS would tag the wheel for the builder.

    Ticket 06 settled that ``MACOSX_DEPLOYMENT_TARGET`` is set explicitly, so an
    unset one is a misconfigured build rather than a default to paper over.
    """
    monkeypatch.delenv("MACOSX_DEPLOYMENT_TARGET", raising=False)

    with pytest.raises(platforms.UnsupportedPlatformError, match="MACOSX"):
        platforms.platform_tag(system="Darwin", machine="arm64")


def test_platform_tag_on_macos_rejects_a_major_only_deployment_target():
    """A bare "15" would tag the wheel macosx_15_None_arm64 or similar."""
    with pytest.raises(platforms.UnsupportedPlatformError, match="MACOSX"):
        platforms.platform_tag(system="Darwin", machine="arm64", macos_version="15")


def test_the_pinned_macos_floor_is_the_tag_the_matrix_row_claims():
    """Ticket 06: on `macos-15` every Homebrew runtime the wheel vendors is
    `minos` exactly 15.0.0, so 15.0 is both the floor the build compiles to and
    the tag delocate will compute from the payload.
    """
    tag = platforms.platform_tag(
        system="Darwin",
        machine="arm64",
        macos_version=platforms.MACOS_DEPLOYMENT_TARGET,
    )

    assert tag == "macosx_15_0_arm64"


def test_a_macos_tag_carries_a_zero_minor_whatever_the_floor_is():
    """Since macOS 11 the minor version is not part of the release version, and
    no wheel tag expresses 15.4. delocate renames the wheel by the same rule, so
    deriving macosx_15_4 here would fail the tag check on every build.
    """
    assert (
        platforms.platform_tag(system="Darwin", machine="arm64", macos_version="15.4")
        == "macosx_15_0_arm64"
    )


def test_an_old_macos_tag_keeps_its_minor():
    """Before macOS 11 the minor version was the release, and 10.15 is not 10.0."""
    assert (
        platforms.platform_tag(system="Darwin", machine="arm64", macos_version="10.15")
        == "macosx_10_15_arm64"
    )


def test_supported_platform_tags_are_the_four_platforms_adr_0006_and_0007_fix():
    """The three of ADR-0006, unchanged, and the Windows one ADR-0007 adds."""
    assert platforms.supported_platform_tags() == (
        "manylinux_2_28_x86_64",
        "manylinux_2_28_aarch64",
        "macosx_15_0_arm64",
        "win_amd64",
    )


def test_supported_platform_tags_does_not_read_the_environment(monkeypatch):
    """A release's platform set is the same question on every builder, so the
    macOS entry comes from the pinned floor rather than from whatever the
    process happens to export.
    """
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "13.0")

    assert "macosx_15_0_arm64" in platforms.supported_platform_tags()


def test_supported_platform_tags_lists_each_platform_once():
    """The architecture table maps several machine spellings onto one
    architecture; each is an alias, not a further platform.
    """
    tags = platforms.supported_platform_tags()

    assert len(tags) == len(set(tags))
