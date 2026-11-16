# /// script
# requires-python = ">=3.11"
# dependencies = ["python-roborock"]
# ///
"""Is the vacuum still accepting commands, is it connected to the local broker, and are its
rrcheck renewals answered?

Background: other models refuse every command (-10002 "rrcheck access
denied") ~48 h after onboarding unless their access-grant renewals get a
valid answer. This vacuum renews via GET /devices/{did}/sh, handled by the
locally applied PR #199 (route=fw_check). See README "Stack".

Broker: a map request whose answer comes only as an MQTT push (protocol 301), so it passes
only if the vacuum's own MQTT session to the local stack works. (2026-11-13/14: after a reboot
the stack ran without its published ports for 29 h; commands over the direct link still worked,
so the old check passed while maps, history and the camera were down.)

Exit codes: 0 all good, 2 commands refused with -10002, 3 vacuum not on the local broker,
1 other failure.
Prints a short summary (first line = headline) for the alert wrapper.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
from vacuum import Vacuum, HOST  # noqa: E402

PATCHED_AT = dt.datetime.fromisoformat("2026-10-29T23:20:00").astimezone()
HTTP_LOG = HERE / "local_roborock_server" / "data" / "runtime" / "decompiled_http.jsonl"


def renewals() -> list[str]:
    out = []
    for path in sorted(glob.glob(f"{HTTP_LOG}*")):
        for line in open(path, errors="ignore"):
            if "/sh" not in line and "/f/c" not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = (d.get("clean_path") or "").rstrip("/")
            if not (p.endswith("/sh") or p.endswith("/f/c")):
                continue
            if not (d.get("remote") or "").startswith(f"{HOST}:"):
                continue  # only the vacuum's own requests, not manual tests
            t = dt.datetime.fromisoformat(d["time"]).astimezone()
            if t >= PATCHED_AT:
                out.append(f"{t:%m-%d %H:%M} {p.rsplit('/', 1)[-1]} -> {d.get('route')}")
    return sorted(set(out))


async def status() -> tuple[int, str]:
    try:
        async with Vacuum() as v:
            s = await v.status()
        return 0, f"commands OK (state {s.get('state')}, battery {s.get('battery')}%)"
    except Exception as e:
        msg = str(e)
        if "10002" in msg or "rrcheck" in msg:
            return 2, f"COMMANDS REFUSED: {msg[:150]}"
        return 1, f"check failed: {type(e).__name__}: {msg[:150]}"


async def broker() -> tuple[bool, str]:
    from vacuum_map import CloudVacuum
    try:
        async with CloudVacuum() as v:
            data = await asyncio.wait_for(v.current_map(), timeout=90)
        return True, f"local broker OK (map push over MQTT, {len(data)} bytes)"
    except Exception as e:
        return False, (f"VACUUM NOT ON THE LOCAL BROKER (map push over MQTT failed: {type(e).__name__}: "
                       f"{str(e)[:150]}); maps, history and the camera are down. README: docker compose ps")


def main() -> None:
    code, headline = asyncio.run(status())
    lines = [headline]
    if code == 0:
        ok, msg = asyncio.run(broker())
        if ok:
            lines.append(msg)
        else:
            code = 3
            lines.insert(0, msg)
    rn = renewals()
    if rn:
        good = sum(1 for r in rn if r.endswith("fw_check"))
        lines.append(f"renewals since patch: {len(rn)} ({good} answered by fw_check)")
        lines += rn[-5:]
    else:
        lines.append("renewals since patch: none seen yet")
    print("\n".join(lines))
    sys.exit(code)


if __name__ == "__main__":
    main()
