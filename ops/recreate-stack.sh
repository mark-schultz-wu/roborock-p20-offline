#!/usr/bin/env bash
# Recreate the local Roborock stack's server container (ops, 2026-11-14). Since the reboot on
# 2026-11-13 13:07 the container runs but Docker publishes none of its ports (no docker-proxy for
# 443/555/8881/3478): the vacuum's MQTT connection to the AP's :8881 is refused every second,
# and it drops Wi-Fi every ~5 min (13 reconnects/hour instead of a few per day). `up -d` alone
# does nothing for a running container, so this recreates it (the AP must be up: it binds
# the AP address's :443). Shows the state before and after.
# NO SUDO: run as a non-root user in the docker group.
#   bash ops/recreate-stack.sh  (from the repo root)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$ROOT/lab.env" ]; then
  echo "error: $ROOT/lab.env missing — copy lab.env.example and fill it in" >&2
  exit 1
fi
. "$ROOT/lab.env"
AP_IFACE="${AP_IFACE:-uap0}"
AP_IP="${AP_IP:?AP_IP}"
# Optional out-of-repo ops notifier; source only if present.
NOTIFY_LIB="${NOTIFY_OPS_LIB:-$HOME/homelab/slurm/lib/notify-ops.sh}"
[ -f "$NOTIFY_LIB" ] && . "$NOTIFY_LIB"
[ "$EUID" -ne 0 ] || { echo "error: run without sudo (needs docker group, not root)" >&2; exit 1; }
ip -4 addr show "$AP_IFACE" | grep -q "${AP_IP}/" || { echo "error: $AP_IFACE has no $AP_IP (AP down): sudo systemctl restart roborock-ap" >&2; exit 1; }
d="$ROOT/local_roborock_server"
dc() { docker compose --project-directory "$d" -f "$d/compose.yaml" -f "$d/compose.local.yaml" "$@"; }
echo "== before"
dc ps --format 'table {{.Name}}\t{{.State}}\t{{.Status}}\t{{.Ports}}'
echo "== recreate roborock-local-server"
dc up -d --force-recreate roborock-local-server
for i in $(seq 1 30); do
  st=$(docker inspect -f '{{.State.Health.Status}}' "$(dc ps -q roborock-local-server)" 2>/dev/null || echo unknown)
  [[ $st == healthy ]] && break
  sleep 5
done
echo "== after (health: $st)"
dc ps --format 'table {{.Name}}\t{{.State}}\t{{.Status}}\t{{.Ports}}'
docker port "$(dc ps -q roborock-local-server)"
docker port "$(dc ps -q roborock-local-server)" | grep -q 8881 || { echo "error: 8881 still not published" >&2; exit 1; }
[[ $st == healthy ]] || { echo "error: container not healthy after 150 s" >&2; exit 1; }
echo "stack recreated; ops checks that the vacuum reconnects"