"""The opencode plugin Reverie installs (plain JS, no npm dependencies).

Hooks used (verified on opencode 1.18.35, interactive and ``opencode run``):
``chat.message`` (the user's prompt), ``event`` -> ``session.idle`` (the turn
is over) and ``experimental.chat.system.transform`` (put Reverie's rules into
the system prompt: the deterministic read path). MCP stays the fallback.
"""

from __future__ import annotations

import json

PLUGIN_NAME = "reverie.js"
MARKER = "// reverie-managed: do not edit (rewritten by `reverie install`)"

_TEMPLATE = r"""MARKER
import { spawn, spawnSync } from "node:child_process"

const ARGV = __ARGV__
const ENV = __ENV__
const env = () => ({ ...process.env, ...ENV })

const fire = (cmd, payload) => {
  try {
    const p = spawn(ARGV[0], [...ARGV.slice(1), cmd], {
      detached: true, stdio: ["pipe", "ignore", "ignore"], env: env() })
    p.on("error", () => {})
    p.stdin.on("error", () => {})
    p.stdin.end(JSON.stringify(payload))
    p.unref()
  } catch (_) {}
}

const startBlock = (cwd) => {
  try {
    const r = spawnSync(ARGV[0], [...ARGV.slice(1), "start"], {
      input: JSON.stringify({ cwd }), env: env(), encoding: "utf8", timeout: 8000 })
    return r.status === 0 ? (r.stdout || "").trim() : ""
  } catch (_) { return "" }
}

export const Reverie = async ({ directory }) => {
  let block = null
  return {
    "chat.message": async (input, output) => {
      const text = (output?.parts || []).filter((p) => p.type === "text" && !p.synthetic)
        .map((p) => p.text).join("\n")
      if (text.trim()) fire("prompt", { cwd: directory, session: input?.sessionID, text })
    },
    event: async ({ event }) => {
      if (event?.type === "session.idle")
        fire("idle", { cwd: directory, session: event.properties?.sessionID })
    },
    "experimental.chat.system.transform": async (_input, output) => {
      if (block === null) block = startBlock(directory)
      if (block) output.system.push(block)
    },
  }
}
"""


def render(argv: list[str], env: dict[str, str]) -> str:
    return (_TEMPLATE.replace("MARKER", MARKER)
            .replace("__ARGV__", json.dumps(argv))
            .replace("__ENV__", json.dumps(env)))
