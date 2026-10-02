from pathlib import Path

import pytest
from pe_files import write_pe

from wheelbuild import link_check, pe_repair


def _windows_build(root: Path) -> dict[str, Path]:
    """Mimic the Windows row: install prefix, fetched MS-MPI, MSYS2's ``bin``.

    The solver imports what the spike measured it importing, less the UCRT
    contracts; ``libceed`` reaches ``libxsmm`` and ``libgfortran`` reaches
    ``libquadmath``, as on the row.
    """
    install = root / "install"
    msmpi = root / "msmpi"
    toolchain = root / "ucrt64" / "bin"
    for directory in (install / "bin", install / "lib", msmpi, toolchain):
        directory.mkdir(parents=True)
    (install / "bin" / "palace").write_text('#!/bin/sh\nexec palace-x86_64.bin "$@"\n')
    write_pe(
        install / "bin" / "palace-x86_64.bin",
        [
            "KERNEL32.dll",
            "libceed.dll",
            "libopenblas.dll",
            "msmpi.dll",
            "libmsmpifec.dll",
            "libgfortran-5.dll",
        ],
    )
    write_pe(install / "bin" / "libopenblas.dll", ["libgfortran-5.dll"])
    write_pe(install / "lib" / "libceed.dll", ["libxsmm.dll"])
    write_pe(install / "lib" / "libxsmm.dll", ["KERNEL32.dll"])
    write_pe(msmpi / "msmpi.dll", ["KERNEL32.dll", "ADVAPI32.dll"])
    write_pe(msmpi / "mpiexec.exe", ["KERNEL32.dll"])
    write_pe(msmpi / "smpd.exe", ["KERNEL32.dll"])
    write_pe(toolchain / "libmsmpifec.dll", ["msmpi.dll", "libgfortran-5.dll"])
    write_pe(toolchain / "libgfortran-5.dll", ["libquadmath-0.dll"])
    write_pe(toolchain / "libquadmath-0.dll", ["KERNEL32.dll"])
    return {"install": install, "msmpi": msmpi, "toolchain": toolchain}


def test_the_search_order_puts_the_verified_msmpi_before_the_toolchain(tmp_path):
    """Only the fetched msmpi.dll has Microsoft's hash checked, so a copy in
    MSYS2's bin must never win over it; and what the superbuild built must win
    over anything MSYS2 carries under the same name.
    """
    install, msmpi, toolchain = tmp_path / "i", tmp_path / "m", tmp_path / "t"

    assert pe_repair.search_path(
        install_prefix=install, msmpi_dir=msmpi, toolchain_dir=toolchain
    ) == (install / "bin", install / "lib", msmpi, toolchain)


def test_the_roots_are_the_solver_and_both_ms_mpi_launchers(tmp_path):
    build = _windows_build(tmp_path)

    roots = pe_repair.roots(install_prefix=build["install"], msmpi_dir=build["msmpi"])

    assert roots == (
        build["install"] / "bin" / "palace-x86_64.bin",
        build["msmpi"] / "mpiexec.exe",
        build["msmpi"] / "smpd.exe",
    )


def test_repair_copies_the_whole_closure_flat(tmp_path):
    build = _windows_build(tmp_path)
    output = tmp_path / "payload"

    pe_repair.main(
        [
            f"--install-prefix={build['install']}",
            f"--msmpi-dir={build['msmpi']}",
            f"--toolchain-dir={build['toolchain']}",
            f"--output-dir={output}",
        ]
    )

    assert sorted(path.name for path in output.iterdir()) == [
        "libceed.dll",
        "libgfortran-5.dll",
        "libmsmpifec.dll",
        "libopenblas.dll",
        "libquadmath-0.dll",
        "libxsmm.dll",
        "msmpi.dll",
    ]


def test_repair_takes_msmpi_from_the_fetch_even_when_msys2_has_one(tmp_path):
    build = _windows_build(tmp_path)
    write_pe(build["toolchain"] / "msmpi.dll", ["USER32.dll"])
    output = tmp_path / "payload"

    pe_repair.repair(
        binaries=pe_repair.roots(
            install_prefix=build["install"], msmpi_dir=build["msmpi"]
        ),
        search=pe_repair.search_path(
            install_prefix=build["install"],
            msmpi_dir=build["msmpi"],
            toolchain_dir=build["toolchain"],
        ),
        output_dir=output,
    )

    assert (output / "msmpi.dll").read_bytes() == (
        build["msmpi"] / "msmpi.dll"
    ).read_bytes()


def test_repair_empties_the_payload_first(tmp_path):
    """A DLL an earlier run needed must not ride along into this run's wheel."""
    build = _windows_build(tmp_path)
    output = tmp_path / "payload"
    output.mkdir()
    (output / "libstale.dll").write_bytes(b"MZ")

    pe_repair.repair(
        binaries=pe_repair.roots(
            install_prefix=build["install"], msmpi_dir=build["msmpi"]
        ),
        search=pe_repair.search_path(
            install_prefix=build["install"],
            msmpi_dir=build["msmpi"],
            toolchain_dir=build["toolchain"],
        ),
        output_dir=output,
    )

    assert not (output / "libstale.dll").exists()


def test_repair_refuses_an_import_it_cannot_find_and_copies_nothing(tmp_path):
    build = _windows_build(tmp_path)
    (build["toolchain"] / "libquadmath-0.dll").unlink()
    output = tmp_path / "payload"

    with pytest.raises(link_check.UnresolvedImportError, match=r"libquadmath-0\.dll"):
        pe_repair.repair(
            binaries=pe_repair.roots(
                install_prefix=build["install"], msmpi_dir=build["msmpi"]
            ),
            search=pe_repair.search_path(
                install_prefix=build["install"],
                msmpi_dir=build["msmpi"],
                toolchain_dir=build["toolchain"],
            ),
            output_dir=output,
        )
    assert not output.exists()
