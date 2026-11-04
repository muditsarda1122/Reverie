import subprocess

import pytest

from reverie import anchors


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    (r / "pkg").mkdir(parents=True)
    (r / "pkg" / "cells.py").write_text(
        "def cell_len(text):\n    return len(text)\n\n"
        "class RetryTransport:\n    pass\n\n_padding_needed = 3\n")
    (r / "pkg" / "other.py").write_text("def helper():\n    pass\n\ndef run():\n    pass\n")
    (r / "pkg" / "more.py").write_text("def run():\n    pass\n")
    (r / "pkg" / "third.py").write_text("def run():\n    pass\n")
    for a in (["init", "-q"], ["add", "-A"],
              ["-c", "user.email=a@b", "-c", "user.name=n", "commit", "-qm", "i"]):
        subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)
    return r


def test_symbol_without_file_gains_its_defining_file(repo):
    g = anchors.ground("Always measure width with `cell_len`, never len().", repo, [], ["cell_len"])
    assert g["grounded"] and g["files"] == ["pkg/cells.py"] and g["symbols"] == ["cell_len"]


def test_invented_file_and_symbol_are_dropped(repo):
    g = anchors.ground("Retries live in RetryTransport.", repo, ["pkg/retry.py"],
                       ["RetryTransport", "TotallyInvented"])
    assert g["files"] == ["pkg/cells.py"] and g["symbols"] == ["RetryTransport"]


def test_identifiers_are_found_in_the_text_itself(repo):
    g = anchors.ground("Fill must be one cell wide, see _padding_needed in pkg/cells.py", repo)
    assert "pkg/cells.py" in g["files"] and "_padding_needed" in g["symbols"]


def test_generic_name_defined_in_many_files_anchors_no_file(repo):
    g = anchors.ground("call `run` last", repo, [], ["run"])
    assert g["symbols"] == ["run"] and g["files"] == []


def test_bare_filename_resolves_when_unique(repo):
    g = anchors.ground("see cells.py for widths", repo)
    assert g["files"] == ["pkg/cells.py"]


def test_no_repo_means_proposal_unchanged(tmp_path):
    g = anchors.ground("x", None, ["a.py"], ["Foo"])
    assert g == {"files": ["a.py"], "symbols": ["Foo"], "grounded": False}
    g2 = anchors.ground("x", tmp_path / "not-git", ["a.py"], ["Foo"])
    assert g2["grounded"] is False and g2["files"] == ["a.py"]


def test_prose_only_stays_repo_level(repo):
    g = anchors.ground("Prefer small commits and ask before refactoring.", repo)
    assert g["grounded"] and g["files"] == [] and g["symbols"] == []
