"""Keep the vendored MPICH and palais's ``mpich`` pin from drifting apart.

The wheel vendors its own MPICH while palais depends on the PyPI ``mpich``
wheel for mpi4py, and the two only interoperate within one MPICH major series;
see ``docs/adr/0004-the-vendored-launcher-is-the-supported-one.md``. Nothing
otherwise links them, so a version bump on either side would pass unnoticed.

Two checks, and they are not equally strong:

- :func:`vendored_problem` compares ``palais_solver.MPICH_VERSION`` against
  :data:`PALAIS_MPICH_REQUIREMENT`, the ``mpich`` pin recorded here as the
  one palais declares. It needs nothing but this repository, so CI runs it on
  every push — but it only catches drift on *this* side.
- :func:`declared_problem` compares that recorded pin against what a palais
  source checkout actually declares, and is what catches drift on palais's
  side. It needs a checkout passed with ``--palais``, so it does not run in
  CI here.

The second is deliberately not resolved from PyPI. palais is not published
there yet, and the name this project used to carry, ``pypalace``, now belongs
to a different Palace frontend, so resolving either name would compare this
wheel against another project's dependencies. Until palais is reachable from
this repository's CI, keeping the two pins together is a manual step at release
time.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

from packaging.requirements import Requirement

from palais_solver import MPICH_VERSION

#: The ``mpich`` requirement palais declares for mpi4py. Bumping the wheel's
#: vendored MPICH past this range, or palais moving its pin, must fail here.
PALAIS_MPICH_REQUIREMENT = "mpich<5"

#: Distribution name of the dependency the pin belongs to.
MPICH_DISTRIBUTION = "mpich"


def satisfies(version: str, requirement: str) -> bool:
    """Whether ``version`` falls inside a requirement's version range.

    Args:
        version: An MPICH release, such as ``4.3.2``.
        requirement: A PEP 508 requirement string, such as ``mpich<5``.

    Returns:
        ``True`` if the release satisfies the requirement.
    """
    return Requirement(requirement).specifier.contains(version)


def mpich_requirement(dependencies: Iterable[str]) -> str | None:
    """Pick the ``mpich`` requirement out of a dependency list.

    Args:
        dependencies: PEP 508 requirement strings.

    Returns:
        The ``mpich`` requirement, or ``None`` if the list declares none.
    """
    for dependency in dependencies:
        if Requirement(dependency).name == MPICH_DISTRIBUTION:
            return dependency
    return None


def checkout_dependencies(palais_dir: Path) -> list[str]:
    """Read the runtime dependencies a palais source checkout declares.

    Args:
        palais_dir: Root of a palais checkout, or its ``pyproject.toml``.

    Returns:
        The ``[project] dependencies`` list.

    Raises:
        FileNotFoundError: If there is no ``pyproject.toml`` to read.
    """
    pyproject = (
        palais_dir
        if palais_dir.name == "pyproject.toml"
        else palais_dir / "pyproject.toml"
    )
    if not pyproject.is_file():
        raise FileNotFoundError(f"no pyproject.toml at {pyproject}")
    with pyproject.open("rb") as handle:
        table = tomllib.load(handle)
    dependencies = table.get("project", {}).get("dependencies", [])
    return [str(dependency) for dependency in dependencies]


def vendored_problem(vendored_version: str, expected: str) -> str | None:
    """Report a vendored MPICH that has left the pin recorded here.

    Args:
        vendored_version: MPICH release the wheel builds and ships.
        expected: The ``mpich`` pin this repository records for palais.

    Returns:
        A message, or ``None`` when the release is inside the range.
    """
    if satisfies(vendored_version, expected):
        return None
    return (
        f"the vendored MPICH {vendored_version} does not satisfy {expected!r}: a "
        "rank launched by the mpiexec from the mpich wheel palais installs "
        "would be talking to a different MPICH major series. Bump "
        "palais_solver.MPICH_VERSION back into range, or move both pins "
        "together."
    )


def declared_problem(expected: str, declared: str | None) -> str | None:
    """Report a palais whose ``mpich`` pin no longer matches the recorded one.

    Args:
        expected: The ``mpich`` pin this repository records for palais.
        declared: The ``mpich`` requirement palais actually declares, or
            ``None`` if it declares none at all.

    Returns:
        A message, or ``None`` when the two agree.
    """
    if declared is None:
        return (
            f"palais declares no mpich requirement, but this repository "
            f"records {expected!r}. Either palais dropped the dependency, in "
            "which case PALAIS_MPICH_REQUIREMENT is stale, or the wrong "
            "project was inspected."
        )
    if Requirement(declared).specifier != Requirement(expected).specifier:
        return (
            f"palais now declares {declared!r} but this repository records "
            f"{expected!r}. Update PALAIS_MPICH_REQUIREMENT and check that "
            "palais_solver.MPICH_VERSION still satisfies the new pin."
        )
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for the pin check.

    Without ``--palais`` only the vendored release is checked, against the
    pin recorded here. That half needs no palais and is what CI runs; it
    cannot see palais moving its own pin, and says so.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--palais",
        type=Path,
        help="root of a palais checkout to compare the recorded pin against",
    )
    args = parser.parse_args(argv)

    found = [vendored_problem(MPICH_VERSION, PALAIS_MPICH_REQUIREMENT)]
    if args.palais is None:
        verdict = (
            "NOT compared against palais: pass --palais <checkout> to check "
            f"that it still declares {PALAIS_MPICH_REQUIREMENT!r}"
        )
    else:
        declared = mpich_requirement(checkout_dependencies(args.palais))
        found.append(declared_problem(PALAIS_MPICH_REQUIREMENT, declared))
        verdict = f"palais at {args.palais} declares {declared!r}"

    reported = [problem for problem in found if problem is not None]
    if reported:
        for problem in reported:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print(
        f"vendored MPICH {MPICH_VERSION} satisfies "
        f"{PALAIS_MPICH_REQUIREMENT!r}; {verdict}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
