import textwrap

import pytest

from palace_solver import MPICH_VERSION
from wheelbuild import pin_check

PYPROJECT = textwrap.dedent("""
    [project]
    name = "palais"
    dependencies = [
      "numpy>=1.20.0",
      "mpi4py>=3.1.0",
      "mpich<5"
    ]
""")


def test_the_vendored_mpich_satisfies_the_pin_palais_declares():
    assert pin_check.satisfies(MPICH_VERSION, pin_check.PALAIS_MPICH_REQUIREMENT)


def test_the_next_major_series_does_not_satisfy_the_pin():
    assert not pin_check.satisfies("5.0.1", "mpich<5")


def test_the_mpich_requirement_is_picked_out_of_a_dependency_list():
    assert pin_check.mpich_requirement(["numpy>=1.20", "mpich<5"]) == "mpich<5"


def test_a_dependency_list_without_mpich_declares_no_requirement():
    assert pin_check.mpich_requirement(["numpy>=1.20", "mpi4py>=3.1.0"]) is None


def test_dependencies_are_read_from_a_palais_checkout(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)

    assert pin_check.mpich_requirement(pin_check.checkout_dependencies(tmp_path)) == (
        "mpich<5"
    )


def test_a_directory_without_a_pyproject_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"pyproject\.toml"):
        pin_check.checkout_dependencies(tmp_path)


def test_a_vendored_mpich_inside_the_pin_is_not_reported():
    assert pin_check.vendored_problem("4.3.2", "mpich<5") is None


def test_a_vendored_mpich_outside_the_pin_is_reported():
    problem = pin_check.vendored_problem("5.0.1", "mpich<5")

    assert problem is not None
    assert "5.0.1" in problem
    assert "mpich<5" in problem


def test_a_palais_pin_spelled_differently_still_agrees():
    assert pin_check.declared_problem("mpich<5", "mpich <5") is None


def test_a_palais_pin_that_moved_is_reported():
    problem = pin_check.declared_problem("mpich<5", "mpich<6")

    assert problem is not None
    assert "mpich<6" in problem
    assert "MPICH_VERSION" in problem


def test_a_palais_that_dropped_mpich_is_reported():
    problem = pin_check.declared_problem("mpich<5", None)

    assert problem is not None
    assert "no mpich requirement" in problem


def test_the_command_line_check_passes_against_the_recorded_pin(capsys):
    assert pin_check.main([]) == 0
    printed = capsys.readouterr().out
    assert MPICH_VERSION in printed
    # Without a checkout the check cannot see palais's side, and must say so
    # rather than report a comparison it did not make.
    assert "NOT compared against palais" in printed


def test_the_command_line_check_names_the_checkout_it_compared_against(
    tmp_path, capsys
):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)

    assert pin_check.main(["--palais", str(tmp_path)]) == 0
    assert "mpich<5" in capsys.readouterr().out


def test_the_command_line_check_fails_against_a_diverged_checkout(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT.replace("mpich<5", "mpich<6"))

    assert pin_check.main(["--palais", str(tmp_path)]) == 1
    assert "mpich<6" in capsys.readouterr().err
