"""The stale worker ran Reverie's own pytest, so a green project reported "tests failed"."""

import copy
import subprocess
import sys

from reverie import staleness
from reverie.config import DEFAULT_CONFIG, _to_attrdict


def cfg():
    return _to_attrdict(copy.deepcopy(DEFAULT_CONFIG))



def test_stale_tests_run_with_the_projects_python_not_reveries(tmp_path):
    root = tmp_path / "p"
    (root / "tests").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("")
    cmd = staleness.project_test_command(str(root))
    assert cmd[0] == str(root / ".venv" / "bin" / "python")
    assert cmd[0] != sys.executable


def test_stale_tests_fall_back_to_path_pytest_and_skip_without_tests(tmp_path, monkeypatch):
    root = tmp_path / "p"
    assert staleness.project_test_command(str(root)) == []
    (root / "tests").mkdir(parents=True)
    monkeypatch.setattr("shutil.which", lambda n: {"pytest": "/x/pytest"}.get(n))
    assert staleness.project_test_command(str(root)) == ["/x/pytest", "-q", "-x"]


def test_a_broken_test_runner_is_not_evidence_that_the_code_fails(tmp_path):
    root = tmp_path / "p"
    (root / "tests").mkdir(parents=True)
    c = cfg()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for code, want in ((2, "skipped (test runner error"), (1, "failed")):
        script = tmp_path / f"exit{code}.py"
        script.write_text(f"import sys\nsys.exit({code})\n")
        c.stale.test_command = f"{sys.executable} {script}"
        assert staleness.run_tests(str(root), c).startswith(want)
