"""The wheel regression run's parser, comparator, verdict and fingerprint."""

import io
import json
import math
import tarfile
import zipfile
from pathlib import Path

import pytest

from palace_solver import PALACE_VERSION
from wheelbuild import upstream_gate, wheel_regression
from wheelbuild.upstream_gate import Exclusion
from wheelbuild.wheel_regression import Case, CasesError, Options

ROOT = Path(__file__).resolve().parents[1]

#: Palace's ``test/unit/regression/cases.cpp`` at the pinned version, kept
#: here so the parser is tested against the real file without a download.
CASES = ROOT / "tests" / "data" / f"palace-{PALACE_VERSION}" / "cases.cpp"

LINUX = "manylinux_2_28_x86_64"


# --------------------------------------------------------------------------
# cases.cpp
# --------------------------------------------------------------------------


def _pinned():
    return CASES.read_text(encoding="utf-8")


def _by_name(cases):
    return {case.name: case for case in cases}


def test_the_kept_cases_cpp_is_the_pinned_palaces():
    """A Palace bump has to bring its cases.cpp here, or this fails."""
    assert CASES.is_file(), f"copy test/unit/regression/cases.cpp to {CASES}"


def test_the_pinned_cases_cpp_parses_into_46_cases_and_40_after_the_skip_list():
    cases = wheel_regression.parse_cases(_pinned())
    selected = wheel_regression.select_cases(cases)

    assert len(cases) == 46
    assert len(selected) == 40
    assert not {case.name for case in selected} & set(wheel_regression.SKIPPED)
    assert "transmon_coarse" not in _by_name(cases)  # [Long]


def test_the_parser_reads_options_as_upstream_sets_them():
    cases = _by_name(wheel_regression.parse_cases(_pinned()))

    spheres = cases["spheres"]
    assert (spheres.directory, spheres.config, spheres.postpro) == (
        "spheres",
        "spheres.json",
        "",
    )
    assert spheres.options.rtol == 1.0e-4
    assert spheres.options.atol == 1.0e-16
    assert spheres.options.gridfunction_fields

    # A named constant, and a \u escape.
    assert cases["cylinder_cavity_pec"].options.excluded_columns == (
        "Maximum",
        "Minimum",
        "Mean",
        "Error (Bkwd.)",
        "Error (Abs.)",
    )
    assert "\u03ba_ext" in cases["cpw_lumped_eigen"].options.excluded_columns

    synth = cases["adapter_driven_synth"].options
    assert synth.min_rows == 5
    assert synth.skip_rowcount
    assert not synth.paraview_fields
    assert set(synth.custom_checks) == {"rom-eigenvalues.csv", "port-S.csv"}
    assert "rom-coupled-S" in synth.unstored_files

    assert math.isinf(cases["cpw_wave_adaptive"].options.rtol)


def test_every_custom_check_file_is_recorded_as_structure_only():
    cases = wheel_regression.parse_cases(_pinned())
    checked = {
        (case.name, name) for case in cases for name in case.options.custom_checks
    }

    assert ("antenna_short_dipole", "farfield-rE.csv") in checked
    assert ("cpw2d_thin", "mode-V.csv") in checked
    assert ("floquet_wave", "port-floquet-S.csv") in checked
    assert len(checked) == 17


@pytest.mark.parametrize(
    ("original", "mutated", "message"),
    [
        ("opts.rtol = 1.0e-4;", "opts.rtol = 2 * 1.0e-4;", "not a form"),
        ("opts.skip_rowcount = true;", "opts.skip_rowcount = 1;", "not a form"),
        ("opts.min_rows = 5;", "opts.min_rows = five;", "not a form"),
        ("opts.atol = 1.0e-16;", "opts.tolerance = 1.0e-16;", "unknown option"),
        ("TestFarfield(opts.rtol);", "TestNewThing(opts.rtol);", "factory"),
        ("opts.atol = 1.0e-16;", "opts.atol = 1.0e-16; Tweak(opts);", "statement"),
        (
            "opts.excluded_columns = eigen_excluded;",
            "opts.excluded_columns = other_list;",
            "not a list",
        ),
        ('"Maximum", "Minimum"}', '"Maximum", "Min\\x69mum"}', "escape"),
        ("opts.rtol = 1.0e-4;", "opts.rtol = 1.0e-4; opts.rtol = 1.0e-3;", "twice"),
    ],
)
def test_a_mutated_cases_cpp_is_refused(original, mutated, message):
    text = _pinned()
    assert original in text

    with pytest.raises(CasesError, match=message):
        wheel_regression.parse_cases(text.replace(original, mutated, 1))


def test_a_case_that_never_runs_is_refused():
    text = """TEST_CASE("a", "[Regression]")
    {
      palace::test::RegressionOptions opts;
      opts.rtol = 1e-3;
    }"""

    with pytest.raises(CasesError, match="never calls RunRegressionCase"):
        wheel_regression.parse_cases(text)


def test_a_statement_after_the_run_is_refused():
    text = """TEST_CASE("a", "[Regression]")
    {
      palace::test::RegressionOptions opts;
      palace::test::RunRegressionCase("a", "a.json", "", opts);
      opts.rtol = 1e-3;
    }"""

    with pytest.raises(CasesError, match="after RunRegressionCase"):
        wheel_regression.parse_cases(text)


def test_comments_and_non_regression_cases_are_ignored():
    text = """// TEST_CASE("commented", "[Regression]") {}
    TEST_CASE("unit", "[Serial]") { REQUIRE(1 == 1); }
    TEST_CASE("long", "[Regression][Long]") { Anything(); }
    TEST_CASE("a", "[Regression]")
    {
      palace::test::RegressionOptions opts;  /* inline; comment */
      opts.excluded_columns = {"x//y",  // trailing
                               "z"};
      palace::test::RunRegressionCase("dir", "a.json", "sub", opts);
    }"""

    (case,) = wheel_regression.parse_cases(text)

    assert case == Case(
        "a", "dir", "a.json", "sub", Options(excluded_columns=("x//y", "z"))
    )


def test_a_skipped_case_that_cases_cpp_no_longer_has_is_refused():
    cases = [
        case
        for case in wheel_regression.parse_cases(_pinned())
        if case.name != "cpw_wave_adaptive"
    ]

    with pytest.raises(CasesError, match="cpw_wave_adaptive"):
        wheel_regression.select_cases(cases)


# --------------------------------------------------------------------------
# Configuration files
# --------------------------------------------------------------------------


def test_the_config_preprocessor_matches_palaces():
    text = """{
      // a comment, with "quotes"
      "Model": { "Mesh": "mesh/a b.msh", "L0": 1.0e-2, },  /* block
      comment */
      "Attributes": [1-3, 7, -1],
      "Note": "keeps // and /* inside */ strings",
    }"""

    assert json.loads(wheel_regression.preprocess_config(text)) == {
        "Model": {"Mesh": "mesh/a b.msh", "L0": 1.0e-2},
        "Attributes": [1, 2, 3, 7, -1],
        "Note": "keeps // and /* inside */ strings",
    }


def test_load_config_refuses_a_repeated_key(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"A": 1, "A": 2}')

    with pytest.raises(ValueError, match="repeated"):
        wheel_regression.load_config(path)


def test_the_solvers_are_injected_as_upstreams_harness_does():
    assert wheel_regression.inject_solvers({}) == {
        "Solver": {"Linear": {"Type": "Default"}}
    }
    config = {"Solver": {"Linear": {"Type": "BoomerAMG"}, "Eigenmode": {"N": 4}}}

    assert wheel_regression.inject_solvers(config) == {
        "Solver": {
            "Linear": {"Type": "Default"},
            "Eigenmode": {"N": 4, "Type": "Default"},
        }
    }
    assert wheel_regression.requested_modes(config) == 4
    assert wheel_regression.refinement_iterations({"Model": {"Refinement": {}}}) == 0


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

HEADER = "f (GHz), Re{S[1][1]} (dB), Maximum, κ_ext"


def _csv(path, rows, header=HEADER):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [header] + [", ".join(f"{value:+.12e}" for value in row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _trees(tmp_path, live_rows, reference_rows, *, config="case.json"):
    output, reference = tmp_path / "out", tmp_path / "ref"
    _csv(output / "port-S.csv", live_rows)
    _csv(reference / "port-S.csv", reference_rows)
    (output / "paraview").mkdir()
    (output / "palace.json").write_text("{}")
    (output / f"{Path(config).stem}_resolved.json").write_text("{}")
    return output, reference


REFERENCE = [[1.0, -3.0, 9.0, 0.5], [2.0, -4.0, 9.0, -0.25]]


TIGHT = Options(rtol=1e-3, atol=1e-12)


def _compare(tmp_path, live_rows, options=TIGHT):
    output, reference = _trees(tmp_path, live_rows, REFERENCE)
    return wheel_regression.compare_case(
        options, output=output, reference=reference, iterations=0, config="case.json"
    )


def test_output_within_tolerance_passes_and_reports_its_worst_cell(tmp_path):
    comparison = _compare(tmp_path, [[1.0, -3.002, 9.0, 0.5], [2.0, -4.0, 9.0, -0.25]])

    assert comparison.problems == []
    assert comparison.worst is not None
    assert (comparison.worst.column, comparison.worst.row) == ("Re{S[1][1]} (dB)", 1)
    assert 0.5 < comparison.worst.used < 1.0


def test_output_outside_tolerance_fails_on_that_cell(tmp_path):
    comparison = _compare(tmp_path, [[1.0, -3.01, 9.0, 0.5], [2.0, -4.0, 9.0, -0.25]])

    assert len(comparison.problems) == 1
    assert "'Re{S[1][1]} (dB)' row 1" in comparison.problems[0]
    assert comparison.worst.used > 1.0


def test_nan_matches_only_nan(tmp_path):
    output, reference = _trees(
        tmp_path, [[1.0, math.nan, 9.0, 0.5]], [[1.0, math.nan, 9.0, 0.5]]
    )
    options = Options(rtol=1e-3, atol=1e-12)

    assert not wheel_regression.compare_case(
        options, output=output, reference=reference, iterations=0, config="case.json"
    ).problems

    _csv(output / "port-S.csv", [[1.0, -3.0, 9.0, 0.5]])
    problems = wheel_regression.compare_case(
        options, output=output, reference=reference, iterations=0, config="case.json"
    ).problems
    assert len(problems) == 1


def test_excluded_and_magnitude_columns(tmp_path):
    live = [[1.0, -3.0, 123.0, -0.5], [2.0, -4.0, -7.0, 0.25]]

    assert len(_compare(tmp_path / "a", live).problems) == 4
    options = Options(
        rtol=1e-3, atol=1e-12, excluded_columns=("Maximum",), abs_columns=("κ_ext",)
    )
    assert _compare(tmp_path / "b", live, options).problems == []


def test_max_rows_caps_the_rows_compared(tmp_path):
    live = [[1.0, -3.0, 9.0, 0.5], [2.0, -9.0, 9.0, -0.25]]

    assert _compare(tmp_path / "a", live).problems
    options = Options(rtol=1e-3, atol=1e-12, max_rows=1)
    assert _compare(tmp_path / "b", live, options).problems == []


def test_row_counts_must_match_unless_skipped(tmp_path):
    live = [[1.0, -3.0, 9.0, 0.5]]

    assert "1 rows" in _compare(tmp_path / "a", live).problems[0]
    options = Options(rtol=1e-3, atol=1e-12, skip_rowcount=True, min_rows=2)
    assert _compare(tmp_path / "b", live, options).problems == [
        "port-S.csv: 1 rows, fewer than 2"
    ]


def test_a_renamed_header_fails(tmp_path):
    output, reference = _trees(tmp_path, REFERENCE, REFERENCE)
    _csv(output / "port-S.csv", REFERENCE, header=HEADER.replace("GHz", "Hz"))

    problems = wheel_regression.compare_case(
        Options(), output=output, reference=reference, iterations=0, config="case.json"
    ).problems
    assert problems == ["port-S.csv: column 0 is 'f (Hz)', the reference has 'f (GHz)'"]


def test_a_custom_checked_file_is_held_to_structure_only(tmp_path):
    live = [[5.0, 5.0, 5.0, 5.0], [6.0, 6.0, 6.0, 6.0]]
    options = Options(rtol=1e-3, atol=1e-12, custom_checks=("port-S.csv",))

    assert _compare(tmp_path / "a", live, options).problems == []
    assert _compare(tmp_path / "b", live[:1], options).problems


def test_infinite_tolerances_check_structure_only(tmp_path):
    live = [[5.0, 5.0, 5.0, 5.0], [6.0, 6.0, 6.0, 6.0]]

    assert (
        _compare(tmp_path, live, Options(rtol=math.inf, atol=math.inf)).problems == []
    )


def test_a_missing_csv_file_fails(tmp_path):
    output, reference = _trees(tmp_path, REFERENCE, REFERENCE)
    _csv(reference / "domain-E.csv", REFERENCE)

    problems = wheel_regression.compare_case(
        Options(), output=output, reference=reference, iterations=0, config="case.json"
    ).problems
    assert problems == ["missing CSV files: domain-E.csv"]


def test_an_unstored_file_is_dropped_and_an_excluded_one_only_present(tmp_path):
    output, reference = _trees(tmp_path, REFERENCE, REFERENCE)
    _csv(output / "rom-Linv.csv", [[1.0, 1.0, 1.0, 1.0]])
    _csv(output / "rom-eigenvectors.csv", [[1.0, 1.0, 1.0, 1.0]])
    _csv(reference / "rom-eigenvectors.csv", [[2.0, 2.0, 2.0, 2.0]] * 3)
    options = Options(
        unstored_files=("rom-Linv",), excluded_files=("rom-eigenvectors",)
    )

    assert not wheel_regression.compare_case(
        options, output=output, reference=reference, iterations=0, config="case.json"
    ).problems


def test_missing_field_and_metadata_output_fails(tmp_path):
    output, reference = _trees(tmp_path, REFERENCE, REFERENCE)
    (output / "paraview").rmdir()
    (output / "case_resolved.json").unlink()

    problems = wheel_regression.compare_case(
        Options(), output=output, reference=reference, iterations=0, config="case.json"
    ).problems
    assert problems == [
        "missing directories: paraview",
        "missing metadata files: case_resolved.json",
    ]


def test_refinement_iterations_expect_their_directories(tmp_path):
    output, reference = _trees(tmp_path, REFERENCE, REFERENCE)

    problems = wheel_regression.compare_case(
        Options(), output=output, reference=reference, iterations=1, config="case.json"
    ).problems
    assert problems == [
        "missing directories: iteration1, iteration1/paraview",
        "missing metadata files: iteration1/palace.json",
    ]


def test_a_missing_output_directory_fails(tmp_path):
    _output, reference = _trees(tmp_path, REFERENCE, REFERENCE)

    problems = wheel_regression.compare_case(
        Options(),
        output=tmp_path / "gone",
        reference=reference,
        iterations=0,
        config="case.json",
    ).problems
    assert problems[0].startswith("no output directory")
    assert "missing CSV files: port-S.csv" in problems


def test_a_table_reads_as_palaces_does(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("a, b ,c\n 1.0, NULL, nan\n+2.5e+00,  , -inf\n")

    a, b, c = wheel_regression.read_table(path)
    assert (a.header, b.header, c.header) == ("a", "b", "c")
    assert a.data == (1.0, 2.5)
    assert b.data == ()
    assert math.isnan(c.data[0])
    assert c.data[1] == -math.inf
    assert wheel_regression.row_count((a, b, c)) == 2


def test_within_is_catch2s_either_condition():
    assert wheel_regression.within(1.0005, 1.0, 1e-3, 0.0)
    assert not wheel_regression.within(1.002, 1.0, 1e-3, 0.0)
    assert wheel_regression.within(1e-17, 0.0, 1e-3, 1e-16)
    assert wheel_regression.within(math.inf, math.inf, 1e-3, 0.0)
    assert not wheel_regression.within(math.nan, 1.0, 1e-3, 1.0)


# --------------------------------------------------------------------------
# Exclusions and the verdict
# --------------------------------------------------------------------------


def _write(tmp_path, text):
    path = tmp_path / "exclusions.toml"
    path.write_text(text)
    return path


def test_an_exclusion_scope_defaults_to_the_gate(tmp_path):
    path = _write(
        tmp_path,
        '[[exclusion]]\ntest = "A"\nplatforms = ["win_amd64"]\nreason = "r"\n'
        '[[exclusion]]\ntest = "B"\nplatforms = ["win_amd64"]\nreason = "r"\n'
        'scope = "wheel"\n'
        '[[exclusion]]\ntest = "C"\nplatforms = ["win_amd64"]\nreason = "r"\n'
        'scope = "both"\n',
    )
    exclusions = upstream_gate.load_exclusions(path)

    assert [e.scope for e in exclusions] == ["gate", "wheel", "both"]
    assert upstream_gate.excluded_on("win_amd64", exclusions) == {"A", "C"}
    assert upstream_gate.excluded_on("win_amd64", exclusions, run="wheel") == {"B", "C"}


def test_an_unknown_scope_is_refused(tmp_path):
    path = _write(
        tmp_path,
        '[[exclusion]]\ntest = "A"\nplatforms = ["win_amd64"]\nreason = "r"\n'
        'scope = "everything"\n',
    )

    with pytest.raises(ValueError, match="scope"):
        upstream_gate.load_exclusions(path)


def test_the_repositorys_exclusions_are_all_gate_scoped():
    """The wheel run starts with no exclusions of its own (ticket 09)."""
    exclusions = upstream_gate.load_exclusions()

    assert {e.scope for e in exclusions} == {"gate"}


def _result(name, *, passed):
    case = Case(name, name, f"{name}.json", "")
    return wheel_regression.Result(case, () if passed else ("bad",), None, 1.0)


def test_the_wheel_run_holds_its_exclusions_to_account():
    cases = [Case(name, name, f"{name}.json", "") for name in ("a", "b")]
    wheel = Exclusion("a", (LINUX,), "r", "wheel")
    gate = Exclusion("a", (LINUX,), "r", "gate")

    def verdict(exclusions, a_passes):
        return wheel_regression.judge(
            platform=LINUX,
            exclusions=exclusions,
            cases=cases,
            results=[_result("a", passed=a_passes), _result("b", passed=True)],
        )

    assert verdict([], a_passes=True) == []
    assert verdict([], a_passes=False) == ["'a' failed"]
    assert verdict([wheel], a_passes=False) == []
    assert verdict([wheel], a_passes=True) == [
        "'a' passed but is excluded; remove its exclusion"
    ]
    assert verdict([gate], a_passes=False) == ["'a' failed"]
    assert verdict([Exclusion("z", (LINUX,), "r", "both")], a_passes=True) == [
        "excluded test case 'z' is not registered by the suite"
    ]


def test_the_gate_ignores_a_wheel_scoped_exclusion():
    problems = upstream_gate.judge(
        platform=LINUX,
        exclusions=[Exclusion("Fails", (LINUX,), "r", "wheel")],
        registered=["serial-Fails"],
        results=[("serial-Fails", "fail")],
    )

    assert problems == ["'serial-Fails' failed"]


def test_the_summary_names_each_case_and_the_verdict():
    text = wheel_regression.summary(
        platform=LINUX,
        results=[_result("a", passed=True), _result("b", passed=False)],
        excluded=frozenset({"b"}),
        problems=[],
    )

    assert "| a | pass | 1 s |" in text
    assert "| b | excluded, failed | 1 s |" in text
    assert "### b" in text
    assert "**Verdict:** pass" in text


def test_only_the_failed_cases_outputs_are_collected(tmp_path):
    work = tmp_path / "work"
    for name in ("a", "b"):
        (work / name / "postpro" / name).mkdir(parents=True)
        (work / name / "postpro" / name / "x.csv").write_text("1")
        (work / name / "palace.log").write_text("log")
        (work / name / f"{name}.json").write_text("{}")
    results = [_result("a", passed=True), _result("b", passed=False)]

    copied = wheel_regression.collect_failures(results, work, tmp_path / "out")

    assert copied == 1
    assert sorted(
        p.relative_to(tmp_path / "out").as_posix()
        for p in (tmp_path / "out").rglob("*")
        if p.is_file()
    ) == [
        "b/palace.log",
        "b/postpro/b/x.csv",
    ]


# --------------------------------------------------------------------------
# Inputs, environment and fingerprint
# --------------------------------------------------------------------------


def _add(archive, name, data=b"", *, link=None):
    info = tarfile.TarInfo(name)
    if link is not None:
        info.type, info.linkname = tarfile.SYMTYPE, link
        archive.addfile(info)
    else:
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def test_inputs_are_extracted_with_links_written_as_files(tmp_path):
    tarball = tmp_path / "palace.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        _add(archive, "palace-1/examples/a/a.json", b"{}")
        _add(archive, "palace-1/test/unit/regression/cases.cpp", b"cases")
        _add(archive, "palace-1/test/unit/other.cpp", b"no")
        _add(archive, "palace-1/test/data/regression/ref/a/x.csv", b"x")
        _add(
            archive,
            "palace-1/test/data/regression/input/a/a.json",
            link="../../../../../examples/a/a.json",
        )

    wheel_regression.extract_inputs(tarball, tmp_path / "out")

    linked = (
        tmp_path / "out" / "test" / "data" / "regression" / "input" / "a" / "a.json"
    )
    assert linked.read_bytes() == b"{}"
    assert not linked.is_symlink()
    assert (tmp_path / "out" / "test" / "unit" / "regression" / "cases.cpp").is_file()
    assert not (tmp_path / "out" / "test" / "unit" / "other.cpp").exists()
    assert not (tmp_path / "out" / "examples").exists()


def test_a_link_out_of_the_tree_is_refused(tmp_path):
    tarball = tmp_path / "palace.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        _add(
            archive,
            "palace-1/test/data/regression/input/a.json",
            link="../../../../../../etc/passwd",
        )

    with pytest.raises(ValueError, match="outside"):
        wheel_regression.extract_inputs(tarball, tmp_path / "out")


def test_the_environment_hides_the_runners_mpi_and_libraries(tmp_path):
    palace = tmp_path / "venv" / "bin" / "palace"
    env = wheel_regression.clean_environment(
        palace,
        {
            "PATH": "/opt/msys64/ucrt64/bin:/usr/bin",
            "LD_LIBRARY_PATH": "/opt/mpich/lib",
            "PMI_RANK": "0",
            "HYDRA_HOST_FILE": "h",
            "MSMPI_BIN": "C:/mpi",
            "OMP_NUM_THREADS": "8",
            "HOME": "/home/u",
        },
    )

    assert env["PATH"].split(":" if ":" in env["PATH"] else ";")[0] == str(
        palace.parent
    )
    assert "msys64" not in env["PATH"]
    assert env["OMP_NUM_THREADS"] == "1"
    assert env["HOME"] == "/home/u"
    assert not {"LD_LIBRARY_PATH", "PMI_RANK", "HYDRA_HOST_FILE", "MSMPI_BIN"} & set(
        env
    )


def _wheel(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return path


def test_the_fingerprint_ignores_dist_info_and_member_order(tmp_path):
    payload = [("palace_solver/bin/palace", b"elf"), ("palace_solver.libs/a.so", b"so")]
    first = _wheel(
        tmp_path / "1.whl",
        [*payload, ("palace_solver-1.dist-info/RECORD", b"one")],
    )
    second = _wheel(
        tmp_path / "2.whl",
        [("palace_solver-1.dist-info/RECORD", b"two"), *reversed(payload)],
    )
    changed = _wheel(
        tmp_path / "3.whl",
        [("palace_solver/bin/palace", b"elf"), ("palace_solver.libs/a.so", b"SO")],
    )
    moved = _wheel(
        tmp_path / "4.whl",
        [("palace_solver/bin/palace", b"elf"), ("palace_solver/lib/a.so", b"so")],
    )

    exclusions = tmp_path / "exclusions.toml"
    exclusions.write_text("")
    fingerprints = [
        wheel_regression.fingerprint(wheel, exclusions)
        for wheel in (first, second, changed, moved)
    ]
    assert fingerprints[0] == fingerprints[1]
    assert len(set(fingerprints)) == 3


def test_the_fingerprint_follows_the_exclusions(tmp_path):
    wheel = _wheel(tmp_path / "1.whl", [("palace_solver/bin/palace", b"elf")])
    exclusions = tmp_path / "exclusions.toml"
    exclusions.write_text("")
    before = wheel_regression.fingerprint(wheel, exclusions)
    exclusions.write_text('[[exclusion]]\nscope = "wheel"\n')

    assert wheel_regression.fingerprint(wheel, exclusions) != before
