"""Embedding model wrapper (Spec §15 embedding block).

all-MiniLM-L6-v2 (384-dim). The model is set at init and never changed. Vectors are
L2-normalized, so cosine similarity is a plain dot product. Stored in SQLite as float32
BLOBs (384 * 4 = 1536 bytes).

Two backends give the same vectors (cosine 1.0, max pairwise difference ~4e-7, measured):

* **onnx** (default): ``onnxruntime`` + ``tokenizers`` run the same model. No PyTorch: a
  ~30 MB install instead of ~1 GB, and the model loads in ~0.2 s instead of ~10 s. The two
  model files (~90 MB) are downloaded once, on first use, into ``~/.cache/reverie/models/`` (``$REVERIE_MODEL_DIR`` overrides) and
  checked against pinned SHA-256 hashes. Hooks never embed, so a hook never downloads.
* **torch** (fallback): ``sentence-transformers``, only when it is already installed and the
  ONNX packages are not. Set ``REVERIE_EMBEDDING_BACKEND=torch`` or ``onnx`` to force one.
"""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import numpy as np

from .config import get_config

log = logging.getLogger("reverie.embeddings")

MAX_SEQ_LEN = 256          # sentence-transformers' own limit for this model

# Pinned to one revision of sentence-transformers/all-MiniLM-L6-v2 so a changed upstream file
# can never silently change the vectors every stored item was embedded with.
_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
_BASE = f"https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/{_REVISION}/"
MODEL_FILES: dict[str, tuple[str, str]] = {      # local name -> (path in the repo, sha256)
    "model.onnx": ("onnx/model.onnx",
                   "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
    "tokenizer.json": ("tokenizer.json",
                       "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"),
}


class EmbeddingError(RuntimeError):
    """Raised when the embedding model cannot be loaded or used.

    Never fail silently — a missing model must surface as a clear error
    (user constraint: 'no silent failure').
    """


def to_blob(vec: np.ndarray) -> bytes:
    """Serialize an embedding to a float32 BLOB."""
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes | bytearray) -> np.ndarray:
    """Deserialize a float32 BLOB back to an embedding vector."""
    return np.frombuffer(blob, dtype=np.float32)


# ---------------------------------------------------------------------------
# the model files (downloaded once)
# ---------------------------------------------------------------------------

def model_dir() -> Path:
    """A machine-wide cache (the model is not user data, and it is the same for every
    ``REVERIE_HOME``): ``$REVERIE_MODEL_DIR``, else ``$XDG_CACHE_HOME`` or ``~/.cache`` + ``reverie/models``."""
    explicit = os.environ.get("REVERIE_MODEL_DIR")
    if explicit:
        return Path(explicit).expanduser()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
    return cache / "reverie" / "models" / "all-MiniLM-L6-v2"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_ready(directory: Path | None = None) -> bool:
    """True when both model files are present (their hashes were checked when they were written)."""
    d = directory or model_dir()
    return all((d / name).is_file() for name in MODEL_FILES)


def ensure_model(progress: Callable[[str], None] | None = None, *,
                 directory: Path | None = None, base_url: str | None = None) -> Path:
    """Make sure the ONNX model files exist, downloading what is missing. Idempotent and safe to
    call from two processes at once (each writes its own temp file, then renames it into place).
    Raises ``EmbeddingError`` with the reason on any failure (offline, bad hash, disk)."""
    d = directory or model_dir()
    base = base_url or _BASE
    say = progress or (lambda _m: None)
    d.mkdir(parents=True, exist_ok=True)
    for name, (remote, want) in MODEL_FILES.items():
        dest = d / name
        if dest.is_file():
            continue
        tmp = d / f".{name}.{os.getpid()}.part"
        say(f"Downloading {name} (one time, from huggingface.co)...")
        try:
            with urllib.request.urlopen(base + remote, timeout=60) as r, tmp.open("wb") as out:
                while chunk := r.read(1 << 20):
                    out.write(chunk)
            got = _sha256(tmp)
            if got != want:
                raise EmbeddingError(f"{name} failed its integrity check (sha256 {got[:12]}..., "
                                     f"expected {want[:12]}...). Nothing was kept; try again.")
            os.replace(tmp, dest)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(
                f"could not download {name} for the embedding model: {exc}. Reverie needs it once "
                f"(about 90 MB, kept in {d}). Check your connection and run `reverie install` again."
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)
    return d


class _OnnxBackend:
    def __init__(self, directory: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._tok = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self._tok.enable_truncation(max_length=MAX_SEQ_LEN)
        self._tok.enable_padding(pad_id=0, pad_token="[PAD]")
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self._sess = ort.InferenceSession(str(directory / "model.onnx"), opts,
                                          providers=["CPUExecutionProvider"])
        self._names = {i.name for i in self._sess.get_inputs()}

    def encode(self, texts: list[str]) -> np.ndarray:
        enc = self._tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._names:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self._sess.run(None, feed)[0]
        m = mask[..., None].astype(np.float32)
        pooled = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)         # mean pooling
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        return cast(np.ndarray, (pooled / np.clip(norm, 1e-12, None)).astype(np.float32))


class _TorchBackend:
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = cast(Any, self._model).encode(texts, convert_to_numpy=True,
                                             normalize_embeddings=True)
        return np.asarray(vecs, dtype=np.float32)


def _backend_choice() -> str:
    return os.environ.get("REVERIE_EMBEDDING_BACKEND", "").strip().lower()


class EmbeddingModel:
    """Lazy-loading singleton wrapper: ONNX by default, sentence-transformers as the fallback."""

    def __init__(self, model_name: str | None = None, dimensions: int | None = None):
        cfg = get_config()
        self.model_name = model_name or cfg.embedding.model
        self.dimensions = dimensions or cfg.embedding.dimensions
        self._model: _OnnxBackend | _TorchBackend | None = None  # loaded on first use

    def _load(self):
        if self._model is not None:
            return
        want = _backend_choice()
        if want != "torch":
            try:
                import onnxruntime  # noqa: F401
                import tokenizers  # noqa: F401
            except ImportError as exc:
                if want == "onnx":
                    raise EmbeddingError(f"the onnx backend needs onnxruntime and tokenizers: {exc}") from exc
            else:
                directory = ensure_model()
                try:
                    self._model = _OnnxBackend(directory)
                except Exception:
                    # a truncated or damaged file: drop the model files and fetch them once more
                    for name in MODEL_FILES:
                        (directory / name).unlink(missing_ok=True)
                    directory = ensure_model()
                    try:
                        self._model = _OnnxBackend(directory)
                    except Exception as exc:
                        raise EmbeddingError(
                            f"Failed to load the embedding model from {directory}: {exc}. "
                            "Run `reverie install` to try again.") from exc
                return
        try:
            self._model = _TorchBackend(self.model_name)
        except ImportError as exc:
            raise EmbeddingError(
                "no embedding backend is installed. Install Reverie's dependencies "
                "(pip install reverie-mem) — embeddings are required for all operations."
            ) from exc
        except Exception as exc:
            raise EmbeddingError(
                f"Failed to load embedding model {self.model_name!r}: {exc}. "
                "Reverie requires this model for conclusion storage and retrieval."
            ) from exc

    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode texts to L2-normalized float32 vectors, shape (N, 384)."""
        if not texts:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        self._load()
        return cast(Any, self._model).encode(texts)

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
