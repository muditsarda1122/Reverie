"""Where a memory item may come from.

An item is evidence-backed only when its supporting quote appears in

* what the USER said (``user``), or
* the real change to the working tree (``diff``: ``git diff HEAD`` plus new
  untracked text files).

What the agent says about its own work (its final answer, its summary, its
reasoning) is narration, never a source: the same model that wrote the
narration would be vouching for itself, and measured runs filled the memory with
"tests were added but not run yet" and "the user should rotate the key".
* real tool output the agent saw (``tool``): the test run, the failing command, the
  error text. It can support FACTS about the environment, a decision's reason, a negative
  result; it can never state a rule or a preference (those come from the user), because tool
  output can carry text written by anyone (a web page, a file, a log).
"""

from __future__ import annotations

import os
from pathlib import Path

from . import spool
from .redact import redact

DIFF_CAP = 40_000          # chars of diff given to the extractor
NEW_FILE_CAP = 4_000       # chars read from each new file
NEW_FILES_MAX = 8
TOOL_CAP = 12_000          # chars of tool output given to the extractor (head and tail)


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


def tools_enabled() -> bool:
    return os.environ.get("REVERIE_PROVENANCE_TOOLS", "1") != "0"


def tool_text(raw: str) -> str:
    """Redacted, bounded tool output (the head and the tail, where errors usually are).
    '' when tool results are switched off. Never raises."""
    try:
        if not raw or not tools_enabled():
            return ""
        text = redact(raw)
        if len(text) > TOOL_CAP:
            half = TOOL_CAP // 2
            text = text[:half] + "\n[... output truncated ...]\n" + text[-half:]
        return text
    except Exception:
        return ""


def evidence_text(interaction: dict) -> str:
    """The only text an agent-inferred item's evidence quote may come from."""
    keys = ("prompt", "diff") + (("tool_results",) if tools_enabled() else ())
    return "\n".join(str(interaction.get(k) or "") for k in keys)


def is_enabled() -> bool:
    return os.environ.get("REVERIE_PROVENANCE", "1") != "0"
