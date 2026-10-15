"""Phase 3 — docs/CLI.md completeness.

Every subcommand and flag the CLI parser knows must be documented in
docs/CLI.md. Fails listing exactly what's missing.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import argparse
from pathlib import Path

import pytest

from reverie import cli
from reverie.review_cli import build_parser as review_parser

ROOT = Path(__file__).resolve().parent.parent
CLI_DOC = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")


def _flags(parser: argparse.ArgumentParser) -> list[str]:
    """Every option string the parser defines (long forms)."""
    out = []
    for action in parser._actions:
        if action.dest in ("help",):
            continue
        for opt in action.option_strings:
            if opt.startswith("--"):
                out.append(opt.split("=")[0])
    return sorted(set(out))


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, list[str]]:
    """subcommand name → its flags (from the CLI's subparser registry)."""
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    result: dict[str, list[str]] = {}
    for name, subparser in sub.choices.items():
        result[name] = _flags(subparser)
    return result


def test_every_subcommand_is_documented():
    subs = _subcommands(cli._build_parser())
    missing = [name for name in subs
               if f"## reverie {name}" not in CLI_DOC
               and not (name == "install" and "## reverie install" in CLI_DOC)]
    assert missing == [], f"subcommands missing from docs/CLI.md: {missing}"


def test_every_flag_is_documented():
    subs = dict(_subcommands(cli._build_parser()))
    subs["review"] = _flags(review_parser())     # review parses its own flags
    missing = []
    for name, flags in subs.items():
        for flag in flags:
            # a flag counts as documented when it appears anywhere in the
            # doc (flags are shared across subcommands, e.g. --repo)
            if flag not in CLI_DOC:
                missing.append(f"reverie {name} {flag}")
    assert missing == [], f"flags missing from docs/CLI.md: {missing}"


def test_removed_commands_are_not_documented_as_current():
    """ec-start / ec-stop / ec-status / ec-retry / ec-repair are gone; the
    reference must not present them as commands (history may mention them)."""
    for line in CLI_DOC.splitlines():
        if line.startswith("## "):
            for gone in ("ec-start", "ec-stop", "ec-status", "ec-retry",
                         "ec-repair"):
                assert gone not in line, f"stale command heading: {line!r}"
