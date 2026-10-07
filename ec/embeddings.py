"""Embedding model wrapper (Spec §15 embedding block).

all-MiniLM-L6-v2 (384-dim, sentence-transformers). The model is set at init
and never changed. Vectors are L2-normalized, so cosine similarity is a plain
dot product. Stored in SQLite as float32 BLOBs (384 * 4 = 1536 bytes).
"""

from __future__ import annotations

import logging

import numpy as np

from .config import get_config

log = logging.getLogger("ec.embeddings")


class EmbeddingError(RuntimeError):
    """Raised when the embedding model cannot be loaded or used.

    Never fail silently — a missing model must surface as a clear error
    (user constraint: 'no silent failure').
    """


def to_blob(vec: np.ndarray) -> bytes:
    """Serialize an embedding to a float32 BLOB."""
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    """Deserialize a float32 BLOB back to an embedding vector."""
    return np.frombuffer(blob, dtype=np.float32)


class EmbeddingModel:
    """Lazy-loading singleton wrapper around SentenceTransformer."""

    def __init__(self, model_name: str | None = None, dimensions: int | None = None):
        cfg = get_config()
        self.model_name = model_name or cfg.embedding.model
        self.dimensions = dimensions or cfg.embedding.dimensions
        self._model = None  # loaded on first use

    def _load(self):
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingError(
                "sentence-transformers is not installed in this environment. "
                "Install EC's dependencies (pip install -e . — see "
                "pyproject.toml) — embeddings are required for all EC operations."
            ) from exc
        try:
            self._model = SentenceTransformer(self.model_name)
        except Exception as exc:
            raise EmbeddingError(
                f"Failed to load embedding model {self.model_name!r}: {exc}. "
                "EC requires this model for ECU storage and retrieval."
            ) from exc

    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode texts to L2-normalized float32 vectors, shape (N, 384)."""
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        self._load()
        vecs = self._model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,  # cosine similarity == dot product
        )
        return np.asarray(vecs, dtype=np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        """Encode a single text to an L2-normalized float32 vector, shape (384,)."""
        return self.encode([text])[0]


_model_singleton: EmbeddingModel | None = None


def get_embedding_model() -> EmbeddingModel:
    """Process-wide singleton — the model is set at init, never changed (§15)."""
    global _model_singleton
    if _model_singleton is None:
        _model_singleton = EmbeddingModel()
    return _model_singleton
