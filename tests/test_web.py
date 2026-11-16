"""Unit tests for web/server.py — no robot, no server, no Cloudflare.

    uv run --with pytest --with python-roborock --with aiohttp --with 'pyjwt[crypto]' pytest tests/
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))
import server as W  # noqa: E402
import vacuum as V  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


# ---------------------------------------------------------------- map helpers

def test_room_grid_flips_rows_and_decodes_rooms():
    room = lambda n: (n << 3) | 7
    # raw layer is bottom-up: first row in the data is the bottom of the image
    data = bytes([room(1), 0, room(2),      # bottom row
                  1, room(3), 255])         # top row (1 = inside, 255 = scan: not rooms)
    grid = W.room_grid({"data": data, "w": 3, "h": 2})
    assert list(grid) == [0, 3, 0,
                          1, 0, 2]


def test_affine_from_calibration():
    md = types.SimpleNamespace(calibration=lambda: [
        {"vacuum": {"x": 25500, "y": 25500}, "map": {"x": 484.0, "y": 292.0}},
        {"vacuum": {"x": 35500, "y": 25500}, "map": {"x": 1284.0, "y": 292.0}},
        {"vacuum": {"x": 25500, "y": 35500}, "map": {"x": 484.0, "y": -508.0}}])
    tf = W.affine(md)
    assert tf == {"ax": 0.08, "bx": -1556.0, "ay": -0.08, "by": 2332.0}


# ---------------------------------------------------------------- identity

def test_identity_refuses_to_start_unconfigured():
    with pytest.raises(SystemExit):
        W.Identity({})
    with pytest.raises(SystemExit):
        W.Identity({"CF_ACCESS_TEAM": "t", "CF_ACCESS_AUD": "FILL-ME-IN"})


def _req(headers=None, cookies=None):
    return types.SimpleNamespace(headers=headers or {}, cookies=cookies or {})


def test_identity_tailnet_mode():
    ident = W.Identity({"VACUUM_WEB_TAILNET": "1"})
    who = asyncio.run(ident.who(_req({"Tailscale-User-Login": "wurenee21@gmail.com"})))
    assert who == "wurenee21@gmail.com"
    with pytest.raises(web.HTTPForbidden):  # direct to the port, not via tailscale serve
        asyncio.run(ident.who(_req()))
    with pytest.raises(web.HTTPForbidden):  # the Cloudflare header means nothing here
        asyncio.run(ident.who(_req({"Cf-Access-Authenticated-User-Email": "x@y"})))


def test_identity_dev_mode():
    ident = W.Identity({"VACUUM_WEB_DEV": "1"})
    assert asyncio.run(ident.who(_req())) == "local"


@pytest.fixture
def signed():
    """An RSA key pair and a token factory, with the JWKS client faked."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ident = W.Identity({"CF_ACCESS_TEAM": "t", "CF_ACCESS_AUD": "the-aud"})
    ident._jwks = types.SimpleNamespace(
        get_signing_key_from_jwt=lambda tok: types.SimpleNamespace(key=key.public_key()))

    def token(aud="the-aud", email="sabrina@example.com", exp=3600):
        return jwt.encode({"aud": aud, "email": email, "exp": int(time.time()) + exp},
                          key, algorithm="RS256")
    return ident, token


def test_identity_accepts_valid_token(signed):
    ident, token = signed
    who = asyncio.run(ident.who(_req({"Cf-Access-Jwt-Assertion": token()})))
    assert who == "sabrina@example.com"


@pytest.mark.parametrize("headers", [{}, {"Cf-Access-Jwt-Assertion": "garbage"}])
def test_identity_rejects_missing_or_bad_token(signed, headers):
    ident, _ = signed
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(ident.who(_req(headers)))


@pytest.mark.parametrize("kw", [{"aud": "other-app"}, {"exp": -60}])
def test_identity_rejects_wrong_audience_or_expired(signed, kw):
    ident, token = signed
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(ident.who(_req({"Cf-Access-Jwt-Assertion": token(**kw)})))


def test_identity_ignores_email_header_outside_dev(signed):
    ident, _ = signed
    with pytest.raises(web.HTTPForbidden):  # a forged header alone is not enough
        asyncio.run(ident.who(_req({"Cf-Access-Authenticated-User-Email": "x@y"})))


# ---------------------------------------------------------------- actions

@pytest.fixture
def robot(monkeypatch):
    r = W.Robot.__new__(W.Robot)  # skip connecting; set only what actions need
    r.names = {3: "Living Room"}
    r.map = {"rooms": [{"segment": s} for s in (1, 2, 3, 4)]}
    r.sent = []
    r.state = 8

    async def cmd(method, params=None, timeout=10.0):
        r.sent.append((method, params))
        if method == "get_status":
            return [{"state": r.state}]
        if method == "get_clean_motor_mode":
            return [{"fan_power": 102, "water_box_mode": 202, "mop_mode": 300}]
        if method in ("app_segment_clean", "app_start"):
            r.state = 23  # starts with the mop wash
        return ["ok"]
    r.v = types.SimpleNamespace(cmd=cmd, status=lambda: _status(r))
    clock = [0.0]

    async def sleep(s=0, *a):
        clock[0] += s
    monkeypatch.setattr(V, "_now", lambda: clock[0])
    monkeypatch.setattr(V.asyncio, "sleep", sleep)
    return r


async def _status(r):
    return (await r.v.cmd("get_status"))[0]


@pytest.mark.parametrize("body,err", [
    ({"action": "clean", "rooms": [9]}, "unknown rooms"),
    ({"action": "clean", "rooms": ["3"]}, "unknown rooms"),
    ({"action": "clean", "repeat": 4}, "repeat"),
    ({"action": "clean", "mode": {"suction": "loud"}}, "bad mode"),
    ({"action": "zone", "rect": [0, 0, 100, 100]}, "at least 30 cm"),
    ({"action": "zone", "rect": [0, 0, 20000, 1000]}, "too large"),
    ({"action": "zone", "rect": "everywhere"}, "rect must be"),
    ({"action": "dry", "on": "yes"}, "on: true|false"),
    ({"action": "app_rc_move"}, "unknown action"),
    ({}, "unknown action"),
])
def test_bad_actions_rejected_before_sending(robot, body, err):
    with pytest.raises(W.BadRequest, match=err):
        W.build_action(robot, body)
    assert robot.sent == []


def test_clean_rooms_with_mode(robot):
    label, fn = W.build_action(robot, {"action": "clean", "rooms": [3, 1], "repeat": 2,
                                       "mode": {"suction": "max"}})
    assert label == "clean Living Room, room 1"
    job = W.Job("clean", label, "t")
    robot.confirm = lambda job, key, resend=None: W.Robot.confirm(robot, job, key, resend)
    msg = asyncio.run(fn(job))
    assert ("set_clean_motor_mode", [{"fan_power": 104, "water_box_mode": 202, "mop_mode": 300}]) in robot.sent
    assert ("app_segment_clean", [{"segments": [3, 1], "repeat": 2}]) in robot.sent
    assert msg == "started: washing mop"


def test_zone_is_normalised(robot):
    _, fn = W.build_action(robot, {"action": "zone", "rect": [25000, 26000, 24000, 25000]})
    robot.confirm = lambda job, key, resend=None: asyncio.sleep(0)
    asyncio.run(fn(W.Job("zone", "", "t")))
    assert robot.sent[0] == ("app_zoned_clean", [[24000, 25000, 25000, 26000, 1]])


# ---------------------------------------------------------------- HTTP

def test_http_state_action_and_activity(robot, tmp_path, monkeypatch):
    monkeypatch.setattr(W, "DATA", tmp_path)
    monkeypatch.setattr(W, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    import collections
    robot.status, robot.status_at, robot.online, robot.error = {"state": 8, "battery": 90}, time.time(), True, None
    robot.job, robot._job_task, robot.map_version, robot._map_due = None, None, 1, 0
    robot.activity = collections.deque(maxlen=200)
    app = W.make_app(robot, W.Identity({"VACUUM_WEB_DEV": "1"}))

    async def go():
        async with TestClient(TestServer(app)) as c:
            st = await (await c.get("/api/state")).json()
            assert st["status"]["state_text"] == "charging" and st["status"]["battery"] == 90
            r = await c.post("/api/action", json={"action": "dock"},
                             headers={"Cf-Access-Authenticated-User-Email": "mark@example.com"})
            assert r.status == 200
            for _ in range(50):
                st = await (await c.get("/api/state")).json()
                if st["job"]["result"]:
                    break
                await asyncio.sleep(0)
            assert st["job"]["result"] == "ok" and st["job"]["message"] == "done: charging"
            assert st["activity"][0]["who"] == "mark@example.com"
            bad = await c.post("/api/action", json={"action": "zone", "rect": [1, 2, 3, 4]})
            assert bad.status == 400
            bad = await c.post("/api/rooms/9/name", json={"name": "Garage"})
            assert bad.status == 400
    asyncio.run(go())
    assert "mark@example.com" in (tmp_path / "activity.jsonl").read_text()


# ---------------------------------------------------------------- camera relay

def test_camera_relay_passes_only_mse(robot, tmp_path, monkeypatch):
    """The page gets go2rtc's MSE stream for a184; nothing else of go2rtc's API."""
    import collections
    import json
    import aiohttp
    monkeypatch.setattr(W, "DATA", tmp_path)
    monkeypatch.setattr(W, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    robot.activity = collections.deque(maxlen=200)
    seen = {"query": None, "msgs": []}
    monkeypatch.setattr(W.CameraKeeper, "LINGER", 0.05)
    monkeypatch.setattr(W.CameraKeeper, "SETTLE", 0.05)

    async def fake_ws(request):        # stands in for go2rtc's /api/ws
        seen["query"] = dict(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for m in ws:
            seen["msgs"].append(json.loads(m.data))
            await ws.send_str(json.dumps({"type": "mse", "value": 'video/mp4; codecs="avc1.42001F"'}))
            await ws.send_bytes(b"\x00\x00\x00\x18ftyp")
        return ws

    fake = web.Application()
    fake.router.add_get("/api/ws", fake_ws)

    async def go():
        async with TestServer(fake) as up:
            monkeypatch.setattr(W, "GO2RTC", str(up.make_url("")).rstrip("/"))
            app = W.make_app(robot, W.Identity({"VACUUM_WEB_DEV": "1"}))
            async with TestClient(TestServer(app)) as c:
                ws = await c.ws_connect("/api/camera/ws?src=something-else")
                await ws.send_json({"type": "webrtc/offer", "value": "x"})   # dropped
                await ws.send_str("not json")                                # dropped
                await ws.send_json({"type": "diag", "value": "1.0s tick rs=0"})  # logged, not forwarded
                await ws.send_json({"type": "mse", "value": "avc1.640029"})
                first = await ws.receive(timeout=5)
                second = await ws.receive(timeout=5)
                await ws.close()
                assert json.loads(first.data)["type"] == "mse"
                assert second.type == aiohttp.WSMsgType.BINARY and second.data.endswith(b"ftyp")
    asyncio.run(go())
    assert seen["query"] == {"src": "a184_web"}      # the browser can't pick the stream
    # the browser's request, plus the server's own keep-alive consumer; nothing else
    assert {"type": "mse", "value": "avc1.640029"} in seen["msgs"]
    assert all(m["type"] == "mse" for m in seen["msgs"])
    assert [a["what"] for a in robot.activity] == ["watched the camera"]


def test_camera_relay_reports_viewer_down(robot, tmp_path, monkeypatch):
    import collections
    import json
    monkeypatch.setattr(W, "DATA", tmp_path)
    monkeypatch.setattr(W, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    robot.activity = collections.deque(maxlen=200)
    monkeypatch.setattr(W, "GO2RTC", "http://127.0.0.1:9")    # nothing listens there
    monkeypatch.setattr(W.CameraKeeper, "LINGER", 0.05)
    app = W.make_app(robot, W.Identity({"VACUUM_WEB_DEV": "1"}))

    async def go():
        async with TestClient(TestServer(app)) as c:
            ws = await c.ws_connect("/api/camera/ws")
            m = await ws.receive(timeout=5)
            assert json.loads(m.data) == {"type": "error", "value": "the camera viewer is not running"}
    asyncio.run(go())


def test_camera_relay_needs_access_login(signed):
    ident, _ = signed
    app = W.make_app(types.SimpleNamespace(), ident)

    async def go():
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/camera/ws", headers={"Upgrade": "websocket", "Connection": "Upgrade",
                                                       "Sec-WebSocket-Version": "13",
                                                       "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
            assert r.status == 403
    asyncio.run(go())


def test_index_versions_static_files():
    html = W.index_html()
    assert '"/static/app.js?v=' in html and '"/static/style.css?v=' in html


def test_camera_keeper_spaces_robot_starts_and_lingers(monkeypatch):
    """A viewer reconnecting within LINGER joins the held stream (no new robot
    session); a new session waits MIN_GAP after the last one ended."""
    starts = []

    async def fake_hold(self, ready):
        starts.append(time.monotonic())
        ready.set_result(True)
        try:
            await asyncio.sleep(3600)
        finally:
            self.stopped_at = time.time()

    monkeypatch.setattr(W.CameraKeeper, "_hold", fake_hold)

    async def go():
        k = W.CameraKeeper()
        k.LINGER, k.MIN_GAP = 0.2, 0.3
        await k.acquire(); k.release()          # viewer leaves...
        await asyncio.sleep(0.05)
        await k.acquire(); k.release()          # ...and reconnects within LINGER
        assert len(starts) == 1
        await asyncio.sleep(0.3)                # LINGER passes: stream stopped
        assert k.task.done()
        t = time.monotonic()
        await k.acquire()                       # next start waits out MIN_GAP
        assert len(starts) == 2 and time.monotonic() - t >= 0.2
        k.release(); k._stop()
    asyncio.run(go())


def test_dock_error_is_explained():
    robot = types.SimpleNamespace(status={"state": 8, "battery": 95, "dock_error_status": 38}, names={})
    v = W.status_view(robot)
    assert v["dock_error"] == 38 and v["dock_problem"] == "Clean water tank is empty"
    assert "Refill the clean water tank" in v["dock_fix"]
    assert W.status_view(types.SimpleNamespace(status={"state": 8}, names={}))["dock_problem"] is None
    assert V.dock_error(77) == ("Dock error 77", "Check the dock.")


def test_camera_keeper_restarts_stuck_go2rtc(robot, tmp_path, monkeypatch):
    """No video within NO_VIDEO: restart go2rtc, tell the viewer plainly."""
    import collections
    import json
    monkeypatch.setattr(W, "DATA", tmp_path)
    monkeypatch.setattr(W, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    robot.activity = collections.deque(maxlen=200)
    monkeypatch.setattr(W.CameraKeeper, "NO_VIDEO", 0.3)
    monkeypatch.setattr(W.CameraKeeper, "LINGER", 0.05)
    restarts = []

    async def silent_ws(request):            # go2rtc stuck: accepts, never sends video
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws

    async def restart(request):
        restarts.append(1)
        return web.Response()

    fake = web.Application()
    fake.router.add_get("/api/ws", silent_ws)
    fake.router.add_post("/api/restart", restart)

    async def go():
        async with TestServer(fake) as up:
            monkeypatch.setattr(W, "GO2RTC", str(up.make_url("")).rstrip("/"))
            app = W.make_app(robot, W.Identity({"VACUUM_WEB_DEV": "1"}))
            async with TestClient(TestServer(app)) as c:
                ws = await c.ws_connect("/api/camera/ws")
                m = await ws.receive(timeout=5)
                assert json.loads(m.data) == {"type": "error", "value": "camera not answering"}
    asyncio.run(go())
    assert restarts == [1]
