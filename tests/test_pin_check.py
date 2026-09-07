from palace_solver import MPICH_VERSION
from wheelbuild import pin_check


def test_the_vendored_mpich_satisfies_the_recorded_range():
    assert pin_check.satisfies(MPICH_VERSION, pin_check.INTEROP_MPICH_REQUIREMENT)


def test_the_next_major_series_does_not_satisfy_the_range():
    assert not pin_check.satisfies("5.0.1", "mpich<5")


def test_a_vendored_mpich_inside_the_range_is_not_reported():
    assert pin_check.vendored_problem("4.3.2", "mpich<5") is None


def test_a_vendored_mpich_outside_the_range_is_reported():
    problem = pin_check.vendored_problem("5.0.1", "mpich<5")

    assert problem is not None
    assert "5.0.1" in problem
    assert "mpich<5" in problem
    # The way out is either to move the version back or to widen the range
    # after re-proving interop; the message must name both knobs.
    assert "MPICH_VERSION" in problem
    assert "INTEROP_MPICH_REQUIREMENT" in problem


def test_the_command_line_check_passes_against_the_recorded_range(capsys):
    assert pin_check.main([]) == 0
    assert MPICH_VERSION in capsys.readouterr().out
