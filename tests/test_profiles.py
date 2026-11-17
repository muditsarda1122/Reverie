import pytest

from reverie import install, profiles
from reverie.install import InstallError
from reverie.integrations import INTEGRATIONS


def _claude(tmp, names):
    for n in names:
        d = tmp / n
        d.mkdir(parents=True)
        (d / "settings.json").write_text("{}")


def test_discover_lists_every_profile_and_marks_the_active_one(tmp_path):
    _claude(tmp_path, [".claude", ".claude-work"])
    (tmp_path / ".codex").mkdir()
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-work"), "CODEX_HOME": str(tmp_path / "cx")}
    found = profiles.discover(tmp_path, env)
    cl = found["claude_code"]
    assert {p.path.name for p in cl} == {".claude", ".claude-work"}
    assert [p.path.name for p in cl if p.active] == [".claude-work"]
    assert [p.path.name for p in found["codex"]] == ["cx", ".codex"] and found["codex"][0].active
    assert found["opencode"] == [] and found["cursor"] == []


def _selected(*keys):
    return [a for a in INTEGRATIONS if a.key in keys]


def test_several_profiles_and_no_pointer_asks_or_refuses_never_guesses(tmp_path):
    _claude(tmp_path, [".claude", ".claude-work"])
    with pytest.raises(InstallError, match="--profile claude_code=PATH"):
        install._choose_profiles(_selected("claude_code"), tmp_path, {}, None, False,
                                 input, print)
    asked = []
    out = install._choose_profiles(_selected("claude_code"), tmp_path, {}, None, True,
                                   lambda q: asked.append(q) or "2", lambda *_: None)
    assert asked and out["CLAUDE_CONFIG_DIR"].endswith((".claude", ".claude-work"))
    with pytest.raises(InstallError, match="no valid choice"):
        install._choose_profiles(_selected("claude_code"), tmp_path, {}, None, True,
                                 lambda q: "9", lambda *_: None)


def test_explicit_profile_and_active_profile_win(tmp_path):
    _claude(tmp_path, [".claude", ".claude-work"])
    out = install._choose_profiles(_selected("claude_code"), tmp_path, {}, [
        f"claude_code={tmp_path / '.claude-work'}"], False, input, print)
    assert out == {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-work")}
    said = []
    out = install._choose_profiles(
        _selected("claude_code"), tmp_path, {"CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-work")},
        None, False, input, said.append)
    assert out == {} and "Also found" in said[0]          # env already decides; others are listed
    with pytest.raises(InstallError, match="KEY=PATH"):
        install._choose_profiles(_selected("claude_code"), tmp_path, {}, ["bogus"], False, input, print)


def test_codex_and_opencode_honour_their_relocated_config_dirs(tmp_path, monkeypatch):
    """Regression class of the CLAUDE_CONFIG_DIR bug: Codex/opencode read CODEX_HOME /
    XDG_CONFIG_HOME, so Reverie must write there, not to ~/.codex."""
    from reverie.integrations.codex import INTEGRATION as CX
    from reverie.integrations.opencode import INTEGRATION as OC
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cxhome"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    home = tmp_path / "h"
    cx = [w.path for w in CX.install(home, reverie_home=tmp_path / "rh")]
    oc = [w.path for w in OC.install(home, reverie_home=tmp_path / "rh")]
    assert (tmp_path / "cxhome" / "config.toml") in cx and (tmp_path / "cxhome" / "AGENTS.md") in cx
    assert not any(str(p).startswith(str(home / ".codex")) for p in cx)
    assert (tmp_path / "xdg" / "opencode" / "opencode.json") in oc
    assert (tmp_path / "xdg" / "opencode" / "plugin" / "reverie.js") in oc
