"""EC-Bench runner tests — offline; the agent is a stub script, review=skip
avoids all LLM calls. Real EC session CLI runs as subprocesses against the
isolated EC_HOME (the same wiring the benchmark uses).

Run: .venv/bin/python -m pytest tests/test_ecbench_runner.py -v
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
import time
from pathlib import Path

import pytest

from ec import run_ecbench
from ec.install import MCP_CONSOLE_SCRIPT, server_command
from ec.run_ecbench import BenchError, _runner_python

ROOT = Path(__file__).resolve().parent.parent

STUB_AGENT = """\
import json, os, sys, time
prompt = sys.argv[-1]
if prompt.startswith("SLEEP"):
    time.sleep(30)
print(json.dumps({
    "type": "text",
    "prompt": prompt,
    "opencode_config": os.environ.get("OPENCODE_CONFIG"),
    "ec_home": os.environ.get("EC_HOME"),
    "cwd": os.getcwd(),
}))
"""


@pytest.fixture()
def stub_agent(tmp_path):
    script = tmp_path / "stub_agent.py"
    script.write_text(STUB_AGENT)
    return f"{{python}} {script} {{prompt}}"


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "fastapi"
    r.mkdir()
    (r / "main.py").write_text("# fake fastapi app\n")
    return r


@pytest.fixture()
def spec(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps({
        "name": "test-bench",
        "sessions": [
            {"id": "s1", "prompts": ["investigate routing", "fix the route"]},
            {"id": "s2", "prompts": ["add a test", "SLEEP more"]},
        ],
    }))
    return p


# ---------------------------------------------------------------------------
# spec loading
# ---------------------------------------------------------------------------

def test_load_spec_ok(spec):
    loaded = run_ecbench.load_spec(spec)
    assert loaded["name"] == "test-bench"
    assert [s["id"] for s in loaded["sessions"]] == ["s1", "s2"]


def test_load_spec_validation(tmp_path):
    with pytest.raises(BenchError, match="not found"):
        run_ecbench.load_spec(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    with pytest.raises(BenchError, match="not valid JSON"):
        run_ecbench.load_spec(bad)
    bad.write_text(json.dumps({"sessions": []}))
    with pytest.raises(BenchError, match="no sessions"):
        run_ecbench.load_spec(bad)
    bad.write_text(json.dumps({"sessions": [{"prompts": []}]}))
    with pytest.raises(BenchError, match="non-empty 'prompts'"):
        run_ecbench.load_spec(bad)
    bad.write_text(json.dumps({"sessions": [{"prompts": ["  "]}]}))
    with pytest.raises(BenchError, match="non-empty string"):
        run_ecbench.load_spec(bad)


# ---------------------------------------------------------------------------
# agent config shapes
# ---------------------------------------------------------------------------

def test_agent_config_ec_shape(tmp_path, monkeypatch):
    ec_home = tmp_path / "ec_home"
    monkeypatch.setattr("ec.install._is_pip_installed", lambda: False)
    cfg = run_ecbench.build_agent_config("ec", ec_home, tmp_path / "wd", "main")
    entry = cfg["mcp"]["ec"]
    python, args, _env = server_command()
    assert entry["command"] == [python, *args]
    assert Path(entry["command"][0]).is_absolute()
    assert entry["enabled"] is True
    env = entry["environment"]
    assert env["PYTHONPATH"] == str(ROOT)
    assert env["EC_HOME"] == str(ec_home)
    assert env["EC_REPO_PATH"] == str(tmp_path / "wd")
    assert env["EC_BRANCH"] == "main"
    assert cfg["instructions"] == [str(ec_home / "AGENTS.md")]


def test_agent_config_ec_shape_console_script(tmp_path, monkeypatch):
    """D45: pip-installed -> the MCP entry is just ["ec-mcp"]; EC_* env vars
    are unaffected (they ride on `environment`, not PYTHONPATH)."""
    monkeypatch.setattr("ec.install._is_pip_installed", lambda: True)
    cfg = run_ecbench.build_agent_config(
        "ec", tmp_path / "ec_home", tmp_path / "wd", "main")
    entry = cfg["mcp"]["ec"]
    assert entry["command"] == [MCP_CONSOLE_SCRIPT]
    assert entry["environment"]["EC_HOME"] == str(tmp_path / "ec_home")
    assert entry["enabled"] is True


def test_agent_config_baseline_disables_ec(tmp_path):
    cfg = run_ecbench.build_agent_config(
        "baseline", tmp_path / "ec_home", tmp_path / "wd", "main")
    assert cfg["mcp"]["ec"]["enabled"] is False
    assert "instructions" not in cfg


# ---------------------------------------------------------------------------
# argv templating
# ---------------------------------------------------------------------------

def test_agent_argv_prompt_is_single_token(stub_agent):
    argv = run_ecbench._agent_argv(stub_agent, 'why "quotes" & spaces?', Path("/r"))
    assert argv[-1] == 'why "quotes" & spaces?'      # never re-split
    # {python} is the harness interpreter (D45 decoupled it from the MCP
    # console script, which is config-only)
    assert argv[0] == _runner_python()
    assert Path(argv[0]).is_absolute()


def test_agent_argv_appends_prompt_without_placeholder(stub_agent):
    argv = run_ecbench._agent_argv("{python} echo", "hello", Path("/r"))
    assert argv[-1] == "hello"


# ---------------------------------------------------------------------------
# end-to-end with a stub agent (offline; review=skip)
# ---------------------------------------------------------------------------

def _run(spec, repo, tmp_path, stub_agent, *extra):
    argv = [
        "--spec", str(spec), "--repo", str(repo), "--runs-dir",
        str(tmp_path / "runs"), "--run-id", "t1", "--review", "skip",
        "--agent-cmd", stub_agent, *extra,
    ]
    rc = run_ecbench.main(argv)
    assert rc == 0
    return tmp_path / "runs" / "t1"


def test_end_to_end_stub_agent(spec, repo, tmp_path, stub_agent):
    run_dir = _run(spec, repo, tmp_path, stub_agent)
    top = json.loads((run_dir / "run.json").read_text())
    assert set(top["conditions"]) == {"ec", "baseline"}

    for condition in ("ec", "baseline"):
        manifest = json.loads(
            (run_dir / condition / "manifest.json").read_text())
        assert manifest["condition"] == condition
        # repo copied per condition; original untouched
        assert (run_dir / condition / "workdir" / "main.py").exists()
        sessions = manifest["sessions"]
        assert [s["id"] for s in sessions] == ["s1", "s2"]
        for s in sessions:
            assert len(s["prompts"]) == 2
            for p in s["prompts"]:
                assert p["exit_code"] == 0 and not p["timed_out"]
                assert Path(p["transcript"]).exists()
        # the stub echoed the injected config; per condition
        first = sessions[0]["prompts"][0]
        echoed = json.loads(Path(first["transcript"]).read_text().strip())
        assert echoed["opencode_config"] == str(
            run_dir / condition / "opencode_config.json")
        assert echoed["cwd"] == str(run_dir / condition / "workdir")

    # ec condition: two closed sessions in the isolated brain
    ec_manifest = json.loads((run_dir / "ec" / "manifest.json").read_text())
    stats = ec_manifest["ec_stats"]
    assert stats["canonical_ecus"] == 0            # review=skip promotes nothing
    assert [s["status"] for s in stats["sessions"]] == ["closed", "closed"]
    for s in ec_manifest["sessions"]:
        assert "Started EC session" in Path(s["start"]["log"]).read_text()
        assert "Review complete." in Path(s["stop"]["log"]).read_text()
        assert s["stop"]["diffusion_warning"] is False
    # baseline: no brain, no EC_HOME in the agent env
    base_first = json.loads(Path(
        json.loads((run_dir / "baseline" / "manifest.json").read_text())
        ["sessions"][0]["prompts"][0]["transcript"]).read_text().strip())
    assert base_first["ec_home"] is None
    assert not (run_dir / "baseline" / "ec_home" / "ec.db").exists()
    baseline_cfg = json.loads(
        (run_dir / "baseline" / "opencode_config.json").read_text())
    assert baseline_cfg["mcp"]["ec"]["enabled"] is False


def test_resume_skips_completed_prompts(spec, repo, tmp_path, stub_agent):
    run_dir = _run(spec, repo, tmp_path, stub_agent)
    meta = run_dir / "ec" / "transcripts" / "s1" / "01.meta.json"
    first = json.loads(meta.read_text())
    time.sleep(0.02)
    _run(spec, repo, tmp_path, stub_agent)         # same run-id → resume
    again = json.loads(meta.read_text())
    assert again == first                          # not re-run (mtime/content same)
    manifest = json.loads((run_dir / "ec" / "manifest.json").read_text())
    skipped = [p for s in manifest["sessions"] for p in s["prompts"]
               if p.get("skipped_existing")]
    assert len(skipped) == 4                       # all 4 ec prompts reused


def test_prompt_timeout_is_recorded(spec, repo, tmp_path, stub_agent):
    run_dir = _run(spec, repo, tmp_path, stub_agent, "--timeout", "1")
    manifest = json.loads((run_dir / "ec" / "manifest.json").read_text())
    by_prompt = {p["prompt"]: p
                 for s in manifest["sessions"] for p in s["prompts"]}
    assert by_prompt["SLEEP more"]["timed_out"] is True
    assert by_prompt["investigate routing"]["timed_out"] is False


def test_dry_run_writes_nothing(spec, repo, tmp_path, capsys):
    rc = run_ecbench.main([
        "--spec", str(spec), "--repo", str(repo),
        "--runs-dir", str(tmp_path / "runs"), "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run — nothing written" in out
    assert '"enabled": true' in out and '"enabled": false' in out
    assert not (tmp_path / "runs").exists()


def test_unknown_condition_errors(spec, repo, tmp_path):
    rc = run_ecbench.main([
        "--spec", str(spec), "--repo", str(repo),
        "--runs-dir", str(tmp_path / "runs"), "--conditions", "ec,placebo",
    ])
    assert rc == 1
