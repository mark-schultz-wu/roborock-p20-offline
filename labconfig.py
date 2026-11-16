"""Environment-specific values for this install: one file, lab.env.

Everything that differs between installations lives in `lab.env` at the
repo root (gitignored; copy `lab.env.example` to start). Precedence:

    real environment variable  >  lab.env  >  lab.env.example

The example file carries placeholder values (TEST-NET addresses, all-zero
keys) so the offline unit tests run on a fresh clone; anything that talks
to the real robot or stack needs a real `lab.env`.

Shell scripts (setup_ap_concurrent.sh, ops/*) source the same file, so
edit lab.env — not code — to repoint the tools at another robot, AP
subnet, Wi-Fi, or stack hostname.
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent

_values: dict[str, str] = {}
# later files win: example first, then the real lab.env
for _name in ("lab.env.example", "lab.env"):
    _path = ROOT / _name
    if _path.is_file():
        for _line in _path.read_text().splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _values[_k.strip()] = _v.strip().strip("'\"")


def get(key: str, default: str | None = None) -> str | None:
    """Config value: environment variable first, then lab.env files."""
    return os.environ.get(key, _values.get(key, default))


# --- the robot (duid/localKey come out of provisioning; host is its DHCP lease)
HOST = get("VACUUM_HOST", "")
LOCAL_KEY = get("VACUUM_KEY", "")
DUID = get("VACUUM_DUID", "")
PID = get("VACUUM_PID", "roborock.vacuum.a184")

# --- the isolated AP this machine hosts for the robot
AP_IP = get("AP_IP", "")
AP_IFACE = get("AP_IFACE", "uap0")
LAN_IFACE = get("LAN_IFACE", "wlo1")
WIFI_SSID = get("WIFI_SSID", "")
WIFI_PASS = get("WIFI_PASS", "")

# --- the local "cloud" stack (must match local_roborock_server/config.toml)
STACK_HOST = get("STACK_HOST", "")
TOKEN_HOST = get("TOKEN_HOST", "")   # host part of token.r (no scheme/slash)
STACK_PORT = int(get("STACK_PORT", "555"))
MQTT_PORT = int(get("MQTT_PORT", "8881"))
# The firmware dials <first 15 chars of token.r> + "iot.roborock.com" for
# the camera-relay bootstrap; the forged leaf must cover it.
MANGLED = TOKEN_HOST[:15] + "iot.roborock.com"

# --- protocol login on the stack ([admin] in config.toml)
PROTOCOL_EMAIL = get("PROTOCOL_EMAIL", "")
PROTOCOL_PIN = get("PROTOCOL_PIN", "")
