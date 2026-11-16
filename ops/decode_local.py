# /// script
# requires-python = ">=3.11"
# dependencies = ["python-roborock", "dpkt"]
# ///
"""Decode the direct local-protocol traffic (TCP 58867) from packet captures.

The vacuum's direct command link is encrypted with its localKey (plus, for
the L01 protocol version, two nonces exchanged in each connection's
hello). This reads the AP captures, reassembles each TCP 58867
connection, takes the nonces from the hello messages, and decodes every
message with python-roborock's own parser.

    uv run ops/decode_local.py                 # all ap_logs/capture* files
    uv run ops/decode_local.py ap_logs/capture.pcap --since 2026-10-29T23:00
    uv run ops/decode_local.py --print         # also print a readable line per message

Output: logs/decoded_local.jsonl (one JSON object per message:
time, conn, dir, version, protocol, seq, and the decoded payload).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import logging
import pathlib
import struct
import sys

import dpkt
from roborock.protocol import MessageParser
from roborock.roborock_message import RoborockMessageProtocol

HERE = pathlib.Path(__file__).resolve().parent.parent  # repo root (script lives in ops/)
sys.path.insert(0, str(HERE))
from vacuum import HOST, LOCAL_KEY  # noqa: E402

PORT = 58867
VERSIONS = (b"1.0", b"L01")


def tcp_streams(paths):
    """Yield (conn_key, direction, [(ts, seq, payload)]) from pcap files."""
    flows: dict[tuple, list] = {}
    for path in paths:
        with open(path, "rb") as f:
            try:
                reader = dpkt.pcap.Reader(f)
            except ValueError:
                continue
            for ts, buf in reader:
                try:
                    eth = dpkt.ethernet.Ethernet(buf)
                except dpkt.dpkt.UnpackError:
                    continue
                ip = eth.data
                if not isinstance(ip, dpkt.ip.IP) or not isinstance(ip.data, dpkt.tcp.TCP):
                    continue
                tcp = ip.data
                if PORT not in (tcp.sport, tcp.dport) or not tcp.data:
                    continue
                src = f"{dpkt.utils.inet_to_str(ip.src)}:{tcp.sport}"
                dst = f"{dpkt.utils.inet_to_str(ip.dst)}:{tcp.dport}"
                client = src if tcp.dport == PORT else dst
                direction = "to_robot" if tcp.dport == PORT else "from_robot"
                flows.setdefault((client, direction), []).append((ts, tcp.seq, bytes(tcp.data)))
    return flows


def reassemble(segments):
    """Order by sequence number and drop retransmitted bytes."""
    segments.sort(key=lambda s: (s[1], s[0]))
    out, next_seq = [], None
    for ts, seq, data in segments:
        if next_seq is not None:
            if seq + len(data) <= next_seq:
                continue                      # pure retransmission
            if seq < next_seq:
                data = data[next_seq - seq:]  # partial overlap
                seq = next_seq
        out.append((ts, data))
        next_seq = seq + len(data)
    return out


def first_header(chunks):
    """(version, random) of the first message in a direction: the hello."""
    raw = b"".join(d for _, d in chunks)[:64]
    for i in range(len(raw) - 3):
        if raw[i:i + 3] in VERSIONS and len(raw) >= i + 13:
            version = raw[i:i + 3]
            random = struct.unpack(">I", raw[i + 7:i + 11])[0]
            return version, random
    return None, None


def frames(chunks):
    """Split a reassembled stream into length-prefixed messages: (ts, frame)."""
    buf, ts0 = b"", None
    for ts, data in chunks:
        if not buf:
            ts0 = ts
        buf += data
        while len(buf) >= 4:
            n = struct.unpack(">I", buf[:4])[0]
            if n > 1 << 20:                   # lost sync; drop a byte and rescan
                buf = buf[1:]
                continue
            if len(buf) < 4 + n:
                break
            yield ts0, buf[:4 + n]
            buf, ts0 = buf[4 + n:], ts


def decode_frame(frame, connect_nonce, ack_nonce) -> dict:
    """Decode one framed message. python-roborock's parser rejects messages
    with an empty payload (the client's hello), so read those from the
    plain header instead."""
    body = frame[4:]
    version, seq, random, _, proto = struct.unpack(">3sIIIH", body[:17])
    try:
        name = RoborockMessageProtocol(proto).name
    except ValueError:
        name = str(proto)
    base = {"version": version.decode(errors="replace"), "protocol": name, "seq": seq}
    try:
        msgs, _ = MessageParser.parse(frame, local_key=LOCAL_KEY,
                                      connect_nonce=connect_nonce, ack_nonce=ack_nonce)
    except Exception as e:
        return base | {"error": str(e)[:120]}
    if msgs:
        return base | {"payload": decode_payload(msgs[0])}
    if len(body) == 21:                       # header + CRC only: no payload
        return base | {"payload": None, "random": random}
    return base | {"error": "undecodable"}


def decode_payload(msg):
    payload = msg.payload
    if payload is None:
        return None
    try:
        obj = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return {"raw_bytes": len(payload)}
    dps = obj.get("dps") if isinstance(obj, dict) else None
    if isinstance(dps, dict):           # unwrap the JSON-in-a-string RPC bodies
        for k, v in list(dps.items()):
            if isinstance(v, str) and v[:1] in "{[":
                try:
                    dps[k] = json.loads(v)
                except ValueError:
                    pass
    return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcaps", nargs="*")
    ap.add_argument("--since", help="ISO time, e.g. 2026-10-29T23:00")
    ap.add_argument("--out", default=str(HERE / "logs" / "decoded_local.jsonl"))
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)
    paths = a.pcaps or sorted(glob.glob(str(HERE / "ap_logs" / "capture*")))
    since = dt.datetime.fromisoformat(a.since).timestamp() if a.since else 0

    flows = tcp_streams(paths)
    clients = sorted({c for c, _ in flows})
    records = []
    for client in clients:
        up = reassemble(flows.get((client, "to_robot"), []))
        down = reassemble(flows.get((client, "from_robot"), []))
        version, connect_nonce = first_header(up)
        _, ack_nonce = first_header(down)
        for direction, chunks in (("to_robot", up), ("from_robot", down)):
            for ts, frame in frames(chunks):
                records.append({"ts": ts, "conn": client, "dir": direction,
                                **decode_frame(frame, connect_nonce, ack_nonce)})
    records = sorted((r for r in records if r["ts"] >= since), key=lambda r: r["ts"])
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for r in records:
            r["time"] = dt.datetime.fromtimestamp(r.pop("ts")).isoformat(timespec="milliseconds")
            f.write(json.dumps(r, default=str) + "\n")
            if a.print:
                body = json.dumps(r.get("payload", r.get("error")), default=str)
                print(f"{r['time'][11:23]} {r['dir']:10} {r.get('protocol', 'ERROR'):18} {body[:110]}")
    errors = sum(1 for r in records if "error" in r)
    print(f"{len(records)} messages from {len(clients)} connections to {HOST}:{PORT} "
          f"({errors} undecodable) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
