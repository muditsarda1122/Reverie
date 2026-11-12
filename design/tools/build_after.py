#!/usr/bin/env python3
"""Builds design/after/*.html (the Proposals screen, redesigned) as static pages.

The data is the seed in design/before/ (see design/before/README.md). Nothing here is a product
feature: every field used below is one the proposal files, `context.proposals()` and `context.rejected()`
already carry (see design/after/README.md for the few the old page did not print).

    python3 design/tools/build_after.py
"""
from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "after"

TOKEN = "B7QFUmwMJf_-KGzCzyaLWeSOsVgFJSzsyN_CXIj9G5E"   # the one-off CSRF value in design/before (kept so the fields match)
API, NOTIF, LEGACY = "4e1e91cd", "81071af5", "4cdea88b"

PEOPLE = {
    "mudit": ("Mudit Sarda", "mudit@payments.example"),
    "priya": ("Priya Nair", "priya@payments.example"),
    "arjun": ("Arjun Mehta", "arjun@payments.example"),
}
DOCS = {   # document name -> owners (CODEOWNERS entries, which are emails here)
    "rules.md": ["priya@payments.example", "mudit@payments.example"],
    "security.md": ["priya@payments.example"],
    "paths-services-ledger.md": ["mudit@payments.example"],
}
# From the seed (design/before/README.md): the unresolved http-retries conflict between two sibling team folders
# (no CODEOWNERS entry covers either document) and the two possibly-outdated rules, whose commits are the seed's own.
CONFLICTS = [dict(
    key="http-retries", owners=[],
    rules=[dict(id="e3d2757f37", folder="orgdocs/platform", doc="rules.md",
                text="All outbound HTTP calls retry up to 3 times with exponential backoff"),
           dict(id="887e6b40e1", folder="orgdocs/payments", doc="rules.md",
                text="Never retry the card processor's `charge` endpoint; use the idempotency key and poll status instead")])]
OUTDATED = [
    dict(id="9c55b68de3", doc="rules.md", commit="d671085a88a14641fe0dd258cf025f52746c25df",
         message="ledger: route writes through LedgerWriter",
         text="Use `app/clients/ledger.py` for all ledger writes; do not call the ledger service directly from handlers, "
              "because it owns retry and outbox semantics.",
         reason="handlers now call LedgerWriter directly; ledger.py no longer wraps retries"),
    dict(id="38b000cf40", doc="security.md", commit="e65f06b67b1bfd4944ea5d5842744b7d9cdd4a68",
         message="webhooks: rename verify_signature to verify_webhook",
         text="Webhook payloads from the card processor must be verified with `verify_signature()` in "
              "`app/webhooks/verify.py` before any parsing.",
         reason="verify_signature no longer exists; verification moved to verify_webhook with a timestamp tolerance"),
]

WAITING = [
    dict(slug="ledger-reconciliation-jobs-must-be-idemp-67b4cd", id="p-b9b43260e2",
         text="Ledger reconciliation jobs must be idempotent per `batch_id`",
         why="the nightly job re-ran twice last week and posted duplicates",
         by="priya", date="2026-11-12", doc="paths-services-ledger.md", paths="services/ledger/**"),
    dict(slug="prefer-small-prs-under-400-changed-lines-4b7962", id="p-ff0b68b93f",
         text="Prefer small PRs: under 400 changed lines unless it is a mechanical rename",
         why="reviews of the 1,200-line refund PR took four days",
         by="arjun", date="2026-11-12", doc="rules.md", paths=""),
    dict(slug="run-pip-audit-in-ci-and-fail-the-build-o-7776a6", id="p-a624ebd52d",
         text="Run `pip-audit` in CI and fail the build on a high severity finding",
         why="we shipped with an unpatched `cryptography` last quarter",
         by="arjun", date="2026-11-12", doc="security.md", paths=""),
]
REJECTED = [
    dict(text="Use the ORM for all queries, no raw SQL",
         reason="Reporting queries need raw SQL for window functions; the ORM version was 10x slower in the "
                "settlement report. Reopen with a benchmark.",
         by="arjun", rejected_by="priya", date="2026-11-12"),
]

# A layout test only: invented rule texts, to see 15 waiting at once. Not product data.
STRESS_TEXTS = [
    ("Every public endpoint documents its error codes in the OpenAPI file", "clients keep guessing at 4xx bodies", "rules.md"),
    ("Background jobs log their `job_id` at start and at finish", "we could not trace the March replay", "rules.md"),
    ("Feature flags are removed within two releases of full rollout", "the flag list has 40 stale entries", "rules.md"),
    ("Never call the card processor from a request handler; enqueue it", "a slow processor stalled the API", "rules.md"),
    ("Rotate the webhook signing secret every quarter", "the last rotation was two years ago", "security.md"),
    ("Admin tool actions write an audit row with the actor and the reason", "auditors asked for who approved refunds", "security.md"),
    ("Ledger reversals reference the original `entry_id`", "reversals were matched by amount and date", "paths-services-ledger.md"),
    ("Ledger exports are signed and checksummed before upload", "a truncated export reached the auditors", "paths-services-ledger.md"),
    ("Tests that need the network are marked and skipped by default", "CI flaked on an outbound call", "rules.md"),
    ("Currency conversion uses the rate stored on the payment, never today's rate", "refunds were off by a cent", "rules.md"),
    ("Dependencies are pinned and updated by one weekly pull request", "three people bumped the same package", "security.md"),
    ("Database indexes ship in their own migration", "an index build locked the payments table", "rules.md"),
]


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def rule_html(text: str) -> str:
    """Rule text, escaped; `backticked` spans are code (paths, commands, file names are monospace)."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))


def slugify(text: str, n: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    return f"{base}-{n:06x}"


def name(key: str) -> str:
    """A person by name; the email is in the title attribute."""
    n, e = PEOPLE[key]
    return f'<span title="{esc(e)}">{esc(n)}</span>'


def people(emails: list[str], me: str, sep: str = ", ") -> str:
    """Names with the email in a title attribute; the viewer is 'First (you)'; an unknown email is shown as is."""
    by_email = {e: (k, n) for k, (n, e) in PEOPLE.items()}
    out = []
    for e in emails:
        if e not in by_email:
            out.append(esc(e))
            continue
        k, n = by_email[e]
        label = f"{n.split()[0]} (you)" if k == me else n
        out.append(f'<span title="{esc(e)}">{esc(label)}</span>')
    return sep.join(out)


def mine(p: dict, me: str) -> bool:
    return PEOPLE[me][1] in DOCS[p["doc"]]


def can_settle(owners: list[str], me: str) -> bool:
    """A document nobody owns (no CODEOWNERS entry) can be settled by anyone."""
    return not owners or PEOPLE[me][1] in owners


# --------------------------------------------------------------------------------------------------
CSS = """/* Hallmark · shape: app screen · genre: modern-minimal · theme: Ink (deal 3/3, seed local-review-tool-where-be9d·5ff087b0·2026-11-11·r0) · redesign: full
 * nav: top bar, typeset wordmark, project switcher, text tabs · signature: the proposal as a ruled entry, its rule text set in serif · states: waiting on you, waiting on others, turned down, empty, accepted · ink: system faces, one teal that means in force and nothing else, no pills
 * axes: light/grotesk-sans/chromatic-teal · posture: restrained · contrast: pass · v2.0.0 */
/* Hallmark · pre-emit critique: P4 H4 E4 S4 R4 V3 · weakest: V, the screen is a ruled list like the catalogue the theme was drawn from; the serif rule text and the single bordered Accept are what make it a review tool */
/* waive 22 · ink and greys at zero chroma · a notice board is printed in plain black */
/* waive 7b · the paper is a tinted off-white, the dark paper a lifted grey; neither is pure */
/* Hallmark · dropped token, slug, why, post /do/accept, post /do/reject · the Accept and Reject forms are not drawn for a proposal the viewer cannot decide; the brief says never to show an action that will be refused, and the old page marked those "Not an owner: the CLI would refuse" */
/* Hallmark · reworded "waiting 3", "turned down 1", "payments-api", "notifications-service" · the proposals are split by who can decide them, turned down is a disclosure, and the repo name moves into the project switcher */
:root {
  color-scheme: light dark;
  --space-3xs: 2px; --space-2xs: 4px; --space-xs: 8px; --space-sm: 12px; --space-md: 16px; --space-lg: 24px; --space-xl: 40px; --space-2xl: 64px;
  --text-xs: 0.75rem; --text-sm: 0.875rem; --text-md: 1rem; --text-lg: 1.2rem; --text-xl: 1.44rem;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1); --dur-micro: 120ms;
  --color-paper: oklch(99.5% 0.003 100); --color-paper-2: oklch(97% 0.004 100); --color-paper-3: oklch(94% 0.006 100);
  --color-ink: oklch(16% 0 0); --color-ink-2: oklch(28% 0 0); --color-muted: oklch(44% 0 0);
  --color-rule: oklch(89% 0.004 100); --color-line: oklch(62% 0 0);
  --color-in: oklch(50% 0.095 188);
  --color-focus: oklch(16% 0 0); --color-selection: oklch(90% 0.01 100);
  --font-serif: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  --font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Helvetica Neue", sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  --radius-control: 10px; --radius-well: 12px; --rule-hair: 1px;
  --tracking-display: -0.02em; --display-weight: 700;
  --page: 60rem;
}
/* The dark theme is drawn, not inverted: a lifted grey, not black, so the tab does not glare or vanish beside a dark
   terminal; text at 92% so serif rule text does not bloom; rules and control lines raised in lightness (shadows do no
   work here); the one teal lifted and softened so it still reads as the single colour on a dark ground. */
@media (prefers-color-scheme: dark) {
  :root {
    --color-paper: oklch(19% 0.003 100); --color-paper-2: oklch(22.5% 0.003 100); --color-paper-3: oklch(27% 0.003 100);
    --color-ink: oklch(92% 0 0); --color-ink-2: oklch(83% 0 0); --color-muted: oklch(68% 0 0);
    --color-rule: oklch(31% 0.003 100); --color-line: oklch(54% 0 0);
    --color-in: oklch(76% 0.09 188);
    --color-focus: oklch(92% 0 0); --color-selection: oklch(34% 0.01 100);
  }
}
*, *::before, *::after { box-sizing: border-box; }
html, body { overflow-x: clip; }
html { scroll-padding-top: var(--space-xl); }
body { margin: 0; background: var(--color-paper); color: var(--color-ink-2); font-family: var(--font-sans); font-size: var(--text-md); line-height: 1.55; -webkit-font-smoothing: antialiased; caret-color: var(--color-ink); }
::selection { background: var(--color-selection); color: var(--color-ink); }
a { color: var(--color-ink); font-weight: 600; text-decoration-thickness: 1px; text-underline-offset: 0.16em; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 2px solid var(--color-focus); outline-offset: 2px; }
h1, h2, h3 { margin: 0; color: var(--color-ink); min-width: 0; overflow-wrap: break-word; }
h2 { font-size: var(--text-xl); font-weight: var(--display-weight); letter-spacing: var(--tracking-display); line-height: 1.15; text-wrap: balance; }
p { margin: 0; text-wrap: pretty; }
code { font-family: var(--font-mono); font-size: 0.88em; overflow-wrap: anywhere; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.page { max-width: var(--page); margin-inline: auto; padding-inline: var(--space-lg); }

/* top bar */
.bar { border-bottom: var(--rule-hair) solid var(--color-rule); }
.bar .page { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-xs) var(--space-lg); min-height: 3.5rem; }
.mark { font-weight: var(--display-weight); font-size: var(--text-lg); letter-spacing: var(--tracking-display); color: var(--color-ink); text-decoration: none; }
.switch { position: relative; }
.switch summary { list-style: none; cursor: pointer; font-weight: 600; color: var(--color-ink); padding: var(--space-xs) 0; display: inline-flex; align-items: center; gap: var(--space-xs); }
.switch summary::-webkit-details-marker { display: none; }
.switch summary::after { content: ""; width: 0.45em; height: 0.45em; border-right: 1.5px solid var(--color-muted); border-bottom: 1.5px solid var(--color-muted); transform: translateY(-0.12em) rotate(45deg); }
.switch[open] summary::after { transform: translateY(0.1em) rotate(-135deg); }
.menu { position: absolute; z-index: 10; top: 100%; left: calc(-1 * var(--space-sm)); min-width: 16rem; margin: 0; padding: var(--space-xs); list-style: none; background: var(--color-paper); border: var(--rule-hair) solid var(--color-line); border-radius: var(--radius-well); }
.menu a { display: block; padding: var(--space-xs) var(--space-sm); border-radius: 8px; text-decoration: none; font-weight: 400; color: var(--color-ink-2); }
.menu a:hover { background: var(--color-paper-2); }
.menu a[aria-current] { font-weight: 600; color: var(--color-ink); }
.menu hr { border: 0; border-top: var(--rule-hair) solid var(--color-rule); margin: var(--space-xs) 0; }
.find { margin-left: auto; }
.find input[type=search] { width: 15rem; max-width: 100%; min-height: 2.25rem; padding: 0 var(--space-sm); background: var(--color-paper-2); color: var(--color-ink); border: var(--rule-hair) solid var(--color-line); border-radius: var(--radius-control); }
.find input[type=search]::placeholder { color: var(--color-muted); }

/* project actions and tabs */
.tabrow { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 0 var(--space-lg); margin-top: var(--space-lg); border-bottom: var(--rule-hair) solid var(--color-rule); }
.tabs { display: flex; flex-wrap: wrap; gap: 0 var(--space-lg); }
.acts { display: flex; flex-wrap: wrap; gap: 0 var(--space-lg); }
.acts a { padding: var(--space-sm) 0; }
.tabs a { flex: none; padding: var(--space-sm) 0; color: var(--color-muted); font-weight: 400; text-decoration: none; border-bottom: 2px solid transparent; margin-bottom: calc(-1 * var(--rule-hair)); }
.tabs a:hover { color: var(--color-ink); }
.tabs a[aria-current] { color: var(--color-ink); font-weight: 600; border-bottom-color: var(--color-ink); }
.tabs .n { margin-left: var(--space-xs); font-weight: 600; color: var(--color-ink); font-variant-numeric: tabular-nums; }

/* controls: Accept is the one bordered button on the page */
.btn { display: inline-flex; align-items: center; justify-content: center; min-height: 2.5rem; padding: 0 var(--space-md); font: inherit; font-weight: 600; color: var(--color-ink); background: var(--color-paper-2); border: var(--rule-hair) solid var(--color-line); border-radius: var(--radius-control); cursor: pointer; transition: background-color var(--dur-micro) var(--ease-out); }
.btn:hover { background: var(--color-paper-3); }
.btn:active { background: var(--color-rule); }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.text-btn { display: inline-flex; align-items: center; flex: none; white-space: nowrap; min-height: 2.5rem; font: inherit; font-weight: 600; background: none; border: 0; padding: 0; color: var(--color-ink); text-decoration: underline; text-underline-offset: 0.16em; cursor: pointer; }
.text-btn:hover { text-decoration-thickness: 2px; }
.text-btn:active { color: var(--color-muted); }
.text-btn:disabled { opacity: 0.55; cursor: not-allowed; }

/* the review */
.main { padding-block: var(--space-lg) var(--space-2xl); }
.flash { max-width: 65ch; margin: 0 0 var(--space-lg); color: var(--color-ink-2); }
.flash .in { color: var(--color-in); font-weight: 700; }
.testnote { margin: 0 0 var(--space-lg); color: var(--color-muted); font-size: var(--text-sm); }
.who { color: var(--color-muted); font-size: var(--text-sm); }
.tally { margin-top: var(--space-xs); color: var(--color-ink); font-weight: 600; }
.group { margin-top: var(--space-xl); }
.group > h2 { padding-bottom: var(--space-sm); border-bottom: var(--rule-hair) solid var(--color-ink); }
.entry { padding-block: var(--space-lg); border-bottom: var(--rule-hair) solid var(--color-rule); }
.rule { font-family: var(--font-serif); font-size: var(--text-lg); font-weight: 400; line-height: 1.4; color: var(--color-ink); }
.rule code { font-size: 0.82em; }
.items { margin: 0; padding: 0; list-style: none; }
.side + .side { margin-top: var(--space-md); }
.side .meta { margin-top: var(--space-2xs); }
.label { color: var(--color-ink); font-weight: 600; font-size: var(--text-sm); margin-bottom: var(--space-2xs); }
.go { margin-top: var(--space-sm); }
.cmdline { margin-top: var(--space-sm); color: var(--color-muted); font-size: var(--text-sm); }
.todo { margin-top: var(--space-sm); font-size: var(--text-sm); color: var(--color-ink-2); }
.cut { margin-top: var(--space-xs); color: var(--color-ink-2); }
.why { margin-top: var(--space-xs); }
.meta { display: flex; flex-wrap: wrap; gap: var(--space-2xs) var(--space-lg); margin-top: var(--space-xs); color: var(--color-muted); font-size: var(--text-sm); }
.meta code { color: var(--color-ink-2); }
.meta a { font-weight: 600; }
.meta a.open { font-weight: 400; color: var(--color-muted); }
.meta a.open:hover { color: var(--color-ink); }
.tally .in { color: var(--color-in); }
.act { display: flex; flex-wrap: wrap; align-items: flex-start; gap: var(--space-xs) var(--space-lg); margin-top: var(--space-md); }
.act form { margin: 0; }
.reject summary, .cmd summary { list-style: none; cursor: pointer; display: inline-flex; align-items: center; min-height: 2.5rem; font-weight: 600; color: var(--color-ink); text-decoration: underline; text-underline-offset: 0.16em; }
.reject summary::-webkit-details-marker, .cmd summary::-webkit-details-marker { display: none; }
.cmd summary { font-weight: 400; color: var(--color-muted); }
.reject[open], .cmd[open] { flex-basis: 100%; }
.reject form { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--space-sm) var(--space-lg); padding-top: var(--space-xs); }
.reject label { display: grid; gap: var(--space-2xs); flex: 1 1 20rem; font-size: var(--text-sm); color: var(--color-muted); }
.reject input[type=text] { min-height: 2.5rem; padding: 0 var(--space-sm); font: inherit; color: var(--color-ink); background: var(--color-paper); border: var(--rule-hair) solid var(--color-line); border-radius: var(--radius-control); }
.cmd pre { width: fit-content; max-width: min(65ch, 100%); margin: var(--space-xs) 0 0; padding: var(--space-sm) var(--space-md); background: var(--color-paper-2); border: var(--rule-hair) solid var(--color-rule); border-radius: var(--radius-control); font-family: var(--font-mono); font-size: var(--text-xs); color: var(--color-ink-2); white-space: pre-wrap; overflow-wrap: anywhere; }
.st { font-weight: 600; color: var(--color-ink); }
.sub { margin-top: var(--space-lg); font-size: var(--text-md); font-weight: 600; letter-spacing: 0; color: var(--color-ink); }
.sub code { font-weight: 400; }
.group > h2 + .sub, .group > .gnote + .sub { margin-top: var(--space-md); }
.group > h2 + .items > .entry:first-child, .group > h2 + .fold > summary + .items > .entry:first-child, .sub + .items > .entry:first-child, .sub + .fold > summary + .items > .entry:first-child { padding-top: var(--space-sm); }
.gnote { margin-top: var(--space-sm); color: var(--color-muted); font-size: var(--text-sm); }
.fold > summary { list-style: none; cursor: pointer; display: flex; align-items: center; min-height: 2.5rem; font-weight: 600; color: var(--color-ink); text-decoration: underline; text-underline-offset: 0.16em; }
.fold > summary::-webkit-details-marker { display: none; }
.fold > summary::before { content: ""; flex: none; width: 0.4em; height: 0.4em; margin-right: var(--space-sm); border-right: 1.5px solid var(--color-ink); border-bottom: 1.5px solid var(--color-ink); transform: translateY(-0.12em) rotate(-45deg); }
.fold[open] > summary::before { transform: translateY(-0.2em) rotate(45deg); }
.fold > summary + .items { border-top: var(--rule-hair) solid var(--color-rule); margin-top: var(--space-xs); }
.note { margin-top: var(--space-xs); max-width: 65ch; color: var(--color-ink-2); font-size: var(--text-sm); }
.doc { width: 100%; max-width: 80ch; margin: var(--space-md) 0 0; padding: var(--space-md); background: var(--color-paper-2); border: var(--rule-hair) solid var(--color-rule); border-radius: var(--radius-control); font-family: var(--font-mono); font-size: var(--text-sm); line-height: 1.55; color: var(--color-ink-2); white-space: pre-wrap; overflow-wrap: anywhere; }
.meta .cmd summary { min-height: 0; }
.meta .cmd pre { margin-top: var(--space-xs); }
.ask { max-width: 40rem; font-size: 1.9rem; line-height: 1.2; }
.addf { display: grid; gap: var(--space-lg); margin-top: var(--space-lg); max-width: 52rem; }
.addf textarea { width: 100%; min-height: 9rem; padding: var(--space-md); font-family: var(--font-serif); font-size: var(--text-xl); line-height: 1.4; color: var(--color-ink); background: var(--color-paper); border: var(--rule-hair) solid var(--color-line); border-radius: var(--radius-control); resize: vertical; }
.addf [aria-invalid="true"] { border: 2px solid var(--color-ink); }
.addf fieldset { margin: 0; padding: 0; border: 0; min-width: 0; }
.addf legend { padding: 0; margin-bottom: var(--space-xs); font-weight: 600; }
.opt { display: grid; grid-template-columns: 1.25rem minmax(0, 1fr); gap: 0 var(--space-sm); padding-block: var(--space-xs); cursor: pointer; }
.opt input { margin: 0.3rem 0 0; accent-color: var(--color-ink); width: 1.1rem; height: 1.1rem; }
.opt b { font-weight: 600; }
.opt span { color: var(--color-ink-2); }
.opt span b { color: var(--color-ink); }
.addf .opt .d { display: block; color: var(--color-muted); font-size: var(--text-sm); }
.addf .why-in { display: grid; gap: var(--space-2xs); color: var(--color-muted); font-size: var(--text-sm); }
.addf .why-in input { min-height: 2.5rem; padding: 0 var(--space-sm); font: inherit; color: var(--color-ink); background: var(--color-paper); border: var(--rule-hair) solid var(--color-line); border-radius: var(--radius-control); }
.err { max-width: 65ch; margin-top: var(--space-lg); font-weight: 600; color: var(--color-ink); }
.addf .err { margin: 0; }
.subtle { margin-top: var(--space-xl); }
.addf .why-in .d { color: var(--color-muted); }
.subtle .cmd p { margin-top: var(--space-md); }
.subtle p { margin-top: var(--space-md); color: var(--color-muted); font-size: var(--text-sm); }
.back { margin-bottom: var(--space-lg); font-size: var(--text-sm); }
.back a { font-weight: 400; color: var(--color-muted); }
.back a:hover { color: var(--color-ink); }
.bigrule { max-width: 52rem; text-wrap: pretty; font-family: var(--font-serif); font-size: 1.9rem; font-weight: 400; line-height: 1.3; letter-spacing: 0; color: var(--color-ink); }
.bigrule code { font-size: 0.82em; }
.status { margin-top: var(--space-sm); font-weight: 600; }
.sec { margin-top: var(--space-xl); }
.sec > h2 { font-size: var(--text-md); font-weight: 600; letter-spacing: 0; padding-bottom: var(--space-sm); border-bottom: var(--rule-hair) solid var(--color-rule); }
.facts { display: grid; grid-template-columns: minmax(8rem, 12rem) minmax(0, 1fr); gap: var(--space-sm) var(--space-lg); margin: var(--space-md) 0 0; }
.facts dt { color: var(--color-muted); font-size: var(--text-sm); padding-top: 0.1em; }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.facts dd code { font-size: 0.88em; }
.other { margin-top: var(--space-md); }
.other .rule { max-width: 44rem; margin-top: var(--space-xs); }
.quote { font-family: var(--font-sans); font-size: var(--text-md); color: var(--color-ink-2); }
.absent { margin-top: var(--space-md); color: var(--color-muted); font-size: var(--text-sm); max-width: 65ch; }
.idline { margin-top: var(--space-md); color: var(--color-muted); font-size: var(--text-sm); }
.idline code { color: var(--color-ink-2); }
.block-cmd { width: fit-content; max-width: min(65ch, 100%); margin-top: var(--space-sm); padding: var(--space-xs) var(--space-md); background: var(--color-paper-2); border: var(--rule-hair) solid var(--color-rule); border-radius: var(--radius-control); font-family: var(--font-mono); font-size: var(--text-xs); color: var(--color-ink-2); white-space: pre-wrap; overflow-wrap: anywhere; }
.waiting-on { margin-top: var(--space-sm); color: var(--color-ink-2); font-size: var(--text-sm); }
.history { margin-top: var(--space-xl); }
.history > summary { list-style: none; cursor: pointer; padding-bottom: var(--space-sm); border-bottom: var(--rule-hair) solid var(--color-rule); color: var(--color-muted); font-weight: 600; }
.history > summary::-webkit-details-marker { display: none; }
.history > summary::before { content: ""; display: inline-block; width: 0.4em; height: 0.4em; margin-right: var(--space-sm); border-right: 1.5px solid var(--color-muted); border-bottom: 1.5px solid var(--color-muted); transform: translateY(-0.12em) rotate(-45deg); }
.history[open] > summary::before { transform: translateY(-0.2em) rotate(45deg); }
.history .rule { color: var(--color-ink-2); }
.empty { margin-top: var(--space-md); }
.empty .lead { color: var(--color-ink); font-size: var(--text-lg); }
.empty .cli { margin-top: var(--space-xl); color: var(--color-muted); font-size: var(--text-sm); }
.empty pre { width: fit-content; max-width: min(65ch, 100%); margin: var(--space-xs) 0 var(--space-md); padding: var(--space-xs) var(--space-md); background: var(--color-paper-2); border: var(--rule-hair) solid var(--color-rule); border-radius: var(--radius-control); font-family: var(--font-mono); font-size: var(--text-xs); color: var(--color-ink-2); white-space: pre-wrap; overflow-wrap: anywhere; }

@media (max-width: 40rem) {
  .page { padding-inline: var(--space-md); }
  .btn, .text-btn, .reject summary, .cmd summary, .fold > summary { min-height: 2.75rem; }
  .find { margin-left: 0; width: 100%; }
  .find input[type=search] { width: 100%; }
  .menu { left: 0; }
  .bigrule { font-size: 1.5rem; }
  .ask { font-size: 1.5rem; }
  .addf textarea { font-size: var(--text-lg); }
  .facts { grid-template-columns: minmax(0, 1fr); gap: var(--space-3xs) 0; }
  .facts dd { margin-bottom: var(--space-sm); }
  .tabs { gap: 0 var(--space-md); }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; scroll-behavior: auto !important; } }
"""


def tabs(pid: str, counts: dict[str, int], active: str = "/proposals") -> str:
    items = [("/project", "Overview"), ("/context", "Rules"), ("/memory", "Memory"),
             ("/start", "What agents are told"), ("/proposals", "Proposals"), ("/connect", "Connect")]
    out = []
    for path, label in items:
        cur = ' aria-current="page"' if path == active else ""
        n = counts.get(path)
        out.append(f'<a href="{path}?p={pid}"{cur}>{label}' + (f'<span class="n">{n}</span>' if n else "") + "</a>")
    return '<nav class="tabs" aria-label="Project">' + "".join(out) + "</nav>"


def shell(*, title: str, pid: str, repo: str, counts: dict, body: str, desc: str, active: str = "/proposals",
          h1: str | None = "Proposals") -> str:
    projects = [(API, "payments-api"), (NOTIF, "notifications-service"), (LEGACY, "legacy-billing")]
    menu = "".join(
        f'<li><a href="/project?p={p}"' + (' aria-current="page"' if p == pid else "") + f">{n}</a></li>"
        for p, n in projects)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<style>
{CSS}</style>
</head>
<body>
<header class="bar"><div class="page">
<a class="mark" href="/" aria-label="Reverie, all projects">Reverie</a>
<details class="switch"><summary>{esc(repo)}</summary>
<ul class="menu">{menu}<li><hr></li><li><a href="/">All projects</a></li><li><a href="/new">Add a project</a></li></ul></details>
<form class="find" action="/search" method="get" role="search"><input type="hidden" name="p" value="{pid}"><input name="q" type="search" placeholder="Search rules and memory" aria-label="Search rules and memory" maxlength="120"></form>
</div></header>
<div class="page">
<div class="tabrow">{tabs(pid, counts, active)}<div class="acts"><a href="/connect?p={pid}">Connect an agent</a><a href="/add?p={pid}">Add a rule</a></div></div>
<main class="main">
{f'<h1 class="visually-hidden">{esc(h1)} for {esc(repo)}</h1>' if h1 else ""}
{body}
</main>
</div>
</body>
</html>
"""


def entry(p: dict, me: str, pid: str) -> str:
    owners = DOCS[p["doc"]]
    docpath = f".reverie/context/{p['doc']}"
    meta = [name(p["by"]), f'<span>{p["date"]}</span>', f'<span title="{esc(docpath)}"><code>{esc(p["doc"])}</code></span>']
    if p["paths"]:
        meta.append(f'<span>only under <code>{esc(p["paths"])}</code></span>')
    why = f'<p class="why">Why: {rule_html(p["why"])}</p>' if p.get("why") else ""
    if mine(p, me):
        slug = esc(p["slug"])
        hidden = (f'<input type="hidden" name="token" value="{TOKEN}"><input type="hidden" name="p" value="{pid}">'
                  f'<input type="hidden" name="slug" value="{slug}">')
        act = (f'<form method="post" action="/do/accept">{hidden}<button class="btn" type="submit">Accept</button></form>'
               f'<details class="reject"><summary>Reject</summary>'
               f'<form method="post" action="/do/reject">{hidden}'
               f'<label>Reason for turning it down (the proposer will read it)'
               f'<input type="text" name="why" maxlength="300" required></label>'
               f'<button class="text-btn" type="submit">Turn down with this reason</button></form></details>'
               f'<details class="cmd"><summary>As commands</summary><pre>reverie context accept {slug}\n'
               f'reverie context reject {slug} --why "..."</pre></details>')
        act = f'<div class="act">{act}</div>'
    else:
        act = (f'<p class="waiting-on">Waiting on an owner of <code>{esc(p["doc"])}</code>: '
               f'{people(owners, me)}.</p>')
    return (f'<article class="entry" id="{p["id"]}"><h3 class="rule">{rule_html(p["text"])}</h3>{why}'
            f'<p class="meta">{"".join(meta)}</p>{act}</article>')


def history(rejected: list[dict]) -> str:
    if not rejected:
        return ""
    rows = []
    for r in rejected:
        rows.append(
            f'<article class="entry"><h3 class="rule">{rule_html(r["text"])}</h3>'
            f'<p class="why">Reason: {rule_html(r["reason"])}</p>'
            f'<p class="meta"><span>Proposed by {name(r["by"])}</span>'
            f'<span>Turned down by {name(r["rejected_by"])}</span><span>{r["date"]}</span></p></article>')
    return f'<details class="history"><summary>Turned down, {len(rejected)}</summary>' + "".join(rows) + "</details>"


def proposals_body(me: str, waiting: list[dict], rejected: list[dict], pid: str, flash: str = "", note: str = "") -> str:
    on_me = [p for p in waiting if mine(p, me)]
    other = [p for p in waiting if not mine(p, me)]
    if on_me and other:
        tally = f"{len(on_me)} waiting on you, {len(other)} waiting on others."
    elif on_me:
        tally = f"{len(on_me)} waiting on you."
    elif other:
        tally = f"Nothing waiting on you. {len(other)} waiting on others."
    else:
        tally = "Nothing waiting."
    ident = f'<p class="who">Acting as {name(me)}, from your git identity.</p>'
    parts = [note, flash, ident, f'<p class="tally">{tally}</p>']
    if on_me:
        parts.append('<section class="group" aria-labelledby="h-you"><h2 id="h-you">Waiting on you</h2>'
                     + "".join(entry(p, me, pid) for p in on_me) + "</section>")
    if other:
        parts.append('<section class="group" aria-labelledby="h-others"><h2 id="h-others">Waiting on others</h2>'
                     + "".join(entry(p, me, pid) for p in other) + "</section>")
    parts.append(history(rejected))
    return "".join(parts)


def empty_body(pid: str) -> str:
    return (f'<div class="empty"><p class="lead">Nothing waiting. Suggest a rule with <a href="/add?p={pid}">Add a rule</a>.</p>'
            '<div class="cli"><p>The same from the command line:</p>'
            '<pre>reverie context add "Money is integer cents" --why "reason"</pre>'
            '<p>To turn a rule you already use into a proposal:</p>'
            '<pre>reverie context propose "Money is integer cents"</pre></div></div>')


def counts_for(me: str, waiting: list[dict]) -> dict:
    """Only what waits on the viewer, and nothing at zero. Overview = proposals you can decide + unresolved conflicts and
    possibly-outdated rules in documents you own or that no CODEOWNERS entry covers; Proposals = the proposals you can
    decide. No counts on Rules or Memory."""
    props = sum(1 for p in waiting if mine(p, me))
    return {"/project": props + sum(1 for c in CONFLICTS if conflict_mine(c, me))
            + sum(1 for o in OUTDATED if can_settle(DOCS[o["doc"]], me)), "/proposals": props}


def conflict_mine(c: dict, me: str) -> bool:
    return can_settle(c["owners"], me)


# ---- Overview ------------------------------------------------------------------------------------------------
def _doc(doc: str, path: str = "") -> str:
    return f'<span title="{esc(path or ".reverie/context/" + doc)}"><code>{esc(doc)}</code></span>'


def prop_item(p: dict, me: str, pid: str) -> str:
    meta = [name(p["by"]), f'<span>{p["date"]}</span>', _doc(p["doc"])]
    if p["paths"]:
        meta.append(f'<span>only under <code>{esc(p["paths"])}</code></span>')
    why = f'<p class="why">Why: {rule_html(p["why"])}</p>' if p.get("why") else ""
    href = f'/proposals?p={pid}#{p["id"]}'
    if mine(p, me):
        tail = f'<p class="go"><a href="{href}">Decide</a></p>'
    else:
        tail = (f'<p class="waiting-on">Waiting on an owner of <code>{esc(p["doc"])}</code>: '
                f'{people(DOCS[p["doc"]], me)}.</p><p class="go"><a href="{href}">View</a></p>')
    return (f'<li><article class="entry"><p class="label">Proposal</p><h3 class="rule">{rule_html(p["text"])}</h3>'
            f'{why}<p class="meta">{"".join(meta)}</p>{tail}</article></li>')


def conflict_item(c: dict, me: str, pid: str) -> str:
    sides = "".join(
        f'<div class="side"><p class="rule">{rule_html(r["text"])}</p>'
        f'<p class="meta"><span>Team folder {esc(r["folder"])}</span>{_doc(r["doc"], r["folder"] + "/" + r["doc"])}'
        f'<a href="/rule?p={pid}&amp;id={r["id"]}">Open rule</a></p></div>' for r in c["rules"])
    if c["owners"]:
        who = ("You own one of the documents, so you can settle it." if conflict_mine(c, me)
               else f'Waiting on an owner: {people(c["owners"], me)}.')
    else:
        who = "No CODEOWNERS entry covers either document, so anyone can settle it."
    return (f'<li><article class="entry"><p class="label">Conflict</p>{sides}'
            f'<p class="why">Agents are told to ask before acting on either. {who}</p>'
            f'<p class="cmdline"><code>reverie context resolve {esc(c["key"])} "your decision"</code></p></article></li>')


def outdated_item(o: dict, me: str, pid: str) -> str:
    # The commit message comes from the judge's stale entry; it can be empty, so then only the hash is shown.
    msg = f' {esc(o["message"])}' if o.get("message") else ""
    meta = [_doc(o["doc"]), f'<span>Commit <code>{esc(o["commit"][:7])}</code>{msg}</span>']
    if can_settle(DOCS[o["doc"]], me):
        tail = (f'<p class="todo">Check the code, then edit or remove the rule in '
                f'<code>.reverie/context/{esc(o["doc"])}</code>.</p>')
    else:
        tail = (f'<p class="waiting-on">Waiting on an owner of <code>{esc(o["doc"])}</code>: '
                f'{people(DOCS[o["doc"]], me)}.</p>')
    return (f'<li><article class="entry"><p class="label">Possibly outdated</p><h3 class="rule">{rule_html(o["text"])}</h3>'
            f'<p class="why">A later change to the code may contradict it: {esc(o["reason"])}.</p>'
            f'<p class="meta">{"".join(meta)}</p>{tail}'
            f'<p class="go"><a href="/rule?p={pid}&amp;id={o["id"]}">Open rule</a></p></article></li>')


def overview_body(me: str, waiting: list[dict], conflicts: list[dict], outdated: list[dict], pid: str, cut: int = 0) -> str:
    you = ([prop_item(p, me, pid) for p in waiting if mine(p, me)]
           + [conflict_item(c, me, pid) for c in conflicts if conflict_mine(c, me)]
           + [outdated_item(o, me, pid) for o in outdated if can_settle(DOCS[o["doc"]], me)])
    others = ([prop_item(p, me, pid) for p in waiting if not mine(p, me)]
              + [conflict_item(c, me, pid) for c in conflicts if not conflict_mine(c, me)]
              + [outdated_item(o, me, pid) for o in outdated if not can_settle(DOCS[o["doc"]], me)])
    if you and others:
        tally = f"{len(you)} waiting on you, {len(others)} waiting on others."
    elif you:
        tally = f"{len(you)} waiting on you."
    elif others:
        tally = f"Nothing waiting on you. {len(others)} waiting on others."
    else:
        tally = "Nothing needs you."
    parts = [f'<p class="who">Acting as {name(me)}, from your git identity.</p>', f'<p class="tally">{tally}</p>']
    if cut:
        parts.append(f'<p class="cut">{cut} more {"rule applies but was" if cut == 1 else "rules apply but were"} cut by the start limit. '
                     f'<a href="/start?p={pid}">See what agents are told.</a></p>')
    if you:
        parts.append('<section class="group" aria-labelledby="h-you"><h2 id="h-you">Waiting on you</h2>'
                     f'<ol class="items">{"".join(you)}</ol></section>')
    if others:
        parts.append('<section class="group" aria-labelledby="h-others"><h2 id="h-others">Waiting on others</h2>'
                     f'<ol class="items">{"".join(others)}</ol></section>')
    return "".join(parts)


# ---- Rules ---------------------------------------------------------------------------------------------------
def bullet_id(text: str) -> str:
    """Bullets in a rules file carry no id of their own; the port would derive a stable one from the text."""
    return hashlib.sha1(" ".join(text.split()).lower().encode()).hexdigest()[:10]


RK, RP, RC = "Team folder orgdocs/platform", "Team folder orgdocs/payments", "This repo"
ODOC = {o["id"]: o for o in OUTDATED}


def R(id, text, group, sub="", doc="rules.md", path="", paths="", locked=False, status="force", **more):
    return dict(id=id, text=text, group=group, sub=sub, doc=doc, path=path or doc, paths=paths, locked=locked,
                status=status, **more)


CUT_REASON = "agents are told at most 30 remembered rules for this repo"
STARTUP_LIMIT = 8000          # deliver.startup_cap() default; the page prints the configured value
RETRY_A = "All outbound HTTP calls retry up to 3 times with exponential backoff"
RETRY_B = "Never retry the card processor's `charge` endpoint; use the idempotency key and poll status instead"
MONEY = ("Money is stored and passed as integer minor units (cents) with an ISO 4217 currency code; never use floats, "
         "because rounding drift broke reconciliation in the March settlement run.")
RG = "Prefer `rg` over `grep` in examples"
MIGR = "Never run migrations against the production replica; use the primary and the `--dry-run` flag first."


def seed_rules(extra: int = 0, cut: int = 0, ids: dict | None = None) -> list[dict]:
    ids = ids or {}
    rs = [
        R("e3d2757f37", RETRY_A, "team", RK, path="orgdocs/platform/rules.md", status="conflict",
          other=dict(text=RETRY_B, where="orgdocs/payments"), owners=[]),
        R("887e6b40e1", RETRY_B, "team", RP, path="orgdocs/payments/rules.md", status="conflict",
          other=dict(text=RETRY_A, where="orgdocs/platform"), owners=[]),
        R("59cf39933e", "Ledger entries are append-only: corrections are new reversing entries, never UPDATE or DELETE, "
                        "because auditors replay the table.", "team", RC, doc="paths-services-ledger.md",
          path=".reverie/context/paths-services-ledger.md", paths="services/ledger/**"),
        R("d89577f1d2", MONEY, "team", RC, path=".reverie/context/rules.md"),
        R("97d2983dbb", "Every endpoint that creates a charge or refund must require an `Idempotency-Key` header and store "
                        "the response for 24 hours, because card-network retries otherwise double-charge.", "team", RC,
          path=".reverie/context/rules.md"),
        R("fac934c10b", "Database migrations must be backward compatible with the previous release, because we deploy "
                        "with rolling restarts and `alembic` runs before the new pods are ready.", "team", RC,
          path=".reverie/context/rules.md"),
        R("9c55b68de3", ODOC["9c55b68de3"]["text"], "team", RC, path=".reverie/context/rules.md", status="outdated",
          reason=ODOC["9c55b68de3"]["reason"]),
        R("54986a9cdb", "Log payment identifiers (`payment_id`, `merchant_id`) but never card numbers, CVV or full bank "
                        "account numbers, because logs ship to a third-party aggregator.", "team", RC,
          path=".reverie/context/rules.md"),
        R("c3ed997047", "Secrets come from the secrets manager through `app/config.py`; never read them from environment "
                        "files committed to the repo, because we rotate them quarterly.", "team", RC, doc="security.md",
          path=".reverie/context/security.md"),
        R("38b000cf40", ODOC["38b000cf40"]["text"], "team", RC, doc="security.md", path=".reverie/context/security.md",
          status="outdated", reason=ODOC["38b000cf40"]["reason"]),
        R(bullet_id(RG), RG, "your", path="~/.reverie/rules.md"),
        R("e3baf78233", MONEY, "your", path="~/.reverie/context/rules.md", status="hidden"),
        R(ids.get(MIGR, "932ff77c"), MIGR, "remembered", doc="", path="", source="You said it"),
    ]
    for n in range(1, extra + 1):
        t = f"Seed rule {n:02d}: module {n} must not run code at import time."
        rs.append(R(ids.get(t) or hashlib.sha1(t.encode()).hexdigest()[:8], t, "remembered", doc="", path="",
                    source="You said it", status="cut" if n > extra - cut else "force", cut_reason=CUT_REASON))
    return rs


def _clip(t: str, n: int = 64) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def _q(t: str) -> str:
    return f"“{rule_html(_clip(t))}”"


def rule_li(r: dict, pid: str) -> str:
    st, extra = r["status"], ""
    label = ""
    if st == "force":
        if r.get("beats"):
            b = r["beats"]
            extra = f'<p class="note">Overrides {_q(b["text"])} in {esc(b["where"])}: {esc(b["why"])}.</p>'
    elif st == "outdated":
        label = '<span class="st">Possibly outdated</span>'
        extra = f'<p class="note">A later change to the code may contradict it: {esc(r["reason"])}.</p>'
    elif st == "conflict":
        label = '<span class="st">Conflict</span>'
        o = r["other"]
        who = (f'Waiting on an owner: {people(r["owners"], "mudit")}.' if r["owners"]
               else "No CODEOWNERS entry covers either document, so anyone can settle it.")
        extra = (f'<p class="note">Disagrees with {_q(o["text"])} in {esc(o["where"])}. '
                 f'Agents are told to ask before acting on either. {who}</p>')
    elif st == "overridden":
        label = '<span class="st">Overridden</span>'
        b = r["by"]
        extra = f'<p class="note">Overridden by {_q(b["text"])} in {esc(b["where"])}: {esc(b["why"])}.</p>'
    elif st == "hidden":
        label = '<span class="st">Hidden: same as a team rule</span>'
        extra = ('<p class="note">Agents are told the team rule, not this copy. The copy comes back if the team rule '
                 'is removed.</p>')
    else:
        label = '<span class="st">Cut by the start limit</span>'
        extra = f'<p class="note">It applies here but was cut, because {esc(r.get("cut_reason", "it did not fit in what agents are told"))}.</p>'
    meta = [label] if label else []
    if r["doc"]:
        meta.append(f'<span title="{esc(r["path"])}"><code>{esc(r["doc"])}</code></span>')
    elif r.get("source"):
        meta.append(f'<span>{esc(r["source"])}</span>')
    if r["paths"]:
        meta.append(f'<span>only under <code>{esc(r["paths"])}</code></span>')
    if r["locked"]:
        meta.append("<span>locked</span>")
    meta.append(f'<a class="open" href="/rule?p={pid}&amp;id={r["id"]}">Open rule</a>')
    return (f'<li class="entry"><p class="rule">{rule_html(r["text"])}</p><p class="meta">{"".join(meta)}</p>{extra}</li>')


def _list(rs: list[dict], pid: str) -> str:
    if len(rs) <= 10:
        return f'<ol class="items">{"".join(rule_li(r, pid) for r in rs)}</ol>'
    # Exceptions (cut, conflict, outdated ...) stay visible above the fold; only the normal rest collapses.
    top = [r for r in rs if r["status"] != "force"]
    rest = [r for r in rs if r["status"] == "force"]
    out = f'<ol class="items">{"".join(rule_li(r, pid) for r in top)}</ol>' if top else ""
    if rest:
        word = "more " if top else "rules "
        out += (f'<details class="fold"><summary>{len(rest)} {word}in force</summary>'
                f'<ol class="items">{"".join(rule_li(r, pid) for r in rest)}</ol></details>')
    return out


def rules_body(rs: list[dict], pid: str, note: str = "") -> str:
    force = sum(1 for r in rs if r["status"] == "force")
    cut = sum(1 for r in rs if r["status"] == "cut")
    tally = f"{force} {'rule' if force == 1 else 'rules'} <span class=\"in\">in force</span>"
    if force == 0:
        tally = "No rules <span class=\"in\">in force</span>"
    if cut:
        tally += f", {cut} more cut by the start limit. <a href=\"/start?p={pid}\">See what agents are told.</a>"
    else:
        tally += "."
    out = [note, f'<p class="tally">{tally}</p>']

    def section(key: str, title: str, gnote: str, inner: str) -> None:
        out.append(f'<section class="group" aria-labelledby="g-{key}"><h2 id="g-{key}">{title}</h2>'
                   + (f'<p class="gnote">{gnote}</p>' if gnote else "") + inner + "</section>")

    team = [r for r in rs if r["group"] == "team"]
    if team:
        subs: dict[str, list[dict]] = {}
        for r in team:
            subs.setdefault(r["sub"], []).append(r)
        order = sorted(subs, key=lambda k: (0 if k.startswith("Organisation") else 1 if k.startswith("Team") else 2))
        inner = ""
        for k in order:
            if k == RC:
                label = 'This repo, <code>.reverie/context</code>'
            else:
                head, _, folder = k.partition(" folder ")
                label = f'{head} folder <code>{esc(folder)}</code>'
            inner += f'<h3 class="sub">{label}</h3>{_list(subs[k], pid)}'
        section("team", "Team rules", "", inner)
    for key, title, gnote in (("shared", "Shared rules file", "Through git, with no owners and no review."),
                              ("your", "Your rules", "Only your agents are told these, in every repo."),
                              ("remembered", "Remembered", "Rules remembered on this machine for this repo.")):
        g = [r for r in rs if r["group"] == key]
        if g:
            section(key, title, gnote, _list(g, pid))
    return "".join(out)


# ---- Rule page -----------------------------------------------------------------------------------------------
LAYER = {"org": "Organisation", "team": "Team", "repo": "This repo", "personal": "Yours"}
ME_OWNERS = {"rules.md": DOCS["rules.md"], "security.md": DOCS["security.md"],
             "paths-services-ledger.md": DOCS["paths-services-ledger.md"]}


def _fact(dt: str, dd: str) -> str:
    return f"<dt>{dt}</dt><dd>{dd}</dd>"


def _owners(owners: list[str]) -> str:
    return people(owners, "mudit") if owners else "No CODEOWNERS entry covers it."


def _where(r: dict) -> str:
    """The 'Where it comes from' facts of a document rule: layer, folder, document, section, owners, scope, lock."""
    rows = [_fact("Source", LAYER[r["layer"]]), _fact("Folder", f'<code>{esc(r["folder"])}</code>'),
            _fact("Document", f'<code>{esc(r["path"])}</code>'), _fact("Section", esc(r["section"])),
            _fact("Owners", _owners(r["owners"]))]
    if r.get("paths"):
        rows.append(_fact("Applies to", f'only under <code>{esc(r["paths"])}</code>'))
    if r.get("locked"):
        rows.append(_fact("Locked", "It cannot be overridden inside its scope."))
    return f'<section class="sec"><h2>Where it comes from</h2><dl class="facts">{"".join(rows)}</dl></section>'


def _other(r: dict, pid: str, lead: str) -> str:
    return (f'<div class="other"><p class="why">{lead}</p><p class="rule">{rule_html(r["text"])}</p>'
            f'<p class="meta"><span>{esc(r["from"])}</span><span title="{esc(r["path"])}"><code>{esc(r["doc"])}</code></span>'
            + (f'<span>only under <code>{esc(r["paths"])}</code></span>' if r.get("paths") else "")
            + f'<a class="open" href="/rule?p={pid}&amp;id={r["id"]}">Open rule</a></p></div>')


def _idline(rid: str) -> str:
    return (f'<p class="idline">Id <code>{esc(rid)}</code>. Open it from a terminal with '
            f'<code>reverie show {esc(rid)}</code>.</p>')


def rule_body(r: dict, pid: str, note: str = "") -> str:
    home = r.get("home", ("Rules", "/context"))
    out = [note, f'<p class="back"><a href="{home[1]}?p={pid}">Back to {home[0]}</a></p>',
           f'<h1 class="bigrule">{rule_html(r["text"])}</h1>']
    if r.get("status"):
        out.append(f'<p class="status">{esc(r["status"])}</p>')
    st = r.get("kind")
    if st == "team":
        out.append(_where(r))
        if r.get("conflict"):
            c = r["conflict"]
            who = (f'Waiting on an owner: {people(c["owners"], "mudit")}.' if c["owners"]
                   else "No CODEOWNERS entry covers either document, so anyone can settle it.")
            out.append('<section class="sec"><h2>Conflict</h2>'
                       + _other(c["other"], pid, f'Disagrees with this rule. Conflict <code>{esc(c["key"])}</code>: {esc(c["reason"])}')
                       + f'<p class="why">Agents are told to ask before acting on either. {who}</p>'
                       f'<pre class="block-cmd">reverie context resolve {esc(c["key"])} "your decision"</pre></section>')
        if r.get("overridden"):
            o = r["overridden"]
            out.append('<section class="sec"><h2>Overridden</h2>'
                       + _other(o["other"], pid, "Overridden by this rule.")
                       + f'<p class="why">Why it wins: {esc(o["why"])}.</p>'
                       f'<p class="absent">The conflict is <code>{esc(o["key"])}</code>. An overridden rule is kept in its document and '
                       'is not told to agents.</p></section>')
        if r.get("outdated"):
            o = r["outdated"]
            msg = f' {esc(o["message"])}' if o.get("message") else ""
            out.append('<section class="sec"><h2>Possibly outdated</h2><dl class="facts">'
                       + _fact("Reason", f'A later change to the code may contradict it: {esc(o["reason"])}.')
                       + _fact("Commit", f'<code>{esc(o["commit"][:7])}</code>{msg}')
                       + _fact("What to do", f'Check the code, then edit or remove the rule in <code>{esc(r["path"])}</code>.')
                       + '</dl></section>')
    else:
        out.append(r["_html"])
    out.append(_idline(r["id"]))
    return "".join(out)


def remembered_html(m: dict, heading: str) -> str:
    rows = [_fact("Said by", esc(m["said"]))]
    if m.get("evidence"):
        rows.append(_fact("Evidence", f'<span class="quote">“{rule_html(m["evidence"])}”</span>'))
    rows.append(_fact("Remembered", esc(m["date"])))
    if m.get("files"):
        rows.append(_fact("Anchored to", ", ".join(f"<code>{esc(f)}</code>" for f in m["files"])))
    if m.get("commit"):
        rows.append(_fact("Grounded on commit", f'<code>{esc(m["commit"][:7])}</code>'))
    if m.get("verified"):
        rows.append(_fact("Last verified", esc(m["verified"])))
    if m.get("usage"):
        rows.append(_fact("Used by agents", esc(m["usage"])))
    if m.get("stale"):
        st = m["stale"]
        msg = f' {esc(st["message"])}' if st.get("message") else ""
        rows.append(_fact("Possibly outdated", f'A later change to the code may contradict it: {esc(st["reason"])}.'))
        rows.append(_fact("Commit", f'<code>{esc(st["commit"][:7])}</code>{msg}'))
    body = f'<section class="sec"><h2>{heading}</h2><dl class="facts">' + "".join(rows) + "</dl>"
    if m.get("challenged"):
        body += ('<p class="why">Challenged: agents contradicted it repeatedly, so agents are now told to confirm or fix it '
                 'before relying on it. See the tally with <code>reverie usage</code>.</p>')
    body += "</section>"
    i = m["id"]
    cmds = [("Print this item in a terminal.", f"reverie show {i}"),
            ("Forget it. It can be restored.", f"reverie undo {i}\nreverie undo {i} --restore"),
            ("Make it apply in every repo, not only this one. Take it back with --demote.",
             f"reverie promote {i}\nreverie promote {i} --demote")]
    if m.get("stale"):
        cmds.append(("Keep it as it is and clear the possibly-outdated flag.", f"reverie stale keep {i}"))
    body += ('<section class="sec"><h2>Commands</h2>'
             + "".join(f'<p class="why">{esc(t)}</p><pre class="block-cmd">{esc(c)}</pre>' for t, c in cmds) + "</section>")
    return body


# ---- What agents are told ------------------------------------------------------------------------------------
def start_text(fname: str) -> str:
    """The text exactly as the start function produced it in the seed capture (design/before/<fname>)."""
    page = (OUT.parent / "before" / fname).read_text(encoding="utf-8")
    return html.unescape(re.search(r"<pre id=start>(.*?)</pre>", page, re.S).group(1))


def index_count(text: str) -> int:
    """Lines of the memory index in the start text (the lines after its header)."""
    m = re.search(r"Reverie memory index\.[^\n]*\n((?:- \[[^\n]*\n?)+)", text)
    return len(m.group(1).strip().splitlines()) if m else 0


def start_body(rs: list[dict], text: str, pid: str, asks: list[dict] = ()) -> str:
    n = sum(1 for r in rs if r["status"] == "force")
    cut = [r for r in rs if r["status"] == "cut"]
    tally = f"{n} {'rule is' if n == 1 else 'rules are'} told to agents"
    idx = index_count(text)
    if idx:
        tally += f", plus an index of {idx} remembered decisions and findings"
    tally += "."
    if cut:
        tally += (f" {len(cut)} more {'rule applies but was' if len(cut) == 1 else 'rules apply but were'} cut, "
                  f"because {cut[0]['cut_reason']}.")
    out = [f'<p class="tally">{tally}</p>',
           '<p class="who">This is what your agents are told. Your rules are only told to your own agents.</p>']
    if cut:
        li = "".join(f'<li class="entry"><p class="rule">{rule_html(r["text"])}</p><p class="meta"><span>{esc(r["source"])}</span>'
                     f'<a class="open" href="/rule?p={pid}&amp;id={r["id"]}">Open rule</a></p></li>' for r in cut)
        out.append('<section class="group" aria-labelledby="g-cut"><h2 id="g-cut">Cut by the start limit</h2>'
                   f'<ol class="items">{li}</ol>'
                   '<p class="gnote">The start text lists at most 30 remembered rules for this repo, 15 of your rules that apply in '
                   f'every repo and 8 possibly outdated ones, and stops at {STARTUP_LIMIT:,} characters. '
                   '<code>reverie rules list</code> shows every rule that applies.</p></section>')
    if asks:
        li = "".join(f'<li class="entry"><p><a href="/rule?p={pid}&amp;id={c["rules"][0]["id"]}"><code>{esc(c["key"])}</code></a></p>'
                     f'<p class="why">{esc(c["rules"][0]["folder"])} and {esc(c["rules"][1]["folder"])} disagree. '
                     'Agents are told to ask before acting on either.</p></li>' for c in asks)
        out.append(f'<section class="group" aria-labelledby="g-ask"><h2 id="g-ask">Agents are told to ask first</h2><ol class="items">{li}</ol></section>')
    if text:
        out.append('<section class="group" aria-labelledby="g-text"><h2 id="g-text">What agents read</h2>'
                   f'<pre class="doc" id="start">{esc(text)}</pre><p class="gnote">{len(text):,} of {STARTUP_LIMIT:,} characters.</p></section>')
    else:
        out.append('<p class="why">Nothing is told to agents here. They start with a clean slate.</p>')
    return "".join(out)


# ---- Memory --------------------------------------------------------------------------------------------------
KINDS = [("rule", "Rules you stated", "Rule"), ("decision", "Decisions", "Decision"),
         ("rejected_approach", "Rejected approaches", "Rejected approach"), ("preference", "Preferences", "Preference"),
         ("environment", "Environment", "Environment")]
SAID = {"user_said": "You said it", "agent_inferred": "An agent inferred it"}
USAGE_WORDS = {"USED": "used", "IGNORED": "ignored", "IRRELEVANT": "not relevant", "CONTRADICTED": "contradicted",
               "OVERRIDDEN": "overridden"}


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def usage_words(u: dict) -> str:
    return ", ".join(f"{USAGE_WORDS[k]} {_times(u[k])}" for k in USAGE_WORDS if u.get(k))


def M(id, kind, text, source, anchors=(), usage=None, challenged=False, stale=None, status="", **more):
    return dict(id=id, kind=kind, text=text, source=source, anchors=list(anchors), usage=usage or {}, challenged=challenged,
                stale=stale, status=status, date="2026-11-12", **more)


def seed_memory(extra: int = 0, cut: int = 0, ids: dict | None = None) -> list[dict]:
    ids = ids or {}
    sid = lambda t, d: ids.get(t, d)       # ids differ per seed run; the capped run's are read from its capture
    ms = [M(sid(MIGR, "932ff77c"), "rule", MIGR, "user_said", status="force"),
          M(sid("Keep handler functions under 40 lines; extract to `app/services/`.", "05394690"), "preference",
            "Keep handler functions under 40 lines; extract to `app/services/`.", "user_said"),
          M("cad8de35", "decision", "We chose the transactional outbox over direct publishing to the event bus for payment events, "
            "because the bus lost two events during a failover.", "user_said", ["app/events/outbox.py"]),
          M("23e2645b", "decision", "Refunds over 10,000 INR need manager approval in the admin tool.", "agent_inferred",
            ["app/refunds/policy.py"], {"USED": 3, "CONTRADICTED": 2}, challenged=True),
          M("44325bee", "rejected_approach", "Rejected: caching merchant fee schedules in process memory; they change intraday and "
            "workers disagreed.", "user_said", ["app/fees/schedule.py"]),
          M("cd79ee58", "environment", "Integration tests need `docker compose up postgres redis` first and take about 90 seconds to start.",
            "agent_inferred", ["docker-compose.yml"])]
    for n in range(1, extra + 1):
        t = f"Seed rule {n:02d}: module {n} must not run code at import time."
        ms.append(M(ids.get(t) or hashlib.sha1(t.encode()).hexdigest()[:8], "rule", t, "user_said",
                    status="cut" if n > extra - cut else "force", cut_reason=CUT_REASON))
    return ms


def _look(m: dict) -> str:
    """One plain sentence for why an item needs a look, or ''."""
    if m.get("stale"):
        st = m["stale"]
        msg = f' {esc(st["message"])}' if st.get("message") else ""
        return (f'A later change to the code may contradict it: {esc(st["reason"])}. '
                f'Commit <code>{esc(st["commit"][:7])}</code>{msg}.')
    if m["challenged"]:
        return "Agents contradicted it repeatedly, so agents are now told to confirm or fix it before relying on it."
    return ""


def mem_li(m: dict, pid: str, label: bool = False) -> str:
    why, fix = _look(m), ""
    if m.get("stale"):
        fix = (f'If it is still true, keep it with <code>reverie stale keep {m["id"]}</code>. '
               f'If not, forget it with <code>reverie undo {m["id"]}</code>.')
    elif m["challenged"]:
        fix = f'If it is wrong, forget it with <code>reverie undo {m["id"]}</code>. It can be restored.'
    kind = {k: lab for k, _t, lab in KINDS}[m["kind"]]
    meta = [f'<span>{esc(SAID[m["source"]])}</span>']
    if m["anchors"]:
        meta.append("<span>" + ", ".join(f'<code title="{esc(a)}">{esc(a)}</code>' for a in m["anchors"]) + "</span>")
    meta.append(f'<span>{esc(m["date"])}</span>')
    if m["usage"] and usage_words(m["usage"]):
        meta.append(f'<span>{esc(usage_words(m["usage"]))}</span>')
    meta.append(f'<a class="open" href="/rule?p={pid}&amp;id={m["id"]}">Open</a>')
    return (f'<li class="entry">' + (f'<p class="label">{kind}</p>' if label else "")
            + f'<p class="rule">{rule_html(m["text"])}</p>' + (f'<p class="why">{why}</p>' if why else "")
            + f'<p class="meta">{"".join(meta)}</p>' + (f'<p class="todo">{fix}</p>' if fix else "") + "</li>")


def mem_list(ms: list[dict], pid: str) -> str:
    if len(ms) <= 10:
        return f'<ol class="items">{"".join(mem_li(m, pid) for m in ms)}</ol>'
    return (f'<details class="fold"><summary>{len(ms)} items</summary>'
            f'<ol class="items">{"".join(mem_li(m, pid) for m in ms)}</ol></details>')


def memory_body(ms: list[dict], pid: str, index: int, note: str = "") -> str:
    """Memory lists every item except rules; those are listed under Rules (each item is on exactly one tab)."""
    rules_n = sum(1 for m in ms if m["kind"] == "rule")
    ms = [m for m in ms if m["kind"] != "rule"]
    if not ms and not rules_n:
        return ('<p class="tally">Nothing remembered for this repo yet.</p>'
                '<p class="who">Decisions and findings from agent sessions appear here once Reverie has captured some.</p>')
    look = [m for m in ms if _look(m)]
    rest = [m for m in ms if not _look(m)]
    n = len(ms)
    if n:
        told = min(index, n)
        tally = f"{n} remembered {'item' if n == 1 else 'items'}."
        if told:
            tally += f" {told} {'is' if told == 1 else 'are'} told to agents in an index."
        if look:
            tally += f" {len(look)} {'needs' if len(look) == 1 else 'need'} a look."
    else:
        tally = "No decisions or findings remembered yet."
    out = [note, f'<p class="tally">{tally}</p>']
    if rules_n:
        out.append(f'<p class="who">{rules_n} {"rule you stated is" if rules_n == 1 else "rules you stated are"} listed under '
                   f'<a href="/context?p={pid}">Rules</a>.</p>')
    if look:
        out.append('<section class="group" aria-labelledby="g-look"><h2 id="g-look">Needs a look</h2>'
                   f'<ol class="items">{"".join(mem_li(m, pid, True) for m in look)}</ol></section>')
    for kind, title, _lab in KINDS[1:]:
        g = [m for m in rest if m["kind"] == kind]
        if g:
            out.append(f'<section class="group" aria-labelledby="g-{kind}"><h2 id="g-{kind}">{title}</h2>{mem_list(g, pid)}</section>')
    return "".join(out)


# ---- Add -----------------------------------------------------------------------------------------------------
TEAM_FOLDERS = [("team|orgdocs/platform", "orgdocs/platform"), ("team|orgdocs/payments", "orgdocs/payments")]


def add_body(me: str, flash: str = "", error: tuple[str, str] | None = None, text: str = "", why: str = "") -> str:
    """The Add page. ``error`` is (server message, the field it is about: text | target | kind)."""
    owner = PEOPLE[me][1] in DOCS["rules.md"]
    err_id = "add-error"

    def bad(field: str) -> str:
        return f' aria-invalid="true" aria-describedby="{err_id}"' if error and error[1] == field else ""

    if owner:
        repo_d = (f"Everyone on this repo, from the next session. No review: you are an owner of "
                  f"<code>rules.md</code> ({people(DOCS['rules.md'], me)}). Reverie edits the file and does not commit it.")
    else:
        repo_d = (f"Everyone on this repo. A rule waits until an owner of <code>rules.md</code> accepts it "
                  f"({people(DOCS['rules.md'], me, ' or ')}); guidance is added straight away. Reverie edits the file and does not commit it.")
    opts = [("repo", "This repo", repo_d)]
    for v, folder in TEAM_FOLDERS:
        opts.append((v, f"Team folder <code>{esc(folder)}</code>",
                     "Everyone whose repos use this folder, from the next session. No CODEOWNERS entry covers it, so there is no review. "
                     "Reverie edits the file there and does not commit it."))
    radios = "".join(
        f'<label class="opt"><input type="radio" name="target" value="{esc(v)}"{" checked" if i == 0 else ""}{bad("target")}>'
        f'<span><b>{lab}</b><span class="d">{d}</span></span></label>' for i, (v, lab, d) in enumerate(opts))
    kinds = "".join(
        f'<label class="opt"><input type="radio" name="kind" value="{v}"{" checked" if v == "rule" else ""}>'
        f'<span><b>{lab}</b><span class="d">{d}</span></span></label>' for v, lab, d in
        (("rule", "A rule", "Agents are told it at the start of every session."),
         ("guidance", "Guidance", "Agents see a pointer to it and open it when it is relevant.")))
    err = f'<p class="err" id="{err_id}" role="alert">{esc(error[0])}</p>' if error else ""
    form = (f'<form class="addf" method="post" action="/do/add">'
            f'<input type="hidden" name="token" value="{TOKEN}"><input type="hidden" name="p" value="{API}">{err}'
            f'<div><label class="visually-hidden" for="text">The rule</label>'
            f'<textarea id="text" name="text" maxlength="1000" required{bad("text")}>{esc(text)}</textarea></div>'
            f'<fieldset><legend>Who is told</legend>{radios}</fieldset>'
            f'<fieldset><legend>How agents get it</legend>{kinds}</fieldset>'
            f'<label class="why-in">Why (optional)<input type="text" name="why" maxlength="300" value="{esc(why)}">'
            f'<span class="d">Only a proposal keeps this: the owners read it next to the rule. A direct add does not save it.</span></label>'
            f'<div><button class="btn" type="submit">Add rule</button></div></form>')
    cmds = ('<section class="subtle"><details class="cmd"><summary>From a terminal</summary>'
            '<p>The same as this form:</p>'
            '<pre class="block-cmd">reverie context add "Money is stored as integer cents" --why "rounding drift"</pre>'
            '<p>Only for your own agents, in every repo:</p>'
            '<pre class="block-cmd">reverie rules add --user "Prefer rg over grep in examples"</pre>'
            '<p>Turn a rule you already use into a team proposal:</p>'
            '<pre class="block-cmd">reverie context propose "Money is stored as integer cents"</pre></details>'
            '<p>A rule that looks like a secret is refused and not kept.</p></section>')
    return (flash + '<h1 class="ask">What should agents know about this repo?</h1>'
            f'<p class="who">Acting as {name(me)}, from your git identity.</p>' + form + cmds)


EX_RULE = "Run `ruff` before every commit and fix what it reports."


def write(fname: str, html_text: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / fname).write_text(html_text, encoding="utf-8")
    print("wrote", fname)


def main() -> None:
    desc = "Rules people suggested for the team, who can decide each one, and what was turned down."
    for me, fname in (("mudit", "proposals.html"), ("arjun", "proposals-arjun.html"), ("priya", "proposals-priya.html")):
        write(fname, shell(title="Proposals for payments-api, Reverie", pid=API, repo="payments-api",
                           counts=counts_for(me, WAITING), desc=desc, body=proposals_body(me, WAITING, REJECTED, API)))
    # empty: notifications-service, as mudit (nothing there waits on anyone)
    write("empty-proposals.html", shell(title="Proposals for notifications-service, Reverie", pid=NOTIF,
                                        repo="notifications-service", counts={}, desc=desc, body=empty_body(NOTIF)))
    # Overview: mudit, arjun, the capped variant (6 rules cut by the start limit), and an empty project
    odesc = "What needs you in this project: proposals, conflicts and possibly outdated rules."
    for me, fname, cut in (("mudit", "overview.html", 0), ("arjun", "overview-arjun.html", 0), ("mudit", "overview-capped.html", 6)):
        write(fname, shell(title="Overview of payments-api, Reverie", pid=API, repo="payments-api", active="/project",
                           h1="Overview", counts=counts_for(me, WAITING), desc=odesc,
                           body=overview_body(me, WAITING, CONFLICTS, OUTDATED, API, cut)))
    write("empty-overview.html", shell(title="Overview of notifications-service, Reverie", pid=NOTIF,
                                       repo="notifications-service", active="/project", h1="Overview", counts={}, desc=odesc,
                                       body=overview_body("mudit", [], [], [], NOTIF)))

    # Rules: mudit's payments-api (the seed), the capped seed (35 extra remembered rules, 6 cut), and the empty project
    rdesc = "Every rule that applies in this project, where it comes from and whether agents are told it."
    cap = open(OUT.parent / "before" / "start-capped.html", encoding="utf-8").read()
    ids = {html.unescape(t): i for i, t in re.findall(r"- \[([0-9a-f]{8})\] ((?:Never run|Seed rule)[^\n<]*)", cap)}
    write("rules.html", shell(title="Rules of payments-api, Reverie", pid=API, repo="payments-api", active="/context",
                              h1="Rules", counts=counts_for("mudit", WAITING), desc=rdesc,
                              body=rules_body(seed_rules(), API)))
    write("rules-capped.html", shell(title="Rules of payments-api, Reverie", pid=API, repo="payments-api", active="/context",
                                     h1="Rules", counts=counts_for("mudit", WAITING), desc=rdesc,
                                     body=rules_body(seed_rules(35, 6, ids), API)))
    write("empty-rules.html", shell(title="Rules of notifications-service, Reverie", pid=NOTIF, repo="notifications-service",
                                    active="/context", h1="Rules", counts={}, desc=rdesc,
                                    body=rules_body([R(bullet_id(RG), RG, "your", path="~/.reverie/rules.md")], NOTIF)))

    # extra 3: layout test for the states the seed does not have (invented rule text, not product data)
    NARROW = "Verify card webhooks before parsing them"
    st = [
        R("aa00000001", "All services log in JSON, one object per line.", "team", "Organisation folder orgdocs/org", path="orgdocs/org/rules.md", locked=True,
          beats=dict(text="Services may log plain text in development.", where="this repo", why="it is locked, so it wins everywhere")),
        R("aa00000002", "Services may log plain text in development.", "team", RC, path=".reverie/context/rules.md", status="overridden",
          by=dict(text="All services log in JSON, one object per line.", where="orgdocs/org", why="it is locked, so it wins everywhere")),
        R("aa00000003", "Payment webhooks are verified before any parsing.", "team", RC, doc="security.md", path=".reverie/context/security.md",
          paths="app/webhooks/**", beats=dict(text="Verify inbound requests at the edge proxy only.", where="orgdocs/platform", why="it is more specific")),
        R("aa00000004", "Verify inbound requests at the edge proxy only.", "team", RK, path="orgdocs/platform/rules.md", status="overridden",
          by=dict(text="Payment webhooks are verified before any parsing.", where="this repo", why="it is more specific")),
        R("aa00000005", "Rate limits are set per merchant, never per IP.", "team", RC, doc="security.md", path=".reverie/context/security.md",
          status="conflict", other=dict(text="Rate limits are set per IP at the edge.", where="this repo"), owners=["priya@payments.example"]),
        R("aa00000006", "Rate limits are set per IP at the edge.", "team", RC, path=".reverie/context/rules.md",
          status="conflict", other=dict(text="Rate limits are set per merchant, never per IP.", where="this repo"), owners=["mudit@payments.example"]),
        R("aa00000007", "Run the linter before every commit.", "shared", doc="rules.md", path=".reverie/rules.md"),
    ]
    st[4]["owners"] = st[5]["owners"] = ["priya@payments.example", "mudit@payments.example"]
    tn = ('<p class="testnote">Layout test: the states the seed does not have (overridden, locked, a conflict whose '
          'documents have owners, the shared rules file). The rule text is invented and is not product data.</p>')
    write("rules-states.html", shell(title="Layout test, rule states (invented rule text), Reverie", pid=API, repo="payments-api",
                                     active="/context", h1="Rules", counts=counts_for("mudit", WAITING), desc=rdesc,
                                     body=rules_body(st, API, tn)))


    # What agents are told: the text is the start function's own output from the seed captures (design/before/start*.html)
    sdesc = "Exactly what an agent in this repo reads when a session starts."
    sp = dict(repo="payments-api", pid=API, active="/start", h1="What agents are told", desc=sdesc)
    write("start.html", shell(title="What agents are told in payments-api, Reverie", counts=counts_for("mudit", WAITING),
                              body=start_body(seed_rules(), start_text("start.html"), API, asks=CONFLICTS), **sp))
    cap_rules = seed_rules(35, 6, ids)
    write("start-capped.html", shell(title="What agents are told in payments-api, Reverie", counts=counts_for("mudit", WAITING),
                                     body=start_body(cap_rules, start_text("start-capped.html"), API, CONFLICTS), **sp))
    write("empty-start.html", shell(title="What agents are told in notifications-service, Reverie", pid=NOTIF, repo="notifications-service",
                                    active="/start", h1="What agents are told", counts={}, desc=sdesc,
                                    body=start_body([R(bullet_id(RG), RG, "your", path="~/.reverie/rules.md")], start_text("empty-start.html"), NOTIF)))

    # Memory: the seed (with the challenged decision), the capped seed (36 remembered rules, 6 cut), the empty project
    mdesc = "What has been remembered for this repo, and which of it needs a look."
    mp = dict(repo="payments-api", pid=API, active="/memory", h1="Memory", desc=mdesc)
    write("memory.html", shell(title="Memory of payments-api, Reverie", counts=counts_for("mudit", WAITING),
                               body=memory_body(seed_memory(), API, index_count(start_text("start.html"))), **mp))
    cap_ids = {**ids, "Keep handler functions under 40 lines; extract to `app/services/`.": "fe3e3049"}
    cm = seed_memory(35, 6, cap_ids)
    for m in cm:                                  # the capped capture's ids for the five non-rule items
        m["id"] = {"cad8de35": "e318a06b", "23e2645b": "24e9f6e5", "44325bee": "9cf674f3", "cd79ee58": "85e2d8a7"}.get(m["id"], m["id"])
    write("memory-capped.html", shell(title="Memory of payments-api, Reverie", counts=counts_for("mudit", WAITING),
                                      body=memory_body(cm, API, index_count(start_text("start-capped.html"))), **mp))
    write("empty-memory.html", shell(title="Memory of notifications-service, Reverie", pid=NOTIF, repo="notifications-service",
                                     active="/memory", h1="Memory", counts={}, desc=mdesc, body=memory_body([], NOTIF, 0)))
    stale_demo = [
        M("aa10000001", "decision", "Payment events go through a RabbitMQ queue.", "agent_inferred", ["app/events/outbox.py"],
          stale=dict(commit="53bd41853a3ba31564dc49bec9d4f7aa44709ca1", message="events: write to the outbox table", reason="events are now written to the outbox table and the queue client is no longer imported")),
        M("aa10000002", "decision", "Refunds over 10,000 INR need manager approval in the admin tool.", "agent_inferred",
          ["app/refunds/policy.py"], {"USED": 3, "CONTRADICTED": 2}, challenged=True),
        M("aa10000003", "environment", "Run `make fixtures` before the integration tests.", "agent_inferred", ["Makefile"],
          stale=dict(commit="ddf941a91198e58d140e4ffac43deb5058f9edb6", message="", reason="the Makefile target was removed")),
        M("aa10000004", "rule", "Never run migrations against the production replica.", "user_said", status="force")]
    write("memory-states.html", shell(title="Layout test, memory states (invented text), Reverie", counts=counts_for("mudit", WAITING),
                                      body=memory_body(stale_demo, API, 3,
                                                       '<p class="testnote">Layout test: items the seed does not have (possibly outdated, with and without a commit message). The text is invented and is not product data.</p>'),
                                      **mp))

    # Add: the form as an owner and as a non-owner, the refusals, and the page after a successful add
    adesc = "Add a rule or guidance for this repo or a team folder."
    ap = dict(title="Add a rule to payments-api, Reverie", pid=API, repo="payments-api", active="/add", h1=None,
              counts=counts_for("mudit", WAITING), desc=adesc)
    write("add.html", shell(body=add_body("mudit"), **ap))
    write("add-arjun.html", shell(**{**ap, "counts": counts_for("arjun", WAITING)}, body=add_body("arjun")))
    write("add-error.html", shell(body=add_body("mudit", error=("Nothing was saved: write the rule first.", "text")), **ap))
    write("add-error-kept.html", shell(body=add_body(
        "mudit", error=("Nothing was saved: this project cannot add to that place. Pick another option.", "target"), text=EX_RULE,
        why="the linter catches what reviews miss"), **ap))
    write("add-error-secret.html", shell(body=add_body(
        "mudit", error=("Nothing was saved: this looks like it contains a secret. Remove the value and try again. The text was cleared on purpose.", "text")), **ap))
    check = ('It has not been checked for conflicts yet: that happens at the next session start, or now with '
             '<code>reverie context check</code>.')
    write("add-done.html", shell(body=add_body("mudit", flash=(
        f'<p class="flash" role="status">Added to <code>rules.md</code>: “{rule_html(EX_RULE)}” It applies from the next session. {check} <a href="/context?p={API}">See it in Rules</a></p>')), **ap))
    write("add-arjun-done.html", shell(**{**ap, "counts": counts_for("arjun", WAITING)}, body=add_body("arjun", flash=(
        f'<p class="flash" role="status">Proposed: “{rule_html(EX_RULE)}” It waits for an owner of <code>rules.md</code>: '
        f'{people(DOCS["rules.md"], "arjun", " or ")}. It is not in force yet. <a href="/proposals?p={API}">See the proposal</a></p>'))))
    # Rule page: one page per case (read-only; commands shown quietly)
    ldesc = "One rule: where it comes from, its status and what to do about it."
    pg = dict(pid=API, repo="payments-api", h1=None, counts=counts_for("mudit", WAITING), desc=ldesc)
    inv = ('<p class="testnote">Layout test: the seed has no overridden rule. The rule text is invented and is not product data.</p>')
    ledger = dict(id="59cf39933e", kind="team", layer="repo", folder=".reverie/context", section="Rules", owners=DOCS["paths-services-ledger.md"],
                  path=".reverie/context/paths-services-ledger.md", paths="services/ledger/**",
                  text="Ledger entries are append-only: corrections are new reversing entries, never UPDATE or DELETE, because auditors replay the table.")
    write("rule-team.html", shell(title="Rule 59cf39933e, Reverie", active="/context", body=rule_body(ledger, API), **pg))
    ra = dict(id="e3d2757f37", kind="team", layer="team", folder="orgdocs/platform", section="Rules", owners=[],
              path="orgdocs/platform/rules.md", text=RETRY_A, status="Conflict",
              conflict=dict(key="http-retries", reason="both come from team folders at the same level, so neither one wins.", owners=[],
                            other=dict(id="887e6b40e1", text=RETRY_B, **{"from": "Team folder orgdocs/payments"}, doc="rules.md",
                                       path="orgdocs/payments/rules.md")))
    write("rule-conflict.html", shell(title="Rule e3d2757f37, Reverie", active="/context", body=rule_body(ra, API), **pg))
    o = ODOC["9c55b68de3"]
    ro = dict(id="9c55b68de3", kind="team", layer="repo", folder=".reverie/context", section="Rules", owners=DOCS["rules.md"],
              path=".reverie/context/rules.md", text=o["text"], status="Possibly outdated",
              outdated=dict(reason=o["reason"], commit=o["commit"], message=o["message"]))
    write("rule-outdated.html", shell(title="Rule 9c55b68de3, Reverie", active="/context", body=rule_body(ro, API), **pg))
    rv = dict(id="aa00000004", kind="team", layer="team", folder="orgdocs/platform", section="Rules", owners=[],
              path="orgdocs/platform/rules.md", text="Verify inbound requests at the edge proxy only.", status="Overridden",
              overridden=dict(key="webhook-verify", why="it is more specific, so it answers this one", other=dict(
                  id="aa00000003", text="Payment webhooks are verified before any parsing.", **{"from": "This repo"}, doc="security.md",
                  path=".reverie/context/security.md", paths="app/webhooks/**")))
    write("rule-overridden.html", shell(title="Rule aa00000004 (layout test), Reverie", active="/context", body=rule_body(rv, API, inv), **pg))
    mem = dict(id="932ff77c", text=MIGR, said="You said it", date="2026-11-12")
    write("rule-remembered.html", shell(title="Rule 932ff77c, Reverie", active="/context", body=rule_body(dict(id=mem["id"], text=MIGR, _html=remembered_html(mem, "Remembered rule")), API), **{**pg, "desc": "One remembered rule: who said it, when, and how agents used it."}))
    od = dict(id="aa10000001", text="Payment events go through a RabbitMQ queue.", said="An agent inferred it", date="2026-11-12",
              files=["app/events/outbox.py"], stale=dict(commit="53bd41853a3ba31564dc49bec9d4f7aa44709ca1", message="events: write to the outbox table",
                                                        reason="events are now written to the outbox table and the queue client is no longer imported"))
    write("rule-remembered-outdated.html", shell(title="Remembered decision aa10000001 (layout test), Reverie", active="/memory", body=rule_body(
        dict(id=od["id"], text=od["text"], status="Possibly outdated", home=("Memory", "/memory"),
             _html=remembered_html(od, "Remembered decision")), API, '<p class="testnote">Layout test: the seed has no possibly outdated memory item. The text is invented and is not product data.</p>'),
        **{**pg, "desc": "One remembered decision: who said it, what it is anchored to and what to do about it."}))
    ch = dict(id="23e2645b", text="Refunds over 10,000 INR need manager approval in the admin tool.", said="An agent inferred it",
              evidence="approval gate in `app/refunds/policy.py`", date="2026-11-12", files=["app/refunds/policy.py"],
              usage="Used 3 times, contradicted twice.", challenged=True)
    write("rule-remembered-challenged.html", shell(title="Remembered decision 23e2645b, Reverie", active="/memory", body=rule_body(
        dict(id=ch["id"], text=ch["text"], status="Challenged", home=("Memory", "/memory"),
             _html=remembered_html(ch, "Remembered decision")), API), **{**pg, "desc": "One remembered decision: who said it, what it is anchored to and how agents used it."}))
    # extra 1: the page right after Mudit accepts "Prefer small PRs" (the one place the teal appears)
    accepted = next(p for p in WAITING if p["slug"].startswith("prefer-small-prs"))
    after = [p for p in WAITING if p is not accepted]
    flash = (f'<p class="flash" role="status">Accepted into rules.md: “{rule_html(accepted["text"])}”. The proposal is gone '
             'and the rule is <span class="in">in force</span>. It has not been checked for conflicts yet: that happens at '
             'the next session start, or now with <code>reverie context check</code>.</p>')
    write("proposals-accepted.html", shell(
        title="Proposals for payments-api, Reverie", pid=API, repo="payments-api",
        counts=counts_for("mudit", after), desc=desc, body=proposals_body("mudit", after, REJECTED, API, flash)))
    # extra 2: layout test, 15 waiting (invented rule texts: not product data)
    many = list(WAITING)
    for i, (text, why, doc) in enumerate(STRESS_TEXTS):
        many.append(dict(slug=slugify(text, 0x1000 + i), id=f"p-{0xa00000 + i:010x}", text=text, why=why,
                         by="priya" if doc.startswith("paths") else "arjun",
                         date="2026-11-12", doc=doc, paths="services/ledger/**" if doc.startswith("paths") else ""))
    note = ('<p class="testnote">Layout test: 15 proposals waiting. The first three are the seed; the other twelve '
            'use invented rule text and are not product data.</p>')
    write("proposals-15.html", shell(
        title="Layout test, 15 proposals waiting (invented rule text), Reverie", pid=API, repo="payments-api",
        counts=counts_for("mudit", many), desc=desc, body=proposals_body("mudit", many, REJECTED, API, note=note)))


if __name__ == "__main__":
    main()
