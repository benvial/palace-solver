"""Keep the vendored MPI inside the range the interop test proves.

The wheel vendors its own MPICH, but nothing stops a user launching the
packaged solver with a process manager from elsewhere — most plausibly the
``mpiexec`` from the PyPI ``mpich`` wheel, which is what
``scripts/interop-test.sh`` pairs the solver with when it requires a two-rank
solve under each launcher to agree. That pairing only holds within one MPICH
major series (see
``docs/adr/0004-the-vendored-launcher-is-the-supported-one.md``), and nothing
else ties the vendored release to it: a bump of
``palace_solver.MPICH_VERSION`` past the series would pass unnoticed until a
foreign launch misbehaved.

:func:`vendored_problem` compares ``palace_solver.MPICH_VERSION`` against
:data:`INTEROP_MPICH_REQUIREMENT`, the range recorded here as the one the
interop test runs against. It needs nothing but this repository, so CI runs it
on every push.

There is no pin anywhere else to compare with: the PyPI ``mpich`` wheel is
C-only and is not what this wheel builds or links against, and no consumer of
this package declares an MPI dependency on its behalf. The interoperability
contract is this repository's own.

Windows has the same contract with a different MPI. The wheel vendors MS-MPI
(``wheelbuild.msmpi.MSMPI_VERSION``), and the foreign launcher a user is most
likely to have is the ``mpiexec`` of an MS-MPI they installed themselves, which
the Windows interop test pairs the solver with inside one release series.
:data:`INTEROP_MSMPI_REQUIREMENT` records that series beside the MPICH range,
and the same command checks both.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from packaging.requirements import Requirement

from palace_solver import MPICH_VERSION
from wheelbuild.msmpi import MSMPI_VERSION

#: The MPICH range the interop test pairs the wheel with. Bumping the wheel's
#: vendored MPICH past this range must fail here; widening the range is a
#: decision to re-run ``scripts/interop-test.sh`` against the new series.
INTEROP_MPICH_REQUIREMENT = "mpich<5"

#: The MS-MPI series the Windows interop test pairs the wheel with: a
#: user-installed MS-MPI 10.1.x. Its smpd-to-PMI protocol carries no
#: compatibility promise, which is why the pairing is measured rather than
#: assumed, and why bumping the vendored MS-MPI out of the series must fail
#: here, as for MPICH.
INTEROP_MSMPI_REQUIREMENT = "msmpi==10.1.*"


def satisfies(version: str, requirement: str) -> bool:
    """Whether ``version`` falls inside a requirement's version range.

    Args:
        version: An MPICH release, such as ``4.3.2``.
        requirement: A PEP 508 requirement string, such as ``mpich<5``.

    Returns:
        ``True`` if the release satisfies the requirement.
    """
    return Requirement(requirement).specifier.contains(version)


def vendored_problem(vendored_version: str, expected: str) -> str | None:
    """Report a vendored MPICH that has left the range recorded here.

    Args:
        vendored_version: MPICH release the wheel builds and ships.
        expected: The range the interop test proves the wheel against.

    Returns:
        A message, or ``None`` when the release is inside the range.
    """
    if satisfies(vendored_version, expected):
        return None
    return (
        f"the vendored MPICH {vendored_version} does not satisfy {expected!r}: "
        "a rank launched by an mpiexec from that range — the pairing "
        "scripts/interop-test.sh proves — would be talking to a different "
        "MPICH major series. Bump palace_solver.MPICH_VERSION back into "
        "range, or widen INTEROP_MPICH_REQUIREMENT and re-run the interop "
        "test against the new series."
    )


def vendored_msmpi_problem(vendored_version: str, expected: str) -> str | None:
    """Report a vendored MS-MPI that has left the series recorded here.

    Args:
        vendored_version: MS-MPI release the Windows wheel ships, such as
            ``10.1.3``.
        expected: The series the Windows interop test proves the wheel against.

    Returns:
        A message, or ``None`` when the release is inside the series.
    """
    if satisfies(vendored_version, expected):
        return None
    return (
        f"the vendored MS-MPI {vendored_version} does not satisfy {expected!r}: "
        "a rank launched by the mpiexec of a user-installed MS-MPI from that "
        "series -- the pairing the Windows interop test proves -- would be "
        "talking to a different MS-MPI release series. Move "
        "wheelbuild.msmpi.MSMPI_VERSION back into range, or widen "
        "INTEROP_MSMPI_REQUIREMENT and re-run the Windows interop test against "
        "the new series."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the pin check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    problems = [
        problem
        for problem in (
            vendored_problem(MPICH_VERSION, INTEROP_MPICH_REQUIREMENT),
            vendored_msmpi_problem(MSMPI_VERSION, INTEROP_MSMPI_REQUIREMENT),
        )
        if problem is not None
    ]
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"vendored MPICH {MPICH_VERSION} satisfies {INTEROP_MPICH_REQUIREMENT!r}")
    print(f"vendored MS-MPI {MSMPI_VERSION} satisfies {INTEROP_MSMPI_REQUIREMENT!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
