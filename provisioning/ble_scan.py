# /// script
# requires-python = ">=3.9"
# dependencies = ["bleak"]
# ///
"""Scan BLE advertisements and flag anything vacuum-shaped.

Usage:
    uv run ble_scan.py [duration_seconds]

Prints every advertiser that has a name/services/mfr-data on first sight;
vacuum-shaped names are highlighted with <<<.
"""
import asyncio
import sys

from bleak import BleakScanner

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 30

HINTS = ("roborock", "rockrobo", "vacuum", "p20", "石头")


def describe(dev, adv):
    lines = []
    name = adv.local_name or (dev.name or "")
    lines.append(f"{dev.address}  RSSI={adv.rssi}  name={name!r}")
    if adv.service_uuids:
        lines.append(f"  service_uuids: {[str(u) for u in adv.service_uuids]}")
    if adv.service_data:
        for k, v in adv.service_data.items():
            lines.append(f"  service_data {k}: {v.hex()}")
    if adv.manufacturer_data:
        for k, v in adv.manufacturer_data.items():
            lines.append(f"  mfr_data 0x{k:04x}: {v.hex()}")
    return "\n".join(lines)


def is_interesting(dev, adv):
    blob = " ".join([
        (adv.local_name or "").lower(),
        (dev.name or "").lower(),
        str([str(u).lower() for u in adv.service_uuids]),
    ])
    return any(h in blob for h in HINTS)


async def main():
    seen: set[str] = set()
    interesting_hits: list[str] = []

    def cb(dev, adv):
        key = dev.address
        fresh = key not in seen
        seen.add(key)
        interesting = is_interesting(dev, adv)
        named = bool(adv.local_name or adv.service_uuids or adv.manufacturer_data)
        if interesting:
            if dev.address not in interesting_hits:
                interesting_hits.append(dev.address)
            print(f"<<< {describe(dev, adv)}", flush=True)
        elif named and fresh:
            print(describe(dev, adv), flush=True)

    print(f"Scanning BLE for {DURATION}s (highlighting {HINTS}) ...", flush=True)
    async with BleakScanner(detection_callback=cb) as scanner:
        await asyncio.sleep(DURATION)

    print(f"\nTotal distinct devices seen: {len(seen)}")
    print(f"Vacuum-shaped hits: {interesting_hits}")


if __name__ == "__main__":
    asyncio.run(main())
