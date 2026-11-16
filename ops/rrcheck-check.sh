#!/usr/bin/env bash
# Run check_rrcheck.py, log the result, and on failure alert via the existing
# forgejo-pushover container (same mechanism as forgejo-backup's alert.sh:
# the Pushover credentials stay inside that container).
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
out=$(uv run --quiet check_rrcheck.py 2>&1)
code=$?
out=$(printf '%s\n' "$out" | grep -v '^Installed')
headline=$(printf '%s\n' "$out" | head -1)
{ echo "== $(date '+%Y-%m-%d %H:%M:%S') exit=$code"; printf '%s\n' "$out"; } >> logs/rrcheck-check.log

case $code in
  0) title="Roborock: still working"; prio=0 ;;
  2) title="Roborock: rrcheck BLOCKED commands"; prio=1 ;;
  3) title="Roborock: vacuum off the local broker"; prio=1 ;;
  *) title="Roborock: check failed"; prio=1 ;;
esac
logger -t roborock-rrcheck -p "user.$([ "$code" = 0 ] && echo info || echo err)" -- "$title: $headline" || true

# Pushover only on failure; a pass is in the log and the journal.
[ "$code" = 0 ] && exit 0

read -r -d '' py <<'PY' || true
import os, sys, urllib.parse, urllib.request
data = urllib.parse.urlencode({
    "token": os.environ["PUSHOVER_APP_TOKEN"], "user": os.environ["PUSHOVER_USER_KEY"],
    "title": sys.argv[1][:250], "message": sys.argv[2][:1024], "priority": sys.argv[3],
}).encode()
with urllib.request.urlopen(urllib.request.Request("https://api.pushover.net/1/messages.json", data=data), timeout=15) as r:
    if r.status >= 300:
        raise SystemExit(f"Pushover HTTP {r.status}")
PY
if docker ps --format '{{.Names}}' | grep -qx forgejo-pushover; then
  docker exec forgejo-pushover python3 -c "$py" "$title" "$out" "$prio" \
    || logger -t roborock-rrcheck -p user.err -- "Pushover alert failed; see logs/rrcheck-check.log" || true
else
  logger -t roborock-rrcheck -p user.warning -- "pushover container absent; alert skipped" || true
fi
exit "$code"
