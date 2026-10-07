"""Phase 13 Session B tests — commit_hash capture at observe time (D49).

Design doc §4: extracted ECUs carry no commit anchor (audit gap 1.15), so
the MCP server stamps the session repo's git HEAD into each ECU's grounding
after extraction and before storage. The extractor prompt stays untouched;
the extractor merely guarantees a grounding dict exists to stamp into.

Run: .venv/bin/python -m pytest tests/test_phase13_commit_hash.py -v
Offline: mocked LLM calls (ec.extractor.call_llm / ec.diffuser.call_llm)
plus real throwaway git repos under tmp_path.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ec.brain import Brain
from ec.extractor import extract_and_store_session, stamp_grounding_commit_hash
from ec.grounding import evaluate_ecu
from ec.mcp_server import ECServer, _current_commit_hash
from ec.session import SessionManager

REPO_BRANCH = "main"

EXTRACT_PAYLOAD = json.dumps({
    "ecus": [
        {
            # no grounding object at all — stamping must create the dict
            "cognition": "Token refresh must be optimistic to avoid race "
                         "conditions",
            "conclusion_type": "invariant",
            "scope": {"level": "repo", "path": "repo:demo > module:auth"},
            "source_type": "debugging",
            "evidence_pointer": "reasoning step 3",
        },
        {
            # extractor-provided commit_hash must survive untouched
            "cognition": "Session cookies must be rotated on privilege change",
            "conclusion_type": "decision",
            "scope": {"level": "module", "path": "repo:demo > module:auth"},
            "source_type": "debugging",
            "grounding": {"files": ["auth/session.py"],
                          "commit_hash": "deadbeefcafe"},
            "evidence_pointer": "reasoning step 5",
        },
    ],
    "rejected_count": 0,
})


# ---------------------------------------------------------------------------
# helpers + fixtures
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=ec@test", "-c", "user.name=ec", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    f = path / "auth" / "token.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("def refresh(): ...\n")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "initial", cwd=path)


def _head_short(path: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                         check=True, capture_output=True, text=True).stdout
    return out.strip()[:12]


@pytest.fixture(scope="session")
def model():
    from ec.embeddings import get_embedding_model
    return get_embedding_model()


@pytest.fixture()
def brain(tmp_path):
    b = Brain(tmp_path / "ec.db")
    yield b
    b.close()


@pytest.fixture()
def manager(brain):
    return SessionManager(brain)


@pytest.fixture()
def server(brain, manager, model):
    return ECServer(brain=brain, manager=manager, embedding_model=model)


def call_tool(server, name, arguments=None, msg_id=1):
    response = server.handle_message({
        "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    assert "result" in response, response
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    return payload, result


def session_ecus_by_cognition(brain, session_id):
    return {ecu["cognition"]: ecu
            for ecu in brain.list_session_ecus(session_id)}


# ---------------------------------------------------------------------------
# unit — _current_commit_hash + the stamping helper
# ---------------------------------------------------------------------------

class TestCurrentCommitHash:
    def test_git_repo_head_short_hash(self, tmp_path):
        _init_git_repo(tmp_path / "repo")
        assert _current_commit_hash(str(tmp_path / "repo")) == \
            _head_short(tmp_path / "repo")
        assert len(_current_commit_hash(str(tmp_path / "repo"))) == 12

    def test_non_git_dir_is_none(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert _current_commit_hash(str(plain)) is None

    def test_missing_path_is_none(self, tmp_path):
        assert _current_commit_hash(str(tmp_path / "nope")) is None

    def test_none_path_is_none(self):
        assert _current_commit_hash(None) is None


class TestStampGroundingCommitHash:
    def test_creates_grounding_when_absent(self):
        ecus = [{"cognition": "c"}]
        stamp_grounding_commit_hash(ecus, "abc123def456")
        assert ecus[0]["grounding"]["commit_hash"] == "abc123def456"

    def test_fills_empty_hash_but_keeps_existing(self):
        ecus = [{"grounding": {}}, {"grounding": {"commit_hash": "keepme"}}]
        stamp_grounding_commit_hash(ecus, "newvalue12")
        assert ecus[0]["grounding"]["commit_hash"] == "newvalue12"
        assert ecus[1]["grounding"]["commit_hash"] == "keepme"

    def test_noop_without_hash(self):
        ecus = [{"cognition": "c"}]
        stamp_grounding_commit_hash(ecus, None)
        assert "grounding" not in ecus[0]
        stamp_grounding_commit_hash(ecus, "")
        assert "grounding" not in ecus[0]

    def test_repairs_non_dict_grounding(self):
        ecus = [{"grounding": None}]
        stamp_grounding_commit_hash(ecus, "abc123def456")
        assert ecus[0]["grounding"]["commit_hash"] == "abc123def456"


# ---------------------------------------------------------------------------
# extract_and_store_session stamps between extraction and storage
# ---------------------------------------------------------------------------

def test_extract_and_store_session_stamps(brain, model):
    sid = brain.create_session(str(model_home := Path("/repos/x")), REPO_BRANCH)
    interaction = {"prompt": "p", "reasoning_trace": "r"}
    with patch("ec.extractor.call_llm", return_value=EXTRACT_PAYLOAD):
        _, ids = extract_and_store_session(
            brain, sid, interaction, embedding_model=model,
            commit_hash="abc123def456")
    stored = brain.get_session_ecu(ids[0])
    assert stored["grounding"]["commit_hash"] == "abc123def456"
    kept = brain.get_session_ecu(ids[1])
    assert kept["grounding"]["commit_hash"] == "deadbeefcafe"


def test_extract_and_store_session_without_hash_stays_clean(brain, model):
    sid = brain.create_session("/repos/x", REPO_BRANCH)
    interaction = {"prompt": "p", "reasoning_trace": "r"}
    with patch("ec.extractor.call_llm", return_value=EXTRACT_PAYLOAD):
        _, ids = extract_and_store_session(
            brain, sid, interaction, embedding_model=model, commit_hash=None)
    assert "commit_hash" not in brain.get_session_ecu(ids[0])["grounding"]
    # extractor-provided hashes survive untouched either way
    assert brain.get_session_ecu(ids[1])["grounding"]["commit_hash"] == \
        "deadbeefcafe"


# ---------------------------------------------------------------------------
# ec_observe end-to-end (§28.13 flow with the D49 stamp)
# ---------------------------------------------------------------------------

def test_commit_hash_stamped(tmp_path, monkeypatch, server, manager, model,
                             brain):
    repo = tmp_path / "demo-repo"
    _init_git_repo(repo)
    monkeypatch.setenv("EC_REPO_PATH", str(repo))
    monkeypatch.setenv("EC_BRANCH", REPO_BRANCH)
    sid = manager.start_session()["session_id"]

    with patch("ec.extractor.call_llm", return_value=EXTRACT_PAYLOAD), \
         patch("ec.diffuser.call_llm",
               return_value=json.dumps({"classifications": []})):
        payload, result = call_tool(server, "ec_observe", {
            "user_prompt": "why do sessions drop?",
            "reasoning_trace": "I traced it to the token refresh race...",
        })
    assert result["isError"] is False
    assert payload["status"] == "ok", payload.get("error")
    assert payload["ecus_extracted"] == 2

    by_cog = session_ecus_by_cognition(brain, sid)
    stamped = by_cog["Token refresh must be optimistic to avoid race "
                     "conditions"]
    assert stamped["grounding"]["commit_hash"] == _head_short(repo)
    preserved = by_cog["Session cookies must be rotated on privilege change"]
    assert preserved["grounding"]["commit_hash"] == "deadbeefcafe"


def test_commit_hash_no_git_repo(tmp_path, monkeypatch, server, manager,
                                 brain):
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    monkeypatch.setenv("EC_REPO_PATH", str(plain))
    monkeypatch.setenv("EC_BRANCH", REPO_BRANCH)
    sid = manager.start_session()["session_id"]

    with patch("ec.extractor.call_llm", return_value=EXTRACT_PAYLOAD), \
         patch("ec.diffuser.call_llm",
               return_value=json.dumps({"classifications": []})):
        payload, result = call_tool(server, "ec_observe", {
            "user_prompt": "p", "reasoning_trace": "r"})
    assert result["isError"] is False
    assert payload["status"] == "ok"

    by_cog = session_ecus_by_cognition(brain, sid)
    unstamped = by_cog["Token refresh must be optimistic to avoid race "
                       "conditions"]
    assert "commit_hash" not in (unstamped["grounding"] or {})
    # the extractor-provided hash is the only one present — nothing stamped
    preserved = by_cog["Session cookies must be rotated on privilege change"]
    assert preserved["grounding"]["commit_hash"] == "deadbeefcafe"


# ---------------------------------------------------------------------------
# the dormant consumer wakes up: §3.3 informational commit staleness
# ---------------------------------------------------------------------------

def test_commit_hash_grounding_uses_it(tmp_path):
    """An ECU whose stamped hash falls behind HEAD gets the stale-commit
    flag from grounding verification — previously impossible because no
    ECU ever carried a commit_hash."""
    repo = tmp_path / "stale-repo"
    _init_git_repo(repo)

    ecu = {
        "id": "test-ecu",
        "scope": {"level": "repo", "path": "repo:stale > module:auth"},
        "grounding": {
            "files": ["auth/token.py"],
            "symbols": ["refresh"],
            "commit_hash": _head_short(repo),   # exactly what D49 stamps
        },
    }

    # At HEAD: zero distance → no flag.
    verdict = evaluate_ecu(ecu, str(repo), commit_staleness_limit=1)
    assert verdict.stale_commit is None
    assert not verdict.deprecated

    # Two commits later: distance 2 > limit 1 → informational stale flag.
    (repo / "auth" / "token.py").write_text("def refresh(): pass\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "touch one", cwd=repo)
    _git("commit", "-qm", "touch two", "--allow-empty", cwd=repo)

    verdict = evaluate_ecu(ecu, str(repo), commit_staleness_limit=1)
    assert verdict.stale_commit == 2          # informational only
    assert not verdict.deprecated             # files still exist

    # The verifier's real default limit (50): 2 commits behind stays quiet.
    verdict_default = evaluate_ecu(ecu, str(repo))
    assert verdict_default.stale_commit is None
