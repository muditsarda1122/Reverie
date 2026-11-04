"""Where a memory item may come from.

An item is evidence-backed only when its supporting quote appears in

* what the USER said (``user``), or
* the real change to the working tree (``diff``: ``git diff HEAD`` plus new
  untracked text files).

What the agent says about its own work (its final answer, its summary, its
reasoning) is narration, never a source: the same model that wrote the
narration would be vouching for itself, and measured runs filled the memory with
"tests were added but not run yet" and "the user should rotate the key".
Tool results are the next source to add (they live in the transcripts).
"""

from __future__ import annotations

import os
from pathlib import Path

from . import spool
from .redact import redact

DIFF_CAP = 40_000          # chars of diff given to the extractor
NEW_FILE_CAP = 4_000       # chars read from each new file
NEW_FILES_MAX = 8


def working_diff(repo_path: str | Path | None) -> str:
    """Redacted ``git diff HEAD`` plus the head of each new untracked text
    file. '' when there is no git repo or no change. Never raises."""
    try:
        if not repo_path:
            return ""
        root = spool.repo_root(repo_path)
        parts = [spool._git(["diff", "HEAD", "--no-color", "-U2"], root, timeout=15.0)]
        new = spool._git(["ls-files", "--others", "--exclude-standard"], root)
        for rel in new.splitlines()[:NEW_FILES_MAX]:
            path = Path(root) / rel
            try:
                if not path.is_file() or path.stat().st_size > 200_000:
                    continue
                raw = path.read_bytes()[:NEW_FILE_CAP]
                if b"\0" in raw:
                    continue
                parts.append(f"+++ new file: {rel}\n" + raw.decode("utf-8", errors="replace"))
            except OSError:
                continue
        text = "\n".join(p for p in parts if p)
        return redact(text)[:DIFF_CAP]
    except Exception:
        return ""


def evidence_text(interaction: dict) -> str:
    """The only text an agent-inferred item's evidence quote may come from."""
    return "\n".join(str(interaction.get(k) or "") for k in ("prompt", "diff"))


def is_enabled() -> bool:
    return os.environ.get("REVERIE_PROVENANCE", "1") != "0"
