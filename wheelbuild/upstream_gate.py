"""Judge the upstream test gate, and fingerprint what it tested.

The gate runs Palace's own suite, built from the superbuild, with ctest
(``scripts/upstream-test-gate.sh``). ctest's exit status is not the verdict:
some upstream test cases cannot pass on some platforms, and those are the
**test exclusions** in ``wheelbuild/data/test-exclusions.toml``. Every entry
still runs, excluded ones included, and :func:`judge` reads ctest's JUnit
report. The row fails when:

- a test fails that is not excluded on this platform;
- an excluded test passes, so its exclusion has to go;
- an exclusion names a test case the suite does not register.

Running the excluded cases rather than filtering them out is safe because
ctest gives each entry a process of its own. An MFEM abort in one ends that
entry and nothing else.

Regression is the slow half, and it runs only when no regression run has
passed yet for the binaries under test. :func:`fingerprint` is that identity:
a content hash of what the gate tests. The workflow keeps one empty cache
entry per fingerprint that passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from wheelbuild.platforms import supported_platform_tags

#: The exclusion file, held to account by :func:`judge` on every row it names.
EXCLUSIONS = Path(__file__).resolve().parent / "data" / "test-exclusions.toml"

#: The prefixes Palace's ``catch_discover_tests`` calls give each test case's
#: ctest entries (``palace:test/unit/CMakeLists.txt``). An exclusion names the
#: case, so it covers every one of these the case is registered under.
CTEST_PREFIXES = ("serial-", "mpi-", "regression-", "long-")

#: What the fingerprint covers, relative to the install prefix: the solver,
#: the test binary and every library either loads, then the test data the
#: tests read. The exclusion file is hashed in as well.
FINGERPRINTED = ("bin", "lib", "share/palace")

#: The JUnit ``status`` values ctest writes for an entry that did not run to
#: an outcome: a Catch2 ``SKIP`` (exit 4, ``SKIP_RETURN_CODE``) or a disabled
#: test. Anything else that is not ``run`` is a failure.
NOT_RUN = frozenset({"notrun", "disabled"})


@dataclass(frozen=True)
class Exclusion:
    """One upstream test case the gate does not require to pass."""

    test: str
    platforms: tuple[str, ...]
    reason: str


def load_exclusions(path: Path = EXCLUSIONS) -> tuple[Exclusion, ...]:
    """Read and validate the exclusion file.

    Args:
        path: The TOML file.

    Returns:
        The exclusions, in file order.

    Raises:
        ValueError: If an entry is malformed, names a platform no wheel is
            built for, or repeats a test case.
    """
    data = tomllib.loads(path.read_text())
    unknown = set(data) - {"exclusion"}
    if unknown:
        raise ValueError(f"{path}: unexpected top-level keys {sorted(unknown)}")
    supported = set(supported_platform_tags())
    exclusions: list[Exclusion] = []
    seen: set[str] = set()
    for index, entry in enumerate(data.get("exclusion", [])):
        where = f"{path}: exclusion {index + 1}"
        if set(entry) != {"test", "platforms", "reason"}:
            raise ValueError(f"{where}: needs exactly test, platforms and reason")
        test, platforms, reason = entry["test"], entry["platforms"], entry["reason"]
        if not isinstance(test, str) or not test.strip():
            raise ValueError(f"{where}: test must be a test case name")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{where} ({test}): reason must say why")
        if (
            not isinstance(platforms, list)
            or not platforms
            or not all(isinstance(tag, str) for tag in platforms)
        ):
            raise ValueError(f"{where} ({test}): platforms must list platform tags")
        if len(set(platforms)) != len(platforms):
            raise ValueError(f"{where} ({test}): a platform is listed twice")
        if not set(platforms) <= supported:
            raise ValueError(
                f"{where} ({test}): no wheel is built for "
                f"{sorted(set(platforms) - supported)}"
            )
        if test in seen:
            raise ValueError(f"{where}: {test!r} is excluded twice")
        seen.add(test)
        exclusions.append(Exclusion(test, tuple(platforms), reason))
    return tuple(exclusions)


def excluded_on(platform: str, exclusions: Iterable[Exclusion]) -> frozenset[str]:
    """Return the test cases excluded on ``platform``."""
    return frozenset(e.test for e in exclusions if platform in e.platforms)


def case_of(entry: str) -> str:
    """Return the Catch2 test case a ctest entry runs."""
    for prefix in CTEST_PREFIXES:
        if entry.startswith(prefix):
            return entry[len(prefix) :]
    return entry


def registered_entries(ctest_json: Path) -> list[str]:
    """Return the name of every entry in ``ctest --show-only=json-v1`` output."""
    return [test["name"] for test in json.loads(ctest_json.read_text())["tests"]]


def junit_results(report: Path) -> list[tuple[str, str]]:
    """Return ``(entry, status)`` for every test case in a ctest JUnit report.

    A list, not a mapping: upstream registers more than one entry under the
    same name (two ``serial-PostOperator``), and each must be judged.
    """
    # ctest wrote it on this runner a step earlier; it is not untrusted input.
    root = ET.parse(report).getroot()  # noqa: S314
    return [
        (case.get("name", ""), case.get("status", "fail"))
        for case in root.iter("testcase")
    ]


def judge(
    *,
    platform: str,
    exclusions: Iterable[Exclusion],
    registered: Iterable[str],
    results: Sequence[tuple[str, str]],
) -> list[str]:
    """Return every reason the gate fails this sweep; empty when it passes.

    Args:
        platform: The row's platform tag.
        exclusions: Every exclusion; those not listing ``platform`` are ignored.
        registered: Every ctest entry the suite registers on this row.
        results: ``(entry, status)`` for each entry this sweep ran.
    """
    excluded = excluded_on(platform, exclusions)
    cases = {case_of(entry) for entry in registered}
    problems = [
        f"excluded test case {test!r} is not registered by the suite"
        for test in sorted(excluded - cases)
    ]
    if not results:
        problems.append("the sweep ran no tests")
    for entry, status in results:
        is_excluded = case_of(entry) in excluded
        if status == "run" and is_excluded:
            problems.append(f"{entry!r} passed but is excluded; remove its exclusion")
        elif status != "run" and status not in NOT_RUN and not is_excluded:
            problems.append(f"{entry!r} failed")
    return problems


def fingerprint(prefix: Path, exclusions: Path = EXCLUSIONS) -> str:
    """Return a content hash of what the gate tests.

    It covers every file under :data:`FINGERPRINTED` in ``prefix``, by path and
    content, and the exclusion file. A symlink counts by its target, not by
    what it points to. Modification times are not hashed, so an unchanged
    rebuild keeps the fingerprint.

    Args:
        prefix: The install prefix.
        exclusions: The exclusion file.

    Returns:
        A hex digest.
    """
    digest = hashlib.sha256()

    def add(kind: str, name: str, payload: bytes) -> None:
        for part in (kind.encode(), name.encode(), payload):
            digest.update(len(part).to_bytes(8, "little"))
            digest.update(part)

    for top in FINGERPRINTED:
        root = prefix / top
        if not root.exists():
            raise FileNotFoundError(f"{root} does not exist")
        for path in sorted(_walk(root)):
            name = path.relative_to(prefix).as_posix()
            if path.is_symlink():
                add("link", name, str(path.readlink()).encode())
            else:
                add("file", name, hashlib.sha256(path.read_bytes()).digest())
    add("exclusions", "", exclusions.read_bytes())
    return digest.hexdigest()


def _walk(root: Path) -> list[Path]:
    """Every file and symlink under ``root``, without following symlinks."""
    found = []
    for directory, subdirectories, filenames in os.walk(root):
        here = Path(directory)
        found += [here / name for name in filenames]
        found += [here / name for name in subdirectories if (here / name).is_symlink()]
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``fingerprint`` or ``judge``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    stamp = commands.add_parser("fingerprint", help="print the gate fingerprint")
    stamp.add_argument("prefix", type=Path)
    verdict = commands.add_parser("judge", help="judge one sweep's JUnit report")
    verdict.add_argument("--platform", required=True)
    verdict.add_argument("--tests", type=Path, required=True, help="ctest json-v1")
    verdict.add_argument("--junit", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "fingerprint":
        print(fingerprint(args.prefix))
        return 0

    results = junit_results(args.junit)
    problems = judge(
        platform=args.platform,
        exclusions=load_exclusions(),
        registered=registered_entries(args.tests),
        results=results,
    )
    passed = sum(status == "run" for _entry, status in results)
    print(f"{args.junit.name}: {len(results)} entries, {passed} passed")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
