import io
import json
import sys
import time
from pathlib import Path

from reverie import importers, spool
from reverie.importers import claude
from reverie.integrations.opencode import INTEGRATION, plugin, shim

SECRET = "sk-ant-api03-Qw8ErTy2UiOp4AsDf6GhJk9LzXc1VbNm"


def _feed(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def test_shim_prompt_logs_unquoted_redacted_text(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "r"
    repo.mkdir()
    _feed(monkeypatch, {"cwd": str(repo), "session": "ses_1",
                        "text": json.dumps(f"never retry 429, key {SECRET}")})
    assert shim.main(["prompt"]) == 0
    rec = spool.read(repo)[0]
    assert rec["harness"] == "opencode" and rec["session"] == "ses_1"
    assert rec["text"].startswith("never retry 429") and SECRET not in rec["text"]
    assert not rec["text"].startswith('"')


def test_shim_start_prints_rules(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "rules.md").write_text("- always use hmac.compare_digest\n")
    monkeypatch.setenv("REVERIE_HOME", str(home))
    repo = tmp_path / "r"
    repo.mkdir()
    _feed(monkeypatch, {"cwd": str(repo)})
    assert shim.main(["start"]) == 0
    assert "hmac.compare_digest" in capsys.readouterr().out


def test_shim_never_raises_on_garbage(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert shim.main(["prompt"]) == 0
    assert shim.main(["bogus"]) == 0


def test_installer_writes_and_removes_the_plugin(tmp_path):
    home = tmp_path / "h"
    (home / ".config" / "opencode").mkdir(parents=True)
    writes = INTEGRATION.install(home, reverie_home=tmp_path / "rh")
    plug = [w for w in writes if w.path.name == plugin.PLUGIN_NAME]
    assert len(plug) == 1 and plug[0].path.parent.name == "plugin"
    body = plug[0].content
    assert plugin.MARKER in body and "chat.message" in body and "session.idle" in body
    assert "experimental.chat.system.transform" in body
    assert "reverie.integrations.opencode.shim" in body or "reverie-opencode-hook" in body
    plug[0].path.parent.mkdir(parents=True, exist_ok=True)
    plug[0].path.write_text(body)
    removals = INTEGRATION.uninstall(home, reverie_home=tmp_path / "rh")
    assert any(r.path == plug[0].path and r.action == "delete-file" for r in removals)
    plug[0].path.write_text(body + "// edited by user\n")
    removals = INTEGRATION.uninstall(home, reverie_home=tmp_path / "rh")
    assert any(r.path == plug[0].path and r.action == "skipped" for r in removals)


def test_finalize_closes_the_open_exchange_now(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    repo = tmp_path / "r"
    repo.mkdir()
    d = tmp_path / "claude" / "projects" / importers.slug_of(str(repo))
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": "do it"}},
        {"type": "assistant", "sessionId": "s1", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "done"}]}}]) + "\n")
    queued: list = []
    monkeypatch.setattr(importers, "_extract", lambda c, ex: queued.extend(ex))
    importers.run(repo, adapters_=[claude.ADAPTER], now=time.time())
    assert not queued                                    # fresh file: not final yet
    importers.run(repo, adapters_=[claude.ADAPTER], now=time.time(), finalize=True)
    assert len(queued) == 1 and queued[0]["reply"] == "done"
    assert Path(spool.repo_dir(repo)).exists()
