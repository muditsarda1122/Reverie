"""`reverie install --trust-codex-hooks`: approve ONLY Reverie's own Codex hooks, via
Codex's app-server API (a fake app-server stands in for `codex`)."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from reverie import install
from reverie.integrations.codex import trust

FAKE = r'''#!{py}
import json, sys, os
home = os.environ["CODEX_HOME"]
state = os.path.join(home, "state.json")
cfg = os.path.join(home, "config.toml")
hooks = [
 {{"key": cfg + ":session_start:0:0", "eventName": "sessionStart", "handlerType": "command",
   "command": "x -m reverie.integrations.codex.shim start", "sourcePath": cfg,
   "currentHash": "sha256:aaa"}},
 {{"key": cfg + ":stop:0:0", "eventName": "stop", "handlerType": "command",
   "command": "/usr/bin/true  # the user's own hook", "sourcePath": cfg,
   "currentHash": "sha256:bbb"}},
]
trusted = json.load(open(state)) if os.path.exists(state) else {{}}
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    method = m["method"]
    if method == "initialize":
        res = {{"codexHome": home}}
    elif method == "hooks/list":
        out = []
        for h in hooks:
            h = dict(h)
            h["trustStatus"] = "trusted" if trusted.get(h["key"]) == h["currentHash"] else "untrusted"
            out.append(h)
        res = {{"data": [{{"cwd": ".", "hooks": out}}]}}
    elif method == "config/batchWrite":
        for e in m["params"]["edits"]:
            key = e["keyPath"].split('hooks.state.', 1)[1].rsplit('.trusted_hash', 1)[0]
            trusted[json.loads(key)] = e["value"]
        json.dump(trusted, open(state, "w"))
        res = {{"status": "ok"}}
    else:
        res = {{}}
    sys.stdout.write(json.dumps({{"id": m["id"], "result": res}}) + "\n"); sys.stdout.flush()
'''


def _fake_codex(tmp_path: Path) -> tuple[str, Path]:
    home = tmp_path / "codex_home"
    home.mkdir()
    (home / "config.toml").write_text("")
    exe = tmp_path / "codex"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe), home


def test_approves_reverie_hooks_only(tmp_path):
    exe, home = _fake_codex(tmp_path)
    report = trust.trust_reverie_hooks(home, tmp_path, codex_bin=exe)
    assert report.ok and len(report.approved) == 1
    assert report.approved[0].endswith("session_start:0:0")
    state = json.loads((home / "state.json").read_text())
    assert list(state) == [report.approved[0]]            # the user's own hook untouched


def test_second_run_is_a_noop(tmp_path):
    exe, home = _fake_codex(tmp_path)
    trust.trust_reverie_hooks(home, tmp_path, codex_bin=exe)
    again = trust.trust_reverie_hooks(home, tmp_path, codex_bin=exe)
    assert again.ok and not again.approved and again.message == "already approved"


def test_status_is_read_only(tmp_path):
    exe, home = _fake_codex(tmp_path)
    status = trust.reverie_hook_status(home, tmp_path, codex_bin=exe)
    assert list(status.values()) == ["untrusted"]
    assert not (home / "state.json").exists()


def test_missing_codex_is_reported_not_raised(tmp_path):
    report = trust.trust_reverie_hooks(tmp_path, tmp_path, codex_bin=str(tmp_path / "nope"))
    assert not report.ok and "codex app-server" in report.message
    assert trust.reverie_hook_status(tmp_path, tmp_path, codex_bin=str(tmp_path / "nope")) is None


def test_hook_from_another_file_is_not_ours(tmp_path):
    cfg = tmp_path / "config.toml"
    hook = {"sourcePath": str(tmp_path / "other.toml"), "handlerType": "command",
            "command": "python -m reverie.integrations.codex.shim start"}
    assert not trust.is_reverie_hook(hook, cfg)
    hook["sourcePath"] = str(cfg)
    assert trust.is_reverie_hook(hook, cfg)
    hook["command"] = "echo hi"
    assert not trust.is_reverie_hook(hook, cfg)


def test_installer_default_and_opt_out(monkeypatch, tmp_path):
    from reverie.integrations.codex import CodexIntegration
    from reverie.integrations.codex import trust as t
    monkeypatch.delenv("REVERIE_NO_CODEX_TRUST", raising=False)
    calls = []
    monkeypatch.setattr(t, "trust_reverie_hooks",
                        lambda home, cwd=None, codex_bin="codex":
                        calls.append(home) or t.TrustReport(True, approved=["k"]))
    out: list[str] = []
    assert install._approve_hooks(CodexIntegration(), tmp_path, out.append) is True
    assert calls == [tmp_path / ".codex"] and "approved Reverie's 1 hook" in out[0]
    monkeypatch.setattr(t, "trust_reverie_hooks",
                        lambda *a, **k: t.TrustReport(False, message="boom"))
    out.clear()
    assert install._approve_hooks(CodexIntegration(), tmp_path, out.append) is False
    assert "/hooks" in out[0]


def test_cli_flag_defaults_on(monkeypatch):
    from reverie import cli
    seen = {}
    monkeypatch.setattr(install, "run", lambda **kw: seen.update(kw) or 0)
    cli.main(["install", "--dry-run"])
    assert seen["trust_codex_hooks"] is True
    cli.main(["install", "--dry-run", "--no-trust-codex-hooks"])
    assert seen["trust_codex_hooks"] is False
