#!/usr/bin/env python3
"""Every internal link on the saved live pages must answer 200 on the running server (no 404, no 500).

    python3 scripts/crawl-links.py PORT FILE.html ...

Follows only links that start with "/" (GET, no writes), once each. A link that is meant to 404 (the not-found demos)
is listed in INTENDED. Exit status 1 when anything is broken.
"""
from __future__ import annotations

import html
import re
import sys
import urllib.error
import urllib.request

INTENDED = ("nosuchrule", "deadbeef", "nosuchpage")


def main(argv: list[str]) -> int:
    port, files = argv[0], argv[1:]
    seen: dict[str, int] = {}
    bad = 0
    for f in files:
        for href in re.findall(r'href="(/[^"#]*)', open(f, encoding="utf-8").read()):
            href = html.unescape(href)
            if href in seen or any(w in href for w in INTENDED):
                continue
            try:
                code = urllib.request.urlopen(f"http://127.0.0.1:{port}{href}").status
            except urllib.error.HTTPError as e:
                code = e.code
            seen[href] = code
            if code != 200:
                print(f"BROKEN {code} {href} (linked from {f})")
                bad += 1
    kinds = sorted({re.sub(r"\?.*", "", h) for h in seen})
    print(f"{len(seen)} distinct internal links checked ({', '.join(kinds)}), {bad} broken")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
