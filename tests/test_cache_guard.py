"""The check that stands between a partly unreadable tree and a silent save.

`actions/cache/save` downgrades its tar's failure to a warning, so the cost of
this check being wrong is not a failed job: it is every later run paying a cold
build with nothing in the log to say why.
"""

import os

import pytest

from wheelbuild import cache_guard

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads everything, so no permission bit can be tested",
)


@pytest.fixture
def tree(tmp_path):
    """A small build tree, all of it readable."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libpalace.dylib").write_text("payload")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "palace").write_text("#!/bin/sh\n")
    return tmp_path


def test_a_readable_tree_reports_nothing(tree):
    assert cache_guard.unreadable_paths(tree) == []


def test_an_unreadable_file_is_reported(tree):
    """The manylinux case: the container wrote it as root and the hand-over
    missed it.
    """
    secret = tree / "lib" / "libpalace.dylib"
    secret.chmod(0o000)

    assert cache_guard.unreadable_paths(tree) == [str(secret)]


def test_an_unsearchable_directory_is_reported_without_descending(tree):
    closed = tree / "lib"
    closed.chmod(0o000)

    try:
        assert cache_guard.unreadable_paths(tree) == [str(closed)]
    finally:
        closed.chmod(0o755)


def test_a_dangling_symlink_is_not_a_failed_save(tree):
    """tar stores the link rather than the target, so it saves fine."""
    (tree / "bin" / "mpiexec").symlink_to(tree / "nowhere")

    assert cache_guard.unreadable_paths(tree) == []


def test_the_report_is_bounded(tree):
    for index in range(cache_guard.REPORT_LIMIT + 5):
        path = tree / f"object-{index}.o"
        path.write_text("")
        path.chmod(0o000)

    assert len(cache_guard.unreadable_paths(tree)) == cache_guard.REPORT_LIMIT


def test_the_command_fails_on_an_unreadable_tree(tree, capsys):
    (tree / "bin" / "palace").chmod(0o000)

    code = cache_guard.main([str(tree)])

    assert code == 1
    assert "::error::" in capsys.readouterr().out


def test_the_command_passes_on_a_readable_tree(tree):
    assert cache_guard.main([str(tree)]) == 0


def test_a_tree_that_was_never_built_is_not_a_failure(tmp_path, capsys):
    """A build that died before the tree existed has nothing to save."""
    code = cache_guard.main([str(tmp_path / "absent")])

    assert code == 0
    assert "nothing to save" in capsys.readouterr().out
