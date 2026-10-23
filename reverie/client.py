"""Process-wide client identity.

The launch command written by each integration carries ``--client <key>``.
Both entry points accept it and store it here so logs (and, later,
client-specific wording) can use it. No behaviour depends on it yet.
"""

from __future__ import annotations

CLIENT: str | None = None


def set_client(client: str | None) -> None:
    global CLIENT
    CLIENT = client or None


def get_client() -> str | None:
    return CLIENT
