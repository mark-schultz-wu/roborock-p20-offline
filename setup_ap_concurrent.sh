#!/usr/bin/env bash
# Concurrent AP on a virtual interface ($AP_IFACE) while $LAN_IFACE stays
# connected.
#
# HARD CONSTRAINT ENFORCED HERE:
#   - FORWARD policy DROP + ip_forward=0 (prior docker rules saved, restored
#     by teardown). No NAT. The AP subnet has no route out.
#   - dnsmasq: no upstream, address=/#/<AP_IP> — every name resolves to
#     this host; dial attempts land on us and are captured/logged.
#
# Channel: derived from the live STA link (same channel = same-PHY constraint).
# Run as root. Restore with: bash teardown_ap_concurrent.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$ROOT/lab.env" ]; then
  echo "error: $ROOT/lab.env missing — copy lab.env.example and fill it in" >&2
  exit 1
fi
. "$ROOT/lab.env"
AP_IFACE="${AP_IFACE:-uap0}"
LAN_IFACE="${LAN_IFACE:-wlo1}"
SSID="${WIFI_SSID:?WIFI_SSID}"
PASS="${WIFI_PASS:?WIFI_PASS}"
AP_IP="${AP_IP:?AP_IP}"
LOGDIR="$ROOT/ap_logs"
mkdir -p "$LOGDIR"

freq=$(iw dev "$LAN_IFACE" link | awk '/freq:/{print $2}')
case "${freq%%.*}" in
  2412) ch=1;; 2417) ch=2;; 2422) ch=3;; 2427) ch=4;; 2432) ch=5;; 2437) ch=6;;
  2442) ch=7;; 2447) ch=8;; 2452) ch=9;; 2457) ch=10;; 2462) ch=11;; 2467) ch=12;;
  2472) ch=13;;
  5180) ch=36;; 5190) ch=38;; 5200) ch=40;; 5210) ch=42;; 5220) ch=44;;
  5230) ch=46;; 5240) ch=48;; 5745) ch=149;; 5755) ch=151;; 5765) ch=153;;
  5775) ch=155;; 5785) ch=157;; 5795) ch=159;;
  *) echo "unsupported STA freq $freq"; exit 1;;
esac
if [ "$ch" -le 13 ]; then hwm=g; else hwm=a; fi
# The a184 only joins 2.4 GHz, so an AP on the LAN link's 5 GHz channel is useless.
if [ "$hwm" = a ] && [ "${ALLOW_5GHZ:-0}" != 1 ]; then
  echo "$LAN_IFACE is on 5 GHz (ch $ch); the vacuum only joins 2.4 GHz."
  echo "Pin the home Wi-Fi to 2.4 GHz: nmcli con modify <name> 802-11-wireless.band bg"
  echo "(or set ALLOW_5GHZ=1 to start anyway)"
  exit 1
fi
echo "== STA on freq ${freq%%.*} -> AP channel $ch (hw_mode $hwm)"

pkill -f "tcpdump -i $AP_IFACE" 2>/dev/null || true
pkill -x hostapd 2>/dev/null || true
pkill -x dnsmasq 2>/dev/null || true
sleep 1
iw dev "$AP_IFACE" del 2>/dev/null || true
iw dev "$LAN_IFACE" interface add "$AP_IFACE" type managed 2>/dev/null || {
  echo "failed to add $AP_IFACE"; exit 1; }
nmcli device set "$AP_IFACE" managed no 2>/dev/null || true
sleep 1

cat > /tmp/hostapd-lab.conf <<EOF
interface=${AP_IFACE}
driver=nl80211
ssid=${SSID}
country_code=US
channel=${ch}
hw_mode=${hwm}
ieee80211n=1
EOF
[ "$hwm" = "a" ] && cat >> /tmp/hostapd-lab.conf <<EOF
ieee80211ac=1
vht_capab=[MAX-MPDU-7991][SHORT-GI-80][RXLDPC]
vht_oper_chwidth=0
EOF
cat >> /tmp/hostapd-lab.conf <<EOF
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=${PASS}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
ignore_broadcast_ssid=0
logger_syslog=-1
logger_syslog_level=2
logger_stdout=-1
logger_stdout_level=2
EOF

echo "== starting hostapd on ${AP_IFACE} (${SSID}, ch ${ch})"
hostapd -B -P /tmp/hostapd-lab.pid -f "$LOGDIR/hostapd.log" /tmp/hostapd-lab.conf

ip link set "$AP_IFACE" up
ip addr add "${AP_IP}/24" dev "$AP_IFACE"

cat > /tmp/dnsmasq-lab.conf <<EOF
interface=${AP_IFACE}
bind-interfaces
dhcp-range=${AP_IP%.*}.50,${AP_IP%.*}.150,12h
dhcp-option=option:router,${AP_IP}
dhcp-option=option:dns-server,${AP_IP}
dhcp-authoritative
no-resolv
address=/#/${AP_IP}
log-queries
log-facility=${LOGDIR}/dnsmasq.log
EOF

echo "== starting dnsmasq (all names -> ${AP_IP}, logged)"
dnsmasq --conf-file=/tmp/dnsmasq-lab.conf

echo "== lockdown (${AP_IFACE} only — docker's chains untouched)"
# The vacuum's subnet must never be forwarded anywhere. One surgical rule;
# leave the rest of the firewall (docker MASQUERADE, FORWARD rules) alone.
iptables -C FORWARD -i "$AP_IFACE" -j DROP 2>/dev/null || iptables -I FORWARD 1 -i "$AP_IFACE" -j DROP
# belt & suspenders: nothing routes INTO the lab subnet from other interfaces
iptables -C FORWARD -o "$AP_IFACE" -j DROP 2>/dev/null || iptables -I FORWARD 3 -o "$AP_IFACE" -j DROP
# let the vacuum reach the local_roborock_server container (docker bridge),
# and let replies flow; docker publishes :555/:8881 via DNAT+FORWARD.
RR_NET_ID=$(docker network inspect local_roborock_server_default --format '{{.Id}}' 2>/dev/null | cut -c1-12)
if [ -n "$RR_NET_ID" ]; then
  iptables -C FORWARD -i "$AP_IFACE" -o br-$RR_NET_ID -j ACCEPT 2>/dev/null || \
    iptables -I FORWARD 1 -i "$AP_IFACE" -o br-$RR_NET_ID -j ACCEPT
fi
iptables -C FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || \
  iptables -I FORWARD 1 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# The vacuum's :443 is served directly by the stack: compose.camera443.yaml
# publishes ${AP_IP}:443 -> container :555, and the stack's certificate
# (minted by apicn/mint_leaf.py) covers every name the vacuum dials
# (api-<token host>, the token host, the 15-char-mangled camera host, and
# api-cn.roborock.com for POST /f/c). No REDIRECT/proxy layer anymore.

pkill -f "tcpdump -i $AP_IFACE" 2>/dev/null || true
# Captures: never overwrite, bounded size, 90-day retention.
# - an old single-file capture.pcap is kept under a timestamped name
# - each run writes a ring of 20 x 100 MB files (capture-<run>.pcap00..19),
#   so one run never exceeds 2 GB; tcpdump stays root (-Z root) so it can
#   create the rotated files in this root-owned directory
# - capture files untouched for 90 days are deleted
if [ -f "${LOGDIR}/capture.pcap" ] && [ ! -L "${LOGDIR}/capture.pcap" ]; then
  mv "${LOGDIR}/capture.pcap" "${LOGDIR}/capture-$(date -r "${LOGDIR}/capture.pcap" +%Y%m%d-%H%M%S)-legacy.pcap"
fi
find "${LOGDIR}" -maxdepth 1 -name 'capture-*' -mtime +90 -delete
CAPTURE="${LOGDIR}/capture-$(date +%Y%m%d-%H%M%S).pcap"
nohup tcpdump -i "$AP_IFACE" -s 0 -U -Z root -C 100 -W 20 -w "$CAPTURE" > "${LOGDIR}/tcpdump.log" 2>&1 &

echo
echo "AP up ($LAN_IFACE STA untouched)."
echo "  SSID: ${SSID}   PASS: ${PASS}   (${hwm} ch ${ch})"
echo "  AP IP: ${AP_IP}; clients get ${AP_IP%.*}.50-150"
echo "  DNS/dial attempts: ${LOGDIR}/dnsmasq.log + $(basename "$CAPTURE")NN (ring of 20 x 100 MB)"
echo "  NO WAN PATH EXISTS. Restore with: bash teardown_ap_concurrent.sh"