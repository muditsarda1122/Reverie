#!/bin/sh
# Reverie: one-command install.
#
#   curl -LsSf https://raw.githubusercontent.com/muditsarda1122/Reverie/feat/continuity-spine/scripts/install.sh | sh
#
# What it does, in order (nothing else):
#   1. uses `uv` if you have it, otherwise installs uv for your user (https://astral.sh/uv;
#      uv also provides a suitable Python, so you need neither Python 3.12 nor a venv)
#   2. installs Reverie as an isolated command-line tool (`uv tool install`)
#   3. runs `reverie install`, which wires your coding agents and downloads the embedding model
#
# You then need one LLM API key in your environment (ANTHROPIC_API_KEY, OPENAI_API_KEY,
# GEMINI_API_KEY, OPENROUTER_API_KEY or OPENCODE_ZEN_API_KEY). Reverie uses the one it finds.
#
# Environment overrides:  REVERIE_SRC  what to install (default: the GitHub branch, later PyPI "reverie")
#                         REVERIE_REF  the git ref when installing from GitHub
set -eu

REF="${REVERIE_REF:-feat/continuity-spine}"
SRC="${REVERIE_SRC:-git+https://github.com/muditsarda1122/Reverie.git@${REF}}"

say() { printf '%s\n' "$*"; }
die() { printf 'reverie install: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || case "$SRC" in git+*) die "git is required to install from GitHub (install git, then run this again)";; esac

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (a fast Python installer) for your user..."
  command -v curl >/dev/null 2>&1 || die "curl is required"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "could not install uv; see https://docs.astral.sh/uv/"
  PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH; open a new shell and run this again"
fi

say "Installing Reverie..."
uv tool install --quiet --python 3.12 --force "$SRC" || die "uv could not install Reverie from $SRC"

BIN="$(uv tool dir --bin 2>/dev/null || true)"
[ -n "$BIN" ] && [ -x "$BIN/reverie" ] || BIN="$(dirname "$(command -v reverie 2>/dev/null || echo "$HOME/.local/bin/reverie")")"
[ -x "$BIN/reverie" ] || die "Reverie installed but the 'reverie' command was not found in $BIN"

"$BIN/reverie" install --all </dev/null

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say ""; say "Add this to your shell profile so 'reverie' is on your PATH:  export PATH=\"$BIN:\$PATH\"" ;;
esac
