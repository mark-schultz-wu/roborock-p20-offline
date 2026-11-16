#!/usr/bin/env bash
# Install the ops/systemd units for this repo by rendering the @...@-placeholder
# templates with this checkout's paths and lab.env values.
#  - without sudo (user mode): install roborock-rrcheck units (service + timer)
#    into ~/.config/systemd/user/ and reload the user manager.
#  - with sudo (system mode): install roborock-ap.service into
#    /etc/systemd/system/ and reload; restarts nothing (the AP and stack keep
#    running; the change applies at the next boot).
# SUDO:   sudo bash ops/install-units.sh  (system mode, any user)
# USER:   bash ops/install-units.sh       (user mode)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$ROOT/lab.env" ]; then
  echo "error: $ROOT/lab.env missing — copy lab.env.example and fill it in" >&2
  exit 1
fi
. "$ROOT/lab.env"
AP_IFACE="${AP_IFACE:-uap0}"
LAN_IFACE="${LAN_IFACE:-wlo1}"
# Optional out-of-repo ops notifier; source only if present.
NOTIFY_LIB="${NOTIFY_OPS_LIB:-$HOME/homelab/slurm/lib/notify-ops.sh}"
[ -f "$NOTIFY_LIB" ] && . "$NOTIFY_LIB"

render() { # template dest
  local tmp
  tmp=$(mktemp)
  sed -e "s|@REPO_DIR@|$ROOT|g" \
      -e "s|@AP_IFACE@|${AP_IFACE:-uap0}|g" \
      -e "s|@LAN_IFACE@|${LAN_IFACE:-wlo1}|g" "$1" > "$tmp"
  install -m 0644 "$tmp" "$2"
  rm -f "$tmp"
}

if [ "$EUID" -eq 0 ]; then
  # system mode: the AP unit (applies at the next boot)
  render "$ROOT/ops/systemd/roborock-ap.service.in" /etc/systemd/system/roborock-ap.service
  systemctl daemon-reload
  grep -q -- '--force-recreate' /etc/systemd/system/roborock-ap.service
  systemctl is-enabled roborock-ap.service
  echo "unit installed (applies at the next boot)"
else
  # user mode: the rrcheck service + timer
  mkdir -p "$HOME/.config/systemd/user"
  render "$ROOT/ops/systemd/roborock-rrcheck.service.in" "$HOME/.config/systemd/user/roborock-rrcheck.service"
  cp "$ROOT/ops/systemd/roborock-rrcheck.timer" "$HOME/.config/systemd/user/roborock-rrcheck.timer"
  systemctl --user daemon-reload
  echo "user units installed; enable with:"
  echo "  systemctl --user enable --now roborock-rrcheck.timer"
fi