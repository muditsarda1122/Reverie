"""feat/flow regression tests — accepted conclusions stay retrievable.

The review path promotes a candidate into the Canonical Brain; query must
then find it from the same repo even when the ECU's scope path is deeper
than the session anchor (repo) or its label does not equal the checkout
directory name. Domain conclusions remain cross-repo.

Run: .venv/bin/python -m pytest tests/test_flow_query_scope.py -v
Offline: FakeModel embeddings; the diffuser is mocked.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

from unittest.mock import patch

import numpy as np
import pytest

from reverie.brain import Brain
from reverie.config import get_config
from reverie.retrieval import retrieve
from reverie.review_queue import apply_decisions


class FakeModel:
    """Deterministic normalized embeddings — no real model load."""

    def encode_one(self, text):
        return np.full(8, 1 / np.sqrt(8), dtype=np.float32)


def _ecu(cognition, level, scope_path, repo_path, files=(), symbols=()):
    return {
        "cognition": cognition,
        "conclusion_type": "invariant",
        "scope": {"level": level, "path": scope_path},
        "provenance": {"source_type": "debugging"},
        "grounding": {
            "repo_path": str(repo_path),
            "files": list(files),
            "symbols": list(symbols),
        },
        "confidence": 0.7,
        "evidence_pointers": list(files),
    }


def _query_ids(brain, session_id, model):
    result = retrieve(
        brain, "what does soft_str return for bytes?", mode="debugging",
        session_id=session_id, embedding_model=model,
        use_llm_mode_detection=False,
    )
    return {g["core_ecu"]["id"] for g in result["groups"]}


@pytest.fixture()
def temp_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REVERIE_HOME", str(home / ".reverie"))
    get_config(refresh=True)
    return tmp_path


@pytest.fixture()
def brain(temp_env):
    b = Brain(temp_env / "brain.db")
    yield b
    b.close()


@pytest.fixture()
def model():
    return FakeModel()


def test_accepted_repo_conclusion_retrievable_from_same_repo(
        brain, model, temp_env):
    """The checkout directory need not match the repo label, and a
    module-scope conclusion captured deeper than the repo anchor must
    survive the hierarchy filter after review acceptance."""
    repo = temp_env / "checkout-any-name"
    (repo / "markupsafe").mkdir(parents=True)
    (repo / "markupsafe" / "__init__.py").write_text(
        "def soft_str(s):\n    return s.decode() if isinstance(s, bytes) else str(s)\n"
    )
    session_id = brain.create_session(str(repo), "main")
    candidate_id = brain.insert_session_ecu(session_id, _ecu(
        "soft_str returns text for bytes input without raising",
        "module", "repo:markupsafe > module:escaping", repo,
        files=["markupsafe/__init__.py"], symbols=["soft_str"],
    ), embedding=model.encode_one("soft_str"))

    with patch("reverie.diffuser.diffuse_ecu"):
        result = apply_decisions(
            brain, {candidate_id: "accept"}, channel="tui",
            config=get_config(), embedding_model=model,
        )
    assert candidate_id in result["decided"]
    assert brain.get_ecu(candidate_id) is not None

    assert candidate_id in _query_ids(brain, session_id, model)


def test_domain_conclusion_retrievable_from_another_repo(
        brain, model, temp_env):
    """Domain scope is universal: a conclusion captured in one repo is
    available from a different repo, while the other repo's bound ECU is
    not pulled sideways."""
    repo_a = temp_env / "repo-a"
    repo_b = temp_env / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    session_b = brain.create_session(str(repo_b), "main")

    domain_id = brain.insert_ecu(
        _ecu("Bytes decoding should be idempotent and encoding explicit",
             "domain", "domain:python-text", repo_a),
        embedding=model.encode_one("domain"),
    )
    repo_only_id = brain.insert_ecu(
        _ecu("soft_str is defined in markupsafe/__init__.py",
             "module", "repo:repo-a > module:escaping", repo_a,
             files=["markupsafe/__init__.py"]),
        embedding=model.encode_one("repo-only"),
    )

    ids = _query_ids(brain, session_b, model)
    assert domain_id in ids
    assert repo_only_id not in ids
