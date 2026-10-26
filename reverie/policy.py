"""The single usage policy for Reverie's commands.

This text is the source for every agent-facing usage section: the MCP server
instructions and the AGENTS.md written by the installer. Keep it harness-free.
"""

#: The reconsolidate trigger, shared verbatim by USAGE_POLICY (the
#: ``reconsolidate`` bullet) and the MCP tool description, so the two
#: cannot drift apart.
RECONSOLIDATE_TRIGGER = (
    "when the code you are reading contradicts a conclusion you retrieved. "
    "Name the file and symbol that show the contradiction. Do not call it "
    "for conclusions you did not retrieve in this task."
)

USAGE_POLICY = f"""\
## When to call each command

- `query`: before a non-trivial decision between approaches, when debugging \
has no obvious cause, and before an architectural change. Verify grounding \
references against the code before acting. Memory is a past belief, not an \
authority.
- Before editing code in a Reverie-enabled repo, call query for the area \
you are changing, even when the change looks small.
- `observe`: after finding a non-obvious invariant, a root cause, an \
architectural decision, or a disproved assumption. AT MOST ONCE per task; \
not for simple changes.
- `reconsolidate`: call {RECONSOLIDATE_TRIGGER}
- `summary`: once at session start, for counts and topics only. Never anchor \
on conclusion contents.
- `review_list` and `review_decide`: show every candidate in full. Relay only \
decisions the user states.
- `set_repo_state`: only from the user's answer to the first-time question. \
Ask once per conversation.
"""
