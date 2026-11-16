# /// script
# requires-python = ">=3.11"
# dependencies = ["python-roborock"]
# ///
"""Map downloads from the vacuum, through the local cloud stack.

Map data does not come back over the direct TCP link that vacuum.py uses.
The vacuum sends it as an encrypted MQTT push (protocol 301), so this tool
logs in to local_roborock_server like a phone app would, then asks for maps
over MQTT. Nothing leaves this machine.

CLI use:
    uv run vacuum_map.py current map.png          # live map (needs a saved map)
    uv run vacuum_map.py records                  # list cleaning records
    uv run vacuum_map.py record 1778297171 r.png  # map of one past cleaning
    uv run vacuum_map.py record-all maps/         # every record that has a map
    uv run vacuum_map.py photo <img_id> out.jpg   # obstacle photo (photo saving must be on)

Each PNG also gets a .bin file next to it with the raw (decrypted,
decompressed) map bytes, for re-parsing later.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys

import lab_tls  # noqa: F401  (must precede roborock imports)
import labconfig as cfg
from roborock.data import UserData
from roborock.devices.device_manager import UserParams, create_device_manager
from roborock.map.map_parser import MapParser, MapParserConfig
from roborock.web_api import RoborockApiClient

BASE_URL = f"https://{cfg.STACK_HOST}:{cfg.STACK_PORT}"
EMAIL = cfg.PROTOCOL_EMAIL
PIN = cfg.PROTOCOL_PIN  # [admin] protocol_login_pin in local_roborock_server/config.toml; both from lab.env
SESSION_FILE = pathlib.Path(__file__).with_name(".cloud_session.json")


async def login() -> UserData:
    """Log in to the local stack once and cache the session."""
    if SESSION_FILE.exists():
        return UserData.from_dict(json.loads(SESSION_FILE.read_text()))
    api = RoborockApiClient(EMAIL, base_url=BASE_URL)
    user_data = await api.code_login_v4(PIN, country="CN", country_code=86)
    SESSION_FILE.write_text(json.dumps(user_data.as_dict()))
    SESSION_FILE.chmod(0o600)
    return user_data


class CloudVacuum:
    """The vacuum, reached through the local stack (MQTT + local TCP)."""

    async def __aenter__(self) -> "CloudVacuum":
        user_data = self.user_data = await login()
        self._dm = await create_device_manager(UserParams(EMAIL, user_data, BASE_URL))
        self.device = (await self._dm.get_devices())[0]
        self.v1 = self.device.v1_properties
        await asyncio.sleep(2)  # let the MQTT subscription settle
        return self

    async def __aexit__(self, *exc) -> None:
        await self._dm.close()

    async def cmd(self, method: str, params=None):
        """Normal RPC (local TCP first, MQTT fallback)."""
        return await self.v1._rpc_channel.send_command(method, params=params)

    async def map_bytes(self, method: str, params) -> bytes:
        """RPC whose answer is a map push. Returns raw map bytes."""
        return await self.v1._map_rpc_channel.send_command(method, params=params)

    async def current_map(self) -> bytes:
        # Same params the official app sends.
        return await self.map_bytes("get_map_v1", {"nonce": -1})

    async def record_map(self, record_id: int) -> bytes:
        return await self.map_bytes("get_clean_record_map", {"start_time": record_id})

    async def photo(self, img_id: str) -> bytes:
        """Download an obstacle photo (JPEG bytes).

        Photo IDs come from obstacles in a saved map. Photo saving
        (camera_status bits 10+11) must be on when you ask. Errors the vacuum
        sends back on the map channel are dropped, so a missing photo or
        photo saving being off shows up as a timeout.
        """
        return (await self.v1.obstacle_photos.get_photo(img_id)).image_content

    async def records(self) -> list[dict]:
        summary = await self.cmd("get_clean_summary")
        out = []
        for rid in summary.get("records", []):
            rec = await self.cmd("get_clean_record", [rid])
            out.append(rec[0] if isinstance(rec, list) and rec else rec)
        return out


def render(raw: bytes, png_path: pathlib.Path) -> dict:
    """Write raw bytes + rendered PNG. Return a short summary."""
    png_path.with_suffix(".bin").write_bytes(raw)
    parsed = MapParser(MapParserConfig()).parse(raw)
    if parsed is None:
        return {"error": "could not parse map"}
    png_path.write_bytes(parsed.image_content or b"")
    md = parsed.map_data
    return {
        "png": str(png_path),
        "bytes": len(raw),
        "has_image": bool(md.image),
        "path_points": len(md.path.path[0]) if md.path and md.path.path else 0,
        # [x, y, heading°]; heading is what `vacuum.py drive face` takes
        "robot": [p.x, p.y, p.a] if (p := md.vacuum_position) else None,
        "dock": [p.x, p.y, p.a] if (p := md.charger) else None,
        # photo = the id for `vacuum.py photo`; None when photo saving was off
        "obstacles": [
            {"what": o.details.description or f"type {o.details.type}",
             "at": [o.x, o.y], "photo": o.details.photo_name}
            for o in (md.obstacles_with_photo or [])
        ],
    }


async def cli(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    op = argv[0]
    async with CloudVacuum() as v:
        if op == "current":
            print(json.dumps(render(await v.current_map(), pathlib.Path(argv[1])), indent=2))
        elif op == "records":
            for r in await v.records():
                when = dt.datetime.fromtimestamp(r["begin"]).strftime("%Y-%m-%d %H:%M")
                print(f"{r['begin']}  {when}  {r['duration'] // 60:>3} min  "
                      f"{r['area'] / 1e6:6.1f} m²  finish_reason={r['finish_reason']}")
        elif op == "record":
            print(json.dumps(render(await v.record_map(int(argv[1])), pathlib.Path(argv[2])), indent=2))
        elif op == "record-all":
            outdir = pathlib.Path(argv[1])
            outdir.mkdir(parents=True, exist_ok=True)
            for r in await v.records():
                if r["duration"] == 0:
                    continue  # dock-only events (mop wash etc.) have no map
                summary = render(await v.record_map(r["begin"]), outdir / f"{r['begin']}.png")
                print(r["begin"], json.dumps(summary))
        elif op == "photo":
            data = await v.photo(argv[1])
            pathlib.Path(argv[2]).write_bytes(data)
            print(f"saved {len(data)} bytes to {argv[2]}")
        else:
            print(f"unknown op: {op}")
            print(__doc__)


if __name__ == "__main__":
    asyncio.run(cli(sys.argv[1:]))
