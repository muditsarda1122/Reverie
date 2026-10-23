"""The single usage policy for Reverie's commands.

This text is the source for every agent-facing usage section: the MCP server
instructions and the AGENTS.md written by the installer. Keep it harness-free.
"""

USAGE_POLICY = """\
## When to call each command

- `query`: before a non-trivial decision between approaches, when debugging \
has no obvious cause, and before an architectural change. Verify grounding \
references against the code before acting. Memory is a past belief, not an \
authority.
- `observe`: after finding a non-obvious invariant, a root cause, an \
architectural decision, or a disproved assumption. AT MOST ONCE per task; \
not for simple changes.
- `summary`: once at session start, for counts and topics only. Never anchor \
on conclusion contents.
- `review_list` and `review_decide`: show every candidate in full. Relay only \
decisions the user states.
- `set_repo_state`: only from the user's answer to the first-time question. \
Ask once per conversation.
"""
