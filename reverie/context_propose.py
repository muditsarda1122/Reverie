"""The promotion path: a rule one person taught their agent, proposed into a team
document. No model call, no new store: it reuses ``context.add`` (an owner of the target
writes directly, anyone else writes a ``proposals/`` file) and ``context accept``/``reject``.

Nothing leaves the personal scope until the user runs ``reverie context propose``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import context, redact, rules


@dataclass
class Source:
    text: str
    origin: str
    meta: dict[str, str]


@dataclass
class Proposed:
    result: context.AddResult
    source: Source
    prior_rejection: dict | None = None


_IDISH = re.compile(r"^[0-9a-f][0-9a-f-]{3,39}$")


def _memory_item(ref: str) -> Source | None:
    """A stored rule the user stated, by id prefix. Agent-inferred items are not rules
    a user taught, so they cannot be promoted."""
    from . import deliver, items
    if not deliver.db_path().exists():
        return None
    conn = items.connect(deliver.db_path())
    try:
        it = items.get(conn, ref)
    finally:
        conn.close()
    if not it:
        return None
    if it.get("kind") not in ("rule", "preference") or it.get("status") != "active":
        raise ValueError(f"[{it['id'][:8]}] is a {it.get('kind')}, not an active rule")
    if it.get("source") != "user_said":
        raise ValueError(f"[{it['id'][:8]}] was inferred by an agent, not stated by you; "
                         f"state it as a rule first (`reverie rules add`)")
    meta = {"from": f"memory item {it['id'][:8]}",
            "first-stated": str(it.get("created_at", ""))[:10]}
    use = (it.get("meta") or {}).get("usage") or {}
    if use:
        meta["usage"] = ", ".join(f"{k.lower()}={v}" for k, v in sorted(use.items()))
    return Source(it["text"], f"memory item {it['id'][:8]}", meta)


def find_source(root: str, ref: str) -> Source:
    """Resolve what the user pointed at: a personal context rule id, a bullet of
    ``~/.reverie/rules.md`` (exact text or a unique fragment), a memory item id, or
    literal text."""
    ref = " ".join(ref.split())
    if not ref:
        raise ValueError("say which rule: an id, or its text")
    for it in context.load_items(root):
        if it.layer == "personal" and it.kind == "rule" and it.id.startswith(ref):
            return Source(it.text, f"personal context rule {it.id}", {"from": f"personal rule {it.id}"})
    bullets = rules.bullets(rules.personal_path().read_text(encoding="utf-8")
                            if rules.personal_path().exists() else "")
    exact = [b for b in bullets if context.norm(b) == context.norm(ref)]
    frag = [b for b in bullets if context.norm(ref) in context.norm(b)]
    hit = exact or (frag if len(frag) == 1 else [])
    if hit:
        return Source(hit[0], "your rules.md", {"from": "personal rules.md"})
    if len(frag) > 1:
        raise ValueError(f"{len(frag)} of your rules match {ref!r}; use more of the text")
    mem = _memory_item(ref) if _IDISH.match(ref) else None
    if mem:
        return mem
    if _IDISH.match(ref):
        raise ValueError(f"no rule of yours has the id {ref!r}")
    return Source(ref, "typed", {"from": "typed at the command line"})


def propose(root: str, ref: str, *, layer: str = "team", scope: str = "",
            paths: tuple[str, ...] = (), why: str = "", again: bool = False,
            doc: str = "") -> Proposed:
    if layer == "personal":
        raise ValueError("propose moves a rule OUT of your personal scope: "
                         "use --layer team, org or repo")
    src = find_source(root, ref)
    if redact.contains_secret(src.text):
        raise ValueError("this rule looks like it contains a secret; it was not proposed. "
                         "Rewrite it without the value and try again.")
    context.target_folder(root, layer, scope)          # fails early on a bad layer/scope
    target = norm = context.norm(src.text)
    for it in context.load_items(root):
        if it.kind == "rule" and it.layer != "personal" and context.norm(it.text) == norm:
            raise ValueError(f"already a rule in {it.label} ({Path(it.file).name}); "
                             f"nothing to propose")
    for p in context.proposals(root):
        if context.norm(context.bullet_text(p["bullet"])) == target:
            raise ValueError(f"already proposed ({p['slug']}); an owner can accept it")
    prior = next((r for r in context.rejected(root)
                  if context.norm(context.bullet_text(r["bullet"])) == target), None)
    if prior and not again:
        raise ValueError(
            f"this rule was rejected on {prior.get('rejected-on', '?')} by "
            f"{prior.get('rejected-by', '?')}: {prior.get('reason', '(no reason given)')}\n"
            f"Not proposing it again. If the situation changed, say why with "
            f"`--again --why \"...\"`.")
    if again and prior and not " ".join(why.split()):
        raise ValueError("re-proposing a rejected rule needs --why: what changed?")
    prov = dict(src.meta)
    prov["proposed-via"] = "reverie context propose"
    if prior:
        prov["re-proposal-of"] = str(prior["slug"])
    added = context.add(root, src.text, layer=layer, scope=scope, paths=paths,
                        why=why or "taught to my agent and found useful", doc=doc,
                        provenance=prov)
    return Proposed(added, src, prior)
