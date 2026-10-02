"""The Windows twins of the wheel tests, as far as Linux can run them.

scripts/smoke-test-windows.py and scripts/interop-test-windows.py run only on
the Windows row; these hold the parts of them that are plain Python.
"""

import base64
import hashlib
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _windows_wheel  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name.removesuffix(".py").replace("-", "_"), SCRIPTS / name
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["smoke-test-windows.py", "interop-test-windows.py"])
def test_each_twin_imports_off_windows(name):
    """Every Windows-only call is inside a function, so a typo is caught here
    rather than on a runner after the build."""
    assert callable(_load(name).main)


def test_the_path_holds_the_venv_and_windows_and_nothing_else():
    scripts = Path(r"C:\t\venv\Scripts")
    environment = _windows_wheel.clean_environment(
        {"PATH": r"C:\msys64\ucrt64\bin", "SYSTEMROOT": r"C:\Windows"},
        scripts,
        "C:\\Windows\\",
    )

    first, *rest = environment["PATH"].split(";")
    assert first == str(scripts)
    assert rest == [
        r"C:\Windows\System32",
        r"C:\Windows",
        r"C:\Windows\System32\Wbem",
        r"C:\Windows\System32\WindowsPowerShell\v1.0",
    ]
    assert environment["SYSTEMROOT"] == r"C:\Windows"


def test_no_launch_setting_reaches_a_check():
    environment = _windows_wheel.clean_environment(
        {
            "PMI_RANK": "1",
            "PMI_KVS": "kvs",
            "MSMPI_LOCAL_ONLY": "0",
            "PALACE_SOLVER_ALLOW_FOREIGN_LAUNCHER": "1",
            "OMP_NUM_THREADS": "8",
            "TEMP": r"C:\tmp",
        },
        Path("Scripts"),
        r"C:\Windows",
    )

    assert set(environment) == {"TEMP", "PATH"}


def test_lines_are_counted_whatever_their_ending():
    output = "Dry-run: ok\r\nnoise Dry-run:\r\nDry-run: again\r\n"

    assert _windows_wheel.count_lines(output, "Dry-run:") == 2
    assert _windows_wheel.count_occurrences(output, "Dry-run:") == 3


def _report(directory, values):
    (directory / "postpro").mkdir(parents=True)
    rows = "\n".join(",".join(str(v) for v in row) for row in values)
    (directory / "postpro" / "domain-E.csv").write_text("a,b\n" + rows + "\n")


def test_two_equal_solves_agree(tmp_path):
    _report(tmp_path / "one", [[1.0, 2.0], [3.0, 4.0]])
    _report(tmp_path / "two", [[1.0, 2.0], [3.0, 4.0 + 1e-12]])

    agreed = _windows_wheel.compare_reports(
        tmp_path / "one", tmp_path / "two", labels=("one", "two")
    )

    assert agreed == ["domain-E.csv: 4 values agree"]


def test_two_different_solves_fail(tmp_path):
    _report(tmp_path / "one", [[1.0, 2.0]])
    _report(tmp_path / "two", [[1.0, 2.5]])

    with pytest.raises(SystemExit):
        _windows_wheel.compare_reports(
            tmp_path / "one", tmp_path / "two", labels=("one", "two")
        )


def test_a_solve_with_no_output_fails(tmp_path):
    (tmp_path / "one").mkdir()
    _report(tmp_path / "two", [[1.0]])

    with pytest.raises(SystemExit):
        _windows_wheel.compare_reports(
            tmp_path / "one", tmp_path / "two", labels=("one", "two")
        )


def test_the_probe_wheel_declares_its_console_script_and_records_its_files(tmp_path):
    wheel = _windows_wheel.write_console_script_wheel(
        tmp_path, name="palace-parent-probe", module="probe", source="def main(): ...\n"
    )

    assert wheel.name == "palace_parent_probe-0-py3-none-any.whl"
    dist_info = "palace_parent_probe-0.dist-info"
    with zipfile.ZipFile(wheel) as archive:
        entry_points = archive.read(f"{dist_info}/entry_points.txt").decode()
        assert "palace-parent-probe = probe:main" in entry_points
        for line in archive.read(f"{dist_info}/RECORD").decode().splitlines():
            path, digest, size = line.split(",")
            if path.endswith("RECORD"):
                continue
            data = archive.read(path)
            expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            assert digest == "sha256=" + expected.rstrip(b"=").decode()
            assert int(size) == len(data)


@pytest.mark.parametrize(
    ("version", "proved"),
    [("10.1.12498.52", True), ("10.0.12498.5", False), ("10.10.1", False)],
)
def test_the_system_msmpi_is_held_to_the_recorded_series(version, proved):
    assert _load("interop-test-windows.py").in_proved_series(version) is proved
