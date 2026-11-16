#!/usr/bin/env bash
# Tear down the concurrent lab AP ($AP_IFACE). The LAN STA is never touched.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# Teardown must run even if lab.env is gone: optional source, default iface.
[ -f "$ROOT/lab.env" ] && . "$ROOT/lab.env"
AP_IFACE="${AP_IFACE:-uap0}"
LAN_IFACE="${LAN_IFACE:-wlo1}"

pkill -f "tcpdump -i $AP_IFACE" 2>/dev/null || true
pkill -x hostapd 2>/dev/null || true
pkill -x dnsmasq 2>/dev/null || true
sleep 1

# Remove only the rules this setup added.
iptables -D FORWARD -i "$AP_IFACE" -j DROP 2>/dev/null || true
iptables -D FORWARD -o "$AP_IFACE" -j DROP 2>/dev/null || true
iptables -D FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
RR_NET_ID=$(docker network inspect local_roborock_server_default --format '{{.Id}}' 2>/dev/null | cut -c1-12)
[ -n "$RR_NET_ID" ] && iptables -D FORWARD -i "$AP_IFACE" -o br-$RR_NET_ID -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -o "$AP_IFACE" -j DROP 2>/dev/null || true

iw dev "$AP_IFACE" del 2>/dev/null || true
echo "$AP_IFACE removed; firewall back to pre-existing state; $LAN_IFACE untouched throughout."