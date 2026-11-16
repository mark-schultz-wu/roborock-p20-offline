# /// script
# requires-python = ">=3.11"
# dependencies = ["python-roborock"]
# ///
"""Control the Roborock P20 (a184) without raw JSON.

Most commands talk to the robot directly (TCP 58867, no login). Maps,
photos, history maps and room names go through the local server
(see vacuum_map.py). Formats come from COMMANDS.md.

Cleaning
  status                       state, battery, modes, dock, errors
  clean [ROOM ...] [-r N]      whole home, or rooms by name or segment id
  zone X0 Y0 X1 Y1 [-r N]      clean a rectangle (map mm)
  spot | goto X Y [--face DEG] go to a map point; optionally turn to a heading
  pause | resume | stop | dock | find
  where                        robot position, heading, room, distance to dock
  clean, zone, spot, goto, pause, resume, stop, dock, empty and wash wait
  until the robot has visibly started (it can answer "ok" and do nothing;
  dock/stop/pause are resent once), printing each state. --wait (-w)
  follows to the end; --no-wait returns at once. Ctrl-C stops watching only

Driving (remote-control mode; ends with `drive end`, `dock`, or ~1 min idle)
  drive forward|back M [--speed S]   up to 2 m, 0.05-0.3 m/s (default 0.2)
  drive left|right DEG               turn in place
  drive face DEG                     turn to a map heading (as `map` reports)
  drive end                          leave remote-control mode

Settings
  mode [--suction S] [--water W] [--route R]   show or set (S: quiet balanced
                               turbo max max+ off smart; W: off low medium high
                               smart; R: standard deep fast smart)
  volume [N]                   show or set 0-100
  dnd [off | HH:MM HH:MM]      do-not-disturb
  empty | wash [stop] | dry on|off            dock actions
  consumables [--reset NAME]   e.g. --reset sensor_dirty_time

Rooms and map (saved map required; map saving is on)
  rooms                        list rooms with names, order, per-room modes
  rooms name SEG NAME          name a room (stored on the local server)
  rooms order SEG [SEG ...]    cleaning order
  rooms mode SEG [--suction S] [--water W] [--route R] [-r N]
  rooms merge SEG SEG | rooms split SEG X1 Y1 X2 Y2 | rooms resegment
  nogo [list]                  no-go zones, no-mop zones, walls
  nogo add X0 Y0 X1 Y1 [--no-mop] | nogo wall X0 Y0 X1 Y1 | nogo clear
  map [OUT.png]                current map (default maps/current.png)
  history [--all]              cleaning runs (--all adds dock-only events)
  record ID [OUT.png]          map of a past run
  photo ID [OUT.jpg]           obstacle photo (IDs from `map` or `record`)
  photos [on|off]              obstacle photo saving

Schedules
  schedule [list]
  schedule add HH:MM [--days daily|weekdays|weekends|mon,wed,...]
  schedule on|off|del ID

Voice
  voice                        active voice pack
  voice install FILE [--sid N] install a pack file (default sid 3 = English),
                               e.g. voice install voice/en-coral.pkg

Raw
  cmd METHOD [JSON]            any command (direct link)

Library use:
    from vacuum import Vacuum
    async with Vacuum() as v:
        print(await v.status())
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import pathlib
import re
import sys
import time
from typing import Any

import lab_tls  # noqa: F401  (must precede roborock imports; see lab_tls.py)
import labconfig as cfg  # values come from lab.env (see lab.env.example)
import roborock.roborock_message as rm
from roborock.devices.rpc.v1_channel import RpcChannel, RpcStrategy, decode_rpc_response
from roborock.devices.transport.local_channel import LocalChannel
from roborock.exceptions import RoborockException
from roborock.protocols.v1_protocol import RequestMessage
from roborock.util import RoborockLoggerAdapter

HOST = cfg.HOST       # vacuum's LAN IP; see lab.env / lab.env.example
LOCAL_KEY = cfg.LOCAL_KEY
DUID = cfg.DUID
HERE = pathlib.Path(__file__).parent

STATES = {1: "starting", 2: "off dock", 3: "idle", 4: "remote control", 5: "cleaning",
          6: "returning", 7: "remote control", 8: "charging", 9: "charging problem",
          10: "paused", 11: "spot cleaning", 12: "error", 13: "shutting down",
          14: "updating", 15: "docking", 16: "going to target", 17: "zone cleaning",
          18: "room cleaning", 22: "emptying bin", 23: "washing mop", 25: "washing mop",
          26: "going to wash mop", 29: "mapping", 100: "charged"}
ERRORS = {0: "none", 1: "lidar blocked", 2: "bumper stuck", 3: "wheels lifted",
          4: "cliff sensor", 5: "main brush jammed", 6: "side brush jammed",
          7: "wheels jammed", 8: "trapped", 9: "no dustbin"}
# Dock errors (dock_error_status): (what's wrong, what to do). Codes and meanings
# from python-roborock's RoborockDockErrorCode.
DOCK_ERRORS = {
    32: ("Dock dust bag or filter is missing", "Check the dust bag and filter in the dock."),
    33: ("Dock dust bin or filter is out of place", "Reseat the dust bin and filter in the dock."),
    34: ("Dock air duct is blocked", "Check and clean the dock's dust bin, filter and air duct."),
    35: ("The dock couldn't empty the robot's bin", "Try emptying again later."),
    38: ("Clean water tank is empty",
         "Refill the clean water tank on the dock and check it sits properly. "
         "The robot can't wash its mop or mop until then."),
    39: ("Dirty water tank is full", "Empty the dirty water tank on the dock and put it back properly."),
    42: ("Dock's cleaning brush is jammed", "Take out the dock's maintenance brush and clean it."),
    44: ("Dock water filter is blocked", "Clean the water filter and put it back."),
    46: ("Dock dust bin is missing", "Put the dust bin back in the dock."),
    53: ("Dock cleaning tray is full or blocked", "Clean the tray and strainer under the robot's parking spot."),
}


def dock_error(code: int | None) -> tuple[str, str] | None:
    """(what's wrong, what to do) for a dock error code, or None for no error."""
    if not code:
        return None
    return DOCK_ERRORS.get(code, (f"Dock error {code}", "Check the dock."))
SUCTION = {"quiet": 101, "balanced": 102, "turbo": 103, "max": 104, "off": 105,
           "custom": 106, "max+": 108, "smart": 110}
WATER = {"off": 200, "low": 201, "medium": 202, "high": 203, "custom": 204, "smart": 209}
ROUTE = {"standard": 300, "deep": 301, "custom": 302, "fast": 304, "smart": 306}
DAY_NAMES = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
DAY_SETS = {"daily": "*", "weekdays": "1,2,3,4,5", "weekends": "0,6"}
CONSUMABLES = ["main_brush_work_time", "side_brush_work_time", "filter_work_time",
               "sensor_dirty_time", "strainer_work_times", "dust_collection_work_times"]
MAX_REPEAT = 3  # the app offers 1-3 passes


# ---------------------------------------------------------------- input checks
# The firmware doesn't always validate params, and junk has wedged it before
# (COMMANDS.md, "Don't probe with junk params"), so reject bad input here.

def hhmm(text: str) -> tuple[int, int]:
    """'HH:MM' -> (hour, minute), 00:00-23:59."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise argparse.ArgumentTypeError(f"bad time {text!r}; use HH:MM, 00:00-23:59")
    return int(m[1]), int(m[2])


def _day(token: str) -> int:
    t = token.strip().lower()
    hits = [i for i, n in enumerate(DAY_NAMES) if len(t) >= 3 and n.startswith(t)]
    if len(hits) != 1:
        raise argparse.ArgumentTypeError(
            f"bad day {token!r}; use daily, weekdays, weekends, or days like mon,wed or mon-fri")
    return hits[0]


def days_field(text: str) -> str:
    """--days value -> cron day-of-week field: daily | weekdays | weekends | mon,wed | mon-fri."""
    t = text.strip().lower()
    if t in DAY_SETS:
        return DAY_SETS[t]
    out: set[int] = set()
    for part in t.split(","):
        if "-" in part:
            a, b = (_day(x) for x in part.split("-", 1))
            out.add(a)
            while a != b:  # ranges may wrap, e.g. sat-sun
                a = (a + 1) % 7
                out.add(a)
        else:
            out.add(_day(part))
    return ",".join(str(d) for d in sorted(out))


def repeat(text: str) -> int:
    n = int(text) if text.isdigit() else 0
    if not 1 <= n <= MAX_REPEAT:
        raise argparse.ArgumentTypeError(f"repeat must be 1-{MAX_REPEAT}, not {text!r}")
    return n


def box(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
    """Corners in any order -> (left, bottom, right, top); refuses zero area."""
    if x0 == x1 or y0 == y1:
        raise SystemExit(f"rectangle {x0} {y0} {x1} {y1} has no area")
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _name(table: dict[str, int], code: Any) -> str:
    return next((k for k, v in table.items() if v == code), str(code))


def _code(table: dict[str, int], name: str | None) -> int | None:
    if name is None:
        return None
    if name not in table:
        raise SystemExit(f"unknown value {name!r}; choose from {', '.join(table)}")
    return table[name]


class Vacuum:
    """Direct local-protocol control of the vacuum (TCP 58867)."""

    def __init__(self, host: str = HOST, local_key: str = LOCAL_KEY, duid: str = DUID):
        self.host, self.local_key, self.duid = host, local_key, duid
        self._channel: LocalChannel | None = None
        self._rpc: RpcChannel | None = None

    async def __aenter__(self) -> "Vacuum":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        self.close()

    async def connect(self) -> None:
        self._channel = LocalChannel(self.host, self.local_key, self.duid)
        await self._channel.connect()

        def encoder(x: RequestMessage):
            return x.encode_message(rm.RoborockMessageProtocol.GENERAL_REQUEST,
                                    version=self._channel.protocol_version)

        self._rpc = RpcChannel(
            lambda: [RpcStrategy(name="local", channel=self._channel, encoder=encoder,
                                 decoder=decode_rpc_response)],
            RoborockLoggerAdapter(duid=self.duid, logger=logging.getLogger("vacuum")))

    def close(self) -> None:
        if self._channel is not None:
            try:
                self._channel.close()
            except TypeError:
                pass
            self._channel = None

    async def cmd(self, method: str, params: Any = None, timeout: float = 10.0) -> Any:
        """Send any command and return the decoded response."""
        assert self._rpc is not None, "not connected"
        return await asyncio.wait_for(self._rpc.send_command(method, params=params), timeout)

    async def edit(self, method: str, data: Any) -> Any:
        """Map/room edit: wrapped params, bracketed by start/end_edit_map."""
        await self.cmd("start_edit_map", [])
        try:
            return await self.cmd(method, {"data": data, "need_retry": 1})
        finally:
            await self.cmd("end_edit_map")

    # simple accessors (kept for library users)
    async def status(self) -> dict[str, Any]:
        res = await self.cmd("get_status")
        return res[0] if isinstance(res, list) and res else res

    async def battery(self) -> int:
        return int((await self.status()).get("battery", -1))

    async def state(self) -> str:
        s = await self.status()
        return STATES.get(s.get("state"), f"state={s.get('state')}")

    async def consumables(self) -> Any:
        return await self.cmd("get_consumable")

    async def network(self) -> Any:
        return await self.cmd("get_network_info")

    async def start(self) -> Any:
        return await self.cmd("app_start")

    async def pause(self) -> Any:
        return await self.cmd("app_pause")

    async def dock(self) -> Any:
        return await self.cmd("app_charge")

    async def find(self) -> Any:
        return await self.cmd("find_me")


# ---------------------------------------------------------------- helpers

async def room_table(v: Vacuum) -> list[dict]:
    """[{segment, iot_id, tag, name}] from the robot plus server room names."""
    mapping = await v.cmd("get_room_mapping") or []
    names: dict[str, str] = {}
    try:
        from vacuum_map import CloudVacuum
        async with CloudVacuum() as cv:
            names = {str(r["id"]): r["name"] for r in await _server_rooms(cv)}
    except Exception as e:  # names are a nicety; segments still work
        print(f"(room names unavailable: {e})", file=sys.stderr)
    return [{"segment": m[0], "iot_id": str(m[1]), "tag": m[2] if len(m) > 2 else None,
             "name": names.get(str(m[1]))} for m in mapping]


def _hawk_auth():
    """python-roborock's request signer. A private function: up to 7.2x it was in
    roborock.web_api, from 7.22.2 in roborock.web_api._hawk (vacuum.py rooms broke on a
    fresh install, 2026-11-14)."""
    try:
        from roborock.web_api import _get_hawk_authentication
    except ImportError:
        from roborock.web_api._hawk import _get_hawk_authentication
    return _get_hawk_authentication


async def _server_rooms(cv) -> list[dict]:
    from vacuum_map import BASE_URL
    from roborock.web_api import PreparedRequest
    _get_hawk_authentication = _hawk_auth()
    import aiohttp
    ud = cv.user_data
    home_id = await _home_id(cv)
    path = f"/user/homes/{home_id}/rooms"
    async with aiohttp.ClientSession() as session:
        req = PreparedRequest(ud.rriot.r.a, session,
                              {"Authorization": _get_hawk_authentication(ud.rriot, path)})
        resp = await req.request("get", path)
    return resp.get("result") or []


async def _home_id(cv) -> int:
    from roborock.web_api import RoborockApiClient
    from vacuum_map import BASE_URL, EMAIL
    api = RoborockApiClient(EMAIL, base_url=BASE_URL)
    return await api._get_home_id(cv.user_data)


async def _create_server_room(cv, name: str) -> str:
    from roborock.web_api import PreparedRequest
    _get_hawk_authentication = _hawk_auth()
    import aiohttp
    ud = cv.user_data
    home_id = await _home_id(cv)
    path = f"/user/homes/{home_id}/rooms"
    form = {"name": name}
    async with aiohttp.ClientSession() as session:
        req = PreparedRequest(ud.rriot.r.a, session,
                              {"Authorization": _get_hawk_authentication(ud.rriot, path, form)})
        resp = await req.request("post", path, data=form)
    if not resp.get("success", True) and "result" not in resp:
        raise SystemExit(f"server refused room: {resp}")
    return str(resp.get("result"))


async def resolve_rooms(v: Vacuum, args: list[str]) -> list[int]:
    if all(a.isdigit() for a in args):
        return [int(a) for a in args]
    table = await room_table(v)
    out = []
    for a in args:
        if a.isdigit():
            out.append(int(a))
            continue
        hit = [r["segment"] for r in table if (r["name"] or "").lower() == a.lower()]
        if not hit:
            raise SystemExit(f"no room named {a!r}; see `vacuum.py rooms`")
        out += hit
    return out


def mode_params(a) -> dict:
    p = {}
    if a.suction: p["fan_power"] = _code(SUCTION, a.suction)
    if a.water: p["water_box_mode"] = _code(WATER, a.water)
    if a.route: p["mop_mode"] = _code(ROUTE, a.route)
    return p


def rect(x0, y0, x1, y1) -> list[int]:
    x0, y0, x1, y1 = box(x0, y0, x1, y1)
    return [x0, y0, x1, y0, x1, y1, x0, y1]


# ---------------------------------------------------------------- manual drive
# Measured on this robot (30 Oct): velocity is m/s and accurate off the dock;
# omega is rad/s, positive = left (counterclockwise on the map); each move
# runs for its duration then stops by itself. The robot silently ignores a
# seqnum that isn't higher than every one it has seen, even across
# app_rc_end/app_rc_start, so seqnums come from the clock.

DRIVE_SPEED = 0.2   # m/s default
MAX_SPEED = 0.3     # m/s, the app's limit for manual drive
TURN_RATE = 1.0     # rad/s
MAX_DISTANCE = 2.0  # m per drive command
MOVE_CHUNK = 1.0    # s per app_rc_move; the next is sent before it ends
RC_EPOCH = 1767225600  # 2026-01-01; seqnum = tenths of a second since
RC_STATE = 7
_now = time.monotonic  # patched in tests
_last_seq = 0


def rc_seq() -> int:
    global _last_seq
    _last_seq = max(int((time.time() - RC_EPOCH) * 10), _last_seq + 1)
    return _last_seq


def speed(text: str) -> float:
    try:
        s = float(text)
    except ValueError:
        s = 0
    if not 0.05 <= s <= MAX_SPEED:
        raise argparse.ArgumentTypeError(f"speed must be 0.05-{MAX_SPEED} m/s, not {text!r}")
    return s


def _bounded(name: str, lo: float, hi: float, lo_open: bool = False):
    def f(text: str) -> float:
        try:
            x = float(text)
        except ValueError:
            x = None
        if x is None or x > hi or x < lo or (lo_open and x == lo):
            raise argparse.ArgumentTypeError(
                f"{name} must be {'over ' if lo_open else ''}{lo:g} to {hi:g}, not {text!r}")
        return x
    f.__name__ = name
    return f


distance = _bounded("distance (m)", 0, MAX_DISTANCE, lo_open=True)
angle = _bounded("angle (degrees)", 0, 360, lo_open=True)
heading = _bounded("heading (degrees)", -180, 180)


def turn_delta(current: float, target: float) -> float:
    """Shortest signed turn in degrees from current to target heading (+ = left)."""
    return (target - current + 180) % 360 - 180


async def rc_enter(v: Vacuum) -> None:
    """Switch to remote-control mode (once) and wait until the robot is in it."""
    if (await v.status()).get("state") == RC_STATE:
        return
    await v.cmd("app_rc_start")
    for _ in range(20):
        if (await v.status()).get("state") == RC_STATE:
            return
        await asyncio.sleep(0.5)
    raise SystemExit("robot did not enter remote-control mode")


async def rc_motion(v: Vacuum, velocity: float, omega: float, seconds: float) -> None:
    """Hold one motion for `seconds`, as overlapping ≤1 s moves, then let it stop."""
    end = _now() + seconds
    while (left := end - _now()) > 0.05:
        await v.cmd("app_rc_move", [{"omega": round(omega, 3), "velocity": round(velocity, 3),
                                     "duration": round(min(MOVE_CHUNK, left) * 1000),
                                     "seqnum": rc_seq()}])
        await asyncio.sleep(min(0.8, left))
    await asyncio.sleep(max(0.0, end - _now()) + 0.3)  # let the last move finish


async def rc_turn(v: Vacuum, degrees: float) -> None:
    rad = abs(degrees) * 3.141592653589793 / 180
    await rc_motion(v, 0.0, TURN_RATE if degrees > 0 else -TURN_RATE, rad / TURN_RATE)


async def live_map():
    """Parsed live map (MapData): robot/dock pose, rooms, zones, obstacles. ~1-3 s."""
    from vacuum_map import CloudVacuum
    from roborock.map.map_parser import MapParser, MapParserConfig
    async with CloudVacuum() as cv:
        return MapParser(MapParserConfig()).parse(await cv.current_map()).map_data


async def robot_pose() -> tuple[int, int, float]:
    """(x, y, heading°) from the live map."""
    p = (await live_map()).vacuum_position
    return p.x, p.y, p.a


def room_bounds(md) -> dict[int, tuple[int, int, int, int]]:
    """{segment: (x0, y0, x1, y1)} bounding boxes from the map, in mm."""
    return {r.number: (r.x0, r.y0, r.x1, r.y1) for r in (md.rooms or {}).values()}


def room_at(bounds: dict[int, tuple], x: float, y: float) -> int | None:
    """Room whose bounding box holds the point; the smallest one if several do."""
    hits = [(abs((b[2] - b[0]) * (b[3] - b[1])), seg) for seg, b in bounds.items()
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]]
    return min(hits)[1] if hits else None


# ---------------------------------------------------------------- --wait
# The robot answers "ok" at once, sometimes without acting (e.g. dock right
# after stop). --wait follows the state until the action is done, prints each
# change, and reports a command that never took effect.

DOCKED = {8, 100}
AT_DOCK = DOCKED | {22, 23, 25}         # charging, emptying, washing at the dock
CLEANING = {1, 2, 5, 11, 17, 18, 23, 25, 26}  # incl. the mop wash before a run
RUN_DONE = DOCKED | {3, 10}             # back on the dock, idle, or paused
# action: (states that show it started, states that mean done, s to start, s total)
FOLLOW = {
    "clean": (CLEANING, RUN_DONE, 60, 4 * 3600),
    "zone": (CLEANING, RUN_DONE, 60, 4 * 3600),
    "resume": (CLEANING | {6, 16}, RUN_DONE, 30, 4 * 3600),
    "spot": ({11}, RUN_DONE, 30, 1800),
    "goto": ({16}, {3}, 20, 600),
    "dock": ({6, 15} | AT_DOCK, AT_DOCK, 20, 900),
    "stop": ({3} | DOCKED, {3} | DOCKED, 15, 15),
    "pause": ({3, 10} | DOCKED, {3, 10} | DOCKED, 15, 15),
    "empty": ({22}, DOCKED, 20, 300),
    "wash": ({23, 25, 26}, DOCKED, 20, 1200),
    "wash stop": (DOCKED, DOCKED, 20, 20),
}


# Known reasons an action is ignored, shown with the "ignored" message.
IGNORED_WHY = {
    "empty": "the dock refuses to empty again soon after the last emptying "
             "(the robot announces it); try later",
}


def _say(line: str) -> None:
    print(line, flush=True)


async def follow(v: Vacuum, key: str, resend=None, to_end: bool = True, say=_say) -> dict:
    """Poll status until `key` has started (to_end=False) or is done; print state
    changes. Returns the last status. If nothing happens in time, call `resend`
    once (when given), else exit with an error."""
    started_set, done_set, start_limit, total = FOLLOW[key]
    t0 = _now()
    last, started, retried = None, False, False
    while True:
        s = await v.status()
        state = s.get("state")
        if state != last:
            say(f"  {_now() - t0:5.0f}s  {STATES.get(state, f'state {state}')}")
            last = state
        if state == 12:
            raise SystemExit(f"{key}: robot error: "
                             f"{ERRORS.get(s.get('error_code'), s.get('error_code'))}")
        started = started or state in started_set
        if started and (state in done_set or not to_end):
            return s
        waited = _now() - t0
        if not started and waited > start_limit:
            if resend is None or retried:
                why = f"; likely because {IGNORED_WHY[key]}" if key in IGNORED_WHY else ""
                raise SystemExit(f"{key}: the robot ignored it (still "
                                 f"{STATES.get(state, state)} after {waited:.0f} s){why}")
            say(f"  the robot ignored {key}; sending it again")
            await resend()
            retried, start_limit = True, waited + start_limit
        if waited > total:
            raise SystemExit(f"{key}: not finished after {total // 60} min")
        await asyncio.sleep(2)


async def track(v: Vacuum, a, key: str, resend=None) -> None:
    """After sending `key`: by default confirm the robot started it; --wait
    follows it to the end; --no-wait returns at once."""
    if a.no_wait:
        return
    started_set, done_set = FOLLOW[key][:2]
    to_end = a.wait or started_set <= done_set  # short actions: started = done
    s = await follow(v, key, resend, to_end)
    if to_end or s.get("state") in done_set:
        print(done_line(key, s))
    else:
        print(f"started: {STATES.get(s.get('state'), s.get('state'))} "
              f"(--wait follows it to the end)")


def done_line(key: str, s: dict) -> str:
    line = f"done: {STATES.get(s.get('state'), s.get('state'))}"
    if key in ("clean", "zone", "resume", "spot"):
        line += f", {s.get('clean_time', 0) // 60} min, {s.get('clean_area', 0) / 1e6:.1f} m²"
    return line


def ok(reply) -> str:
    return "ok" if reply == ["ok"] else str(reply)


def finish_text(code: int) -> str:
    from roborock.data.v1.v1_code_mappings import RoborockFinishReason as F
    if code not in F._value2member_map_:  # the library would log and guess
        return f"code {code}"
    name = re.sub(r"_\d+$", "", F(code).name).replace("clenaing", "cleaning")
    return name.replace("_", " ")


def history_lines(records: list[dict], show_all: bool = False) -> list[str]:
    """Readable history; skips dock-only events (no time, no area) unless show_all."""
    out = []
    for r in records:
        if not show_all and r["duration"] < 60 and r["area"] < 50_000:
            continue
        when = dt.datetime.fromtimestamp(r["begin"]).strftime("%Y-%m-%d %H:%M")
        out.append(f"{when}  {r['duration'] // 60:>3} min  {r['area'] / 1e6:5.1f} m²  "
                   f"{finish_text(r['finish_reason']):<20} id {r['begin']}")
    return out or ["no cleaning runs (dock-only events hidden; --all shows them)"]


PHOTO_BITS = 1 << 10 | 1 << 11  # camera_status: photo saving needs both


async def face(v: Vacuum, target: float) -> float:
    """Turn in place to a map heading, correcting from the map up to twice
    (long turns land ~10-20° off). Returns the final heading."""
    for _ in range(3):
        _, _, heading = await robot_pose()
        delta = turn_delta(heading, target)
        if abs(delta) < 8:
            return heading
        await rc_enter(v)
        await rc_turn(v, delta)
    return (await robot_pose())[2]


async def current_zones(v: Vacuum) -> list[list[int]]:
    """Existing no-go / no-mop zones and walls in save_map form."""
    md = await live_map()
    pts = lambda a: [a.x0, a.y0, a.x1, a.y1, a.x2, a.y2, a.x3, a.y3]
    return ([[0, *pts(a)] for a in md.no_go_areas or []]
            + [[2, *pts(a)] for a in md.no_mopping_areas or []]
            + [[1, w.x0, w.y0, w.x1, w.y1] for w in md.walls or []])


def print_status(s: dict) -> None:
    ci = s.get("cleaning_info") or {}
    lines = [
        f"state      {STATES.get(s['state'], s['state'])}"
        + (f" (room {ci['target_segment_id']})" if ci.get("target_segment_id", -1) > 0 else ""),
        f"battery    {s['battery']}%" + (" charging" if s.get("charge_status") == 1 else ""),
        f"error      {ERRORS.get(s['error_code'], s['error_code'])}"
        + (f"; DOCK: {dock_error(s['dock_error_status'])[0]}. {dock_error(s['dock_error_status'])[1]}"
           if s.get("dock_error_status") else ""),
        f"suction    {_name(SUCTION, s.get('fan_power'))}",
        f"water      {_name(WATER, s.get('water_box_mode'))}",
        f"route      {_name(ROUTE, s.get('mop_mode'))}",
        f"this run   {s.get('clean_time', 0) // 60} min, {s.get('clean_area', 0) / 1e6:.1f} m²",
        f"map        {'saved' if s.get('map_status', 0) >> 2 != 63 else 'none'}",
    ]
    dock = [n for n, on in (("emptying", s.get("dust_collection_status")),
                            ("drying", s.get("dry_status")),
                            ("washing", s.get("wash_phase"))) if on]
    if dock:
        lines.append(f"dock       {', '.join(dock)}")
    print("\n".join(lines))


def cron(time_: str, days: str) -> str:
    """('09:00', 'weekdays') -> '0 9 * * 1,2,3,4,5'. Raises ArgumentTypeError on bad input."""
    h, m = hhmm(time_)
    return f"{m} {h} * * {days_field(days)}"


def dow_names(field: str) -> str:
    """Cron day-of-week field -> 'daily' or 'mon,tue,…'. Handles ranges the app may write."""
    if field == "*":
        return "daily"
    days: list[int] = []
    for part in field.split(","):
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        days += [d % 7 for d in range(lo, hi + 1)]  # cron allows 7 = Sunday
    return ",".join(DAY_NAMES[d][:3] for d in sorted(set(days)))


# ---------------------------------------------------------------- CLI

def _checked(check):
    """argparse type that validates a string but keeps it as given."""
    def f(text: str) -> str:
        check(text)
        return text
    f.__name__ = check.__name__  # argparse names the type in its error
    return f


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vacuum.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="op", required=True)

    def add(name, **kw):
        return sub.add_parser(name, **kw)

    add("status").add_argument("--json", action="store_true")
    add("battery")
    def waitable(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("-w", "--wait", action="store_true",
                       help="follow until finished (default: until it has started)")
        g.add_argument("--no-wait", action="store_true",
                       help="return as soon as the robot answers 'ok'")
        return sp

    for n in ("pause", "resume", "stop", "dock", "spot", "empty"):
        waitable(add(n))
    add("find")
    add("where")
    c = waitable(add("clean")); c.add_argument("rooms", nargs="*"); c.add_argument("-r", "--repeat", type=repeat, default=1)
    z = waitable(add("zone")); [z.add_argument(k, type=int) for k in ("x0", "y0", "x1", "y1")]
    z.add_argument("-r", "--repeat", type=repeat, default=1)
    g = waitable(add("goto")); g.add_argument("x", type=int); g.add_argument("y", type=int)
    g.add_argument("--face", type=heading, help="then turn to this map heading (degrees)")
    dr = add("drive"); drs = dr.add_subparsers(dest="dop", required=True)
    for k in ("forward", "back"):
        p_ = drs.add_parser(k); p_.add_argument("metres", type=distance)
        p_.add_argument("--speed", type=speed, default=DRIVE_SPEED)
    for k in ("left", "right"):
        drs.add_parser(k).add_argument("degrees", type=angle)
    drs.add_parser("face").add_argument("heading", type=heading)
    drs.add_parser("end")
    m = add("mode")
    for k in ("--suction", "--water", "--route"):
        m.add_argument(k)
    add("volume").add_argument("level", nargs="?", type=int)
    d = add("dnd"); d.add_argument("args", nargs="*")
    w = waitable(add("wash")); w.add_argument("action", nargs="?", choices=["stop"])
    add("photos").add_argument("onoff", nargs="?", choices=["on", "off"])
    add("dry").add_argument("onoff", choices=["on", "off"])
    add("consumables").add_argument("--reset", choices=CONSUMABLES)

    r = add("rooms"); rs = r.add_subparsers(dest="rop")
    rn = rs.add_parser("name"); rn.add_argument("segment", type=int); rn.add_argument("name")
    ro = rs.add_parser("order"); ro.add_argument("segments", nargs="+", type=int)
    rm_ = rs.add_parser("mode"); rm_.add_argument("segment", type=int)
    for k in ("--suction", "--water", "--route"):
        rm_.add_argument(k)
    rm_.add_argument("-r", "--repeat", type=repeat, default=1)
    rg = rs.add_parser("merge"); rg.add_argument("a", type=int); rg.add_argument("b", type=int)
    rp = rs.add_parser("split"); rp.add_argument("segment", type=int)
    [rp.add_argument(k, type=int) for k in ("x1", "y1", "x2", "y2")]
    rs.add_parser("resegment")

    n = add("nogo"); ns = n.add_subparsers(dest="nop")
    ns.add_parser("list")
    na = ns.add_parser("add"); [na.add_argument(k, type=int) for k in ("x0", "y0", "x1", "y1")]
    na.add_argument("--no-mop", action="store_true")
    nw = ns.add_parser("wall"); [nw.add_argument(k, type=int) for k in ("x0", "y0", "x1", "y1")]
    ns.add_parser("clear")

    add("map").add_argument("out", nargs="?", default=str(HERE / "maps" / "current.png"))
    add("history").add_argument("--all", action="store_true", help="include dock-only events")
    rc = add("record"); rc.add_argument("id", type=int); rc.add_argument("out", nargs="?")
    ph = add("photo"); ph.add_argument("id"); ph.add_argument("out", nargs="?")

    s = add("schedule"); ss = s.add_subparsers(dest="sop")
    ss.add_parser("list")
    sa = ss.add_parser("add"); sa.add_argument("time", type=_checked(hhmm))
    sa.add_argument("--days", default="daily", type=_checked(days_field))
    for k in ("on", "off", "del"):
        ss.add_parser(k).add_argument("id")

    vo = add("voice"); vs = vo.add_subparsers(dest="vop")
    vi = vs.add_parser("install"); vi.add_argument("file"); vi.add_argument("--sid", type=int, default=3)

    cm = add("cmd"); cm.add_argument("method"); cm.add_argument("params", nargs="?")
    return p


async def run(a) -> None:
    op = a.op
    # cloud-only commands
    if op in ("map", "history", "record", "photo"):
        from vacuum_map import CloudVacuum, render
        async with CloudVacuum() as cv:
            if op == "map":
                out = pathlib.Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
                print(json.dumps(render(await cv.current_map(), out), indent=2))
            elif op == "record":
                out = pathlib.Path(a.out or HERE / "maps" / f"{a.id}.png")
                print(json.dumps(render(await cv.record_map(a.id), out), indent=2))
            elif op == "photo":
                out = pathlib.Path(a.out or HERE / "maps" / f"photo_{a.id}.jpg")
                out.write_bytes(await cv.photo(a.id)); print(f"saved {out}")
            else:
                print("\n".join(history_lines(await cv.records(), a.all)))
        return

    async with Vacuum() as v:
        if op == "status":
            s = await v.status()
            print(json.dumps(s, indent=2)) if a.json else print_status(s)
        elif op == "battery":
            print(f"{await v.battery()}%")
        elif op == "find":
            print(f"find: {ok(await v.cmd('find_me'))}")
        elif op in ("pause", "dock", "spot", "empty", "stop"):
            if op == "dock" and (await v.status()).get("state") == RC_STATE:
                await v.cmd("app_rc_end")  # leave manual drive before docking
            method = {"pause": "app_pause", "dock": "app_charge", "spot": "app_spot",
                      "empty": "app_start_collect_dust", "stop": "app_stop"}[op]
            print(f"{op}: {ok(await v.cmd(method))}")
            resend = (lambda: v.cmd(method)) if op in ("dock", "stop", "pause") else None
            await track(v, a, op, resend)
        elif op == "resume":
            s = await v.status()
            method = {3: "resume_segment_clean", 2: "resume_zoned_clean"}.get(s.get("in_cleaning"), "app_start")
            print(f"resume ({method}): {ok(await v.cmd(method))}")
            await track(v, a, op)
        elif op == "clean":
            if not a.rooms:
                print(f"clean: {ok(await v.cmd('app_start'))}")
            else:
                segs = await resolve_rooms(v, a.rooms)
                print(f"clean rooms {segs}: "
                      f"{ok(await v.cmd('app_segment_clean', [{'segments': segs, 'repeat': a.repeat}]))}")
            await track(v, a, op)
        elif op == "zone":
            print(f"zone: {ok(await v.cmd('app_zoned_clean', [[*box(a.x0, a.y0, a.x1, a.y1), a.repeat]]))}")
            await track(v, a, op)
        elif op == "goto":
            print(f"goto: {ok(await v.cmd('app_goto_target', [a.x, a.y]))}")
            if a.face is not None:  # must arrive before turning
                print(done_line(op, await follow(v, op)))
            else:
                await track(v, a, op)
            if a.face is not None:
                print(f"facing {await face(v, a.face):.0f}°")
        elif op == "where":
            await where_cmd(v)
        elif op == "photos":
            cur = (await v.cmd("get_camera_status"))[0]
            if a.onoff:
                new = cur | PHOTO_BITS if a.onoff == "on" else cur & ~PHOTO_BITS
                if new != cur:
                    await v.cmd("set_camera_status", [new])
                    cur = (await v.cmd("get_camera_status"))[0]
            on = cur & PHOTO_BITS == PHOTO_BITS
            print(f"obstacle photo saving {'on' if on else 'off'} (camera_status {cur})")
        elif op == "drive":
            await drive_cmd(v, a)
        elif op == "mode":
            p = mode_params(a)
            if p:
                cur = (await v.cmd("get_clean_motor_mode"))[0]
                new = {k: cur[k] for k in ("fan_power", "water_box_mode", "mop_mode")} | p
                await v.cmd("set_clean_motor_mode", [new])
            cur = (await v.cmd("get_clean_motor_mode"))[0]
            print(f"suction {_name(SUCTION, cur['fan_power'])}, water {_name(WATER, cur['water_box_mode'])}, "
                  f"route {_name(ROUTE, cur['mop_mode'])}")
        elif op == "volume":
            if a.level is not None:
                await v.cmd("change_sound_volume", [max(0, min(100, a.level))])
            print(f"volume {(await v.cmd('get_sound_volume'))[0]}")
        elif op == "dnd":
            if a.args == ["off"]:
                await v.cmd("close_dnd_timer")
            elif len(a.args) == 2:
                try:
                    (sh, sm), (eh, em) = (hhmm(t) for t in a.args)
                except argparse.ArgumentTypeError as e:
                    raise SystemExit(f"dnd: {e}") from None
                await v.cmd("set_dnd_timer", [sh, sm, eh, em])
            elif a.args:
                raise SystemExit("usage: dnd [off | HH:MM HH:MM]")
            d = (await v.cmd("get_dnd_timer"))[0]
            print(f"do-not-disturb {'on' if d['enabled'] else 'off'}: "
                  f"{d['start_hour']:02}:{d['start_minute']:02}-{d['end_hour']:02}:{d['end_minute']:02}")
        elif op == "wash":
            print(f"wash: {ok(await v.cmd('app_stop_wash' if a.action == 'stop' else 'app_start_wash'))}")
            await track(v, a, "wash stop" if a.action == "stop" else "wash")
        elif op == "dry":
            print(f"dry {a.onoff}: {ok(await v.cmd('app_set_dryer_status', {'status': int(a.onoff == 'on')}))}")
        elif op == "consumables":
            if a.reset:
                await v.cmd("reset_consumable", [a.reset])
            c = (await v.consumables())[0]
            life = {"main_brush_work_time": 300, "side_brush_work_time": 200,
                    "filter_work_time": 150, "sensor_dirty_time": 30}
            for k, hours in life.items():
                print(f"{k:24} {c.get(k, 0) / 3600:6.1f} h of {hours} h ({100 * c.get(k, 0) / 3600 / hours:.0f}%)")
            for k in ("strainer_work_times", "dust_collection_work_times"):
                print(f"{k:24} {c.get(k)}")
        elif op == "rooms":
            await rooms_cmd(v, a)
        elif op == "nogo":
            await nogo_cmd(v, a)
        elif op == "schedule":
            await schedule_cmd(v, a)
        elif op == "voice":
            if a.vop == "install":
                await install_voice(v, pathlib.Path(a.file), a.sid)
            cur = (await v.cmd("get_current_sound"))[0]
            langs = {1: "Chinese (prc)", 2: "Chinese (tw)", 3: "English"}
            print(f"voice pack {cur['sid_in_use']} ({langs.get(cur['sid_in_use'], 'custom')}), "
                  f"version {cur['sid_version']}")
        elif op == "cmd":
            try:
                params = json.loads(a.params) if a.params else None
            except json.JSONDecodeError as e:
                raise SystemExit(f"cmd: params must be JSON ({e})") from None
            print(json.dumps(await v.cmd(a.method, params), indent=2, default=str))


async def drive_cmd(v: Vacuum, a) -> None:
    """Short manual moves; the robot stays in remote-control mode until `drive end`."""
    if a.dop == "end":
        print(f"drive end: {ok(await v.cmd('app_rc_end'))}")
        return
    if a.dop == "face":
        print(f"facing {await face(v, a.heading):.0f}°")
        return
    await rc_enter(v)
    if a.dop in ("forward", "back"):
        sign = 1 if a.dop == "forward" else -1
        await rc_motion(v, sign * a.speed, 0.0, a.metres / a.speed)
        print(f"drove {a.dop} {a.metres:g} m")
    else:
        await rc_turn(v, a.degrees if a.dop == "left" else -a.degrees)
        print(f"turned {a.dop} {a.degrees:g}°")


async def where_cmd(v: Vacuum) -> None:
    md = await live_map()
    p, c = md.vacuum_position, md.charger
    seg = room_at(room_bounds(md), p.x, p.y)
    names = {r["segment"]: r["name"] for r in await room_table(v)} if seg else {}
    room = f"room {seg}" + (f" ({names[seg]})" if names.get(seg) else "") if seg else "no room"
    print(f"robot  ({p.x}, {p.y}) facing {p.a:.0f}°, in {room}")
    if c:
        print(f"dock   ({c.x}, {c.y}), {((p.x - c.x) ** 2 + (p.y - c.y) ** 2) ** 0.5 / 1000:.1f} m away")


async def name_room(v: Vacuum, cv, segment: int, name: str) -> None:
    """Name a room: find or create the server room, then link it to the segment."""
    rooms = await _server_rooms(cv)
    iot = next((str(r["id"]) for r in rooms if r["name"].lower() == name.lower()), None)
    iot = iot or await _create_server_room(cv, name)
    mapping = await v.cmd("get_room_mapping") or []
    table = [{"iotRoomId": iot if m[0] == segment else str(m[1]), "robotRoomId": m[0],
              "robotTagId": m[2] if len(m) > 2 else 0} for m in mapping]
    if segment not in [m[0] for m in mapping]:
        table.append({"iotRoomId": iot, "robotRoomId": segment, "robotTagId": 0})
    await v.edit("name_segment", sorted(table, key=lambda t: t["robotRoomId"]))


async def rooms_cmd(v: Vacuum, a) -> None:
    rop = a.rop
    if rop == "name":
        from vacuum_map import CloudVacuum
        async with CloudVacuum() as cv:
            await name_room(v, cv, a.segment, a.name)
    elif rop == "order":
        await v.cmd("set_clean_sequence", a.segments)
    elif rop == "mode":
        rows = {r["segment"]: r for r in (await v.cmd("get_customize_clean_mode") or [])}
        cur = (await v.cmd("get_clean_motor_mode"))[0]
        row = rows.get(a.segment) or {"segment": a.segment, "fan_power": cur["fan_power"],
                                      "water_box_mode": cur["water_box_mode"],
                                      "mop_mode": cur["mop_mode"], "repeat": 1, "seq_type": 0}
        row |= mode_params(a) | {"repeat": a.repeat}
        rows[a.segment] = row
        await v.edit("set_customize_clean_mode", list(rows.values()))
        print("per-room modes are used when the global mode is custom: "
              "`vacuum.py mode --suction custom --water custom --route custom`")
    elif rop == "merge":
        await v.edit("merge_segment", [a.a, a.b])
    elif rop == "split":
        await v.edit("split_segment", [a.segment, a.x1, a.y1, a.x2, a.y2])
    elif rop == "resegment":
        await v.cmd("manual_segment_map", [{"map_flag": 0}], timeout=30)
    if rop in ("merge", "split", "resegment"):
        await asyncio.sleep(3)
    table = await room_table(v)
    order = await v.cmd("get_clean_sequence") or []
    modes = {r["segment"]: r for r in (await v.cmd("get_customize_clean_mode") or [])}
    try:
        bounds = room_bounds(await live_map())
    except Exception as e:  # sizes are a nicety
        print(f"(room sizes unavailable: {e})", file=sys.stderr)
        bounds = {}
    if not table:
        print("no rooms (is a map saved?)")
    for r in table:
        mrow = modes.get(r["segment"])
        mtxt = (f"  mode {_name(SUCTION, mrow['fan_power'])}/{_name(WATER, mrow['water_box_mode'])}/"
                f"{_name(ROUTE, mrow['mop_mode'])} x{mrow.get('repeat', 1)}") if mrow else ""
        pos = f"  #{order.index(r['segment']) + 1} in order" if r["segment"] in order else ""
        b = bounds.get(r["segment"])
        size = (f"  {(b[2] - b[0]) / 1000:.1f}×{(b[3] - b[1]) / 1000:.1f} m"
                f" at ({(b[0] + b[2]) // 2}, {(b[1] + b[3]) // 2})") if b else ""
        print(f"room {r['segment']:>3}  {r['name'] or '(unnamed)'}{size}{pos}{mtxt}")
    for seg in sorted(set(bounds) - {r["segment"] for r in table}):
        print(f"room {seg:>3}  on the map but not in the robot's room list (can't be cleaned by number)")


async def nogo_cmd(v: Vacuum, a) -> None:
    zones = await current_zones(v)
    if a.nop == "add":
        zones.append([2 if a.no_mop else 0, *rect(a.x0, a.y0, a.x1, a.y1)])
    elif a.nop == "wall":
        if (a.x0, a.y0) == (a.x1, a.y1):
            raise SystemExit("wall needs two different end points")
        zones.append([1, a.x0, a.y0, a.x1, a.y1])
    elif a.nop == "clear":
        zones = []
    if a.nop in ("add", "wall", "clear"):
        await v.cmd("save_map", zones)
        zones = await current_zones(v)
    kinds = {0: "no-go", 1: "wall", 2: "no-mop"}
    for z in zones:
        print(f"{kinds[z[0]]:7} {z[1:]}")
    if not zones:
        print("no zones or walls")


async def schedule_cmd(v: Vacuum, a) -> None:
    sop = a.sop
    if sop == "add":
        tid = str(int(time.time() * 1000))
        cur = (await v.cmd("get_clean_motor_mode"))[0]
        action = {"fan_power": cur["fan_power"], "water_box_mode": cur["water_box_mode"],
                  "mop_mode": cur["mop_mode"], "repeat": 1, "segments": "0"}
        await v.cmd("set_timer", [[tid, [cron(a.time, a.days), ["start_clean", action]]]])
    elif sop in ("on", "off"):
        await v.cmd("upd_timer", [a.id, sop])
    elif sop == "del":
        await v.cmd("del_timer", [a.id])
    timers = await v.cmd("get_timer") or []
    for t in timers:
        tid, state, (cr, (_, action)) = t
        m, h, _, _, dow = cr.split()
        print(f"{tid}  {state:3}  {int(h):02}:{int(m):02}  {dow_names(dow)}  "
              f"{_name(SUCTION, action.get('fan_power'))}/{_name(WATER, action.get('water_box_mode'))}")
    if not timers:
        print("no schedules")


async def install_voice(v: Vacuum, path: pathlib.Path, sid: int) -> None:
    """Serve the pack on the AP address for the robot to fetch, then install it."""
    import functools, hashlib, http.server, threading
    if not path.is_file():
        raise SystemExit(f"no such file: {path}")
    md5 = hashlib.md5(path.read_bytes()).hexdigest()
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    handler = functools.partial(Quiet, directory=str(path.parent))
    srv = http.server.ThreadingHTTPServer((cfg.AP_IP, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://{cfg.AP_IP}:{srv.server_address[1]}/{path.name}"
    try:
        await v.cmd("dnld_install_sound", {"url": url, "md5": md5, "sid": sid})
        for _ in range(100):
            await asyncio.sleep(3)
            p = (await v.cmd("get_sound_progress"))[0]
            print(f"  installing: {p['progress']}%", end="\r", flush=True)
            if p.get("state") in (3, 4):
                break
        print()
        if p.get("state") != 3:
            raise SystemExit(f"install failed: {p} (error 2 = download failed)")
    finally:
        srv.shutdown()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    a = build_parser().parse_args()
    try:
        asyncio.run(run(a))
    except RoborockException as e:  # the robot's refusal is an answer, not a crash
        raise SystemExit(f"{a.op}: {type(e).__name__}: {e}") from None
    except (TimeoutError, asyncio.TimeoutError):
        raise SystemExit(f"{a.op}: no reply from the vacuum (timed out)") from None
    except KeyboardInterrupt:
        raise SystemExit("\ninterrupted: the robot stops within a second" if a.op == "drive"
                         else "\nstopped watching; the robot carries on") from None


if __name__ == "__main__":
    main()
