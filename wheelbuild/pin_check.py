"""Keep the vendored MPICH inside the range the interop test proves.

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

palais itself declares no MPI dependency — the PyPI ``mpich`` wheel is C-only
and is not what this wheel builds or links against — so there is no pin on
palais's side to compare with. The interoperability contract is this
repository's own.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from packaging.requirements import Requirement

from palace_solver import MPICH_VERSION

#: The MPICH range the interop test pairs the wheel with. Bumping the wheel's
#: vendored MPICH past this range must fail here; widening the range is a
#: decision to re-run ``scripts/interop-test.sh`` against the new series.
INTEROP_MPICH_REQUIREMENT = "mpich<5"


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


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the pin check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    problem = vendored_problem(MPICH_VERSION, INTEROP_MPICH_REQUIREMENT)
    if problem is not None:
        print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print(f"vendored MPICH {MPICH_VERSION} satisfies {INTEROP_MPICH_REQUIREMENT!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
