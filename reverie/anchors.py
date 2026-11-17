"""Ground an item's anchor in the actual code, at capture time.

The extractor proposes a file and some identifiers; a model can invent both,
and an item with no real anchor can never be re-checked by the staleness
worker. So after extraction:

1. identifiers are collected from the item text (backticked, snake_case,
   CamelCase, dotted, file-like) plus the model's proposals;
2. each is searched in the repository (``git grep``): a proposed file must
   exist, a symbol must be defined somewhere;
3. the anchor keeps only what was found and gains the defining files.

An item that mentions nothing findable stays repo-level (honest: unanchored).
"""

from __future__ import annotations

import re
from pathlib import Path

from . import spool

MAX_IDENTS = 10
MAX_FILES = 3
MAX_SYMBOLS = 6
_BACKTICK = re.compile(r"`([^`\n]{2,80})`")
_SNAKE = re.compile(r"\b_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL = re.compile(r"\b_?[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_FILE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|c|h|cc|cpp|md|toml|ya?ml|json|sh))\b")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,60}$")
_DEF = "(def|class|function|func|fn|struct|interface|type|enum|trait|const|let|var)"
_COMMON = {"the", "and", "for", "not", "self", "none", "true", "false", "test", "tests"}


def _git_files(root: str) -> set[str]:
    out = spool._git(["ls-files"], root, timeout=10.0)
    return set(out.splitlines())


def candidates(text: str) -> tuple[list[str], list[str]]:
    """-> (file-like tokens, identifier tokens) mentioned in ``text``."""
    files = [m for m in _FILE.findall(text)]
    idents: list[str] = []
    for tok in _BACKTICK.findall(text):
        tok = tok.strip().rstrip("()")
        if _FILE.fullmatch(tok):
            files.append(tok)
            continue
        for part in re.split(r"[.:]", tok):
            if _IDENT.match(part):
                idents.append(part)
    idents += _SNAKE.findall(text) + _CAMEL.findall(text)
    ordered = [i for i in dict.fromkeys(idents) if i.lower() not in _COMMON]
    return list(dict.fromkeys(files)), ordered


def _defined_in(root: str, name: str) -> list[str]:
    """Files where ``name`` is defined (def/class/assignment), best first."""
    # POSIX ERE (git grep -E on macOS has no \s, \b or (?:...))
    pat = (rf"{_DEF}[[:space:]]+{name}([^A-Za-z0-9_]|$)"
           rf"|^[[:space:]]*{name}[[:space:]]*[:=]")
    out = spool._git(["grep", "-l", "-E", "-e", pat, "--", "."], root, timeout=8.0)
    return out.splitlines()


def ground(text: str, repo_path: str | Path | None, files: list[str] | None = None,
           symbols: list[str] | None = None) -> dict:
    """-> ``{"files": [...], "symbols": [...], "grounded": bool}``.

    Without a repository the proposal is returned unchanged (``grounded`` False)."""
    files = list(files or [])
    symbols = list(symbols or [])
    if not repo_path:
        return {"files": files, "symbols": symbols, "grounded": False}
    root = spool.repo_root(repo_path)
    tracked = _git_files(root)
    if not tracked:
        return {"files": files, "symbols": symbols, "grounded": False}
    t_files, t_idents = candidates(text)
    out_files: list[str] = []
    out_syms: list[str] = []

    def add_file(f: str) -> None:
        if f in tracked and f not in out_files and len(out_files) < MAX_FILES:
            out_files.append(f)

    for f in files + t_files:
        f = f.lstrip("./")
        if f in tracked:
            add_file(f)
        else:                                       # bare name: unique suffix match
            hits = [t for t in tracked if t.endswith("/" + f) or t == f]
            if len(hits) == 1:
                add_file(hits[0])
    for name in (symbols + t_idents)[:MAX_IDENTS * 2]:
        if name in out_syms or len(out_syms) >= MAX_SYMBOLS or not _IDENT.match(name):
            continue
        where = _defined_in(root, name)
        if not where:
            continue                                # invented or not code: dropped
        out_syms.append(name)
        if len(where) <= 2:                         # a generic name defined everywhere
            for f in where:                         # says nothing about one file
                add_file(f)
    return {"files": out_files, "symbols": out_syms, "grounded": True}
