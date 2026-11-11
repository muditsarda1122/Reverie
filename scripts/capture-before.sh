#!/usr/bin/env bash
# Save the Reverie web UI, as it is today, as static HTML in design/before/.
# Builds two throwaway seeds with scripts/seed-demo.sh (the normal one and one with 35 extra repo rules),
# serves each with `reverie ui`, and fetches every page as the identity named in design/before/README.md.
# Nothing under ~/.reverie or your real git config is touched. Usage: scripts/capture-before.sh [OUT_DIR]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/design/before}"
SEED="$ROOT/scripts/seed-demo.sh"
mkdir -p "$OUT"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/reverie-capture.XXXXXX")"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
pid_of() {   # the UI's opaque project id: sha1 of the repo's toplevel path, first 8 hex
  python3 -c 'import hashlib,subprocess,sys; t=subprocess.check_output(["git","-C",sys.argv[1],"rev-parse","--show-toplevel"],text=True).strip(); print(hashlib.sha1(t.encode()).hexdigest()[:8])' "$1"
}
start() {    # start DIR -> sets PORT, serving as mudit
  "$SEED" --dir "$1" --as mudit "${@:2}" >/dev/null 2>&1
  PORT="$(free_port)"
  "$SEED" --dir "$1" --as mudit --ui --port "$PORT" >"$TMP/ui-$PORT.log" 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 60); do curl -fs "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && return 0; sleep 0.5; done
  echo "ui did not start; see $TMP/ui-$PORT.log" >&2; exit 1
}
grab() { curl -fs "http://127.0.0.1:$PORT$1" -o "$OUT/$2"; echo "saved $2"; }
as() { "$SEED" --dir "$1" --as "$2" >/dev/null 2>&1; }     # identity is read per request: no restart needed

# ---- the normal seed ----------------------------------------------------------------------------
D1="$TMP/seed"; start "$D1"
API="$(pid_of "$D1/payments-api")"; NOTIF="$(pid_of "$D1/notifications-service")"; OFF="$(pid_of "$D1/legacy-billing")"
grab "/"                         projects.html
grab "/new"                      new.html
grab "/project?p=$API"           overview.html
grab "/context?p=$API"           rules.html
grab "/memory?p=$API"            memory.html
grab "/start?p=$API"             start.html
grab "/proposals?p=$API"         proposals.html
grab "/add?p=$API"               add.html
grab "/connect?p=$API"           connect.html
grab "/search?q=ledger&p=$API"   search-ledger.html
grab "/project?p=$OFF"           disabled-project.html
as "$D1" arjun; grab "/proposals?p=$API" proposals-arjun.html
as "$D1" priya; grab "/proposals?p=$API" proposals-priya.html
as "$D1" mudit
# The personal-layer rule lives in REVERIE_HOME and so applies in every repo; set it aside so the empty project's
# Rules and start screens show the true empty state, then put it back.
mv "$D1/home/context" "$D1/home/context.aside"
grab "/project?p=$NOTIF"         empty-overview.html
grab "/context?p=$NOTIF"         empty-rules.html
grab "/memory?p=$NOTIF"          empty-memory.html
grab "/proposals?p=$NOTIF"       empty-proposals.html
grab "/start?p=$NOTIF"           empty-start.html
mv "$D1/home/context.aside" "$D1/home/context"

# ---- the variant with 35 extra repo rules (start-limit cut) -------------------------------------
D2="$TMP/seed-capped"; start "$D2" --extra-repo-rules 35
API2="$(pid_of "$D2/payments-api")"
grab "/project?p=$API2"          overview-capped.html
grab "/start?p=$API2"            start-capped.html
echo "seeds were in $TMP (throwaway)"
