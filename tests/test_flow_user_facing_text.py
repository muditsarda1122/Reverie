"""Smoke-group-5 tests — user-facing text says "conclusion" (items 24, 40).

- 24: user-facing text (CLI, TUI, MCP, agent instructions) says "conclusion";
      "candidate" is only for pending review items.
- 40: no "EC" / "ECU" words in user-facing strings. Only identifiers, the
      research papers, and EC-Bench keep them.

The scan walks every string constant in ``reverie/*.py`` (docstrings are
internal documentation, not user-facing text) and fails on the words:
  - ``ECU`` / ``ECUs`` (uppercase — the internal record name)
  - ``EC`` (uppercase, standalone — "EC Review Gate")
  - ``ec`` (lowercase, standalone — "no ec entry")

Two justified exceptions:
  - Relationship-classifier prompts use ``ECU_A`` / ``ECU_B`` — those are the
    JSON field names (``ecu_a`` / ``ecu_b``) the model must emit; renaming
    the prompt labels would desynchronise them from the schema.
  - ``run_ecbench.py`` is EC-Bench tooling; EC-Bench keeps its name, and its
    subset identifiers ("ec", "baseline") are CLI values.

Run: .venv/bin/python -m pytest tests/test_flow_user_facing_text.py -v
Offline: pure source scan, no LLM, no DB.
"""

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = REPO_ROOT / "reverie"
EC_WORD = re.compile(r"\bECUs?\b|\bEC\b(?!-Bench)|\bec\b")
SKIP_FILES = {"run_ecbench.py"}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            ids.add(id(node.body[0].value))
    return ids


def _scan(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_ids(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings or "ECU_A" in node.value:
            continue
        for match in EC_WORD.finditer(node.value):
            context = node.value[max(0, match.start() - 30):match.end() + 30]
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                f"{match.group(0)!r} in {context!r}")
    return offenders


def test_no_ec_words_in_reverie_strings():
    """Item 40 regressed here if this fails — fix the string, not the test."""
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name in SKIP_FILES:
            continue
        offenders.extend(_scan(path))
    assert not offenders, "user-facing EC/ECU words:\n" + "\n".join(offenders)
