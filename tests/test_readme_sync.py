"""The README must keep up with the product: a new command or harness that is
not mentioned fails this test, so the fix is made in the same change."""

import re
from pathlib import Path

from reverie import cli
from reverie.integrations import INTEGRATIONS

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()
#: plumbing that users never type
INTERNAL = {"hook", "capture-turn"}


def test_every_user_facing_command_is_in_the_readme():
    missing = [c for c in cli.SUBCOMMANDS
               if c not in INTERNAL and f"`reverie {c}" not in README]
    assert not missing, f"README.md is missing commands: {missing} (add them to the Commands table)"


def test_every_supported_harness_is_in_the_readme():
    names = [i.name for i in INTEGRATIONS]
    missing = [n for n in names if n.lower().replace(" cli", "") not in README.lower()]
    assert not missing, f"README.md does not mention harnesses: {missing}"


def test_readme_documents_the_headline_behaviours():
    for word in ("redact", "reverie resume", "transcript", "stale"):
        assert re.search(word, README, re.I), f"README lost its section on {word!r}"
