#!/usr/bin/env python3
"""Compare every link, form, field and anchor id in design/before/<page> with design/after/<page>.

    python3 scripts/check-design-wiring.py [page.html ...]      (default: every page present in design/after)

Reads the HTML with the standard parser, so unquoted attributes (the old pages use them) are seen. For each page it reports
what the old page had that the new one lacks (MISSING), and what the new one adds (added). The only drops allowed are the
Accept and Reject forms of a proposal the viewer cannot decide: the old page showed them marked "Not an owner: the CLI would
refuse", and the brief says never to show an action that will be refused. Those are listed as EXPECTED, and anything else
missing makes the exit status 1.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BEFORE, AFTER = ROOT / "design" / "before", ROOT / "design" / "after"


class Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: Counter = Counter()
        self.forms: list[dict] = []
        self.ids: set[str] = set()
        self.refused: set[str] = set()          # slugs of proposals the old page marked "Not an owner"
        self._form: dict | None = None
        self._article_slugs: list[str] = []
        self._article_text = ""
        self._in_article = False
        self._art_forms: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "a" and a.get("href"):
            self.links[re.sub(r"p=[0-9a-f]{8}", "p=*", a["href"])] += 1   # project ids differ between seeds
        if tag == "article":
            self._in_article, self._article_text, self._art_forms = True, "", []
        if tag == "form":
            self._form = {"action": a.get("action", ""), "method": (a.get("method") or "get").lower(), "fields": [], "slug": ""}
            self.forms.append(self._form)
            self._art_forms.append(self._form)
        if tag in ("input", "textarea", "select", "button") and self._form is not None and a.get("name"):
            self._form["fields"].append((a["name"], a.get("type", tag)))
            if a["name"] == "slug":
                self._form["slug"] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        if tag == "article":
            if "Not an owner" in self._article_text:
                self.refused |= {f["slug"] for f in self._art_forms if f["slug"]}
            self._in_article = False

    def handle_data(self, data):
        if self._in_article:
            self._article_text += data


def scan(path: Path) -> Scan:
    s = Scan()
    s.feed(path.read_text(encoding="utf-8"))
    return s


def form_key(f: dict) -> tuple:
    return (f["action"], f["method"], f["slug"], tuple(sorted(set(n for n, _ in f["fields"]))))


ALIASES = {"overview-arjun.html": "overview.html", "rules-capped.html": "rules.html", "memory-capped.html": "memory.html"}   # a page with no capture of its own, compared with the same screen as mudit


def compare(name: str) -> int:
    b, a = scan(BEFORE / ALIASES.get(name, name)), scan(AFTER / name)
    bad = 0
    print(f"{name}")
    miss_links = b.links - a.links
    for href, n in sorted(miss_links.items()):
        # a link that appeared n times before may legitimately appear fewer times now; only a link that vanished is a loss
        if a.links[href] == 0:
            print(f"  MISSING link  {href}  (x{n})")
            bad += 1
    extra_links = [h for h in a.links if h not in b.links]
    bf, af = {form_key(f) for f in b.forms}, {form_key(f) for f in a.forms}
    for k in sorted(bf - af):
        action, method, slug, fields = k
        if action in ("/do/accept", "/do/reject") and slug in b.refused:
            print(f"  EXPECTED drop {method.upper()} {action} slug={slug[:28]}  (viewer cannot decide it; the old page said \"Not an owner\")")
        else:
            print(f"  MISSING form  {method.upper()} {action} {slug} fields={list(fields)}")
            bad += 1
    for k in sorted(af - bf):
        print(f"  added form    {k[1].upper()} {k[0]} slug={k[2][:28]} fields={list(k[3])}")
    for i in sorted(b.ids - a.ids):
        print(f"  MISSING id    #{i}")
        bad += 1
    if extra_links:
        print(f"  added links   {sorted(extra_links)}")
    ok_links = sum(1 for h in b.links if a.links[h])
    print(f"  links {ok_links}/{len(b.links)} kept, forms {len(bf & af)}/{len(bf)} kept, ids {len(b.ids & a.ids)}/{len(b.ids)} kept")
    return bad


def main() -> int:
    names = sys.argv[1:] or sorted(p.name for p in AFTER.glob("*.html") if (BEFORE / ALIASES.get(p.name, p.name)).exists())
    bad = sum(compare(n) for n in names)
    print("wiring: " + ("every link, form, field and id is kept (expected drops listed)" if not bad else f"{bad} item(s) missing"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
