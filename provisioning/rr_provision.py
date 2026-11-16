# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak", "cryptography"]
# ///
"""App-less Roborock BLE provisioning client.

Implements the provisioning protocol reverse-engineered from
com.roborock.smart 4.74.08 (BluetoothConfigManager / ConfigNetworkMessageUtil):

  Frame:  [3B magic "A01"|"1.2"]["1.0"][int32=1][int16 type][int16 len][payload][crc32]
          big-endian; crc32 = zlib.crc32 over bytes [0 .. payload_end)
  Types:  16 app-hello, 17 device-hello, 32 ssid-list req, 33 ssid-list resp,
          1 config-wifi, 2 config reply, 255 error
  TLV:    [int16 tag][int16 len][value]  (big-endian)
  V2_ECC: app hello TLVs {1:{0,1}, 2:X.509 secp521r1 pub, 3:16B nonce,
          [4:HMAC-SHA256(pin, ...)]} sent unencrypted.
          device hello TLVs {1:flag, 2:X.509 EC pub, 3:rand, [4:HMAC]}.
          keys: session = SHA256(ECDH(app_priv, dev_pub));
               ivsrc[32]: ivsrc[0:14]=appNonce, ivsrc[16:28]=devPubDER[0:12];
               d=SHA256(ivsrc); gcm_iv=d[0:12], cbc_iv=d[0:16].
          config TLVs {1:{0,1}, 2:AES-GCM256(session,gcm_iv)(json) [+3:HMAC]},
          frame "A01" type 1, unencrypted frame level.
  V1_RSA: hello JSON {"id":1,"method":"hello","params":{"app_ver":"1.0",
          "key":"<PEM RSA-1024 pub>"}} AES-ECB(MD5(devname))-encrypted in a
          "1.2" frame; ssid-list request ("1.2",1,32,2,{0,1}) same encryption.
  GATT:   service 726f626f-...-1051, write ...1052 (NO_RESPONSE, chunks of
          mtu-5), indicate ...1053 (CCCD ENABLE_INDICATION). MTU request 517.

Usage:
    uv run rr_provision.py <addr> [--ssid SSID] [--passwd PASS] [--probe]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import struct
import secrets
import time
import zlib
from dataclasses import dataclass

from bleak import BleakClient
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SERVICE = "726f626f-c4eb-4040-963b-22076b601051"
CHAR_WRITE = "726f626f-c4eb-4040-963b-22076b601052"
CHAR_INDICATE = "726f626f-c4eb-4040-963b-22076b601053"

T_APP_HELLO = 16
T_DEV_HELLO = 17
T_SSID_REQ = 32
T_SSID_LIST = 33
T_CONFIG = 1
T_CONFIG_REPLY = 2
T_ERROR = 255

MAGICS = (b"A01", b"1.2", b"1.0")


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def frame(magic: str, msg_type: int, payload: bytes) -> bytes:
    head = magic.encode() + struct.pack(">Ihh", 1, msg_type, len(payload))
    body = head + payload
    return body + struct.pack(">I", crc32(body))


def tlv(tag: int, value: bytes) -> bytes:
    return struct.pack(">hh", tag, len(value)) + value


def parse_tlvs(data: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    off = 0
    while off + 4 <= len(data):
        tag, length = struct.unpack_from(">hh", data, off)
        off += 4
        out[tag] = data[off:off + length]
        off += length
    return out


def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes) -> bytes:
    return data[: -data[-1]]


def aes_ecb_enc(key: bytes, data: bytes) -> bytes:
    op = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return op.update(pkcs7_pad(data)) + op.finalize()


def aes_ecb_dec(key: bytes, data: bytes) -> bytes:
    op = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return pkcs7_unpad(op.update(data) + op.finalize())

@dataclass
class ParsedFrame:
    magic: str
    msg_type: int
    payload: bytes

class FrameScanner:
    """Reassembles indication chunks into frames (16KB ring semantics)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[ParsedFrame]:
        self.buf += data
        out: list[ParsedFrame] = []
        while True:
            i = self._find_magic()
            if i < 0:
                # keep a tail that might contain a partial magic
                if len(self.buf) > 3:
                    del self.buf[:-3]
                return out
            del self.buf[:i]
            if len(self.buf) < 15:
                return out
            _, msg_type, length = struct.unpack_from(">Ihh", self.buf, 3)
            if len(self.buf) < 15 + length:
                return out
            body = bytes(self.buf[: 11 + length])
            payload = body[11:]
            rx_crc = struct.unpack_from(">I", self.buf, 11 + length)[0]
            del self.buf[: 15 + length]
            if crc32(body) != rx_crc:
                print(f"  crc mismatch on type={msg_type}, dropping")
                continue
            magic = body[:3].decode()
            out.append(ParsedFrame(magic, msg_type, payload))
            print(f"  <- frame {magic!r} type={msg_type} len={length}")

    def _find_magic(self) -> int:
        for m in MAGICS:
            i = self.buf.find(m)
            if i >= 0:
                return i
        return -1


class RoborockBleProvisioner:
    def __init__(self, addr: str, name: str | None = None):
        self.addr = addr
        self.client: BleakClient | None = None
        self.scanner = FrameScanner()
        self.frames: asyncio.Queue[ParsedFrame] = asyncio.Queue()
        self.chunk = 20
        self.device_name: str | None = name

        # V2 session state
        self.app_priv = ec.generate_private_key(ec.SECP521R1())
        self.app_nonce = secrets.token_bytes(16)
        self.session_key: bytes | None = None
        self.gcm_iv: bytes | None = None
        self.cbc_iv: bytes | None = None
        self.v1_session_key_str: str | None = None
        self._pending: list[ParsedFrame] = []

    # ---------- GATT plumbing ----------

    def _on_indicate(self, _h, data: bytearray):
        print(f"   [indicate] {len(data)}B: {bytes(data)[:48].hex()}")
        for fr in self.scanner.feed(bytes(data)):
            self.frames.put_nowait(fr)

    def _on_disconnect(self, client):
        print("   [BLE DISCONNECTED]", flush=True)

    async def connect(self):
        self.client = BleakClient(self.addr, disconnected_callback=self._on_disconnect)
        await self.client.connect()
        # grab advertised local name (basis for the V1 default key)
        if self.device_name is None:
            try:
                self.device_name = await self._read_adv_name()
            except Exception:
                self.device_name = None
        try:
            await self.client.exchange_mtu(517)
        except Exception:
            pass
        mtu = getattr(self.client, "mtu_size", None) or 23
        self.chunk = max(16, mtu - 5)
        await self.client.start_notify(CHAR_INDICATE, self._on_indicate)
        print(f"connected mtu={mtu} chunk={self.chunk} name={self.device_name!r}")

    async def _read_adv_name(self) -> str | None:
        # scan again briefly to capture the local name
        from bleak import BleakScanner
        dev = await BleakScanner.find_device_by_address(self.addr, timeout=5.0)
        return dev.name if dev else None

    async def write_payload(self, data: bytes):
        assert self.client
        for off in range(0, len(data), self.chunk):
            await self.client.write_gatt_char(
                CHAR_WRITE, data[off:off + self.chunk], response=False)
            # pace like the app (which waits on write callbacks) — helps
            # under radio-coexistence load and on lossy links
            await asyncio.sleep(0.02)

    async def recv(self, want_type: int, timeout: float = 5.0) -> ParsedFrame:
        end = time.monotonic() + timeout
        while True:
            # service previously-stashed frames first
            for i, fr in enumerate(self._pending):
                if fr.msg_type == want_type:
                    del self._pending[i]
                    return fr
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                fr = await asyncio.wait_for(self.frames.get(), timeout=left)
            except asyncio.TimeoutError:
                break
            if fr.msg_type == want_type:
                return fr
            self._pending.append(fr)
        raise TimeoutError(f"no type={want_type} frame within {timeout}s")

    # ---------- hello negotiation ----------

    def _v1_default_key(self) -> bytes:
        name = self.device_name or ""
        return hashlib.md5(name.encode()).digest()

    async def v1_hello(self) -> ParsedFrame:
        """Opener: RSA-1024 PEM JSON, AES-ECB(MD5(name))-encrypted."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.rsa_priv = rsa.generate_private_key(
            public_exponent=65537, key_size=1024)
        pem = self.rsa_priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        payload = ('{"id": 1, "method":"hello", '
                   '"params":{"app_ver":"1.0", '
                   f'"key": "{pem}"}}}}').encode()
        await self.write_payload(
            frame("1.2", T_APP_HELLO, aes_ecb_enc(self._v1_default_key(), payload)))
        print(f"-> v1 app hello ({len(payload)}B json w/ RSA-1024 PEM)")
        dev = await self.recv(T_DEV_HELLO, timeout=8.0)
        print(f"   device hello: magic={dev.magic!r} len={len(dev.payload)} "
              f"head={dev.payload[:48].hex()}")
        return dev

    def _v1_rsa_decrypt(self, data: bytes) -> bytes:
        from cryptography.hazmat.primitives.asymmetric import padding
        out = b""
        for off in range(0, len(data), 128):
            out += self.rsa_priv.decrypt(data[off:off + 128],
                                         padding.PKCS1v15())
        return out

    def _v2_derive(self, tlvs: dict[int, bytes]):
        dev_pub_der = tlvs[2]
        dev_rand = tlvs[3]
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        dev_pub = load_der_public_key(dev_pub_der)
        secret = self.app_priv.exchange(ec.ECDH(), dev_pub)
        self.session_key = hashlib.sha256(secret).digest()
        ivsrc = bytearray(32)
        ivsrc[0:14] = self.app_nonce[0:14]
        ivsrc[16:28] = dev_pub_der[0:12]
        d = hashlib.sha256(bytes(ivsrc)).digest()
        self.gcm_iv = d[0:12]
        self.cbc_iv = d[0:16]
        print(f"   derived session_key={self.session_key.hex()} "
              f"gcm_iv={self.gcm_iv.hex()} dev_rand={dev_rand.hex()}")

    async def negotiate(self) -> ParsedFrame:
        dev = await self.v1_hello()
        if dev.magic == "1.2" or dev.magic == "1.0":
            plain = self._v1_rsa_decrypt(dev.payload)
            print(f"   V1 RSA-decrypted: {plain[:200]!r}")
            obj = json.loads(plain.decode(errors="replace"))
            self.v1_session_key_str = obj["params"]["key"]
            print(f"   V1 session key: {self.v1_session_key_str[:24]}…")
            return dev
        # A01 TLV device hello -> answer with our EC pub + nonce
        tlvs = parse_tlvs(dev.payload)
        print(f"   A01 device hello tags: { {k: len(v) for k, v in tlvs.items()} }")
        pub = self.app_priv.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        payload = tlv(1, b"\x00\x01") + tlv(2, pub) + tlv(3, self.app_nonce)
        await self.write_payload(frame("A01", T_APP_HELLO, payload))
        print(f"-> v2 app hello ({len(payload)}B tlvs, pub {len(pub)}B)")
        # device may send a second hello (post-ECDH); use the newest one
        try:
            dev2 = await self.recv(T_DEV_HELLO, timeout=6.0)
            tlvs = parse_tlvs(dev2.payload)
            print(f"   second device hello tags: { {k: len(v) for k, v in tlvs.items()} }")
            self._v2_derive(tlvs)
            return dev2
        except TimeoutError:
            print("   (no second hello; deriving from first)")
            self._v2_derive(tlvs)
            return dev

    # ---------- V1 (ssid-list probe / fallback) ----------


    async def ssid_list_probe(self):
        key = self._v1_default_key()
        if not key:
            print("  (no device name -> cannot build V1 default key)")
            return
        payload = aes_ecb_enc(key, b"\x00\x01")
        await self.write_payload(frame("1.2", T_SSID_REQ, payload))
        print("-> ssid-list request (V1-encrypted)")
        fr = await self.recv(T_SSID_LIST, timeout=8.0)
        if fr.magic == "A01":
            tlvs = parse_tlvs(fr.payload)
            print(f"   ssid list (A01): {list(tlvs)[:8]}")
        else:
            plain = aes_ecb_dec(key, fr.payload)
            print(f"   ssid list (V1 json?): {plain[:200]!r}")

    # ---------- config ----------

    async def config_wifi(self, ssid: str, passwd: str, tz: str = "Asia/Shanghai",
                          cst: str = "CN", use_gcm: bool = True,
                          extra: dict | None = None):
        bean = {
            "ssid": ssid,
            "passwd": passwd,
            "u": (extra or {}).get("u", ""),
            "token": (extra or {}).get("token"),
            "tokens": (extra or {}).get("tokens"),
            "tz": tz,
            "cst": cst,
            "ts": int(time.time()),
        }
        if (extra or {}).get("country_domain"):
            # reference local_roborock_server onboarding body
            bean["country_domain"] = extra["country_domain"]
            bean["u"] = extra.get("u") or "1234567890"
        raw = json.dumps(bean, separators=(",", ":")).encode()
        print(f"-> config-wifi json: {raw.decode()}")
        if self.v1_session_key_str is not None:
            # V1 path: whole JSON AES-ECB(session-key string bytes), "1.2" frame
            cipher = aes_ecb_enc(self.v1_session_key_str.encode(), raw)
            await self.write_payload(frame("1.2", T_CONFIG, cipher))
            print(f"   sent V1 config ({len(cipher)}B AES-ECB)")
        elif self.session_key is not None:
            if use_gcm:
                cipher = AESGCM(self.session_key).encrypt(
                    self.gcm_iv, raw, None)  # ct||tag == JCE doFinal output
            else:
                op = Cipher(algorithms.AES(self.session_key),
                            modes.CBC(self.cbc_iv)).encryptor()
                cipher = op.update(pkcs7_pad(raw)) + op.finalize()
            payload = tlv(1, b"\x00\x01") + tlv(2, cipher)
            await self.write_payload(frame("A01", T_CONFIG, payload))
            print(f"   sent V2 config ({len(payload)}B tlvs, "
                  f"{'gcm' if use_gcm else 'cbc'})")
        else:
            raise RuntimeError("no session; run negotiate() first")

        fr = await self.recv(T_CONFIG_REPLY, timeout=15.0)
        print(f"<- config reply type={fr.msg_type} payload={fr.payload[:64].hex()}")
        return fr


def _strip_spki_header(der: bytes) -> bytes:
    """Extract raw EC point from X.509 SPKI via cryptography itself."""
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    key = load_der_public_key(der)
    return key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("addr")
    ap.add_argument("--name", default=None,
                    help="advertised BLE local name (V1 default-key basis)")
    ap.add_argument("--ssid")
    ap.add_argument("--passwd")
    ap.add_argument("--probe", action="store_true",
                    help="hello + ssid-list probe only, no config")
    ap.add_argument("--cbc", action="store_true", help="use AES-CBC variant")
    ap.add_argument("--fake-token", action="store_true",
                    help="send shape-valid but bogus cloud token beans")
    ap.add_argument("--stack-server",
                    help="token.r value, e.g. 'rr.roborock.lab/'")
    ap.add_argument("--country-domain", default="cn",
                    help="country_domain sent in the onboarding body")
    args = ap.parse_args()

    p = RoborockBleProvisioner(args.addr, name=args.name)
    await p.connect()
    await p.negotiate()

    if args.probe or not (args.ssid and args.passwd):
        print("\nprobe mode: requesting ssid list from device")
        try:
            await p.ssid_list_probe()
        except TimeoutError as e:
            print(f"probe incomplete: {e}")
        print("\ndone (no config sent).")
        return

    extra = None
    if args.fake_token:
        extra = {
            "u": "1",
            "token": {"s": "faketoken", "t": "faketoken", "r": "faketoken",
                      "tz": "Asia/Shanghai", "cst": "CN"},
            "tokens": [{"s": "faketoken", "t": "faketoken", "r": "faketoken"}],
        }
    elif args.stack_server:
        token_s = f"S_TOKEN_{secrets.token_hex(16)}"
        token_t = f"T_TOKEN_{secrets.token_hex(16)}"
        extra = {
            "u": "1234567890",
            "token": {
                "r": args.stack_server,
                "tz": "Asia/Shanghai",
                "s": token_s,
                "cst": "CST-8",
                "t": token_t,
            },
            "tokens": [{"s": token_s, "t": token_t, "r": args.stack_server}],
            "country_domain": args.country_domain,
        }
        print(f"token.r={args.stack_server} s={token_s} t={token_t} "
              f"country_domain={args.country_domain}")
    await p.config_wifi(args.ssid, args.passwd, use_gcm=not args.cbc,
                        extra=extra)
    print("\nconfig-wifi sent; clean-disconnecting so the device applies it.")
    try:
        await p.client.disconnect()
    except Exception as e:
        print(f"(disconnect: {e})")
    print("done — watch uap0 for association.")


if __name__ == "__main__":
    asyncio.run(main())
