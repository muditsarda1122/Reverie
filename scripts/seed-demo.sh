#!/usr/bin/env bash
# Build the `payments-api` demo seed from docs/ui-inventory.md section 8, in a throwaway folder.
#
#   scripts/seed-demo.sh [--dir DIR] [--as mudit|priya|arjun] [--extra-repo-rules N] [--ui [--port P]]
#
#   --dir DIR              where to build (default: a new temp folder). If DIR already holds a seed it is reused
#                          (nothing is rebuilt), so `--dir DIR --as arjun --ui` just views it as someone else.
#   --as WHO               the git identity used when viewing (default mudit). Seeding itself writes each
#                          document as the person who owns it, then switches to WHO.
#   --extra-repo-rules N   also remember N extra repo-level rules (35 gives the "cut by the start limit" state).
#   --ui [--port P]        start `reverie ui` on 127.0.0.1 against the seed (foreground; Ctrl-C stops it).
#
# Safety: everything lives under DIR. HOME, REVERIE_HOME and the git config are all pointed inside it, so
# neither ~/.reverie nor your real git config is read or written. No network, no model calls.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REVERIE_PY:-$ROOT/.venv/bin/python}"
DIR="" WHO="mudit" EXTRA=0 UI=0 PORT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --as) WHO="$2"; shift 2 ;;
    --extra-repo-rules) EXTRA="$2"; shift 2 ;;
    --ui) UI=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$WHO" in mudit|priya|arjun) ;; *) echo "--as must be mudit, priya or arjun" >&2; exit 2 ;; esac
[ -x "$PY" ] || { echo "python not found at $PY (set REVERIE_PY)" >&2; exit 2; }

[ -n "$DIR" ] || DIR="$(mktemp -d "${TMPDIR:-/tmp}/reverie-demo.XXXXXX")"
mkdir -p "$DIR"; DIR="$(cd "$DIR" && pwd)"

# Everything below sees only the throwaway world.
export HOME="$DIR/userhome" REVERIE_HOME="$DIR/home" REVERIE_NO_BACKGROUND=1
export GIT_CONFIG_GLOBAL="$DIR/gitconfig" GIT_CONFIG_NOSYSTEM=1 PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1
unset ANTHROPIC_API_KEY OPENAI_API_KEY OPENCODE_ZEN_API_KEY GEMINI_API_KEY OPENROUTER_API_KEY
mkdir -p "$HOME"

set_identity() {   # rewrite the throwaway global git config
  case "$1" in
    mudit) n="Mudit Sarda"; e="mudit@payments.example" ;;
    priya) n="Priya Nair"; e="priya@payments.example" ;;
    arjun) n="Arjun Mehta"; e="arjun@payments.example" ;;
  esac
  printf '[user]\n\tname = %s\n\temail = %s\n[init]\n\tdefaultBranch = main\n' "$n" "$e" > "$GIT_CONFIG_GLOBAL"
}
export -f set_identity 2>/dev/null || true

if [ ! -f "$DIR/.seeded" ]; then
  echo "seeding $DIR" >&2
  export DEMO_DIR="$DIR" DEMO_EXTRA="$EXTRA"
  "$PY" -I - <<'PYEOF'
import os, subprocess, sys
sys.path.insert(0, os.environ["PYTHONPATH"])
from pathlib import Path
from reverie import cli, context, deliver, items, rules

W = Path(os.environ["DEMO_DIR"])
EXTRA = int(os.environ["DEMO_EXTRA"])
GC = Path(os.environ["GIT_CONFIG_GLOBAL"])
PEOPLE = {"mudit": ("Mudit Sarda", "mudit@payments.example"),
          "priya": ("Priya Nair", "priya@payments.example"),
          "arjun": ("Arjun Mehta", "arjun@payments.example")}


def who(p):
    n, e = PEOPLE[p]
    GC.write_text(f"[user]\n\tname = {n}\n\temail = {e}\n[init]\n\tdefaultBranch = main\n")


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


who("mudit")
api, notif, legacy = W / "payments-api", W / "notifications-service", W / "legacy-billing"
for r in (api, notif, legacy):
    r.mkdir(parents=True)
    git(r, "init", "-q")

# --- the repo's code (real files the rules point at) -------------------------------------------
write(api / "README.md", "# payments-api\n")
write(api / "app/clients/ledger.py",
      "def write_entry(client, entry):\n    return client.post('/entries', entry)  # retry and outbox live in here\n")
write(api / "app/webhooks/verify.py",
      "def verify_signature(payload, header):\n    return header == sign(payload)\n\n\ndef sign(payload):\n    return payload\n")
write(api / "app/events/outbox.py", "def publish(event):\n    pass\n")
write(api / "app/fees/schedule.py", "FEES = {}\n")
write(api / "app/refunds/policy.py", "LIMIT = 10_000\n")
write(api / "docker-compose.yml", "services: {}\n")
commit(api, "payments-api: initial service skeleton")

# --- authority, sources, and the two sibling team folders (the tag conflict) -------------------
write(api / ".github/CODEOWNERS",
      "/.reverie/context/rules.md       priya@payments.example mudit@payments.example\n"
      "/.reverie/context/security.md    priya@payments.example\n"
      "/.reverie/context/paths-services-ledger.md  mudit@payments.example\n")
write(api / ".reverie/context.toml", '[sources]\nteam = ["../orgdocs/platform", "../orgdocs/payments"]\n')
write(W / "orgdocs/platform/rules.md",
      "# Platform team context\n\n## Rules\n\n"
      "- All outbound HTTP calls retry up to 3 times with exponential backoff {id: http-retries}\n")
write(W / "orgdocs/payments/rules.md",
      "# Payments team context\n\n## Rules\n\n"
      "- Never retry the card processor's `charge` endpoint; use the idempotency key and poll status instead {id: http-retries}\n")
(api / ".reverie/context").mkdir(parents=True, exist_ok=True)
commit(api, "authority: CODEOWNERS and team sources")

R = str(api)

# --- repo rules, each written by the person who owns that document -----------------------------
who("mudit")
for t in (
    "Money is stored and passed as integer minor units (cents) with an ISO 4217 currency code; never use floats, because rounding drift broke reconciliation in the March settlement run.",
    "Every endpoint that creates a charge or refund must require an `Idempotency-Key` header and store the response for 24 hours, because card-network retries otherwise double-charge.",
    "Database migrations must be backward compatible with the previous release, because we deploy with rolling restarts and `alembic` runs before the new pods are ready.",
    "Use `app/clients/ledger.py` for all ledger writes; do not call the ledger service directly from handlers, because it owns retry and outbox semantics.",
    "Log payment identifiers (`payment_id`, `merchant_id`) but never card numbers, CVV or full bank account numbers, because logs ship to a third-party aggregator.",
):
    assert not context.add(R, t).proposal
assert not context.add(
    R, "Ledger entries are append-only: corrections are new reversing entries, never UPDATE or DELETE, because auditors replay the table.",
    doc="paths-services-ledger", paths=("services/ledger/**",)).proposal
who("priya")
for t in (
    "Secrets come from the secrets manager through `app/config.py`; never read them from environment files committed to the repo, because we rotate them quarterly.",
    "Webhook payloads from the card processor must be verified with `verify_signature()` in `app/webhooks/verify.py` before any parsing.",
):
    assert not context.add(R, t, doc="security").proposal
who("mudit")
commit(api, "context: payments rules")

# --- two real commits that make rules 4 and 7 possibly outdated --------------------------------
write(api / "app/clients/ledger.py",
      "class LedgerWriter:\n    \"\"\"Owns the outbox; handlers call it directly.\"\"\"\n\n"
      "    def write(self, entry):\n        self.outbox.append(entry)\n")
c_ledger = commit(api, "ledger: route writes through LedgerWriter")
write(api / "app/webhooks/verify.py",
      "def verify_webhook(payload, header, tolerance=300):\n    return header == sign(payload)\n\n\ndef sign(payload):\n    return payload\n")
c_verify = commit(api, "webhooks: rename verify_signature to verify_webhook")

res = context.resolve(R)
by_text = {i.text: i for i in res.items}
r4 = next(i for t, i in by_text.items() if t.startswith("Use `app/clients/ledger.py`"))
r7 = next(i for t, i in by_text.items() if t.startswith("Webhook payloads"))
st = context.load_state()
st["stale"] = {
    r4.id: {"commit": c_ledger, "message": "ledger: route writes through LedgerWriter",
            "reason": "handlers now call LedgerWriter directly; ledger.py no longer wraps retries", "file": r4.file},
    r7.id: {"commit": c_verify, "message": "webhooks: rename verify_signature to verify_webhook",
            "reason": "verify_signature no longer exists; verification moved to verify_webhook with a timestamp tolerance",
            "file": r7.file},
}
context.save_state(st)

# --- proposals: 3 waiting, 1 turned down --------------------------------------------------------
who("priya")
assert context.add(R, "Ledger reconciliation jobs must be idempotent per `batch_id`", doc="paths-services-ledger",
                   paths=("services/ledger/**",),
                   why="the nightly job re-ran twice last week and posted duplicates").proposal
who("arjun")
assert context.add(R, "Prefer small PRs: under 400 changed lines unless it is a mechanical rename",
                   why="reviews of the 1,200-line refund PR took four days").proposal
assert context.add(R, "Run `pip-audit` in CI and fail the build on a high severity finding", doc="security",
                   why="we shipped with an unpatched `cryptography` last quarter").proposal
assert context.add(R, "Use the ORM for all queries, no raw SQL", why="raw SQL keeps creeping into handlers").proposal
who("priya")
orm = next(p for p in context.proposals(R) if "ORM" in p["bullet"])
context.reject(R, orm["slug"], "Reporting queries need raw SQL for window functions; the ORM version was 10x slower "
                               "in the settlement report. Reopen with a benchmark.")
who("mudit")
commit(api, "context: proposals")

# --- memory (6 items), personal rules, and the other projects -----------------------------------
os.chdir(api)
assert cli.main(["enable"]) == 0
conn = items.connect(deliver.db_path())
key = deliver.repo_key(R)
items.add(conn, text="Never run migrations against the production replica; use the primary and the `--dry-run` flag first.",
          kind="rule", source="user_said", repo_key=key)
items.add(conn, text="Keep handler functions under 40 lines; extract to `app/services/`.",
          kind="preference", source="user_said", repo_key=key)
items.add(conn, text="We chose the transactional outbox over direct publishing to the event bus for payment events, "
                     "because the bus lost two events during a failover.",
          kind="decision", source="user_said", repo_key=key, anchor_=items.anchor(files=["app/events/outbox.py"]))
items.add(conn, text="Rejected: caching merchant fee schedules in process memory; they change intraday and workers disagreed.",
          kind="rejected_approach", source="user_said", repo_key=key, anchor_=items.anchor(files=["app/fees/schedule.py"]))
items.add(conn, text="Integration tests need `docker compose up postgres redis` first and take about 90 seconds to start.",
          kind="environment", source="agent_inferred", repo_key=key, anchor_=items.anchor(files=["docker-compose.yml"]),
          evidence="Integration tests need `docker compose up postgres redis` first and take about 90 seconds to start.")
items.add(conn, text="Refunds over 10,000 INR need manager approval in the admin tool.",
          kind="decision", source="agent_inferred", repo_key=key, anchor_=items.anchor(files=["app/refunds/policy.py"]),
          evidence="approval gate in `app/refunds/policy.py`",
          meta={"usage": {"USED": 3, "CONTRADICTED": 2}, "challenged": True})
for n in range(1, EXTRA + 1):
    items.add(conn, text=f"Seed rule {n:02d}: module {n} must not run code at import time.",
              kind="rule", source="user_said", repo_key=key)
conn.close()

# Personal rules. The first is an exact copy of team rule 1, kept as a personal-layer context rule: the Rules page marks it
# "also a shared rule: hidden here" and the start block leaves it out. The second is a plain bullet in ~/.reverie/rules.md.
RULE1 = "Money is stored and passed as integer minor units (cents) with an ISO 4217 currency code; never use floats, because rounding drift broke reconciliation in the March settlement run."
context.add(R, RULE1, layer="personal")
rules.add("Prefer `rg` over `grep` in examples", level="user")

for r in (notif, legacy):
    write(r / "README.md", f"# {r.name}\n")
    commit(r, "initial")
os.chdir(notif)
assert cli.main(["enable"]) == 0
os.chdir(legacy)
assert cli.main(["enable"]) == 0
assert cli.main(["disable"]) == 0
(W / ".seeded").write_text("payments-api\n")
PYEOF
fi

set_identity "$WHO"
cat >&2 <<EOF

seed:      $DIR
projects:  $DIR/payments-api  $DIR/notifications-service  $DIR/legacy-billing (turned off)
viewing as: $WHO ($(git config --file "$GIT_CONFIG_GLOBAL" user.email))
environment for your own commands against this seed:
  export HOME='$HOME' REVERIE_HOME='$REVERIE_HOME' GIT_CONFIG_GLOBAL='$GIT_CONFIG_GLOBAL' GIT_CONFIG_NOSYSTEM=1
EOF

if [ "$UI" = 1 ]; then
  cd "$DIR/payments-api"
  exec "$PY" -I -c "import sys; sys.path.insert(0, '$ROOT'); from reverie.cli import main; sys.exit(main(sys.argv[1:]))" \
    ui --no-open --port "$PORT"
fi
echo "$DIR"
