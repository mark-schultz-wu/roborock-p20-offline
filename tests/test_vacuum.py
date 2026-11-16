"""Unit tests for vacuum.py — no robot, no server.

A fake vacuum records every command the CLI would send, so each test checks
the exact method and params. That matters on this robot: junk params have
wedged it before (COMMANDS.md, "Don't probe with junk params"), so bad input
must stop before anything is sent.

    uv run --with pytest --with python-roborock pytest tests/
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import vacuum as V  # noqa: E402

RealVacuum = V.Vacuum

STATUS = {"state": 8, "battery": 100, "charge_status": 1, "error_code": 0,
          "fan_power": 102, "water_box_mode": 202, "mop_mode": 300,
          "clean_time": 600, "clean_area": 12_500_000, "map_status": 3, "in_cleaning": 0}
MOTOR = {"fan_power": 102, "water_box_mode": 202, "mop_mode": 300}
ROOMS = [{"segment": 1, "iot_id": "11", "tag": 14, "name": "Kitchen"},
         {"segment": 2, "iot_id": "12", "tag": 2, "name": "Living Room"},
         {"segment": 3, "iot_id": "13", "tag": 0, "name": None}]


def _room(n, x0, y0, x1, y1):
    return types.SimpleNamespace(number=n, x0=x0, y0=y0, x1=x1, y1=y1)


MAP = types.SimpleNamespace(
    vacuum_position=types.SimpleNamespace(x=23000, y=24000, a=-90.0),
    charger=types.SimpleNamespace(x=26000, y=24000, a=180.0),
    rooms={1: _room(1, 20000, 20000, 22000, 23000), 2: _room(2, 22000, 22000, 26000, 26000),
           3: _room(3, 20000, 20000, 30000, 30000), 4: _room(4, 19000, 26000, 20500, 28000)},
)


class FakeVacuum(RealVacuum):
    """The real Vacuum with the network swapped for a recorder."""

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, object]] = []
        self.zones: list[list[int]] = []
        self.replies = {
            "get_status": [STATUS],
            "get_clean_motor_mode": [MOTOR],
            "get_sound_volume": [90],
            "get_dnd_timer": [{"enabled": 1, "start_hour": 22, "start_minute": 0,
                               "end_hour": 8, "end_minute": 0}],
            "get_consumable": [{"main_brush_work_time": 3600, "sensor_dirty_time": 0}],
            "get_timer": [],
            "get_room_mapping": [[1, "11", 14], [2, "12", 2], [3, "13", 0]],
            "get_clean_sequence": [1, 2],
            "get_customize_clean_mode": [],
            "get_current_sound": [{"sid_in_use": 3, "sid_version": 1}],
        }

    async def connect(self):
        pass

    def close(self):
        pass

    async def cmd(self, method, params=None, timeout=10.0):
        self.sent.append((method, params))
        if method == "save_map":
            self.zones = copy.deepcopy(params)
        if method == "app_rc_start":
            self.replies["get_status"] = [STATUS | {"state": V.RC_STATE}]
        reply = self.replies.get(method, ["ok"])
        if callable(reply):
            reply = reply(params)
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)

    def moves(self) -> list[dict]:
        return [p[0] for m, p in self.sent if m == "app_rc_move"]

    def methods(self) -> list[str]:
        return [m for m, _ in self.sent]


class FakeCloud:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


@pytest.fixture
def robot(monkeypatch):
    fake = FakeVacuum()
    monkeypatch.setattr(V, "Vacuum", lambda *a, **k: fake)

    async def room_table(v):
        return copy.deepcopy(ROOMS)

    async def current_zones(v):
        return copy.deepcopy(fake.zones)

    async def server_rooms(cv):
        return [{"id": 11, "name": "Kitchen"}, {"id": 12, "name": "Living Room"}]

    async def create_server_room(cv, name):
        fake.sent.append(("server: create room", name))
        return "99"

    clock = [0.0]  # fake time: only sleeping advances it, so waits finish instantly

    async def no_sleep(s=0, *a):
        clock[0] += s

    monkeypatch.setattr(V, "_now", lambda: clock[0])

    async def live_map():
        return MAP

    monkeypatch.setattr(V, "live_map", live_map)
    monkeypatch.setattr(V, "room_table", room_table)
    monkeypatch.setattr(V, "current_zones", current_zones)
    monkeypatch.setattr(V, "_server_rooms", server_rooms)
    monkeypatch.setattr(V, "_create_server_room", create_server_room)
    monkeypatch.setattr(V.asyncio, "sleep", no_sleep)
    monkeypatch.setitem(sys.modules, "vacuum_map",
                        types.SimpleNamespace(CloudVacuum=FakeCloud))
    return fake


def cli(*argv: str) -> None:
    asyncio.run(V.run(V.build_parser().parse_args(list(argv))))


def refused(robot: FakeVacuum, *argv: str) -> None:
    """The CLI must exit with an error and send nothing to the robot."""
    with pytest.raises(SystemExit):
        cli(*argv)
    assert robot.sent == []


# ---------------------------------------------------------------- input checks

@pytest.mark.parametrize("text,want", [("09:00", (9, 0)), ("9:05", (9, 5)),
                                       ("00:00", (0, 0)), ("23:59", (23, 59))])
def test_hhmm_ok(text, want):
    assert V.hhmm(text) == want


@pytest.mark.parametrize("text", ["24:00", "25:00", "12:60", "12", "12:5", "ab:cd", "-1:00", ""])
def test_hhmm_bad(text):
    with pytest.raises(argparse.ArgumentTypeError):
        V.hhmm(text)


@pytest.mark.parametrize("text,want", [
    ("daily", "*"), ("weekdays", "1,2,3,4,5"), ("weekends", "0,6"),
    ("mon,wed", "1,3"), ("wed,mon", "1,3"), ("mon-fri", "1,2,3,4,5"),
    ("sat-sun", "0,6"), ("fri-mon", "0,1,5,6"), ("Tuesday", "2"), ("thur", "4"),
    ("mon-wed,sat", "1,2,3,6"), ("mon,mon", "1"),
])
def test_days_field_ok(text, want):
    assert V.days_field(text) == want


@pytest.mark.parametrize("text", ["mon-fry", "xyz", "m", "mo", "", "mon,", "monday-"])
def test_days_field_bad(text):
    with pytest.raises(argparse.ArgumentTypeError):
        V.days_field(text)


def test_cron():
    assert V.cron("09:00", "weekdays") == "0 9 * * 1,2,3,4,5"
    assert V.cron("7:05", "mon-fri") == "5 7 * * 1,2,3,4,5"  # was Monday only
    with pytest.raises(argparse.ArgumentTypeError):
        V.cron("25:00", "daily")  # was "0 25 * * *"


@pytest.mark.parametrize("field,want", [("*", "daily"), ("1-5", "mon,tue,wed,thu,fri"),
                                        ("0,6", "sun,sat"), ("7", "sun"), ("1,3", "mon,wed")])
def test_dow_names(field, want):
    assert V.dow_names(field) == want


@pytest.mark.parametrize("days", ["weekdays", "weekends", "mon,wed", "fri-mon"])
def test_days_round_trip(days):
    """What we write, we read back as the same days."""
    shown = V.dow_names(V.days_field(days))
    assert V.days_field(shown if shown != "daily" else "daily") == V.days_field(days)


@pytest.mark.parametrize("text,ok", [("1", True), ("3", True), ("0", False), ("4", False),
                                     ("x", False), ("-1", False)])
def test_repeat(text, ok):
    if ok:
        assert V.repeat(text) == int(text)
    else:
        with pytest.raises(argparse.ArgumentTypeError):
            V.repeat(text)


def test_box_and_rect():
    assert V.box(30, 40, 10, 20) == (10, 20, 30, 40)
    assert V.rect(30, 40, 10, 20) == [10, 20, 30, 20, 30, 40, 10, 40]
    with pytest.raises(SystemExit):
        V.box(10, 20, 10, 40)


def test_mode_tables():
    assert V._code(V.SUCTION, "max+") == 108
    assert V._code(V.SUCTION, None) is None
    assert V._name(V.WATER, 209) == "smart"
    assert V._name(V.WATER, 999) == "999"
    with pytest.raises(SystemExit):
        V._code(V.ROUTE, "turbo")


def test_mode_params():
    a = argparse.Namespace(suction="turbo", water=None, route="deep")
    assert V.mode_params(a) == {"fan_power": 103, "mop_mode": 301}


def test_print_status(capsys):
    V.print_status(STATUS | {"dry_status": 1, "cleaning_info": {"target_segment_id": 2}})
    out = capsys.readouterr().out
    assert "state      charging (room 2)" in out
    assert "battery    100% charging" in out
    assert "suction    balanced" in out and "water      medium" in out
    assert "this run   10 min, 12.5 m²" in out
    assert "map        saved" in out
    assert "dock       drying" in out


# ---------------------------------------------------------------- simple commands

@pytest.mark.parametrize("argv,method,params", [
    (["pause"], "app_pause", None), (["find"], "find_me", None),
    (["spot"], "app_spot", None),
    (["empty"], "app_start_collect_dust", None), (["stop"], "app_stop", None),
    (["wash"], "app_start_wash", None), (["wash", "stop"], "app_stop_wash", None),
    (["dry", "on"], "app_set_dryer_status", {"status": 1}),
    (["dry", "off"], "app_set_dryer_status", {"status": 0}),
    (["goto", "25000", "26000"], "app_goto_target", [25000, 26000]),
    (["clean"], "app_start", None),
])
def test_simple(robot, argv, method, params):
    cli(*argv, *([] if argv[0] in ("find", "dry") else ["--no-wait"]))
    assert robot.sent == [(method, params)]


def test_status(robot, capsys):
    cli("status")
    assert robot.sent == [("get_status", None)]
    assert "battery    100%" in capsys.readouterr().out


@pytest.mark.parametrize("in_cleaning,method", [(3, "resume_segment_clean"),
                                                (2, "resume_zoned_clean"),
                                                (0, "app_start")])
def test_resume(robot, in_cleaning, method):
    robot.replies["get_status"] = [STATUS | {"in_cleaning": in_cleaning}]
    cli("resume", "--no-wait")
    assert robot.methods() == ["get_status", method]


# ---------------------------------------------------------------- cleaning

def test_clean_segments(robot):
    cli("clean", "2", "3", "-r", "2", "--no-wait")
    assert robot.sent == [("app_segment_clean", [{"segments": [2, 3], "repeat": 2}])]


def test_clean_by_name(robot):
    cli("clean", "living room", "1", "--no-wait")
    assert robot.sent == [("app_segment_clean", [{"segments": [2, 1], "repeat": 1}])]


def test_clean_unknown_room(robot):
    refused(robot, "clean", "Garage")


@pytest.mark.parametrize("r", ["0", "4"])
def test_clean_bad_repeat(robot, r):
    refused(robot, "clean", "2", "-r", r)


def test_zone_normalizes_corners(robot):
    cli("zone", "25300", "26600", "24300", "25600", "-r", "2", "--no-wait")
    assert robot.sent == [("app_zoned_clean", [[24300, 25600, 25300, 26600, 2]])]


def test_zone_refuses_no_area(robot):
    refused(robot, "zone", "100", "200", "100", "300")


# ---------------------------------------------------------------- settings

def test_mode_set_keeps_unchanged_fields(robot):
    cli("mode", "--suction", "max")
    assert robot.sent[1] == ("set_clean_motor_mode",
                             [{"fan_power": 104, "water_box_mode": 202, "mop_mode": 300}])


def test_mode_show_only_reads(robot):
    cli("mode")
    assert robot.methods() == ["get_clean_motor_mode"]


def test_mode_bad_value(robot):
    refused(robot, "mode", "--suction", "loud")


@pytest.mark.parametrize("level,sent", [("60", 60), ("150", 100)])
def test_volume(robot, level, sent):
    cli("volume", level)
    assert robot.sent[0] == ("change_sound_volume", [sent])


def test_dnd_set(robot):
    cli("dnd", "22:30", "7:00")
    assert robot.sent[0] == ("set_dnd_timer", [22, 30, 7, 0])


def test_dnd_off(robot):
    cli("dnd", "off")
    assert robot.methods() == ["close_dnd_timer", "get_dnd_timer"]


@pytest.mark.parametrize("args", [["25:00", "08:00"], ["22:00", "8"], ["22:00"]])
def test_dnd_bad(robot, args):
    refused(robot, "dnd", *args)


def test_consumables_reset(robot, capsys):
    cli("consumables", "--reset", "sensor_dirty_time")
    assert robot.sent[0] == ("reset_consumable", ["sensor_dirty_time"])
    assert "main_brush_work_time" in capsys.readouterr().out


def test_consumables_bad_name(robot):
    refused(robot, "consumables", "--reset", "brush")


# ---------------------------------------------------------------- schedules

def test_schedule_add(robot):
    cli("schedule", "add", "09:00", "--days", "mon-fri")
    method, params = robot.sent[1]
    assert method == "set_timer"
    [[tid, [cron, [verb, action]]]] = params
    assert tid.isdigit() and cron == "0 9 * * 1,2,3,4,5" and verb == "start_clean"
    assert action == MOTOR | {"repeat": 1, "segments": "0"}


@pytest.mark.parametrize("args", [["25:00"], ["09:00", "--days", "mon-fry"], ["9"]])
def test_schedule_add_bad(robot, args):
    refused(robot, "schedule", "add", *args)


@pytest.mark.parametrize("op,method,params", [("on", "upd_timer", ["123", "on"]),
                                              ("off", "upd_timer", ["123", "off"]),
                                              ("del", "del_timer", ["123"])])
def test_schedule_toggle(robot, op, method, params):
    cli("schedule", op, "123")
    assert robot.sent[0] == (method, params)


def test_schedule_list_shows_ranges(robot, capsys):
    robot.replies["get_timer"] = [["123", "on", ["0 9 * * 1-5", ["start_clean", MOTOR]]]]
    cli("schedule")
    assert "123  on   09:00  mon,tue,wed,thu,fri  balanced/medium" in capsys.readouterr().out


# ---------------------------------------------------------------- rooms

def test_rooms_list(robot, capsys):
    robot.replies["get_customize_clean_mode"] = [
        {"segment": 1, "fan_power": 104, "water_box_mode": 200, "mop_mode": 301, "repeat": 2}]
    cli("rooms")
    out = capsys.readouterr().out
    assert "room   1  Kitchen  2.0×3.0 m at (21000, 21500)  #1 in order  mode max/off/deep x2" in out
    assert "room   3  (unnamed)  10.0×10.0 m" in out
    assert "room   4  on the map but not in the robot's room list" in out


@pytest.mark.parametrize("argv,method,data", [
    (["merge", "1", "2"], "merge_segment", [1, 2]),
    (["split", "1", "10", "20", "30", "40"], "split_segment", [1, 10, 20, 30, 40]),
])
def test_rooms_edits_bracketed(robot, argv, method, data):
    cli("rooms", *argv)
    assert robot.sent[:3] == [("start_edit_map", []),
                              (method, {"data": data, "need_retry": 1}),
                              ("end_edit_map", None)]


def test_edit_closes_on_failure(robot):
    robot.replies["merge_segment"] = RuntimeError("refused")
    with pytest.raises(RuntimeError):
        cli("rooms", "merge", "1", "2")
    assert robot.methods() == ["start_edit_map", "merge_segment", "end_edit_map"]


def test_rooms_order(robot):
    cli("rooms", "order", "2", "1")
    assert robot.sent[0] == ("set_clean_sequence", [2, 1])


def test_rooms_mode_new_row(robot):
    cli("rooms", "mode", "2", "--suction", "max", "-r", "2")
    assert robot.methods()[:5] == ["get_customize_clean_mode", "get_clean_motor_mode",
                                   "start_edit_map", "set_customize_clean_mode", "end_edit_map"]
    assert robot.sent[3][1]["data"] == [{"segment": 2, "fan_power": 104, "water_box_mode": 202,
                               "mop_mode": 300, "repeat": 2, "seq_type": 0}]


def test_rooms_name_existing_server_room(robot):
    cli("rooms", "name", "3", "kitchen")
    method, params = robot.sent[2]
    assert method == "name_segment"
    assert params["data"] == [
        {"iotRoomId": "11", "robotRoomId": 1, "robotTagId": 14},
        {"iotRoomId": "12", "robotRoomId": 2, "robotTagId": 2},
        {"iotRoomId": "11", "robotRoomId": 3, "robotTagId": 0},
    ]


def test_rooms_name_creates_server_room(robot):
    cli("rooms", "name", "3", "Office")
    assert robot.sent[0] == ("server: create room", "Office")
    assert robot.sent[3][1]["data"][2] == {"iotRoomId": "99", "robotRoomId": 3, "robotTagId": 0}


# ---------------------------------------------------------------- no-go zones

def test_nogo_add_normalizes(robot):
    cli("nogo", "add", "300", "400", "100", "200", "--no-mop")
    assert robot.sent == [("save_map", [[2, 100, 200, 300, 200, 300, 400, 100, 400]])]


def test_nogo_add_keeps_existing(robot):
    robot.zones = [[1, 0, 0, 10, 10]]
    cli("nogo", "add", "100", "200", "300", "400")
    assert robot.sent == [("save_map", [[1, 0, 0, 10, 10],
                                        [0, 100, 200, 300, 200, 300, 400, 100, 400]])]


def test_nogo_wall(robot):
    cli("nogo", "wall", "1", "2", "3", "4")
    assert robot.sent == [("save_map", [[1, 1, 2, 3, 4]])]


def test_nogo_bad_shapes(robot):
    refused(robot, "nogo", "add", "100", "200", "100", "400")
    refused(robot, "nogo", "wall", "1", "2", "1", "2")


def test_nogo_clear(robot):
    robot.zones = [[1, 0, 0, 10, 10]]
    cli("nogo", "clear")
    assert robot.sent == [("save_map", [])]


def test_nogo_list_sends_nothing(robot, capsys):
    cli("nogo")
    assert robot.sent == []
    assert "no zones or walls" in capsys.readouterr().out


# ---------------------------------------------------------------- raw commands

def test_cmd_raw(robot):
    cli("cmd", "get_sound_volume")
    cli("cmd", "app_segment_clean", '[{"segments": [2], "repeat": 1}]')
    assert robot.sent == [("get_sound_volume", None),
                          ("app_segment_clean", [{"segments": [2], "repeat": 1}])]


def test_cmd_bad_json(robot):
    refused(robot, "cmd", "app_goto_target", "[1, 2")


# ---------------------------------------------------------------- TLS trust

def test_server_trust_set_before_aiohttp():
    """aiohttp freezes its SSL context on import; vacuum.py must set the
    stack's trust anchor first or every server-backed command fails."""
    import subprocess
    code = ("import vacuum, aiohttp.connector as c; "
            "print(any('AAA Certificate Services' in str(x) "
            "for x in c._SSL_CONTEXT_VERIFIED.get_ca_certs()))")
    env = {k: v for k, v in __import__("os").environ.items() if k != "SSL_CERT_FILE"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=pathlib.Path(V.__file__).parent, env=env, timeout=60)
    assert out.stdout.strip() == "True", out.stderr[-500:]


def test_robot_refusal_is_a_clean_error(robot, monkeypatch):
    from roborock.exceptions import RoborockUnsupportedFeature
    robot.replies["get_lab_status"] = RoborockUnsupportedFeature("not recognized")
    monkeypatch.setattr(sys, "argv", ["vacuum.py", "cmd", "get_lab_status"])
    with pytest.raises(SystemExit) as e:
        V.main()
    assert str(e.value) == "cmd: RoborockUnsupportedFeature: not recognized"


def test_timeout_is_a_clean_error(robot, monkeypatch):
    robot.replies["get_status"] = TimeoutError()
    monkeypatch.setattr(sys, "argv", ["vacuum.py", "status"])
    with pytest.raises(SystemExit) as e:
        V.main()
    assert "no reply from the vacuum" in str(e.value)


# ---------------------------------------------------------------- manual drive

@pytest.fixture
def clock(monkeypatch):
    """A fake clock that only advances when the code sleeps."""
    t = [0.0]

    async def sleep(s):
        t[0] += s

    monkeypatch.setattr(V, "_now", lambda: t[0])
    monkeypatch.setattr(V.asyncio, "sleep", sleep)
    return t


@pytest.fixture
def poses(monkeypatch):
    """Headings robot_pose() reports, one per call."""
    seq: list[float] = []

    async def robot_pose():
        return 25000, 25000, seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(V, "robot_pose", robot_pose)
    return seq


def test_drive_forward(robot, clock):
    cli("drive", "forward", "0.5")  # 2.5 s at 0.2 m/s
    assert robot.methods()[:3] == ["get_status", "app_rc_start", "get_status"]
    moves = robot.moves()
    assert all(m["velocity"] == 0.2 and m["omega"] == 0.0 for m in moves)
    # overlapping 1 s chunks re-sent every 0.8 s, last one trimmed to the end
    assert [m["duration"] for m in moves] == [1000, 1000, 900, 100]
    seqs = [m["seqnum"] for m in moves]
    assert seqs == sorted(set(seqs))  # strictly increasing: the robot drops repeats
    assert clock[0] >= 2.5


def test_drive_back_already_in_rc_mode(robot, clock):
    robot.replies["get_status"] = [STATUS | {"state": V.RC_STATE}]
    cli("drive", "back", "0.2", "--speed", "0.1")
    assert "app_rc_start" not in robot.methods()
    assert {m["velocity"] for m in robot.moves()} == {-0.1}


@pytest.mark.parametrize("way,omega", [("left", 1.0), ("right", -1.0)])
def test_drive_turn(robot, clock, way, omega):
    cli("drive", way, "90")
    moves = robot.moves()
    assert {m["omega"] for m in moves} == {omega} and {m["velocity"] for m in moves} == {0.0}
    assert sum(m["duration"] for m in moves) >= 1500  # ~1.57 s at 1 rad/s


def test_drive_face_with_correction(robot, clock, poses):
    poses[:] = [-178, -100, -93]  # first turn falls short, one correction, then close enough
    cli("drive", "face", "-90")
    omegas = [m["omega"] for m in robot.moves()]
    assert omegas and set(omegas) == {1.0}  # both turns to the left


def test_drive_face_already_there(robot, clock, poses):
    poses[:] = [-92]
    cli("drive", "face", "-90")
    assert robot.moves() == [] and "app_rc_start" not in robot.methods()


def test_drive_end(robot):
    cli("drive", "end")
    assert robot.sent == [("app_rc_end", None)]


@pytest.mark.parametrize("current,target,delta", [(170, -170, 20), (-170, 170, -20),
                                                  (-178, -90, 88), (0, 90, 90), (90, -90, -180)])
def test_turn_delta(current, target, delta):
    assert V.turn_delta(current, target) == delta


def test_rc_seq_strictly_increases():
    a, b = V.rc_seq(), V.rc_seq()
    assert b > a and a < 2**31


@pytest.mark.parametrize("argv", [
    ["drive", "forward", "0"], ["drive", "forward", "3"], ["drive", "forward", "-1"],
    ["drive", "forward", "x"], ["drive", "back", "1", "--speed", "0.5"],
    ["drive", "left", "0"], ["drive", "right", "400"], ["drive", "face", "200"],
    ["goto", "1", "2", "--face", "181"],
])
def test_drive_bad_input(robot, argv):
    refused(robot, *argv)


def test_goto_then_face(robot, clock, poses):
    states = iter([8, 16, 16])
    robot.replies["get_status"] = lambda p: [STATUS | {"state": next(states, 3)}]
    poses[:] = [0, 88]
    cli("goto", "25000", "26000", "--face", "90")
    assert robot.sent[0] == ("app_goto_target", [25000, 26000])
    assert robot.methods().count("app_rc_move") > 0
    assert robot.methods().index("app_rc_start") > 3  # only after arrival


def test_dock_leaves_rc_mode_first(robot):
    robot.replies["get_status"] = [STATUS | {"state": V.RC_STATE}]
    cli("dock", "--no-wait")
    assert robot.methods() == ["get_status", "app_rc_end", "app_charge"]


def test_dock_from_idle_just_docks(robot):
    cli("dock", "--no-wait")
    assert robot.methods() == ["get_status", "app_charge"]


# ---------------------------------------------------------------- --wait

def states(robot, *seq, then=None):
    """Make get_status walk through `seq`, then stay on `then` (or the last)."""
    it = iter(seq)
    last = [seq[-1] if then is None else then]

    def reply(params):
        try:
            last[0] = next(it)
        except StopIteration:
            last[0] = seq[-1] if then is None else then
        return [STATUS | {"state": last[0], "clean_time": 1500, "clean_area": 13_500_000}]
    robot.replies["get_status"] = reply


def test_clean_wait_follows_to_the_dock(robot, clock, capsys):
    states(robot, 8, 23, 23, 18, 18, 26, 23, 8)
    cli("clean", "3", "--wait")
    out = capsys.readouterr().out
    for name in ("washing mop", "room cleaning", "going to wash mop", "charging"):
        assert name in out
    assert out.strip().endswith("done: charging, 25 min, 13.5 m²")


def test_clean_wait_reports_ignored(robot, clock):
    states(robot, 8)  # never leaves the dock
    with pytest.raises(SystemExit, match="ignored"):
        cli("clean", "--wait")
    assert robot.methods().count("app_start") == 1  # not resent: not safe to repeat


def test_dock_wait_resends_once_when_ignored(robot, clock, capsys):
    # idle for 30 s (the dock-after-stop drop), then the resend works
    seq = [3] * 13 + [6, 6, 8]
    states(robot, *seq)
    cli("dock", "--wait")
    assert robot.methods().count("app_charge") == 2
    assert "sending it again" in capsys.readouterr().out


def test_dock_wait_gives_up_after_one_resend(robot, clock):
    states(robot, 3)
    with pytest.raises(SystemExit, match="ignored"):
        cli("dock", "--wait")
    assert robot.methods().count("app_charge") == 2


def test_wait_reports_robot_error(robot, clock):
    robot.replies["get_status"] = [STATUS | {"state": 12, "error_code": 8}]
    with pytest.raises(SystemExit, match="trapped"):
        cli("zone", "1", "2", "3", "4", "--wait")


def test_stop_wait_when_already_idle(robot, clock, capsys):
    states(robot, 3)
    cli("stop", "-w")
    assert "done: idle" in capsys.readouterr().out


@pytest.mark.parametrize("argv,key", [(["empty", "-w"], "emptying bin"),
                                      (["wash", "-w"], "washing mop")])
def test_dock_actions_wait(robot, clock, capsys, argv, key):
    states(robot, 8, 22 if argv[0] == "empty" else 23, 8)
    cli(*argv)
    out = capsys.readouterr().out
    assert key in out and "done: charging" in out


def test_no_wait_does_not_poll(robot):
    cli("dock", "--no-wait")
    assert robot.methods().count("get_status") == 1  # only the remote-control check


# ---------------------------------------------------------------- where, photos, history

def test_where(robot, capsys):
    cli("where")
    out = capsys.readouterr().out
    assert "robot  (23000, 24000) facing -90°, in room 2 (Living Room)" in out
    assert "dock   (26000, 24000), 3.0 m away" in out
    assert not robot.moves()


def test_room_at_prefers_smallest():
    b = V.room_bounds(MAP)
    assert V.room_at(b, 21000, 21000) == 1   # inside 1 and 3; 1 is smaller
    assert V.room_at(b, 29000, 29000) == 3
    assert V.room_at(b, 0, 0) is None


@pytest.mark.parametrize("argv,start,want", [
    (["photos"], 8869, None),
    (["photos", "on"], 8869, 11941),
    (["photos", "off"], 11941, 8869),
    (["photos", "on"], 11941, None),  # already on: nothing written
])
def test_photos(robot, capsys, argv, start, want):
    robot.replies["get_camera_status"] = lambda p: [robot.camera]
    robot.camera = start

    def set_status(params):
        robot.camera = params[0]
        return ["ok"]
    robot.replies["set_camera_status"] = set_status
    cli(*argv)
    writes = [p for m, p in robot.sent if m == "set_camera_status"]
    assert writes == ([[want]] if want else [])
    out = capsys.readouterr().out
    assert ("on" if robot.camera & V.PHOTO_BITS == V.PHOTO_BITS else "off") in out


@pytest.mark.parametrize("code,text", [(52, "finished cleaning"), (43, "manual interrupt"),
                                       (21, "manual interrupt"), (57, "finished cleaning"),
                                       (61, "area unreachable"), (999, "code 999")])
def test_finish_text(code, text):
    assert V.finish_text(code) == text


def test_history_hides_dock_events():
    recs = [{"begin": 1793388815, "duration": 1500, "area": 13_500_000, "finish_reason": 52},
            {"begin": 1793387817, "duration": 0, "area": 0, "finish_reason": 43}]
    lines = V.history_lines(recs)
    assert len(lines) == 1 and "25 min   13.5 m²  finished cleaning" in lines[0]
    assert lines[0].endswith("id 1793388815")
    assert len(V.history_lines(recs, show_all=True)) == 2
    assert "no cleaning runs" in V.history_lines(recs[1:])[0]


# ---------------------------------------------------------------- default: until started

def test_clean_default_returns_once_started(robot, capsys):
    states(robot, 8, 8, 23, 23, 18)
    cli("clean", "3")
    out = capsys.readouterr().out
    assert "started: washing mop (--wait follows it to the end)" in out
    assert "room cleaning" not in out  # stopped watching at the first sign of life


def test_clean_default_still_reports_ignored(robot):
    states(robot, 8)
    with pytest.raises(SystemExit, match="ignored"):
        cli("clean", "3")


def test_dock_default_resends_and_confirms(robot, capsys):
    states(robot, *([3] * 13 + [6]))
    cli("dock")
    out = capsys.readouterr().out
    assert robot.methods().count("app_charge") == 2
    assert "started: returning" in out


def test_stop_default_prints_done(robot, capsys):
    states(robot, 3)
    cli("stop")
    assert "done: idle" in capsys.readouterr().out


def test_wait_and_no_wait_conflict(robot):
    refused(robot, "dock", "--wait", "--no-wait")


@pytest.mark.parametrize("op,msg", [("clean", "stopped watching; the robot carries on"),
                                    ("drive", "the robot stops within a second")])
def test_ctrl_c_message(monkeypatch, op, msg):
    async def interrupted(a):
        raise KeyboardInterrupt
    monkeypatch.setattr(V, "run", interrupted)
    argv = {"clean": ["clean"], "drive": ["drive", "forward", "1"]}[op]
    monkeypatch.setattr(sys, "argv", ["vacuum.py", *argv])
    with pytest.raises(SystemExit, match=msg):
        V.main()


def test_dock_default_when_already_docked(robot, capsys):
    cli("dock")  # fake robot is charging
    assert "done: charging" in capsys.readouterr().out


def test_empty_ignored_explains_why(robot):
    states(robot, 8)
    with pytest.raises(SystemExit, match="soon after the last emptying"):
        cli("empty")
