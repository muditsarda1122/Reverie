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

rid() {   # rid PAGE_HTML TEXT_FRAGMENT -> the id in the Open link that follows that rule text (HTML tags and entities ignored)
  python3 - "$1" "$2" <<'PY'
import html, re, sys
page = open(sys.argv[1], encoding="utf-8").read()
for m in re.finditer(r'<li class="entry">(.*?)</li>', page, re.S):
    block = m.group(1)
    first = re.search(r'<p class="rule">(.*?)</p>', block, re.S)
    if first and sys.argv[2] in html.unescape(re.sub(r"<[^>]+>", "", first.group(1))):
        found = re.search(r'/rule\?p=[0-9a-f]{8}&amp;id=([^"]+)"', block)
        if found:
            print(found.group(1)); break
PY
}
linkcheck() {   # linkcheck PORT FILE... : every internal link on the saved pages must answer 200 on the live server
  python3 "$ROOT/scripts/crawl-links.py" "$@"
}
post() {   # post PATH FIELDS SAVE_AS : submit a form the way a browser does (token from /add), follow the redirect, save the page
  local tok
  tok="$(curl -s "http://127.0.0.1:$PORT/add?p=$API" | python3 -c 'import re,sys; print(re.search(r"name=\"token\" value=\"([^\"]+)\"", sys.stdin.read()).group(1))')"
  curl -s -L -H "Origin: http://127.0.0.1:$PORT" --data-urlencode "token=$tok" --data-urlencode "p=$API" "${@:2:$#-2}" \
       "http://127.0.0.1:$PORT$1" -o "$OUT/${@: -1}"; echo "saved ${@: -1}"
}
D1="$TMP/seed"; start "$D1"
API="$(pid_of "$D1/payments-api")"; NOTIF="$(pid_of "$D1/notifications-service")"; OFF="$(pid_of "$D1/legacy-billing")"
for who in mudit arjun priya; do
  as "$D1" "$who"
  sfx=""; [ "$who" != mudit ] && sfx="-$who"
  grab "/project?p=$API"   "overview$sfx.html"
  grab "/proposals?p=$API" "proposals$sfx.html"
done
as "$D1" mudit
grab "/context?p=$API" rules.html
grab "/memory?p=$API"  memory.html
grab "/start?p=$API"   start.html
for pair in "rule-team:Ledger entries are append-only:rules.html" "rule-conflict:All outbound HTTP calls:rules.html" \
            "rule-outdated:do not call the ledger service:rules.html" "rule-remembered:Never run migrations:rules.html" \
            "rule-remembered-challenged:Refunds over 10,000 INR:memory.html"; do
  name="${pair%%:*}"; rest="${pair#*:}"; src="${rest##*:}"; frag="${rest%:*}"
  id="$(rid "$OUT/$src" "$frag")"
  [ -n "$id" ] && grab "/rule?p=$API&id=$id" "$name.html" || echo "no id for $name" >&2
done
grab "/rule?p=$API&id=nosuchrule" not-found-rule.html
linkcheck "$PORT" "$OUT"/overview.html "$OUT"/overview-arjun.html "$OUT"/overview-priya.html "$OUT"/proposals*.html "$OUT"/rules.html "$OUT"/start.html "$OUT"/memory.html "$OUT"/rule-*.html
mv "$D1/home/context" "$D1/home/context.aside"
grab "/project?p=$NOTIF"   empty-overview.html
grab "/proposals?p=$NOTIF" empty-proposals.html
grab "/context?p=$NOTIF"   empty-rules.html
grab "/memory?p=$NOTIF"    empty-memory.html
grab "/start?p=$NOTIF"     empty-start.html
mv "$D1/home/context.aside" "$D1/home/context"
grab "/project?p=deadbeef" unknown-project.html
grab "/" board.html
grab "/new" new.html
grab "/connect?p=$API" connect.html
grab "/connect?p=$OFF" connect-off.html
grab "/project?p=$OFF" off.html
grab "/context?p=$OFF" off-rules.html
grab "/nosuchpage" not-found.html
grab "/search?p=$API&q=ledger" search.html
grab "/search?p=$API&q=refund" search-both.html
grab "/search?p=$API&q=kubernetes" search-empty.html
grab "/search?p=$API&q=" search-blank.html
python3 -c 'from reverie import ui; print(ui.error_page(), end="")' > "$OUT/error.html"; echo "saved error.html (ui.error_page(): a 500 cannot be forced on the live server)"
grab "/add?p=$API" add.html
post /do/add --data-urlencode "text=" --data-urlencode "kind=rule" --data-urlencode "target=repo" add-error.html
post /do/add --data-urlencode "text=Run \`ruff\` before every commit and fix what it reports." --data-urlencode "kind=rule" \
     --data-urlencode "target=nowhere" --data-urlencode "why=the linter catches what reviews miss" add-error-kept.html
post /do/add --data-urlencode "text=The key is sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" --data-urlencode "kind=rule" \
     --data-urlencode "target=repo" add-error-secret.html
post /do/add --data-urlencode "text=Run \`ruff\` before every commit and fix what it reports." --data-urlencode "kind=rule" \
     --data-urlencode "target=repo" add-done.html
as "$D1" arjun
grab "/add?p=$API" add-arjun.html
post /do/add --data-urlencode "text=Run \`ruff\` before every commit and fix what it reports." --data-urlencode "kind=rule" \
     --data-urlencode "target=repo" add-arjun-done.html
as "$D1" mudit
linkcheck "$PORT" "$OUT"/board.html "$OUT"/new.html "$OUT"/connect.html "$OUT"/connect-off.html "$OUT"/off.html "$OUT"/search*.html "$OUT"/add*.html \
          "$OUT"/not-found.html "$OUT"/error.html "$OUT"/unknown-project.html
D2="$TMP/seed-capped"; start "$D2" --extra-repo-rules 35
API2="$(pid_of "$D2/payments-api")"
grab "/project?p=$API2" overview-capped.html
grab "/context?p=$API2" rules-capped.html
grab "/memory?p=$API2"  memory-capped.html
grab "/start?p=$API2"   start-capped.html
linkcheck "$PORT" "$OUT"/overview-capped.html "$OUT"/rules-capped.html "$OUT"/memory-capped.html "$OUT"/start-capped.html
echo "seeds were in $TMP (throwaway)"
