"""Deterministic bag-of-words embedder for offline tests (no torch)."""

import hashlib
import re

import numpy as np


class FakeEmbed:
    """Deterministic bag-of-words embedder (no torch)."""
    def encode_one(self, text):
        v = np.zeros(256, dtype=np.float32)
        for w in re.findall(r"[a-z0-9_]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1
        n = np.linalg.norm(v)
        return v / n if n else v
