#!/usr/bin/env bash
# Save the pages of the REAL `reverie ui` (the code in reverie/ui.py), served on the demo seeds, as static HTML in
# design/live/, named like the mockups in design/after/ so the two can be compared. Same seeds as capture-before.sh:
# throwaway home and git config, nothing under ~/.reverie is touched. Usage: scripts/capture-live.sh [OUT_DIR]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/design/live}"
SEED="$ROOT/scripts/seed-demo.sh"
mkdir -p "$OUT"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/reverie-live.XXXXXX")"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
pid_of() { python3 -c 'import hashlib,subprocess,sys; t=subprocess.check_output(["git","-C",sys.argv[1],"rev-parse","--show-toplevel"],text=True).strip(); print(hashlib.sha1(t.encode()).hexdigest()[:8])' "$1"; }
start() {
  "$SEED" --dir "$1" --as mudit "${@:2}" >/dev/null 2>&1
  PORT="$(free_port)"
  "$SEED" --dir "$1" --as mudit --ui --port "$PORT" >"$TMP/ui-$PORT.log" 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 60); do curl -fs "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && return 0; sleep 0.5; done
  echo "ui did not start; see $TMP/ui-$PORT.log" >&2; exit 1
}
grab() { curl -s "http://127.0.0.1:$PORT$1" -o "$OUT/$2"; echo "saved $2"; }       # no -f: 404 and 500 pages are pages too
as() { "$SEED" --dir "$1" --as "$2" >/dev/null 2>&1; }

D1="$TMP/seed"; start "$D1"
API="$(pid_of "$D1/payments-api")"; NOTIF="$(pid_of "$D1/notifications-service")"; OFF="$(pid_of "$D1/legacy-billing")"
for who in mudit arjun priya; do
  as "$D1" "$who"
  sfx=""; [ "$who" != mudit ] && sfx="-$who"
  grab "/project?p=$API"   "overview$sfx.html"
  grab "/proposals?p=$API" "proposals$sfx.html"
done
as "$D1" mudit
mv "$D1/home/context" "$D1/home/context.aside"
grab "/project?p=$NOTIF"   empty-overview.html
grab "/proposals?p=$NOTIF" empty-proposals.html
mv "$D1/home/context.aside" "$D1/home/context"
grab "/project?p=deadbeef" unknown-project.html
D2="$TMP/seed-capped"; start "$D2" --extra-repo-rules 35
API2="$(pid_of "$D2/payments-api")"
grab "/project?p=$API2" overview-capped.html
echo "seeds were in $TMP (throwaway)"
