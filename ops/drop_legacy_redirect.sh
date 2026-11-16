#!/usr/bin/env bash
# Drop the legacy :443 REDIRECT rules.
#
# setup_ap_concurrent.sh (versions before 2026-10-30) inserted these to send
# the vacuum's :443 to the retired api-cn front-end listener (:8443). The
# stack now serves that traffic directly: compose.local.yaml publishes
# ${AP_IP}:443 -> :555 with the forged leaf covering every name the
# vacuum dials. One-shot migration; idempotent, safe to re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$ROOT/lab.env" ]; then
  echo "error: $ROOT/lab.env missing — copy lab.env.example and fill it in" >&2
  exit 1
fi
. "$ROOT/lab.env"
AP_IFACE="${AP_IFACE:-uap0}"

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo" >&2
  exit 1
fi

VAC_IP="${VACUUM_HOST:?VACUUM_HOST}"   # the vacuum's dnsmasq lease
AP_IP="${AP_IP:?AP_IP}"                # the AP interface

n=0
while iptables -t nat -C PREROUTING -s ${VAC_IP}/32 -d ${AP_IP}/32 \
        -p tcp --dport 443 -j REDIRECT --to-port 8443 2>/dev/null; do
  iptables -t nat -D PREROUTING -s ${VAC_IP}/32 -d ${AP_IP}/32 \
    -p tcp --dport 443 -j REDIRECT --to-port 8443
  n=$((n + 1))
done
echo "removed $n nat REDIRECT rule(s)"

n=0
while iptables -C INPUT -i "$AP_IFACE" -p tcp --dport 8443 -j ACCEPT 2>/dev/null; do
  iptables -D INPUT -i "$AP_IFACE" -p tcp --dport 8443 -j ACCEPT
  n=$((n + 1))
done
echo "removed $n INPUT :8443 ACCEPT rule(s)"

echo "the vacuum's :443 now reaches the stack directly (${AP_IP}:443 -> :555)."