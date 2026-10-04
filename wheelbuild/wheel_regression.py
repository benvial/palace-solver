"""Run Palace's regression cases through an installed wheel, and judge them.

The **wheel regression run** asks the upstream test gate's regression cases of
what ships: the ``palace`` console script of an installed wheel, its launcher
guard, ``palace-mpiexec`` and the vendored MPI, on a machine with no build tree.
Its question is whether packaging changed Palace's answers. Whether the build's
numerics are right is the gate's question, and the gate already answers it.

Upstream runs these cases in-process, in ``palace-unit-tests``, and diffs the
output against reference CSVs in ``test/unit/regression_helpers.cpp``. That
comparator is not reachable from outside the test binary, so this module ports
it. Each case's tolerances and options are parsed out of
``test/unit/regression/cases.cpp`` at the pinned Palace, and the parser is
strict: a statement, option or custom-check factory it does not know raises,
so a Palace bump that changes the harness stops the run instead of drifting
past it.

Upstream registers seven custom checks (far-field magnitudes, complex
magnitudes, Floquet dB, wave-port losslessness, ROM eigenvalues and both ROM
round-trips). They are left out: the files they own get the structure checks
only. They encode numerical semantics the gate owns, and a packaging fault
garbles every file of a case, not one. Revisit only if a packaging bug ever
shows up that only such a file would catch.

The verdict reuses the gate's **test exclusions** and their accountability
rules (:func:`wheelbuild.upstream_gate.judge`), with the exclusions scoped to
the wheel run. The cases in :data:`SKIPPED` are not exclusions: nothing is
wrong with them, they are left to the gate for cost.

What the run is due on is the wheel's payload and the exclusions:
:func:`fingerprint`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import math
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from wheelbuild import upstream_gate

#: The cases the wheel regression run leaves to the gate, and why. They are
#: the six heaviest at two ranks; lighter cases still cover the adaptive,
#: synthesis and wave-port paths (palace-tests ticket 09).
SKIPPED = (
    "iris_filter_wave_eigen",
    "iris_filter_driven_wave_synth",
    "cpw_lumped_uniform",
    "cpw_wave_uniform",
    "cpw_lumped_adaptive",
    "cpw_wave_adaptive",
)
SKIP_REASON = "cost; the upstream test gate runs them"

#: The custom-check factories ``cases.cpp`` may register. A file one of them
#: owns is checked for structure only (see the module docstring).
CUSTOM_CHECKS = frozenset(
    {
        "TestFarfield",
        "CompareComplexMagnitudes",
        "TestFloquetSParams",
        "TestWavePortLossless",
        "CompareRomEigenvalues",
        "TestWavePortCoupledRoundTrip",
        "TestWavePortSRoundTrip",
    }
)
CUSTOM_CHECK_REASON = (
    "custom checks encode numerical semantics the upstream test gate owns, and "
    "a packaging fault garbles every file of a case, not one"
)

#: Where the run's inputs sit in the Palace release tarball, below its top
#: directory, and so below the directory :func:`extract_inputs` writes.
REGRESSION_DATA = "test/data/regression"
CASES_SOURCE = "test/unit/regression/cases.cpp"

#: How many ranks each case runs on: the README's ``palace -np 2``.
RANKS = 2
#: Per-case ceiling, in seconds.
CASE_TIMEOUT = 20 * 60

#: The solver choice upstream's harness injects into every case. Its
#: ``--palace-linear-solver`` and ``--palace-eigensolver`` default to
#: ``"Default"`` (``test/unit/main.cpp``), and a case's override policy only
#: chooses between that global and a forced ``"Default"``, so every case runs
#: with it.
SOLVER_TYPE = "Default"

#: Environment variables a case must not inherit: library search paths that
#: would hide a library the wheel failed to carry, the interpreter's own
#: search paths, and anything an MPI launcher or rank reads.
LEAKY_VARIABLES = frozenset(
    {"LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"}
    | {"PYTHONPATH", "PYTHONHOME"}
)
LEAKY_PREFIXES = (
    "PMI_",
    "PMIX_",
    "OMPI_",
    "HYDRA_",
    "MPIEXEC_",
    "MPICH_",
    "I_MPI_",
    "MSMPI_",
)


# --------------------------------------------------------------------------
# cases.cpp
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """One case's ``palace::test::RegressionOptions``, as far as they matter.

    The defaults are upstream's. The solver policies are not kept, since both
    resolve to :data:`SOLVER_TYPE`. ``custom_checks`` names the files a custom
    check owns, which are checked for structure only.
    """

    rtol: float = 1e-6
    atol: float = 1e-18
    excluded_columns: tuple[str, ...] = ()
    abs_columns: tuple[str, ...] = ()
    excluded_files: tuple[str, ...] = ()
    unstored_files: tuple[str, ...] = ()
    skip_rowcount: bool = False
    max_rows: int | None = None
    min_rows: int | None = None
    paraview_fields: bool = True
    gridfunction_fields: bool = False
    custom_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    """One ``[Regression]`` test case of ``cases.cpp``."""

    name: str
    directory: str
    config: str
    postpro: str
    options: Options = field(default_factory=Options)


_FLOAT_OPTIONS = frozenset({"rtol", "atol"})
_LIST_OPTIONS = frozenset(
    {"excluded_columns", "abs_columns", "excluded_files", "unstored_files"}
)
_BOOL_OPTIONS = frozenset({"skip_rowcount", "paraview_fields", "gridfunction_fields"})
_COUNT_OPTIONS = frozenset({"max_rows", "min_rows"})
_POLICY_OPTIONS = frozenset({"linear_solver_policy", "eigen_solver_policy"})
_POLICIES = frozenset(
    {
        "palace::test::SolverOverridePolicy::ForceDefault",
        "palace::test::SolverOverridePolicy::UseGlobalOverride",
    }
)

_STRING = r'"(?:[^"\\\n]|\\.)*"'
_IDENTIFIER = r"[A-Za-z_]\w*"
_FLOAT = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_INFINITY = "std::numeric_limits<double>::infinity()"


class CasesError(ValueError):
    """``cases.cpp`` holds something the parser does not know."""


def strip_cpp_comments(text: str) -> str:
    """Return C++ ``text`` with its comments blanked out.

    String and character literals are kept whole, so a ``//`` inside one is
    not a comment. Each comment becomes one space, which keeps tokens apart.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char in "\"'":
            end = i + 1
            while end < len(text) and text[end] != char:
                end += 2 if text[end] == "\\" else 1
            out.append(text[i : end + 1])
            i = end + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            out.append(" ")
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise CasesError("unterminated /* comment")
            i = end + 2
            out.append(" ")
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _string(literal: str) -> str:
    """Decode one C++ string literal; only the escapes ``cases.cpp`` uses."""
    if not re.fullmatch(_STRING, literal):
        raise CasesError(f"not a string literal: {literal}")
    body = literal[1:-1]

    def escape(match: re.Match[str]) -> str:
        sequence = match.group(0)
        if sequence[1] == "u":
            return chr(int(sequence.removeprefix("\\u"), 16))
        if sequence[1] in "\"\\'":
            return sequence[1]
        raise CasesError(f"unknown escape {sequence} in {literal}")

    return re.sub(r"\\u[0-9A-Fa-f]{4}|\\.", escape, body)


def _strings(value: str, constants: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    """A ``{"a", "b"}`` initialiser list, or a named list constant."""
    value = value.strip()
    if value in constants:
        return constants[value]
    match = re.fullmatch(r"\{(.*)\}", value, re.DOTALL)
    if not match:
        raise CasesError(f"not a list of strings: {value}")
    items = [item.strip() for item in _split(match.group(1), ",")]
    if items and not items[-1]:
        items.pop()
    return tuple(_string(item) for item in items)


def _split(text: str, separator: str) -> list[str]:
    """Split ``text`` on ``separator`` outside strings and brackets."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        char = text[i]
        if char == '"':
            end = i + 1
            while end < len(text) and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            i = end + 1
            continue
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _constants(source: str) -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    """The named string lists and solver policies ``cases.cpp`` declares."""
    lists = {
        name: _strings(value, {})
        for name, value in re.findall(
            rf"const\s+std::vector<std::string>\s+({_IDENTIFIER})\s*=\s*(\{{.*?\}})\s*;",
            source,
            re.DOTALL,
        )
    }
    policies = frozenset(
        name
        for name, value in re.findall(
            rf"constexpr\s+auto\s+({_IDENTIFIER})\s*=\s*([\w:]+)\s*;", source
        )
        if value in _POLICIES
    )
    return lists, policies


def _body(source: str, start: int) -> tuple[str, int]:
    """The brace-delimited block opening at or after ``start``, and its end."""
    opening = source.index("{", start)
    depth = 0
    i = opening
    while i < len(source):
        char = source[i]
        if char == '"':
            end = i + 1
            while source[end] != '"':
                end += 2 if source[end] == "\\" else 1
            i = end
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : i], i + 1
        i += 1
    raise CasesError("unbalanced braces")


def parse_cases(text: str) -> tuple[Case, ...]:
    """Return every ``[Regression]`` case of ``cases.cpp`` not tagged ``[Long]``.

    Args:
        text: The source of ``test/unit/regression/cases.cpp``.

    Returns:
        The cases, in file order.

    Raises:
        CasesError: If a case holds a statement, option, value or custom-check
            factory the parser does not know, or none is found.
    """
    source = strip_cpp_comments(text)
    lists, policies = _constants(source)
    cases: list[Case] = []
    header = re.compile(rf"TEST_CASE\s*\(\s*({_STRING})\s*,\s*({_STRING})\s*\)")
    position = 0
    while match := header.search(source, position):
        body, position = _body(source, match.end())
        name, tags = _string(match.group(1)), _string(match.group(2))
        if "[Regression]" not in tags or "[Long]" in tags:
            continue
        try:
            cases.append(_parse_case(name, body, lists, policies))
        except CasesError as error:
            raise CasesError(f"case {name!r}: {error}") from None
    if not cases:
        raise CasesError("no [Regression] case found")
    names = [case.name for case in cases]
    if len(set(names)) != len(names):
        raise CasesError("a case name is registered twice")
    return tuple(cases)


_RUN = re.compile(
    rf"palace::test::RunRegressionCase\( ?({_STRING}) ?, ?({_STRING}) ?, "
    rf"?({_STRING}) ?, ?opts ?\)"
)
_CHECK = re.compile(
    rf"opts\.custom_checks ?\[ ?({_STRING}) ?\] ?= ?({_IDENTIFIER}) ?\(.*\)"
)
_ASSIGNMENT = re.compile(rf"opts\.({_IDENTIFIER}) ?= ?(.+)")


def _parse_case(
    name: str,
    body: str,
    lists: Mapping[str, tuple[str, ...]],
    policies: frozenset[str],
) -> Case:
    values: dict[str, Any] = {}
    checks: list[str] = []
    runs: list[tuple[str, str, str]] = []
    for statement in _statements(body):
        if runs:
            raise CasesError(f"statement after RunRegressionCase: {statement!r}")
        if call := _RUN.fullmatch(statement):
            runs.append((_string(call[1]), _string(call[2]), _string(call[3])))
        elif check := _CHECK.fullmatch(statement):
            if check[2] not in CUSTOM_CHECKS:
                raise CasesError(f"unknown custom-check factory {check[2]!r}")
            checks.append(_string(check[1]))
        elif option := _ASSIGNMENT.fullmatch(statement):
            if option[1] in values:
                raise CasesError(f"opts.{option[1]} is set twice")
            values[option[1]] = _option(option[1], option[2], lists, policies)
        else:
            raise CasesError(f"unknown statement {statement!r}")
    if not runs:
        raise CasesError("never calls RunRegressionCase")
    values = {key: value for key, value in values.items() if key not in _POLICY_OPTIONS}
    return Case(name, *runs[0], options=Options(**values, custom_checks=tuple(checks)))


def _statements(body: str) -> list[str]:
    """A case body's statements after ``opts`` is declared, whitespace folded."""
    statements = [" ".join(statement.split()) for statement in _split(body, ";")]
    if statements[-1]:
        raise CasesError(f"trailing text {statements[-1]!r}")
    if statements[0] != "palace::test::RegressionOptions opts":
        raise CasesError("does not start by declaring opts")
    return statements[1:-1]


def _option(
    key: str,
    value: str,
    lists: Mapping[str, tuple[str, ...]],
    policies: frozenset[str],
) -> object:
    if key in _LIST_OPTIONS:
        return _strings(value, lists)
    readers: dict[str, Callable[[str], object]] = {
        **dict.fromkeys(_FLOAT_OPTIONS, _real),
        **dict.fromkeys(_BOOL_OPTIONS, {"true": True, "false": False}.get),
        **dict.fromkeys(_COUNT_OPTIONS, _count),
        **dict.fromkeys(
            _POLICY_OPTIONS, lambda text: text if text in policies | _POLICIES else None
        ),
    }
    if key not in readers:
        raise CasesError(f"unknown option opts.{key}")
    parsed = readers[key](value)
    if parsed is None:
        raise CasesError(f"opts.{key} = {value!r} is not a form the parser knows")
    return parsed


def _count(text: str) -> int | None:
    return int(text) if text.isdigit() else None


def _real(text: str) -> float | None:
    if text == _INFINITY:
        return math.inf
    return float(text) if _FLOAT.fullmatch(text) else None


def select_cases(cases: Iterable[Case]) -> tuple[Case, ...]:
    """Drop the cases in :data:`SKIPPED`, each of which must be registered.

    Raises:
        CasesError: If a skipped name is not a case, so a renamed heavy case
            cannot rejoin the run unnoticed.
    """
    cases = tuple(cases)
    missing = set(SKIPPED) - {case.name for case in cases}
    if missing:
        raise CasesError(f"skipped cases not in cases.cpp: {sorted(missing)}")
    return tuple(case for case in cases if case.name not in SKIPPED)


# --------------------------------------------------------------------------
# Palace's configuration files
# --------------------------------------------------------------------------

_JSON_STRING = re.compile(r"""(["'])(?:\\.|(?!\1).)*?\1""")


def preprocess_config(text: str) -> str:
    """Port of Palace's ``PreprocessFile`` (``palace/utils/iodata.cpp``).

    Palace's configuration files are JSON with comments, trailing commas and
    integer ranges (``[1-4, 7]``). This strips the comments, the whitespace
    and the trailing commas outside strings, then expands the ranges, which
    Palace does without regard to strings.
    """
    pieces: list[str] = []
    i = 0
    while i < len(text):
        if string := _JSON_STRING.match(text, i):
            pieces.append(string.group(0))
            i = string.end()
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
        elif text[i].isspace():
            i += 1
        else:
            pieces.append(text[i])
            i += 1
    stripped = "".join(pieces)
    stripped = re.sub(
        r"""((["'])(?:\\.|(?!\2).)*?\2)|,+(?=[}\]])""",
        lambda match: match.group(1) or "",
        stripped,
    )
    return _expand_ranges(stripped)


def _expand_ranges(text: str) -> str:
    out: list[str] = []
    start, inside = 0, False
    for i, char in enumerate(text):
        if inside:
            if char == "]":
                items = text[start + 1 : i].split(",")
                out.append("[" + ",".join(_expand_range(item) for item in items) + "]")
                start, inside = i + 1, False
            elif char == "[":
                out.append(text[start:i])
                start = i
            elif char not in "-0123456789,":
                out.append(text[start:i])
                start, inside = i, False
        elif char == "[":
            out.append(text[start:i])
            start, inside = i, True
    out.append(text[start:])
    return "".join(out)


_INTEGER = re.compile(r"-?[0-9]+")


def _expand_range(item: str) -> str:
    if not item:
        return ""
    first = _INTEGER.match(item)
    if not first:
        raise ValueError(f"invalid integer {item!r} in range expansion")
    if first.end() == len(item):
        return item
    second = _INTEGER.match(item, first.end() + 1)
    if not second:
        raise ValueError(f"invalid integer {item!r} in range expansion")
    low, high = int(first.group(0)), int(second.group(0))
    return ",".join(str(n) for n in range(low, max(low, high) + 1))


def load_config(path: Path) -> dict[str, Any]:
    """Read a Palace configuration file the way Palace does.

    Raises:
        ValueError: If it is not valid after preprocessing, or repeats a key
            in one object, which Palace refuses as well.
    """

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _value in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{path}: a key is repeated in one object")
        return dict(pairs)

    text = preprocess_config(path.read_text(encoding="utf-8"))
    config = json.loads(text, object_pairs_hook=unique)
    if not isinstance(config, dict):
        raise TypeError(f"{path}: not a JSON object")
    return config


def inject_solvers(config: dict[str, Any]) -> dict[str, Any]:
    """Set the solver types as upstream's harness does (``LoadCaseIoData``)."""
    solver = config.setdefault("Solver", {})
    solver.setdefault("Linear", {})["Type"] = SOLVER_TYPE
    if "Eigenmode" in solver:
        solver["Eigenmode"]["Type"] = SOLVER_TYPE
    return config


def refinement_iterations(config: Mapping[str, Any]) -> int:
    """``Model.Refinement.MaxIts``, or 0."""
    refinement = config.get("Model", {}).get("Refinement", {})
    return int(refinement.get("MaxIts", 0)) if isinstance(refinement, dict) else 0


def requested_modes(config: Mapping[str, Any]) -> int | None:
    """``Solver.Eigenmode.N``, which caps the rows an eigenmode case compares."""
    eigenmode = config.get("Solver", {}).get("Eigenmode")
    if isinstance(eigenmode, dict) and "N" in eigenmode:
        return int(eigenmode["N"])
    return None


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    """One CSV column: its header text and the cells that hold a number."""

    header: str
    data: tuple[float, ...]


def read_table(path: Path) -> tuple[Column, ...]:
    """Read a Palace CSV file as ``palace::Table``'s string constructor does.

    The first row is the header. Cells are trimmed of blanks, and an empty or
    ``NULL`` cell is skipped, so a column holds only the numbers it has.

    Raises:
        ValueError: If a cell is not a number or a row has more cells than the
            header.
    """
    text = path.read_text(encoding="utf-8")
    rows = _fields(text, "\n")
    if not rows:
        return ()
    headers = [cell.strip(" \t\f\v") for cell in _fields(rows[0], ",")]
    data: list[list[float]] = [[] for _header in headers]
    for number, row in enumerate(rows[1:], start=2):
        for index, cell in enumerate(_fields(row, ",")):
            value = cell.strip(" \t\f\v")
            if value in {"", "NULL"}:
                continue
            if index >= len(data):
                raise ValueError(f"{path}: row {number} has more cells than the header")
            try:
                data[index].append(float(value))
            except ValueError:
                message = f"{path}: row {number}: {value!r} is not a number"
                raise ValueError(message) from None
    return tuple(Column(h, tuple(d)) for h, d in zip(headers, data, strict=True))


def _fields(text: str, separator: str) -> list[str]:
    """Split as Palace's ``sv_split_r`` does: no empty field after a final one."""
    if not text:
        return []
    fields = text.split(separator)
    if text.endswith(separator):
        fields.pop()
    return fields


def row_count(table: Sequence[Column]) -> int:
    """``palace::Table::n_rows``: the longest column's length."""
    return max((len(column.data) for column in table), default=0)


@dataclass(frozen=True)
class Cell:
    """A compared cell, and how much of its tolerance it used."""

    file: str
    column: str
    row: int
    value: float
    reference: float
    tolerance: float
    used: float

    def describe(self) -> str:
        """One line for a report."""
        return (
            f"{self.file} '{self.column}' row {self.row}: {self.value:.12g} vs "
            f"{self.reference:.12g} (tolerance {self.tolerance:.3g}, "
            f"{self.used:.3g} of it)"
        )


@dataclass
class Comparison:
    """What comparing one case's output with its reference found."""

    problems: list[str] = field(default_factory=list)
    worst: Cell | None = None

    def cell(self, cell: Cell) -> None:
        """Record a compared cell; it fails when it used more than all of it."""
        if self.worst is None or cell.used > self.worst.used:
            self.worst = cell
        if not cell.used <= 1.0:
            self.problems.append(f"{cell.describe()} is out of tolerance")


def _matches(header: str, patterns: Iterable[str]) -> bool:
    return any(pattern and pattern in header for pattern in patterns)


def _margin(actual: float, target: float, margin: float) -> bool:
    """Catch2's ``marginComparison``."""
    return actual + margin >= target and target + margin >= actual


def within(actual: float, reference: float, rtol: float, atol: float) -> bool:
    """``WithinRel(reference, rtol) || WithinAbs(reference, atol)``, as Catch2."""
    relative = rtol * max(abs(actual), abs(reference))
    relative = 0.0 if math.isinf(relative) else relative
    return _margin(actual, reference, relative) or _margin(actual, reference, atol)


def _used(actual: float, reference: float, rtol: float, atol: float) -> float:
    """How much of its tolerance a cell used: above 1 exactly when it fails."""
    passed = within(actual, reference, rtol, atol)
    difference = abs(actual - reference)
    allowed = max(atol, rtol * max(abs(actual), abs(reference)))
    if not math.isfinite(difference) or not allowed > 0:
        return 0.0 if passed else math.inf
    used = difference / allowed
    return min(used, 1.0) if passed else max(used, math.nextafter(1.0, math.inf))


def validate_tables(
    name: str,
    actual: Sequence[Column],
    reference: Sequence[Column],
    options: Options,
    comparison: Comparison,
) -> bool:
    """Port of ``ValidateCSVTables``: shape and headers.

    Returns:
        Whether the column counts agree, so values can be compared.
    """
    if len(actual) != len(reference):
        comparison.problems.append(
            f"{name}: {len(actual)} columns, the reference has {len(reference)}"
        )
    rows, reference_rows = row_count(actual), row_count(reference)
    if not options.skip_rowcount:
        if rows != reference_rows:
            comparison.problems.append(
                f"{name}: {rows} rows, the reference has {reference_rows}"
            )
    else:
        if (rows > 0) != (reference_rows > 0):
            comparison.problems.append(
                f"{name}: {rows} rows, the reference has {reference_rows}"
            )
        if options.min_rows is not None:
            floor = min(options.min_rows, reference_rows)
            if rows < floor:
                comparison.problems.append(f"{name}: {rows} rows, fewer than {floor}")
    for index, (column, expected) in enumerate(zip(actual, reference, strict=False)):
        if _matches(expected.header, options.excluded_columns):
            continue
        if column.header != expected.header:
            comparison.problems.append(
                f"{name}: column {index} is {column.header!r}, "
                f"the reference has {expected.header!r}"
            )
    return len(actual) == len(reference)


def compare_tables(
    name: str,
    actual: Sequence[Column],
    reference: Sequence[Column],
    options: Options,
    comparison: Comparison,
) -> None:
    """Port of ``CompareCSVFiles``: shape, headers, then every cell."""
    if not validate_tables(name, actual, reference, options, comparison):
        return
    if math.isinf(options.rtol) and math.isinf(options.atol):
        return
    rows = min(row_count(actual), row_count(reference))
    if options.max_rows is not None:
        rows = min(rows, options.max_rows)
    for column, expected in zip(actual, reference, strict=True):
        if not _matches(expected.header, options.excluded_columns):
            _compare_column(
                name,
                column,
                expected,
                rows=rows,
                options=options,
                comparison=comparison,
            )


def _compare_column(
    name: str,
    column: Column,
    expected: Column,
    *,
    rows: int,
    options: Options,
    comparison: Comparison,
) -> None:
    by_magnitude = _matches(expected.header, options.abs_columns)
    for row in range(rows):
        if row >= len(column.data) or row >= len(expected.data):
            if (row < len(column.data)) != (row < len(expected.data)):
                comparison.problems.append(
                    f"{name} '{expected.header}' row {row + 1}: a cell is empty"
                )
            continue
        value, target = column.data[row], expected.data[row]
        if by_magnitude:
            value, target = abs(value), abs(target)
        if math.isnan(value) and math.isnan(target):
            continue
        comparison.cell(
            Cell(
                file=name,
                column=expected.header,
                row=row + 1,
                value=value,
                reference=target,
                tolerance=max(
                    options.atol, options.rtol * max(abs(value), abs(target))
                ),
                used=_used(value, target, options.rtol, options.atol),
            )
        )


#: Volumetric field output: listed, never descended into.
FIELD_DIRECTORIES = frozenset({"paraview", "gridfunction"})


@dataclass(frozen=True)
class Listing:
    """A postpro tree: its directories, CSV files and other files."""

    directories: frozenset[str]
    csv_files: frozenset[str]
    other_files: frozenset[str]


def list_postpro(root: Path) -> Listing:
    """Port of ``ListRegressionFiles``, not descending into field output."""
    directories: set[str] = set()
    csv_files: set[str] = set()
    other_files: set[str] = set()
    if root.is_dir():
        for directory, subdirectories, filenames in os.walk(root):
            here = Path(directory)
            for name in subdirectories:
                directories.add((here / name).relative_to(root).as_posix())
            subdirectories[:] = [
                name for name in subdirectories if name not in FIELD_DIRECTORIES
            ]
            for name in filenames:
                relative = (here / name).relative_to(root).as_posix()
                (csv_files if name.endswith(".csv") else other_files).add(relative)
    return Listing(frozenset(directories), frozenset(csv_files), frozenset(other_files))


def expected_directories(iterations: int, options: Options) -> frozenset[str]:
    """Port of ``ExpectedDirectories``."""
    expected = {"gridfunction"} if options.gridfunction_fields else set()
    for i in range(1, iterations + 1):
        expected.add(f"iteration{i}")
        if options.gridfunction_fields:
            expected.add(f"iteration{i}/gridfunction")
        if options.paraview_fields:
            expected.add(f"iteration{i}/paraview")
    if options.paraview_fields:
        expected.add("paraview")
    return frozenset(expected)


def expected_metadata(iterations: int, config: str) -> frozenset[str]:
    """Port of ``ExpectedMetadataFiles``, plus the command line's resolved config.

    ``palace``'s ``main`` writes ``<config stem>_resolved.json`` into the
    output directory. Upstream's harness calls ``palace::Run`` and never does,
    so its list lacks the file; a run through the command line must have it.
    """
    return frozenset(
        {"palace.json", f"{PurePosixPath(config).stem}_resolved.json"}
        | {f"iteration{i}/palace.json" for i in range(1, iterations + 1)}
    )


def compare_case(
    options: Options, *, output: Path, reference: Path, iterations: int, config: str
) -> Comparison:
    """Compare one case's postpro tree with its reference tree.

    This is ``RunRegressionCase``'s check, with every custom-checked file held
    to the structure checks only.

    Args:
        options: The case's options, ``max_rows`` already resolved.
        output: The live ``postpro/<subdir>`` directory.
        reference: The reference directory.
        iterations: The case's ``Model.Refinement.MaxIts``.
        config: The configuration file's name.
    """
    comparison = Comparison()
    if not reference.is_dir():
        comparison.problems.append(f"no reference directory {reference}")
        return comparison
    got, want = list_postpro(output), list_postpro(reference)
    if not output.is_dir():
        comparison.problems.append(f"no output directory {output}")
    live_csv = {
        name for name in got.csv_files if not _matches(name, options.unstored_files)
    }
    for label, present, required in (
        ("directories", got.directories, expected_directories(iterations, options)),
        ("CSV files", live_csv, want.csv_files),
        ("metadata files", got.other_files, expected_metadata(iterations, config)),
    ):
        if missing := sorted(set(required) - set(present)):
            comparison.problems.append(f"missing {label}: {', '.join(missing)}")
        if extra := sorted(set(present) - set(required)):
            comparison.problems.append(f"unexpected {label}: {', '.join(extra)}")
    for name in sorted(want.csv_files):
        if not (output / name).is_file():
            continue
        if _matches(name, options.excluded_files):
            continue
        actual, expected = read_table(output / name), read_table(reference / name)
        if name in options.custom_checks:
            validate_tables(name, actual, expected, options, comparison)
        else:
            compare_tables(name, actual, expected, options, comparison)
    return comparison


# --------------------------------------------------------------------------
# Inputs, staging and solving
# --------------------------------------------------------------------------


def extract_inputs(tarball: Path, destination: Path) -> None:
    """Write the run's inputs out of a Palace release tarball.

    It writes ``test/data/regression/`` and ``cases.cpp`` under
    ``destination``, below the tarball's top directory. Upstream's inputs are
    symlinks into ``examples/``; each is written as a copy of the file it
    names, so no platform has to make a symlink.

    Raises:
        ValueError: If a member escapes the tarball's tree, or a link names
            something that is not a regular file in it.
    """
    with tarfile.open(tarball) as archive:
        members = {member.name: member for member in archive.getmembers()}
        for member in members.values():
            top, _, relative = member.name.partition("/")
            wanted = (
                relative.startswith(REGRESSION_DATA + "/") or relative == CASES_SOURCE
            )
            if not wanted:
                continue
            if ".." in relative.split("/"):
                raise ValueError(f"{member.name}: escapes the tree")
            target = member
            while target.issym():
                linked = posixpath.normpath(
                    posixpath.join(posixpath.dirname(target.name), target.linkname)
                )
                if not linked.startswith(top + "/") or linked not in members:
                    raise ValueError(f"{member.name}: links outside the tree")
                target = members[linked]
            path = destination.joinpath(*relative.split("/"))
            if target.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif target.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(target)
                if source is None:
                    raise ValueError(f"{member.name}: cannot be read")
                with source, path.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
            else:
                raise ValueError(f"{member.name}: not a file, directory or link")


def stage_case(case: Case, inputs: Path, stage: Path) -> dict[str, Any]:
    """Copy a case's inputs into ``stage``, with the solvers injected.

    Upstream links the inputs into a staging directory instead. Copies work
    on every platform, and they cannot reach back into the inputs.

    Returns:
        The configuration the case runs with.
    """
    source = inputs / "input" / case.directory
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for entry in source.iterdir():
        if entry.name in {"postpro", "log"}:
            continue
        if entry.is_dir():
            shutil.copytree(entry, stage / entry.name)
        else:
            shutil.copyfile(entry, stage / entry.name)
    config = inject_solvers(load_config(source / case.config))
    (stage / case.config).write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def clean_environment(palace: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a case runs in.

    ``PATH`` holds the directory of the ``palace`` under test and the
    operating system's own, so no MPI, compiler runtime or MSYS2 tool on the
    runner can stand in for one the wheel failed to carry. One OpenMP thread
    per rank keeps concurrent cases from oversubscribing the cores.
    """
    if os.name == "nt":
        root = environ.get("SYSTEMROOT", r"C:\Windows")
        system = [rf"{root}\System32", root, rf"{root}\System32\Wbem"]
    else:
        system = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    kept = {
        name: value
        for name, value in environ.items()
        if name.upper() not in LEAKY_VARIABLES
        and not name.upper().startswith(LEAKY_PREFIXES)
        and name.upper() not in {"PATH", "VIRTUAL_ENV", "OMP_NUM_THREADS"}
    }
    return {
        **kept,
        "PATH": os.pathsep.join([str(palace.parent), *system]),
        "OMP_NUM_THREADS": "1",
    }


@dataclass(frozen=True)
class Solve:
    """How one case's solve ended."""

    returncode: int | None
    seconds: float


def solve(
    case: Case,
    stage: Path,
    *,
    palace: Path,
    env: Mapping[str, str],
    timeout: float = CASE_TIMEOUT,
) -> Solve:
    """Run ``palace -np 2 <config>`` in ``stage``, logging to ``palace.log``.

    A case that outlives ``timeout`` is killed with every process it started,
    and its return code is ``None``.
    """
    command = [str(palace), "-np", str(RANKS), case.config]
    started = time.monotonic()
    with (stage / "palace.log").open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=stage,
            env=dict(env),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        try:
            returncode: int | None = process.wait(timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            process.wait()
            returncode = None
    return Solve(returncode, time.monotonic() - started)


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        subprocess.run(
            [rf"{root}\System32\taskkill.exe", "/F", "/T", "/PID", str(process.pid)],
            check=False,
            capture_output=True,
        )
    else:
        os.killpg(process.pid, signal.SIGKILL)


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    """One case's outcome."""

    case: Case
    problems: tuple[str, ...]
    worst: Cell | None
    seconds: float | None

    @property
    def passed(self) -> bool:
        """Whether the case ran and its output matched."""
        return not self.problems


def check_case(
    case: Case, stage: Path, inputs: Path, solved: Solve | None = None
) -> Result:
    """Judge one staged case's output against its reference."""
    config = load_config(stage / case.config)
    options = case.options
    if options.max_rows is None:
        options = replace(options, max_rows=requested_modes(config))
    comparison = compare_case(
        options,
        output=stage / "postpro" / case.postpro,
        reference=inputs / "ref" / case.directory / case.postpro,
        iterations=refinement_iterations(config),
        config=case.config,
    )
    problems = list(comparison.problems)
    if solved is not None and solved.returncode is None:
        problems.insert(0, f"palace ran out of time ({solved.seconds:.0f} s)")
    elif solved is not None and solved.returncode != 0:
        problems.insert(0, f"palace exited with status {solved.returncode}")
    seconds = None if solved is None else solved.seconds
    return Result(case, tuple(problems), comparison.worst, seconds)


def judge(
    *,
    platform: str,
    exclusions: Iterable[upstream_gate.Exclusion],
    cases: Iterable[Case],
    results: Sequence[Result],
) -> list[str]:
    """Return every reason the run fails; empty when it passes.

    The gate's rules, scoped to the wheel run: a failing case fails the row
    unless excluded, an excluded case that passes fails it, and so does an
    exclusion naming no case the run registers.
    """
    return upstream_gate.judge(
        platform=platform,
        exclusions=exclusions,
        registered=[case.name for case in cases],
        results=[
            (result.case.name, "run" if result.passed else "fail") for result in results
        ],
        run="wheel",
    )


def summary(
    *,
    platform: str,
    results: Sequence[Result],
    excluded: frozenset[str],
    problems: Sequence[str],
) -> str:
    """The job-summary text: each case's verdict, time and worst cell."""
    passed = sum(result.passed for result in results)
    lines = [
        f"## Wheel regression run: {platform}",
        "",
        (
            f"{len(results)} cases at {RANKS} ranks, {passed} passed. "
            f"Skipped ({SKIP_REASON}): {', '.join(SKIPPED)}."
        ),
        "",
        "| Case | Verdict | Time | Worst cell |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        verdict = "pass" if result.passed else "FAIL"
        if result.case.name in excluded:
            verdict = "excluded, " + ("PASSED" if result.passed else "failed")
        time_text = "" if result.seconds is None else f"{result.seconds:.0f} s"
        worst = "" if result.worst is None else result.worst.describe()
        worst = worst.replace("|", "\\|")
        lines.append(f"| {result.case.name} | {verdict} | {time_text} | {worst} |")
    for result in results:
        if result.problems:
            lines += ["", f"### {result.case.name}", ""]
            lines += [f"- {problem}" for problem in result.problems[:20]]
            if len(result.problems) > 20:
                lines.append(f"- ... and {len(result.problems) - 20} more")
    lines += ["", "**Verdict:** " + ("pass" if not problems else "fail"), ""]
    lines += [f"- {problem}" for problem in problems]
    return "\n".join(lines) + "\n"


def collect_failures(results: Iterable[Result], work: Path, destination: Path) -> int:
    """Copy each failed case's ``postpro`` and ``palace.log`` to ``destination``.

    This is the evidence a red row uploads. Passing cases are left behind,
    and so is everything a failed case was staged with: the inputs are in
    the Palace release, and its outputs are what a reader has to look at.

    Returns:
        How many cases were copied.
    """
    copied = 0
    for result in results:
        if result.passed:
            continue
        stage, target = work / result.case.name, destination / result.case.name
        target.mkdir(parents=True, exist_ok=True)
        if (stage / "postpro").is_dir():
            shutil.copytree(stage / "postpro", target / "postpro", dirs_exist_ok=True)
        if (stage / "palace.log").is_file():
            shutil.copy2(stage / "palace.log", target / "palace.log")
        copied += 1
    return copied


# --------------------------------------------------------------------------
# The wheel's payload
# --------------------------------------------------------------------------


def fingerprint(wheel: Path, exclusions: Path = upstream_gate.EXCLUSIONS) -> str:
    """Return what the run's pass record is keyed on.

    It is a content hash of the wheel's package files and the exclusion file.
    Every member counts by its path and content, vendored libraries included,
    except the ``.dist-info`` directory: a rebuild that changes only the
    record or the metadata leaves the payload, and so its pass record, alone.
    Member order and timestamps are not hashed. The exclusion file counts as
    it does in the gate's fingerprint, so a new wheel-scoped exclusion makes
    the run due and is held to account at once.

    Args:
        wheel: The wheel under test.
        exclusions: The exclusion file.

    Returns:
        A hex digest.
    """
    digest = hashlib.sha256()

    def add(part: bytes) -> None:
        digest.update(len(part).to_bytes(8, "little"))
        digest.update(part)

    with zipfile.ZipFile(wheel) as archive:
        for info in sorted(archive.infolist(), key=lambda info: info.filename):
            top = info.filename.split("/", 1)[0]
            if info.is_dir() or top.endswith(".dist-info"):
                continue
            add(info.filename.encode())
            add(hashlib.sha256(archive.read(info)).digest())
    add(b"exclusions")
    add(exclusions.read_bytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def run(
    *,
    source: Path,
    work: Path,
    platform: str,
    palace: Path | None,
    names: Sequence[str] = (),
    jobs: int | None = None,
    timeout: float = CASE_TIMEOUT,
    exclusions: Sequence[upstream_gate.Exclusion] | None = None,
) -> tuple[list[Result], list[str], str]:
    """Solve (or, with no ``palace``, only re-judge) the run's cases.

    Args:
        source: Where :func:`extract_inputs` wrote the Palace inputs.
        work: Where each case is staged, one directory per case.
        platform: The row's platform tag.
        palace: The ``palace`` under test; ``None`` judges the outputs
            already in ``work``.
        names: Only these cases, when given.
        jobs: Cases at a time; half the cores by default.
        timeout: Per-case ceiling, in seconds.
        exclusions: The exclusions; the repository's file by default.

    Returns:
        Each case's result, the run's problems and the summary text.
    """
    cases = select_cases(parse_cases((source / CASES_SOURCE).read_text("utf-8")))
    unknown = set(names) - {case.name for case in cases}
    if unknown:
        raise ValueError(f"not cases of the run: {sorted(unknown)}")
    chosen = [case for case in cases if not names or case.name in names]
    inputs = source / REGRESSION_DATA
    exclusions = upstream_gate.load_exclusions() if exclusions is None else exclusions

    def one(case: Case) -> Result:
        stage = work / case.name
        if palace is None:
            return check_case(case, stage, inputs)
        stage_case(case, inputs, stage)
        solved = solve(
            case,
            stage,
            palace=palace,
            env=clean_environment(palace, os.environ),
            timeout=timeout,
        )
        result = check_case(case, stage, inputs, solved)
        print(
            f"{case.name}: {'pass' if result.passed else 'FAIL'} "
            f"({solved.seconds:.0f} s)",
            flush=True,
        )
        return result

    workers = jobs or max(1, (os.cpu_count() or 2) // RANKS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, chosen))
    problems = judge(
        platform=platform, exclusions=exclusions, cases=cases, results=results
    )
    excluded = upstream_gate.excluded_on(platform, exclusions, run="wheel")
    text = summary(
        platform=platform, results=results, excluded=excluded, problems=problems
    )
    return results, problems, text


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: ``fingerprint``, ``extract`` or ``run``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    stamp = commands.add_parser("fingerprint", help="print the pass-record key")
    stamp.add_argument("wheel", type=Path)
    unpack = commands.add_parser("extract", help="write the inputs out of a tarball")
    unpack.add_argument("tarball", type=Path)
    unpack.add_argument("destination", type=Path)
    solve_parser = commands.add_parser("run", help="solve and judge the cases")
    solve_parser.add_argument("--source", type=Path, required=True)
    solve_parser.add_argument("--work", type=Path, required=True)
    solve_parser.add_argument("--platform", required=True)
    target = solve_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--palace", type=Path, help="the palace under test")
    target.add_argument(
        "--judge-only", action="store_true", help="judge the outputs already in --work"
    )
    solve_parser.add_argument("--case", action="append", default=[], dest="names")
    solve_parser.add_argument("--jobs", type=int)
    solve_parser.add_argument("--timeout", type=float, default=CASE_TIMEOUT)
    solve_parser.add_argument("--summary", type=Path, help="append the summary here")
    solve_parser.add_argument(
        "--failures", type=Path, help="copy the failed cases' outputs here"
    )
    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.command == "fingerprint":
        print(fingerprint(args.wheel))
        return 0
    if args.command == "extract":
        extract_inputs(args.tarball, args.destination)
        return 0

    started = time.monotonic()
    results, problems, text = run(
        source=args.source,
        work=args.work,
        platform=args.platform,
        palace=None if args.judge_only else args.palace.resolve(),
        names=args.names,
        jobs=args.jobs,
        timeout=args.timeout,
    )
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as sink:
            sink.write(text)
    if args.failures:
        collect_failures(results, args.work, args.failures)
    print(text)
    print(f"total {time.monotonic() - started:.0f} s")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
