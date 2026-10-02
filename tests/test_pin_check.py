from palace_solver import MPICH_VERSION
from wheelbuild import pin_check
from wheelbuild.msmpi import MSMPI_VERSION


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
    out = capsys.readouterr().out
    assert MPICH_VERSION in out
    assert MSMPI_VERSION in out


def test_the_vendored_msmpi_satisfies_the_recorded_series():
    assert pin_check.satisfies(MSMPI_VERSION, pin_check.INTEROP_MSMPI_REQUIREMENT)


def test_the_msmpi_series_is_10_1():
    assert pin_check.satisfies("10.1.0", pin_check.INTEROP_MSMPI_REQUIREMENT)
    assert not pin_check.satisfies("10.0", pin_check.INTEROP_MSMPI_REQUIREMENT)
    assert not pin_check.satisfies("10.2.0", pin_check.INTEROP_MSMPI_REQUIREMENT)


def test_a_vendored_msmpi_outside_the_series_is_reported():
    problem = pin_check.vendored_msmpi_problem("10.2.0", "msmpi==10.1.*")

    assert problem is not None
    assert "10.2.0" in problem
    assert "MSMPI_VERSION" in problem
    assert "INTEROP_MSMPI_REQUIREMENT" in problem


def test_the_command_line_check_fails_on_an_msmpi_out_of_series(monkeypatch, capsys):
    monkeypatch.setattr(pin_check, "MSMPI_VERSION", "10.2.0")

    assert pin_check.main([]) == 1
    assert "MS-MPI 10.2.0" in capsys.readouterr().err
