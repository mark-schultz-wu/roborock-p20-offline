#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["paho-mqtt>=2"]
# ///
"""Local frontend checks: every piece of the vacuum-facing stack, from this machine.

Covers the whole chain the vacuum and our tools depend on: name resolution
(host + dnsmasq), the forged leaf's SAN coverage and expiry, TLS on every
robot-facing port, the stack's routes (including the camera relay
bootstrap, emulated exactly as the firmware dials it), admin UI, the
protocol-login session, MQTT auth, TURN, the go2rtc viewer, the systemd
timer — and, when the vacuum is online, the direct command link and a
live camera frame.

Robot-dependent checks SKIP (not FAIL) while the vacuum is offline, e.g.
after a re-provision, before its first successful bootstrap dial.

    uv run tests/check_local.py            # run all
    uv run tests/check_local.py -v         # also show PASS details

Exit code: 0 = no failures (skips fine), 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import socket
import ssl
import struct
import subprocess
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import labconfig as cfg  # noqa: E402

ROOT_CERT = HERE / "apicn" / "aaa-root.pem"
INTER_CERT = HERE / "apicn" / "inter-cross.pem"
LEAF = HERE / "apicn" / "leaf.pem"
GO2RTC_CONTAINER = "local_roborock_server-go2rtc-1"
GO2RTC_PIN = "/etc/ssl/extra/leaf.pem"
GO2RTC_YAML = HERE / ".tools" / "go2rtc.yaml"
SESSION = HERE / ".cloud_session.json"

AP_IP = cfg.AP_IP
VAC_IP = cfg.HOST
DID = cfg.DUID
PID = cfg.PID
STACK = cfg.STACK_HOST
TOKEN_HOST = cfg.TOKEN_HOST
# the firmware dials <first 15 chars of token.r>iot.roborock.com for createca
MANGLED = cfg.MANGLED
REQUIRED_SANS = {STACK, TOKEN_HOST, MANGLED, "api-cn.roborock.com"}

robot_online = False  # set by check_robot_online, used by the dependent checks
verbose = False


def ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ROOT_CERT))


def http_get(url: str, timeout: float = 10) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout, context=ctx()) as r:
        return r.status, r.read(65536)


def go2rtc_params() -> dict[str, str]:
    m = re.search(r"roborock://([^?]+)\?(\S+)", GO2RTC_YAML.read_text())
    assert m, "no roborock:// source in .tools/go2rtc.yaml"
    return {"host": m.group(1), **dict(p.split("=", 1) for p in m.group(2).split("&"))}


def session_rriot() -> dict:
    return json.loads(SESSION.read_text())["rriot"]


RESULTS: list[tuple[str, str, str]] = []  # (status, name, detail)


def run_check(name: str, fn) -> None:
    global robot_online
    try:
        detail = fn()
        status, extra = "PASS", detail or ""
    except _Skip as e:
        status, extra = "SKIP", str(e)
    except Exception as e:  # noqa: BLE001 — a check failure is data, not a crash
        status, extra = "FAIL", f"{type(e).__name__}: {e}"
    RESULTS.append((status, name, extra))
    if status == "PASS":
        robot_online = robot_online or name == "robot_online" and bool(extra)
    shown = extra if status != "PASS" or verbose else ""
    print(f"{status:<4}  {name:<18} {shown}".rstrip(), flush=True)


class _Skip(Exception):
    """Raised by checks that cannot run in the current state."""


def skip_if(cond: bool, why: str) -> None:
    if cond:
        raise _Skip(why)


def c_etc_hosts() -> str:
    for name in (STACK, TOKEN_HOST):
        ip = socket.gethostbyname(name)
        assert ip == AP_IP, f"{name} resolves to {ip}, expected {AP_IP}"
    return f"{STACK} + {TOKEN_HOST} -> {AP_IP}"


def c_dnsmasq() -> str:
    r = subprocess.run(["dig", "+short", f"@{AP_IP}", *REQUIRED_SANS],
                       capture_output=True, text=True, timeout=10)
    skip_if(r.returncode != 0, "dig not available")
    answers = [a for a in r.stdout.split() if a.strip()]
    assert len(answers) == len(REQUIRED_SANS) and all(a == AP_IP for a in answers), \
        f"dnsmasq answers: {answers}"
    return f"{len(REQUIRED_SANS)} names -> {AP_IP} (what the vacuum sees)"


def cert_enddate(path: pathlib.Path) -> dt.datetime:
    out = subprocess.run(["openssl", "x509", "-in", str(path), "-noout", "-enddate"],
                         capture_output=True, text=True, check=True).stdout
    return dt.datetime.strptime(out.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z") \
        .replace(tzinfo=dt.timezone.utc)


def c_leaf() -> str:
    r = subprocess.run(["openssl", "x509", "-in", str(LEAF), "-noout",
                        "-ext", "subjectAltName"], capture_output=True, text=True)
    sans = set(re.findall(r"DNS:([^,\s]+)", r.stdout))
    missing = REQUIRED_SANS - sans
    assert not missing, f"leaf SANs missing: {missing}"
    # The chain dies with its earliest cert: the intermediate and root expire
    # before the leaf, so check all three.
    ends = {p.name: cert_enddate(p) for p in (LEAF, INTER_CERT, ROOT_CERT)}
    first, end = min(ends.items(), key=lambda kv: kv[1])
    left = end - dt.datetime.now(dt.timezone.utc)
    assert left > dt.timedelta(days=30), \
        f"{first} expires {end:%Y-%m-%d} ({left.days} days) — the chain needs replacing"
    return f"SANs cover {len(REQUIRED_SANS)} names; chain valid to {end:%Y-%m-%d} ({first})"


def c_tls_ports() -> str:
    for host, port, name in ((STACK, cfg.STACK_PORT, STACK), (STACK, cfg.MQTT_PORT, STACK), (AP_IP, 443, STACK)):
        with socket.create_connection((host, port), timeout=5) as s:
            with ctx().wrap_socket(s, server_hostname=name) as ss:  # raises on bad SAN/chain
                ss.getpeercert()
    return f":{cfg.STACK_PORT} :{cfg.MQTT_PORT} :443 all serve the forged leaf with matching SANs"


def c_stack_routes() -> str:
    base = f"https://{STACK}:{cfg.STACK_PORT}"
    paths = [
        f"/region?did={DID}&pid={PID}&region={TOKEN_HOST}/",
        "/time/now", "/location",
        f"/devices/{DID}/sh?did={DID}&pid={PID}",
        f"/iot.roborock.com/fwapi/checkda?did={DID}&duid={DID}&pid={PID}&uid=1234567890",
    ]
    for p in paths:
        status, body = http_get(base + p)
        assert status == 200, f"{p} -> HTTP {status}"
        json.loads(body)  # every route answers JSON
    return f"{len(paths)} routes answer 200/JSON (region, time, location, sh, checkda)"


def c_camera_bootstrap() -> str:
    """Emulate the firmware's createca dial: mangled host :443, POST, TURN creds back."""
    body = f"did={DID}&pid={PID}&uid=1234567890".encode()
    req = (f"POST /fwapi/createca HTTP/1.1\r\nHost: {MANGLED}\r\n"
           f"Content-Type: application/x-www-form-urlencoded\r\n"
           f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    with socket.create_connection((AP_IP, 443), timeout=5) as s:
        with ctx().wrap_socket(s, server_hostname=MANGLED) as ss:  # SAN must match mangled
            ss.sendall(req)
            raw = b""
            while chunk := ss.recv(65536):
                raw += chunk
    status_line = raw.split(b"\r\n", 1)[0].decode()
    assert re.match(r"HTTP/\S+ 200", status_line), f"createca -> {status_line}"
    payload = raw.split(b"\r\n\r\n", 1)[-1]
    assert b"turn:" in payload, f"no TURN url in reply: {payload[:200]!r}"
    return f"createca via {MANGLED}:443 -> 200 + TURN creds (SAN matches mangled host)"


def c_admin() -> str:
    status, body = http_get(f"https://{STACK}:{cfg.STACK_PORT}/admin")
    assert status == 200 and body, f"admin -> {status}"
    return "admin UI up"


def c_session() -> str:
    r = session_rriot()
    urls = r["r"]
    assert urls["a"] == f"https://{STACK}:{cfg.STACK_PORT}" and urls["l"] == urls["a"], urls
    assert urls["m"] == f"ssl://{STACK}:{cfg.MQTT_PORT}", urls
    assert r.get("u") and r.get("s") and r.get("k"), "rriot u/s/k missing"
    return "protocol-login session present, URLs on the lab name"


def c_go2rtc_creds() -> str:
    p, rr = go2rtc_params(), session_rriot()
    assert p["host"] == f"{STACK}:{cfg.MQTT_PORT}", p["host"]
    for key in ("u", "s", "k"):
        assert p[key] == rr[key], \
            f"go2rtc.yaml {key} is stale vs .cloud_session.json ({p[key][:8]}… != {rr[key][:8]}…)"
    assert p["key"] == cfg.LOCAL_KEY, "go2rtc.yaml key != configured LOCAL_KEY"
    assert p["did"] == DID and p.get("pin"), "did/pin malformed"
    return "go2rtc.yaml in sync with session + lab.env"


def c_mqtt() -> str:
    import hashlib
    import time
    import paho.mqtt.client as mqtt
    rr = session_rriot()

    # Broker auth derives from rriot u/s/k (go2rtc pkg/roborock/iot/client.go):
    # user = hex(md5(u + ":" + k)[1:5]), pass = hex(md5(s + ":" + k)[8:])
    def md5(text: str) -> bytes:
        return hashlib.md5(text.encode()).digest()

    user = md5(f"{rr['u']}:{rr['k']}")[1:5].hex()
    password = md5(f"{rr['s']}:{rr['k']}")[8:].hex()
    state = {"rc": None}

    def on_connect(client, userdata, flags, reason_code, properties):
        state["rc"] = reason_code.value if hasattr(reason_code, "value") else reason_code

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                     client_id=f"check-{os.getpid()}")
    c.username_pw_set(user, password)
    c.tls_set_context(ctx())
    c.on_connect = on_connect
    c.connect(STACK, cfg.MQTT_PORT, keepalive=30)
    c.loop_start()
    for _ in range(40):
        if state["rc"] is not None:
            break
        time.sleep(0.25)
    c.loop_stop()
    c.disconnect()
    assert state["rc"] == 0, f"MQTT connect rc={state['rc']} (0 = accepted)"
    return f"MQTT TLS :{cfg.MQTT_PORT} auth accepted (derived u/s/k)"


def c_turn() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(5)
    s.sendto(struct.pack("!HHI12s", 1, 0, 0x2112A442, b"\x00" * 12), (AP_IP, 3478))
    raw, _ = s.recvfrom(1024)
    t = struct.unpack("!H", raw[:2])[0]
    assert t == 0x0101, f"STUN reply type {t:#x}, expected binding success 0x0101"
    return "TURN UDP 3478 answers STUN binding (relay alive)"


def c_go2rtc() -> str:
    with urllib.request.urlopen("http://127.0.0.1:1984/api/streams", timeout=10) as r:
        body = r.read().decode()
    assert "a184" in body and f"roborock://{STACK}:{cfg.MQTT_PORT}" in body, body[:200]
    return f"viewer up, stream a184 ({go2rtc_params()['host']})"


def c_go2rtc_pin() -> str:
    """go2rtc (Go) rejects our chain's SHA-1 root, so it trusts only a pinned
    copy of the leaf. Without the pin, or with a stale one after a re-mint,
    the camera breaks — and camera_frame only notices while the robot is online."""
    env = subprocess.run(["docker", "exec", GO2RTC_CONTAINER, "printenv", "SSL_CERT_FILE"],
                         capture_output=True, text=True, timeout=15)
    assert env.stdout.strip() == GO2RTC_PIN, \
        f"go2rtc SSL_CERT_FILE={env.stdout.strip() or '(unset)'}, expected {GO2RTC_PIN}"
    pem = subprocess.run(["docker", "exec", GO2RTC_CONTAINER, "cat", GO2RTC_PIN],
                         capture_output=True, text=True, timeout=15)
    assert pem.returncode == 0, f"{GO2RTC_PIN} missing in container"
    pinned = ssl.PEM_cert_to_DER_cert(pem.stdout)
    with socket.create_connection((STACK, cfg.MQTT_PORT), timeout=5) as s:
        with ctx().wrap_socket(s, server_hostname=STACK) as ss:
            served = ss.getpeercert(binary_form=True)
    assert pinned == served, \
        "go2rtc's pinned leaf != the leaf the MQTT port serves (re-minted? restart go2rtc)"
    return f"pinned leaf matches the one :{cfg.MQTT_PORT} serves"


def c_rtsp() -> str:
    with socket.create_connection(("127.0.0.1", 8554), timeout=5):
        pass
    return "RTSP :8554 listening"


def c_timer() -> str:
    r = subprocess.run(["systemctl", "--user", "show", "roborock-rrcheck.timer",
                        "-p", "ActiveState", "-p", "NextElapseUSecRealtimeMonotonic"],
                       capture_output=True, text=True, timeout=15)
    assert "ActiveState=active" in r.stdout, r.stdout.strip()
    assert "NextElapseUSecRealtimeMonotonic=0" not in r.stdout, "no next trigger"
    return "roborock-rrcheck.timer active with a next trigger"


def c_robot_online() -> str:
    global robot_online
    try:
        with socket.create_connection((VAC_IP, 58867), timeout=3):
            robot_online = True
            return "direct link :58867 open"
    except OSError:
        robot_online = False
        raise _Skip("vacuum offline (:58867 closed — after provisioning it retries "
                    "on its own cadence; wake it to speed that up)") from None


def c_direct_link() -> str:
    skip_if(not robot_online, "vacuum offline")
    r = subprocess.run(["uv", "run", "--quiet", "vacuum.py", "status"],
                       cwd=HERE, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"vacuum.py status exit {r.returncode}: {r.stdout[-200:]}"
    return "vacuum.py status answers"


def c_server_link() -> str:
    """vacuum.py's server-backed path (room names, zones, maps, history)."""
    skip_if(not robot_online, "vacuum offline")
    r = subprocess.run(["uv", "run", "--quiet", "vacuum.py", "rooms"],
                       cwd=HERE, capture_output=True, text=True, timeout=90)
    assert r.returncode == 0 and "unavailable" not in r.stderr, \
        f"vacuum.py rooms: {(r.stderr or r.stdout).strip()[-300:]}"
    return "vacuum.py reaches the local server (rooms)"


def c_camera_frame() -> str:
    skip_if(not robot_online, "vacuum offline")
    with urllib.request.urlopen(
            "http://127.0.0.1:1984/api/frame.jpeg?src=a184", timeout=45) as r:
        body = r.read()
    assert body[:2] == b"\xff\xd8" and len(body) > 5000, \
        f"frame is {len(body)}B, not a JPEG"
    return f"live JPEG frame, {len(body) // 1024} KiB"


def main() -> None:
    global verbose
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true", help="also show PASS details")
    verbose = ap.parse_args().verbose
    print(f"== local frontend checks ({dt.datetime.now():%Y-%m-%d %H:%M}) ==")
    for name, fn in [
        ("etc_hosts", c_etc_hosts),
        ("dnsmasq", c_dnsmasq),
        ("leaf", c_leaf),
        ("tls_ports", c_tls_ports),
        ("stack_routes", c_stack_routes),
        ("camera_bootstrap", c_camera_bootstrap),
        ("admin", c_admin),
        ("session", c_session),
        ("go2rtc_creds", c_go2rtc_creds),
        ("mqtt", c_mqtt),
        ("turn", c_turn),
        ("go2rtc", c_go2rtc),
        ("go2rtc_pin", c_go2rtc_pin),
        ("rtsp", c_rtsp),
        ("timer", c_timer),
        ("robot_online", c_robot_online),
        ("direct_link", c_direct_link),
        ("server_link", c_server_link),
        ("camera_frame", c_camera_frame),
    ]:
        run_check(name, fn)
    n = {s: sum(1 for r in RESULTS if r[0] == s) for s in ("PASS", "FAIL", "SKIP")}
    print(f"== {n['PASS']} pass, {n['FAIL']} fail, {n['SKIP']} skip ==")
    sys.exit(1 if n["FAIL"] else 0)


if __name__ == "__main__":
    main()