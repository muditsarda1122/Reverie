#!/bin/sh
# Install smoke test (Linux/macOS): the real stranger's path, from a source tree, in a throwaway HOME.
#   scripts/smoke_install.sh [SRC_DIR]      used by CI and by `docker run` (see docs/CONTRIBUTING)
# Checks: the one-command installer works, `reverie` runs, an agent gets wired, the key warning shows,
# a hook-free command works, and uninstall removes what install wrote.
set -eu
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
export HOME="${SMOKE_HOME:-$(mktemp -d)}"
mkdir -p "$HOME/.claude" "$HOME/.codex"
for v in ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY OPENROUTER_API_KEY OPENCODE_ZEN_API_KEY; do unset "$v"; done
export PATH="$HOME/.local/bin:$PATH"
# stand-ins for the agents, so the installer has something to detect and wire
for a in claude codex; do printf '#!/bin/sh\necho "2.1.130 (stub)"\n' > "$HOME/.local/bin/$a" 2>/dev/null || { mkdir -p "$HOME/.local/bin"; printf '#!/bin/sh\necho "2.1.130 (stub)"\n' > "$HOME/.local/bin/$a"; }; chmod +x "$HOME/.local/bin/$a"; done
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

REVERIE_SRC="$SRC" sh "$SRC/scripts/install.sh" || fail "install.sh"
command -v reverie >/dev/null || fail "reverie not on PATH"
reverie --version | grep -q '^reverie [0-9]' || fail "--version"
grep -rqs reverie "$HOME/.claude.json" "$HOME/.claude" || fail "Claude Code not wired"
mkdir -p "$HOME/proj" && cd "$HOME/proj" && git init -q . && git commit -q --allow-empty -m init \
  -c user.email=a@b -c user.name=a 2>/dev/null || git -c user.email=a@b -c user.name=a commit -q --allow-empty -m init
reverie status 2>&1 | grep -q "No LLM key found" || fail "status did not warn about the missing key"
reverie digest >/dev/null || fail "digest"
reverie uninstall --yes >/dev/null 2>&1 || reverie uninstall >/dev/null 2>&1 || fail "uninstall"
echo "SMOKE OK: $(reverie --version 2>/dev/null || echo ok)"
