#!/usr/bin/env bash
# Make this machine resolve the local Roborock stack's new hostnames to the
# AP address (the vacuum's own network gets the same answer from dnsmasq).
# Run with: sudo bash ops/add_hosts.sh  (from the repo root)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$ROOT/lab.env" ]; then
  echo "error: $ROOT/lab.env missing — copy lab.env.example and fill it in" >&2
  exit 1
fi
. "$ROOT/lab.env"
AP_IP="${AP_IP:?AP_IP}"
STACK_HOST="${STACK_HOST:?STACK_HOST}"
TOKEN_HOST="${TOKEN_HOST:?TOKEN_HOST}"

LINE="${AP_IP} ${STACK_HOST} ${TOKEN_HOST}"

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo" >&2
  exit 1
fi

if grep -qE "^[^#]*\b${STACK_HOST}\b" /etc/hosts; then
  echo "already present:"
  grep -nE "(${STACK_HOST}|${TOKEN_HOST})" /etc/hosts
  exit 0
fi

cp /etc/hosts "/etc/hosts.bak-$(date +%Y%m%d-%H%M%S)"
printf '%s\n' "$LINE" >> /etc/hosts
echo "added: $LINE"
getent hosts "$STACK_HOST" "$TOKEN_HOST"