"""feat/flow — MCP tool names must match the names used in agent-facing text.

Review fix 6/7 (smoke findings): the tools are registered under BARE names —
``observe``, ``query``, ``summary``, ``reconsolidate``, ``set_repo_state``,
``review_list``, ``review_decide``. Harnesses prefix them with the server
name (``reverie``) themselves; a tool registered as ``reverie_observe``
shows up as ``reverie_reverie_observe`` in UI prefixed clients, which is
what the smoke test tripped over.

This test scans every agent-facing string — server instructions, tool
descriptions/schemas, the AGENTS.md written by install, the reference block
and every slash-command body — for tool-name tokens and asserts they all
match the server's tool list (and that no ``reverie_``-prefixed form
survives).

Run: .venv/bin/python -m pytest tests/test_flow_tool_names.py -v
Offline: pure string inspection, no DB.
"""

import json
import re

from reverie import install, mcp_server
from reverie.integrations import (
    CLAUDE_CODE_COMMANDS,
    CODEX_PROMPTS,
    OPENCODE_COMMANDS,
)

EXPECTED_TOOLS = [
    "observe",
    "query",
    "summary",
    "reconsolidate",
    "set_repo_state",
    "review_list",
    "review_decide",
]

#: (reverie_)?<toolname> — the optional prefix is what must never appear.
TOOL_TOKEN_RE = re.compile(
    r"\b(reverie_)?(observe|query|summary|reconsolidate|set_repo_state|"
    r"review_list|review_decide)\b"
)


def _agent_facing_texts() -> dict[str, str]:
    texts = {
        "SERVER_INSTRUCTIONS": mcp_server.SERVER_INSTRUCTIONS,
        "tool definitions": json.dumps(mcp_server.TOOLS),
        "AGENTS.md (install)": install.AGENTS_MD,
        "reference block (install)": install._reference_block(
            "~/.reverie/AGENTS.md"),
    }
    for label, commands in (
        ("claude_code command", CLAUDE_CODE_COMMANDS),
        ("opencode command", OPENCODE_COMMANDS),
        ("codex prompt", CODEX_PROMPTS),
    ):
        for name, body in commands.items():
            texts[f"{label} {name}"] = body
    return texts


def test_server_registers_bare_tool_names():
    assert [tool["name"] for tool in mcp_server.TOOLS] == EXPECTED_TOOLS


def test_no_reverie_prefixed_tool_names_in_agent_facing_text():
    """The old names (``reverie_query`` …) must be gone everywhere."""
    for label, text in _agent_facing_texts().items():
        for match in TOOL_TOKEN_RE.finditer(text):
            assert match.group(1) is None, (
                f"{label}: {match.group(0)!r} is prefixed — the registered "
                f"name is {match.group(2)!r}")


def test_every_tool_name_token_matches_the_tool_list():
    """Any (optionally prefixed) tool-name token found in agent-facing text
    must name a registered tool (the regex is the closed tool vocabulary)."""
    for label, text in _agent_facing_texts().items():
        for match in TOOL_TOKEN_RE.finditer(text):
            assert match.group(2) in EXPECTED_TOOLS, (label, match.group(2))


def test_instructions_point_at_the_registered_names():
    """Spot checks: the exact contract sentences reference the new names."""
    assert "`query`" in mcp_server.SERVER_INSTRUCTIONS
    assert "`observe`" in mcp_server.SERVER_INSTRUCTIONS
    assert "`review_list`" in mcp_server.SERVER_INSTRUCTIONS
    assert "`review_decide`" in mcp_server.SERVER_INSTRUCTIONS
    assert "`set_repo_state`" in mcp_server.SERVER_INSTRUCTIONS
    assert "- observe:" in install.AGENTS_MD
    assert "- review_decide:" in install.AGENTS_MD
