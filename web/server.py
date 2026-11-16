# /// script
# requires-python = ">=3.11"
# dependencies = ["python-roborock", "aiohttp", "pyjwt[crypto]"]
# ///
"""Web backend for the vacuum page (docs/WEB.md).

One long-running process: keeps the direct robot link and the local-server
session open, polls status, refreshes the map, and runs actions with the same
logic as vacuum.py (including "the robot said ok but ignored it" detection).

    uv run web/server.py                    # http://127.0.0.1:8094

Who may use it (fails closed; the server refuses to start with none set):
- Cloudflare Access (production, web/deploy/): every request's Access JWT
  (Cf-Access-Jwt-Assertion) is verified against CF_ACCESS_TEAM + CF_ACCESS_AUD.
- VACUUM_WEB_TAILNET=1: reached through `tailscale serve` (tailnet only, never
  Funnel); requests must carry the Tailscale-User-Login header serve adds.
- VACUUM_WEB_DEV=1: no checks, requests are "local". Local development only.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import lab_tls  # noqa: F401,E402  (before roborock/aiohttp)

import vacuum as V  # noqa: E402
from aiohttp import web  # noqa: E402
from roborock.map.map_parser import MapParser, MapParserConfig  # noqa: E402

log = logging.getLogger("vacuum-web")
DATA = HERE / "data"
STATIC = HERE / "static"
ACTIVITY_FILE = DATA / "activity.jsonl"
PORT = int(os.environ.get("VACUUM_WEB_PORT", "8094"))
GO2RTC = os.environ.get("VACUUM_GO2RTC", "http://127.0.0.1:1984")  # camera viewer (README)
CAMERA_STREAM = os.environ.get("VACUUM_CAMERA_STREAM", "a184_web")  # re-encoded by go2rtc (.tools/go2rtc.yaml): one clean 960x540 stream
CAMERA_LOG_EVERY = 600    # s: note "watched the camera" at most this often per person
CAMERA_CODECS = "avc1.640029,avc1.64002A,avc1.640033"

STATUS_EVERY = 2          # s
MAP_EVERY_BUSY = 5        # s, while the robot is out
MAP_EVERY_IDLE = 60       # s, while docked/idle
NAMES_EVERY = 300         # s
IDLE_STATES = V.DOCKED | {3}


# ---------------------------------------------------------------- map

class MapReader:
    """MapParser that also keeps the raw room-per-pixel layer, which the
    library reads and discards. Needed to know which room a tap lands in."""

    def __init__(self):
        self.mp = MapParser(MapParserConfig())
        ip = self.mp._map_parser._image_parser
        self._raw: dict = {}
        orig = ip.parse

        def parse(data, width, height, *a, **k):
            self._raw = {"data": bytes(data), "w": width, "h": height}
            return orig(data, width, height, *a, **k)
        ip.parse = parse

    def read(self, raw: bytes):
        parsed = self.mp.parse(raw)
        return parsed, self._raw


def room_grid(raw: dict) -> bytes:
    """Room number per map cell, rows top-down as rendered (the raw layer is
    bottom-up). 0 = not a room."""
    d, w, h = raw["data"], raw["w"], raw["h"]
    out = bytearray(w * h)
    for y in range(h):
        src = (h - 1 - y) * w
        for x in range(w):
            t = d[src + x]
            if t & 7 == 7 and t not in (1, 255):
                out[y * w + x] = (t & 0xFF) >> 3
    return bytes(out)


def affine(md) -> dict:
    """mm -> rendered-image px: px = ax*x + bx, py = ay*y + by (no rotation)."""
    (v0, m0), (v1, m1), (v2, m2) = [(c["vacuum"], c["map"]) for c in md.calibration()]
    ax = (m1["x"] - m0["x"]) / (v1["x"] - v0["x"])
    ay = (m2["y"] - m0["y"]) / (v2["y"] - v0["y"])
    return {"ax": ax, "bx": m0["x"] - ax * v0["x"], "ay": ay, "by": m0["y"] - ay * v0["y"]}


def map_payload(md, grid: bytes, raw: dict, names: dict[int, str], version: int) -> dict:
    tf = affine(md)
    px = lambda x, y: [round(tf["ax"] * x + tf["bx"], 1), round(tf["ay"] * y + tf["by"], 1)]
    pose = lambda p: {"x": p.x, "y": p.y, "a": p.a, "px": px(p.x, p.y)} if p else None
    area = lambda a: [px(a.x0, a.y0), px(a.x1, a.y1), px(a.x2, a.y2), px(a.x3, a.y3)]
    path = []
    if md.path and md.path.path:
        for seg in md.path.path:
            path += [px(p.x, p.y) for p in seg[::3]]  # thinned; it's only drawn
    return {
        "version": version,
        "png": f"/api/map.png?v={version}",
        "scale": md.image.dimensions.scale,
        "grid": {"w": raw["w"], "h": raw["h"], "cells": base64.b64encode(grid).decode()},
        "transform": tf,
        "rooms": [{"segment": r.number, "name": names.get(r.number),
                   "centre": px((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2),
                   "size_m": [round(abs(r.x1 - r.x0) / 1000, 1), round(abs(r.y1 - r.y0) / 1000, 1)]}
                  for r in sorted((md.rooms or {}).values(), key=lambda r: r.number)],
        "robot": pose(md.vacuum_position),
        "dock": pose(md.charger),
        "room_now": md.vacuum_room,
        "obstacles": [{"px": px(o.x, o.y), "what": o.details.description or f"type {o.details.type}",
                       "photo": o.details.photo_name} for o in (md.obstacles_with_photo or [])],
        "no_go": [area(a) for a in md.no_go_areas or []],
        "no_mop": [area(a) for a in md.no_mopping_areas or []],
        "walls": [[px(w.x0, w.y0), px(w.x1, w.y1)] for w in md.walls or []],
        "path": path,
        "updated": time.time(),
    }


# ---------------------------------------------------------------- the robot

class LockedVacuum(V.Vacuum):
    """Commands go to the robot one at a time; the link reconnects after errors."""

    def __init__(self):
        super().__init__()
        self._lock = asyncio.Lock()
        self.connected = False

    async def cmd(self, method, params=None, timeout=10.0):
        async with self._lock:
            if not self.connected:
                await self.connect()
                self.connected = True
            try:
                return await super().cmd(method, params, timeout)
            except Exception:
                self.close()
                self.connected = False
                raise


class Job:
    """One action as the page shows it: progress lines, then a result."""

    def __init__(self, action: str, label: str, who: str):
        self.id = uuid.uuid4().hex[:8]
        self.action, self.label, self.who = action, label, who
        self.started = time.time()
        self.steps: list[str] = []
        self.result: str | None = None   # "ok" | "failed"
        self.message = ""

    def say(self, line: str) -> None:
        self.steps.append(line.strip())

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "action", "label", "who", "started", "steps", "result", "message")}


class Robot:
    def __init__(self):
        self.v = LockedVacuum()
        self.cv = None
        self.status: dict | None = None
        self.status_at = 0.0
        self.online = False
        self.error: str | None = None
        self.names: dict[int, str] = {}
        self.map: dict | None = None
        self.map_png = b""
        self.map_version = 0
        self._map_due = 0.0
        self._names_due = 0.0
        self.job: Job | None = None
        self._job_task: asyncio.Task | None = None
        self.activity: collections.deque = collections.deque(maxlen=200)
        self.reader = MapReader()
        self._load_activity()

    # ---- background loops
    async def run(self) -> None:
        await asyncio.gather(self._status_loop(), self._map_loop())

    async def _status_loop(self) -> None:
        while True:
            try:
                self.status = await self.v.status()
                self.status_at, self.online, self.error = time.time(), True, None
            except Exception as e:  # noqa: BLE001 — shown on the page
                self.online, self.error = False, f"{type(e).__name__}: {e}"
                log.warning("status: %s", self.error)
                await asyncio.sleep(5)
            await asyncio.sleep(STATUS_EVERY)

    async def _cloud(self):
        if self.cv is None:
            from vacuum_map import CloudVacuum
            cv = CloudVacuum()
            await cv.__aenter__()
            self.cv = cv
        return self.cv

    async def _drop_cloud(self) -> None:
        cv, self.cv = self.cv, None
        if cv is not None:
            try:
                await cv.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    def refresh_map_soon(self, delay: float = 2.0) -> None:
        self._map_due = min(self._map_due, time.time() + delay)

    async def _map_loop(self) -> None:
        while True:
            if time.time() >= self._map_due:
                try:
                    await self.refresh_map()
                except Exception as e:  # noqa: BLE001
                    log.warning("map: %s: %s", type(e).__name__, e)
                    await self._drop_cloud()
                busy = (self.status or {}).get("state") not in IDLE_STATES
                self._map_due = time.time() + (MAP_EVERY_BUSY if busy else MAP_EVERY_IDLE)
            await asyncio.sleep(1)

    async def refresh_names(self) -> None:
        cv = await self._cloud()
        server = {str(r["id"]): r["name"] for r in await V._server_rooms(cv)}
        mapping = await self.v.cmd("get_room_mapping") or []
        self.names = {m[0]: server[str(m[1])] for m in mapping if str(m[1]) in server}
        self._names_due = time.time() + NAMES_EVERY

    async def refresh_map(self) -> None:
        cv = await self._cloud()
        if time.time() >= self._names_due:
            await self.refresh_names()
        parsed, raw = self.reader.read(await cv.current_map())
        if parsed is None or not raw:
            raise RuntimeError("could not parse map")
        self.map_version += 1
        self.map_png = parsed.image_content or b""
        self.map = map_payload(parsed.map_data, room_grid(raw), raw, self.names, self.map_version)

    # ---- activity
    def _load_activity(self) -> None:
        try:
            for line in ACTIVITY_FILE.read_text().splitlines()[-200:]:
                self.activity.append(json.loads(line))
        except FileNotFoundError:
            pass

    def record(self, who: str, what: str, result: str = "", message: str = "") -> None:
        entry = {"at": time.time(), "who": who, "what": what, "result": result, "message": message}
        self.activity.append(entry)
        DATA.mkdir(exist_ok=True)
        with ACTIVITY_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    # ---- actions
    def start(self, action: str, label: str, who: str, coro_fn) -> Job:
        """Run an action in the background; a newer action takes over the page's
        progress display (the robot itself just gets both commands, in order)."""
        job = Job(action, label, who)
        if self._job_task and not self._job_task.done():
            self._job_task.cancel()
        self.job = job

        async def runner():
            try:
                job.message = await coro_fn(job) or "ok"
                job.result = "ok"
            except asyncio.CancelledError:
                job.result, job.message = "superseded", "replaced by a newer action"
                raise
            except SystemExit as e:  # vacuum.py's "ignored" / robot-error reports
                job.result, job.message = "failed", str(e)
            except Exception as e:  # noqa: BLE001
                job.result, job.message = "failed", f"{type(e).__name__}: {e}"
            finally:
                if job.result != "superseded":
                    self.record(who, label, job.result or "", job.message)
                self.refresh_map_soon()
        self._job_task = asyncio.create_task(runner())
        return job

    async def confirm(self, job: Job, key: str, resend=None) -> str:
        """vacuum.py's default: wait until the robot has visibly started."""
        started_set, done_set = V.FOLLOW[key][:2]
        to_end = started_set <= done_set
        s = await V.follow(self.v, key, resend, to_end, say=job.say)
        state = V.STATES.get(s.get("state"), s.get("state"))
        return f"done: {state}" if to_end or s.get("state") in done_set else f"started: {state}"


# ---------------------------------------------------------------- actions

MODE_KEYS = {"suction": ("fan_power", V.SUCTION), "water": ("water_box_mode", V.WATER),
             "route": ("mop_mode", V.ROUTE)}


class BadRequest(Exception):
    pass


def _repeat(body: dict) -> int:
    r = body.get("repeat", 1)
    if r not in (1, 2, 3):
        raise BadRequest("repeat must be 1, 2 or 3")
    return r


def _mode(body: dict) -> dict:
    """{"suction": "turbo", ...} -> {"fan_power": 103, ...}; validated."""
    out = {}
    for k, v in (body.get("mode") or {}).items():
        if k not in MODE_KEYS or v not in MODE_KEYS[k][1]:
            raise BadRequest(f"bad mode {k}={v!r}")
        out[MODE_KEYS[k][0]] = MODE_KEYS[k][1][v]
    return out


def build_action(robot: Robot, body: dict):
    """Validate a request and return (label, coroutine function). Raises BadRequest."""
    v = robot.v
    act = body.get("action")

    async def send_and_confirm(job, method, key, params=None, resend=False):
        job.say(f"sent: {V.ok(await v.cmd(method, params))}")
        return await robot.confirm(job, key, (lambda: v.cmd(method, params)) if resend else None)

    if act == "clean":
        rooms = body.get("rooms") or []
        valid = {r["segment"] for r in (robot.map or {}).get("rooms", [])}
        if not all(isinstance(s, int) and s in valid for s in rooms):
            raise BadRequest(f"unknown rooms {rooms}")
        repeat, mode = _repeat(body), _mode(body)
        names = [robot.names.get(s) or f"room {s}" for s in rooms]
        label = f"clean {', '.join(names)}" if rooms else "clean everywhere"

        async def run(job):
            if mode:
                cur = (await v.cmd("get_clean_motor_mode"))[0]
                await v.cmd("set_clean_motor_mode",
                            [{k: cur[k] for k in ("fan_power", "water_box_mode", "mop_mode")} | mode])
            if rooms:
                return await send_and_confirm(job, "app_segment_clean", "clean",
                                              [{"segments": rooms, "repeat": repeat}])
            return await send_and_confirm(job, "app_start", "clean")
        return label, run

    if act == "zone":
        rect = body.get("rect")
        if not (isinstance(rect, list) and len(rect) == 4 and all(isinstance(n, int) for n in rect)):
            raise BadRequest("rect must be [x0, y0, x1, y1] in map mm")
        x0, y0, x1, y1 = rect
        if abs(x1 - x0) < 300 or abs(y1 - y0) < 300:
            raise BadRequest("zone must be at least 30 cm on each side")
        if abs(x1 - x0) > 15000 or abs(y1 - y0) > 15000:
            raise BadRequest("zone too large")
        repeat = _repeat(body)
        b = V.box(x0, y0, x1, y1)
        label = f"clean a {abs(x1 - x0) / 1000:.1f}×{abs(y1 - y0) / 1000:.1f} m zone"
        return label, lambda job: send_and_confirm(job, "app_zoned_clean", "zone", [[*b, repeat]])

    simple = {"pause": ("app_pause", "pause", True), "stop": ("app_stop", "stop", True),
              "dock": ("app_charge", "dock", True), "empty": ("app_start_collect_dust", "empty", False),
              "wash": ("app_start_wash", "wash", False), "wash_stop": ("app_stop_wash", "wash stop", False)}
    if act in simple:
        method, key, resend = simple[act]

        async def run(job):
            if act == "dock" and (await v.status()).get("state") == V.RC_STATE:
                await v.cmd("app_rc_end")
            return await send_and_confirm(job, method, key, resend=resend)
        return {"wash_stop": "stop mop wash", "empty": "empty the bin",
                "wash": "wash the mop"}.get(act, act), run

    if act == "resume":
        async def run(job):
            s = await v.status()
            method = {3: "resume_segment_clean", 2: "resume_zoned_clean"}.get(s.get("in_cleaning"), "app_start")
            return await send_and_confirm(job, method, "resume")
        return "resume", run

    if act == "dry":
        on = body.get("on")
        if not isinstance(on, bool):
            raise BadRequest("dry needs on: true|false")

        async def run(job):
            return V.ok(await v.cmd("app_set_dryer_status", {"status": int(on)}))
        return f"dryer {'on' if on else 'off'}", run

    raise BadRequest(f"unknown action {act!r}")


# ---------------------------------------------------------------- identity

class Identity:
    """Who is asking, from Cloudflare Access. Fails closed."""

    def __init__(self, env=os.environ):
        self.dev = env.get("VACUUM_WEB_DEV") == "1"
        self.tailnet = env.get("VACUUM_WEB_TAILNET") == "1"
        self.team = env.get("CF_ACCESS_TEAM")   # e.g. your-team
        self.aud = env.get("CF_ACCESS_AUD")
        self._jwks = None
        if self.dev:
            log.warning("VACUUM_WEB_DEV=1: no Access checks; never expose this instance")
        elif self.tailnet:
            log.info("tailnet mode: identity from tailscale serve's Tailscale-User-Login")
        elif not (self.team and self.aud) or self.aud == "FILL-ME-IN":
            raise SystemExit("set CF_ACCESS_TEAM and CF_ACCESS_AUD, or VACUUM_WEB_TAILNET=1 "
                             "behind tailscale serve (or VACUUM_WEB_DEV=1 locally)")

    def _verify(self, token: str) -> dict:
        import jwt
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(f"https://{self.team}.cloudflareaccess.com/cdn-cgi/access/certs")
        key = self._jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=self.aud)

    async def who(self, request: web.Request) -> str:
        if self.dev:
            return request.headers.get("Cf-Access-Authenticated-User-Email", "local")
        if self.tailnet:
            login = request.headers.get("Tailscale-User-Login")
            if not login:  # not via tailscale serve, or a tagged device with no user
                raise web.HTTPForbidden(text="open this through the tailnet address")
            return login
        import jwt
        token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")
        if not token:
            raise web.HTTPForbidden(text="no Cloudflare Access token")
        try:  # the key fetch is blocking; keep it off the event loop
            claims = await asyncio.to_thread(self._verify, token)
        except jwt.PyJWTError as e:
            raise web.HTTPForbidden(text=f"bad Cloudflare Access token: {e}") from None
        return claims.get("email", "unknown")


# ---------------------------------------------------------------- HTTP

def status_view(robot: Robot) -> dict | None:
    s = robot.status
    if not s:
        return None
    ci = s.get("cleaning_info") or {}
    seg = ci.get("target_segment_id", -1)
    dock = [n for n, on in (("emptying", s.get("dust_collection_status")),
                            ("drying", s.get("dry_status")), ("washing", s.get("wash_phase"))) if on]
    return {
        "state": s.get("state"),
        "state_text": V.STATES.get(s.get("state"), f"state {s.get('state')}"),
        "battery": s.get("battery"),
        "charging": s.get("charge_status") == 1,
        "error": s.get("error_code") or None,
        "error_text": V.ERRORS.get(s.get("error_code"), f"error {s.get('error_code')}")
        if s.get("error_code") else None,
        "dock_error": s.get("dock_error_status") or None,
        "dock_problem": (V.dock_error(s.get("dock_error_status")) or (None, None))[0],
        "dock_fix": (V.dock_error(s.get("dock_error_status")) or (None, None))[1],
        "room": seg if seg and seg > 0 else None,
        "room_name": robot.names.get(seg) if seg and seg > 0 else None,
        "suction": V._name(V.SUCTION, s.get("fan_power")),
        "water": V._name(V.WATER, s.get("water_box_mode")),
        "route": V._name(V.ROUTE, s.get("mop_mode")),
        "run_minutes": s.get("clean_time", 0) // 60,
        "run_m2": round(s.get("clean_area", 0) / 1e6, 1),
        "in_cleaning": s.get("in_cleaning"),
        "dock": dock,
        "photos": s.get("camera_status", 0) & V.PHOTO_BITS == V.PHOTO_BITS,
    }


class CameraKeeper:
    """Holds one go2rtc consumer of the camera while anyone watches (and LINGER s
    after), so a viewer that reconnects joins the running stream instead of making
    the robot stop and restart its camera: a quick stop-then-start rebooted the
    robot once (3 Nov). Also spaces robot camera starts by MIN_GAP s, and lets a
    viewer in only once the stream has passed its 128x96 -> 960x540 switch."""

    LINGER = 20       # s to keep the stream after the last viewer leaves
    MIN_GAP = 10      # s between one robot camera session ending and the next
    SETTLE = 2.5      # s after the first video data (the size switch comes within ~2 s)
    NO_VIDEO = 25     # s without any video: go2rtc is stuck (seen after a robot reboot)

    def __init__(self) -> None:
        self.viewers = 0
        self.task: asyncio.Task | None = None
        self.ready: asyncio.Future | None = None
        self.stopped_at = 0.0
        self.why = "camera not answering"       # why the last stream failed, for viewers
        self._linger: asyncio.TimerHandle | None = None

    async def acquire(self) -> bool:
        """Count a viewer in; True once the stream is up, False if it isn't coming."""
        self.viewers += 1
        if self._linger:
            self._linger.cancel()
            self._linger = None
        if self.task is None or self.task.done():
            wait = self.MIN_GAP - (time.time() - self.stopped_at)
            if wait > 0:
                await asyncio.sleep(wait)
            if self.task is None or self.task.done():
                self.ready = asyncio.get_running_loop().create_future()
                self.task = asyncio.create_task(self._hold(self.ready))
        try:
            return await asyncio.wait_for(asyncio.shield(self.ready), self.NO_VIDEO + 5)
        except asyncio.TimeoutError:
            return False

    @staticmethod
    async def _restart_go2rtc(session) -> None:
        try:   # go2rtc restarts itself in a few seconds; the call's connection drops
            await session.post(f"{GO2RTC}/api/restart", timeout=__import__("aiohttp").ClientTimeout(total=5))
        except Exception:  # noqa: BLE001
            pass

    def release(self) -> None:
        self.viewers = max(0, self.viewers - 1)
        if self.viewers == 0 and self.task and not self.task.done():
            self._linger = asyncio.get_running_loop().call_later(self.LINGER, self._stop)

    def _stop(self) -> None:
        self._linger = None
        if self.viewers == 0 and self.task:
            self.task.cancel()

    async def _hold(self, ready: asyncio.Future) -> None:
        import aiohttp
        url = GO2RTC.replace("http", "ws", 1) + f"/api/ws?src={CAMERA_STREAM}"
        loop = asyncio.get_running_loop()
        self.why = "camera not answering"
        try:
            async with aiohttp.ClientSession() as s, s.ws_connect(url, heartbeat=30) as up:
                await up.send_json({"type": "mse", "value": CAMERA_CODECS})
                deadline, got_video = loop.time() + self.NO_VIDEO, False
                while True:
                    try:
                        m = await up.receive(timeout=None if got_video else max(0.1, deadline - loop.time()))
                    except asyncio.TimeoutError:
                        log.warning("camera: no video in %d s; restarting go2rtc", self.NO_VIDEO)
                        await self._restart_go2rtc(s)
                        break
                    if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    if m.type == aiohttp.WSMsgType.BINARY and not got_video:   # up; let it settle
                        got_video = True
                        loop.call_later(self.SETTLE, lambda: ready.done() or ready.set_result(True))
                    elif m.type == aiohttp.WSMsgType.TEXT and '"error"' in m.data:
                        log.warning("camera: %s", m.data[:200])
                        break
        except (aiohttp.ClientError, OSError) as e:
            log.warning("camera: go2rtc unreachable: %s", e)
            self.why = "the camera viewer is not running"
        finally:
            if not ready.done():
                ready.set_result(False)
            self.stopped_at = time.time()


def index_html() -> str:
    """index.html with ?v=<mtime> on app.js and style.css, so phones (Safari
    especially) fetch the new files after a change instead of a cached copy."""
    html = (STATIC / "index.html").read_text()
    for name in ("app.js", "style.css"):
        v = int((STATIC / name).stat().st_mtime)
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
    return html


def make_app(robot: Robot, ident: Identity) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/api/state")
    async def state(request):
        await ident.who(request)
        return web.json_response({
            "online": robot.online,
            "error": robot.error,
            "status": status_view(robot),
            "status_age": round(time.time() - robot.status_at, 1) if robot.status_at else None,
            "job": robot.job.as_dict() if robot.job else None,
            "activity": list(robot.activity)[-30:][::-1],
            "map_version": robot.map_version,
            "names": {str(k): v for k, v in robot.names.items()},
            "modes": {"suction": list(V.SUCTION), "water": list(V.WATER), "route": list(V.ROUTE)},
        })

    @routes.get("/api/map")
    async def map_json(request):
        await ident.who(request)
        if not robot.map:
            raise web.HTTPServiceUnavailable(text="map not loaded yet")
        return web.json_response(robot.map)

    @routes.get("/api/map.png")
    async def map_png(request):
        await ident.who(request)
        if not robot.map_png:
            raise web.HTTPServiceUnavailable(text="map not loaded yet")
        return web.Response(body=robot.map_png, content_type="image/png",
                            headers={"Cache-Control": "private, max-age=86400"})

    @routes.post("/api/action")
    async def action(request):
        who = await ident.who(request)
        try:
            body = await request.json()
            label, fn = build_action(robot, body)
        except (BadRequest, json.JSONDecodeError, AttributeError) as e:
            raise web.HTTPBadRequest(text=str(e)) from None
        return web.json_response(robot.start(body["action"], label, who, fn).as_dict())

    @routes.post("/api/rooms/{segment}/name")
    async def rename(request):
        who = await ident.who(request)
        seg = int(request.match_info["segment"])
        name = ((await request.json()).get("name") or "").strip()
        if not 1 <= len(name) <= 40:
            raise web.HTTPBadRequest(text="name must be 1-40 characters")
        if seg not in {r["segment"] for r in (robot.map or {}).get("rooms", [])}:
            raise web.HTTPBadRequest(text=f"no room {seg}")
        await V.name_room(robot.v, await robot._cloud(), seg, name)
        await robot.refresh_names()
        robot.record(who, f"named room {seg} \"{name}\"", "ok")
        robot.refresh_map_soon(0)
        return web.json_response({"segment": seg, "name": name})

    watched: dict[str, float] = {}
    keeper = CameraKeeper()

    @routes.get("/api/camera/ws")
    async def camera_ws(request):
        """Live video: go2rtc's MSE stream for our one camera, nothing else of its API.
        The browser may only ask for MSE (with its codec list); go2rtc's replies and
        video segments pass through unchanged."""
        import aiohttp
        who = await ident.who(request)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        if time.time() - watched.get(who, 0) > CAMERA_LOG_EVERY:
            watched[who] = time.time()
            robot.record(who, "watched the camera", "ok")
        url = GO2RTC.replace("http", "ws", 1) + f"/api/ws?src={CAMERA_STREAM}"
        if not await keeper.acquire():
            keeper.release()
            await ws.send_json({"type": "error", "value": keeper.why})
            await ws.close()
            return ws
        try:
            async with aiohttp.ClientSession() as s, s.ws_connect(url, heartbeat=30) as up:
                async def to_browser():
                    async for m in up:
                        if m.type == aiohttp.WSMsgType.BINARY:
                            await ws.send_bytes(m.data)
                        elif m.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_str(m.data)

                async def to_go2rtc():
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            msg = json.loads(m.data)
                        except ValueError:
                            continue
                        if not (isinstance(msg, dict) and isinstance(msg.get("value"), str)):
                            continue
                        if msg.get("type") == "mse":
                            await up.send_json({"type": "mse", "value": msg["value"][:500]})
                        elif msg.get("type") == "diag":   # the player's own report; stays here
                            log.info("camera diag (%s): %s", who, msg["value"][:600])

                tasks = [asyncio.create_task(to_browser()), asyncio.create_task(to_go2rtc())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in tasks:
                        t.cancel()
        except (aiohttp.ClientError, OSError) as e:
            log.warning("camera: go2rtc unreachable: %s", e)
            if not ws.closed:
                await ws.send_json({"type": "error", "value": "the camera viewer is not running"})
        finally:
            keeper.release()
            await ws.close()
        return ws

    @routes.get("/")
    async def index(request):
        await ident.who(request)
        return web.Response(text=index_html(), content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", STATIC)
    return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    robot = Robot()
    app = make_app(robot, Identity())
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    log.info("listening on http://127.0.0.1:%d", PORT)
    await robot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
