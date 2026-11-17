"""Phase 3 — docs/CLI.md completeness.

Every subcommand and flag the CLI parser knows must be documented in
docs/CLI.md. Fails listing exactly what's missing.
"""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import argparse
from pathlib import Path

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


def _section(name: str) -> str:
    """The docs/CLI.md text under ``## reverie <name>`` (and ``## reverie <name>: ...``
    sub-sections), each up to the next ``## `` heading."""
    out = []
    for part in CLI_DOC.split("\n## "):
        head = (part.splitlines() or [""])[0].strip().lstrip("# ")
        if head == f"reverie {name}" or head.startswith(f"reverie {name}:"):
            out.append(part)
    return "\n".join(out)


def test_each_flag_is_documented_under_its_own_subcommand():
    """Run 2 bug: --why/--paths/--scope were registered on the `stale` parser but
    documented under `context`, and the global check above passed. A flag must
    appear in the section of the subcommand whose parser defines it."""
    wrong = []
    for name, flags in _subcommands(cli._build_parser()).items():
        text = _section(name)
        wrong += [f"reverie {name} {f}" for f in flags if f != "--repo" and f not in text]
    assert wrong == [], f"flags missing from their own subcommand's section: {wrong}"


def test_every_documented_context_flag_exists_on_the_real_parser():
    """The other direction, and an actual parse: every flag the context section
    shows must be accepted by `reverie context`, and `context add` must parse."""
    import re
    subs = _subcommands(cli._build_parser())
    shown = set(re.findall(r"`?(--[a-z][a-z-]+)", _section("context")))
    unknown = sorted(f for f in shown if f not in subs["context"])
    assert unknown == [], f"documented under `reverie context` but not on its parser: {unknown}"
    ns = cli._build_parser().parse_args(
        ["context", "add", "a rule", "--layer", "team", "--scope", "t", "--paths", "src/**",
         "--why", "because", "--locked", "--tag", "x", "--doc", "d", "--guidance"])
    assert ns.scope == "t" and ns.paths == "src/**" and ns.why == "because"
