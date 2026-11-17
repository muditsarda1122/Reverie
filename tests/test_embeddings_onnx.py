"""The ONNX embedding backend: the one-time model download (verified, atomic, offline-safe), the
backend choice, and the vectors. Network-free except where the real model is already on disk."""

from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from reverie import embeddings
from reverie.embeddings import EmbeddingError, EmbeddingModel, ensure_model, model_ready


def _fake_remote(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A local 'huggingface': two small files, with MODEL_FILES re-pinned to their hashes."""
    remote = tmp_path / "remote"
    (remote / "onnx").mkdir(parents=True)
    (remote / "onnx" / "model.onnx").write_bytes(b"not a real model" * 100)
    (remote / "tokenizer.json").write_bytes(b"{}")
    pins = {name: (path, hashlib.sha256((remote / path).read_bytes()).hexdigest())
            for name, (path, _h) in embeddings.MODEL_FILES.items()}
    monkeypatch.setattr(embeddings, "MODEL_FILES", pins)
    return remote, remote.as_uri() + "/"


def test_ensure_model_downloads_checks_and_is_idempotent(tmp_path, monkeypatch):
    remote, base = _fake_remote(tmp_path, monkeypatch)
    dest = tmp_path / "cache"
    said: list[str] = []
    assert not model_ready(dest)
    ensure_model(said.append, directory=dest, base_url=base)
    assert model_ready(dest) and len(said) == 2
    said.clear()
    ensure_model(said.append, directory=dest, base_url=base)       # second call downloads nothing
    assert said == []
    assert not list(dest.glob(".*.part"))


def test_a_file_that_fails_its_hash_is_not_kept(tmp_path, monkeypatch):
    remote, base = _fake_remote(tmp_path, monkeypatch)
    (remote / "onnx" / "model.onnx").write_bytes(b"tampered")
    dest = tmp_path / "cache"
    with pytest.raises(EmbeddingError, match="integrity check"):
        ensure_model(directory=dest, base_url=base)
    assert not (dest / "model.onnx").exists() and not list(dest.glob(".*.part"))


def test_offline_failure_says_what_to_do(tmp_path):
    with pytest.raises(EmbeddingError, match="could not download model.onnx.*reverie install"):
        ensure_model(directory=tmp_path / "cache", base_url=(tmp_path / "nowhere").as_uri() + "/")


def test_model_dir_follows_the_env_then_the_xdg_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_MODEL_DIR", str(tmp_path / "m"))
    assert embeddings.model_dir() == tmp_path / "m"
    monkeypatch.delenv("REVERIE_MODEL_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    assert embeddings.model_dir() == tmp_path / "c" / "reverie" / "models" / "all-MiniLM-L6-v2"


def test_forcing_onnx_without_its_packages_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("REVERIE_EMBEDDING_BACKEND", "onnx")
    monkeypatch.setitem(sys.modules, "onnxruntime", None)          # import raises ImportError
    with pytest.raises(EmbeddingError, match="onnxruntime and tokenizers"):
        EmbeddingModel().encode(["x"])


def test_nothing_installed_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("REVERIE_EMBEDDING_BACKEND", "")
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(EmbeddingError, match="no embedding backend"):
        EmbeddingModel().encode(["x"])


def _damaged_model(tmp_path, monkeypatch, *, heals: bool):
    d = tmp_path / "m"
    d.mkdir()
    for name in embeddings.MODEL_FILES:
        (d / name).write_bytes(b"junk")
    attempts: list[bytes] = []

    def fake_ensure(*_a, **_k):
        for name in embeddings.MODEL_FILES:
            if not (d / name).exists():
                (d / name).write_bytes(b"fresh" if heals else b"junk")
        return d

    class Backend:
        def __init__(self, directory):
            attempts.append((directory / "model.onnx").read_bytes())
            if attempts[-1] == b"junk":
                raise ValueError("damaged")

        def encode(self, texts):
            return np.ones((len(texts), 384), dtype=np.float32)

    for mod in ("onnxruntime", "tokenizers"):                       # present, whatever this env has
        monkeypatch.setitem(sys.modules, mod, types.ModuleType(mod))
    monkeypatch.setenv("REVERIE_EMBEDDING_BACKEND", "onnx")
    monkeypatch.setattr(embeddings, "ensure_model", fake_ensure)
    monkeypatch.setattr(embeddings, "_OnnxBackend", Backend)
    return attempts


def test_a_damaged_model_is_fetched_again_once(tmp_path, monkeypatch):
    attempts = _damaged_model(tmp_path, monkeypatch, heals=True)
    assert EmbeddingModel().encode(["x"]).shape == (1, 384)
    assert attempts == [b"junk", b"fresh"]


def test_a_model_that_stays_damaged_is_a_clear_error_not_a_loop(tmp_path, monkeypatch):
    attempts = _damaged_model(tmp_path, monkeypatch, heals=False)
    with pytest.raises(EmbeddingError, match="Run `reverie install`"):
        EmbeddingModel().encode(["x"])
    assert len(attempts) == 2


def test_empty_input_needs_no_model():
    assert EmbeddingModel().encode([]).shape == (0, 384)


@pytest.mark.skipif(not model_ready(), reason="the ONNX model is not on this machine (run `reverie install`)")
def test_vectors_are_unit_length_and_meaningful():
    m = EmbeddingModel()
    v = m.encode(["Never commit directly to main.", "Do not push straight to the main branch.",
                  "The kitchen sink is full of dishes."])
    assert v.shape == (3, 384) and v.dtype == np.float32
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    assert v[0] @ v[1] > 0.6 > v[0] @ v[2]                        # paraphrase beats an unrelated sentence
    assert np.allclose(m.encode_one("a rule"), m.encode(["a rule", "x" * 5000])[0], atol=1e-5)   # padding does not change a vector
