"""Approve Reverie's own Codex hooks through Codex's own app-server API.

Codex runs a hook only after the user approves it (the same thing ``/hooks``
does). The approval is a ``trusted_hash`` that Codex itself computes for the
hook definition. This module never computes a hash: it asks Codex
(``hooks/list``) for each hook's ``currentHash`` and hands that value back
through ``config/batchWrite``, exactly as the ``/hooks`` screen does.

Scope is deliberately narrow, and the installer only calls this when the user
asked for it (``--trust-codex-hooks``, the default; ``--no-trust-codex-hooks``
leaves approval to the user):
- only hooks whose source is THIS Codex home's ``config.toml``,
- only hooks whose command is Reverie's own shim,
- nothing else is touched. A hook the user wrote is never approved here.

STDLIB ONLY.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["TrustReport", "trust_reverie_hooks", "reverie_hook_status", "is_reverie_hook"]

_MARKERS = ("reverie.integrations.codex.shim", "reverie-codex-hook")
_TIMEOUT = 20.0


@dataclass
class TrustReport:
    ok: bool
    approved: list[str] = field(default_factory=list)      # hook keys newly approved
    already: list[str] = field(default_factory=list)       # were already trusted
    message: str = ""


class _Rpc:
    def __init__(self, codex_home: Path, codex_bin: str = "codex") -> None:
        env = dict(os.environ, CODEX_HOME=str(codex_home))
        self.proc = subprocess.Popen(
            [codex_bin, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env)
        self._id = 0

    def call(self, method: str, params: dict) -> dict:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._id += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed the connection")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                result = msg.get("result")
                return result if isinstance(result, dict) else {}

    def notify(self, method: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def is_reverie_hook(hook: dict, config_path: Path) -> bool:
    """True only for a command hook defined in ``config_path`` that runs Reverie's shim."""
    try:
        same = Path(str(hook.get("sourcePath", ""))).resolve() == config_path.resolve()
    except OSError:
        same = False
    cmd = str(hook.get("command", ""))
    return bool(same and hook.get("handlerType") == "command"
                and any(m in cmd for m in _MARKERS))


def _open(codex_home: Path, codex_bin: str) -> _Rpc:
    rpc = _Rpc(codex_home, codex_bin)
    rpc.call("initialize", {"clientInfo": {"name": "reverie", "version": "0"}})
    rpc.notify("initialized")
    return rpc


def _mine(rpc: _Rpc, codex_home: Path, cwd: Path) -> list[dict]:
    data = rpc.call("hooks/list", {"cwds": [str(cwd)]}).get("data") or []
    cfg = codex_home / "config.toml"
    seen: dict[str, dict] = {}
    for entry in data:
        for hook in entry.get("hooks") or []:
            if is_reverie_hook(hook, cfg):
                seen[str(hook["key"])] = hook
    return list(seen.values())


def reverie_hook_status(codex_home: Path, cwd: Path | None = None,
                        codex_bin: str = "codex") -> dict[str, str] | None:
    """Read-only: ``{hook key: trustStatus}`` for Reverie's hooks, or None when
    Codex's app-server is not available."""
    try:
        rpc = _open(codex_home, codex_bin)
    except (OSError, RuntimeError):
        return None
    try:
        return {str(h["key"]): str(h.get("trustStatus", "")) for h in
                _mine(rpc, codex_home, cwd or Path.cwd())}
    except (OSError, RuntimeError):
        return None
    finally:
        rpc.close()


def trust_reverie_hooks(codex_home: Path, cwd: Path | None = None,
                        codex_bin: str = "codex") -> TrustReport:
    """Approve Reverie's hooks (see module docstring). Never raises."""
    codex_home = Path(codex_home)
    try:
        rpc = _open(codex_home, codex_bin)
    except (OSError, RuntimeError) as exc:
        return TrustReport(False, message=f"could not start `codex app-server` ({exc})")
    try:
        mine = _mine(rpc, codex_home, cwd or Path.cwd())
        if not mine:
            return TrustReport(False, message="Codex lists no Reverie hooks yet "
                               "(is `hooks = true` set under [features]?)")
        todo = [h for h in mine if h.get("trustStatus") != "trusted" and h.get("currentHash")]
        already = [str(h["key"]) for h in mine if h.get("trustStatus") == "trusted"]
        if not todo:
            return TrustReport(True, already=already, message="already approved")
        edits = [{"keyPath": f"hooks.state.{json.dumps(str(h['key']))}.trusted_hash",
                  "value": h["currentHash"], "mergeStrategy": "upsert"} for h in todo]
        rpc.call("config/batchWrite", {"edits": edits,
                                       "filePath": str(codex_home / "config.toml")})
        after = {str(h["key"]): h.get("trustStatus") for h in _mine(
            rpc, codex_home, cwd or Path.cwd())}
        bad = [str(h["key"]) for h in todo if after.get(str(h["key"])) != "trusted"]
        if bad:
            return TrustReport(False, already=already, message=(
                "wrote the approval but Codex does not report these hooks as trusted: "
                + ", ".join(k.rsplit(":", 3)[-3] for k in bad)))
        return TrustReport(True, approved=[str(h["key"]) for h in todo], already=already)
    except (OSError, RuntimeError) as exc:
        return TrustReport(False, message=str(exc))
    finally:
        rpc.close()
