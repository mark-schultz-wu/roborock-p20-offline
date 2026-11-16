# Roborock P20-series BLE Provisioning — Reverse-Engineered Protocol

Device: mainland-China Roborock P20 (BLE adv name `rr-a184_TA-98B4`, internal
model code `a184`), never provisioned. Source: static analysis of
com.roborock.smart 4.74.08 (international APK; provisioning code is
protocol-identical — all JCE crypto, no librrcodec involvement).

## GATT

| Item | UUID / value |
|---|---|
| Service | `726f626f-c4eb-4040-963b-22076b601051` (prefix ASCII "robo") |
| Write char (app→device) | `726f626f-c4eb-4040-963b-22076b601052`, WRITE_TYPE_NO_RESPONSE |
| Indicate char (device→app) | `726f626f-c4eb-4040-963b-22076b601053`, CCCD 0x2902 = ENABLE_INDICATION |

- MTU request: 517; write chunks = `mtu - 5` (512), plain slices, no per-chunk headers.
- Receive: 16 KB buffer; scan for magic, parse frames, compact.
- PnP ID vendor 0x1d6b (Linux Foundation) → vacuum runs BlueZ on embedded Linux.

## Frame (all big-endian)

```
[3B magic "A01"|"1.2"|"1.0"][int32 1][int16 type][int16 len][payload][uint32 CRC32]
```
CRC32 (java.util.zip, = zlib) over bytes up to end of payload.
Types: 16 app-hello, 17 device-hello, 32 ssid-list-req, 33 ssid-list-resp,
1 config-wifi, 2 config-reply, 255 error.

## TLV (V2 payloads)

`[int16 tag][int16 len][value]`, big-endian.

## V2_ECC ("A01") — the flow our client implements

1. App hello (type 16, **unencrypted**): TLV 1=`{0,1}`; TLV 2=app ECDH
   **secp521r1** public key (X.509 SPKI); TLV 3=16 B random nonce;
   TLV 4=HMAC-SHA256(pin, `{0,1}‖pub‖nonce`) only if PIN set.
2. Device hello (type 17, plaintext TLVs): 1=flag; 2=device EC pub (SPKI);
   3=rand; 4=HMAC-SHA256(pin, flag‖devPub‖appNonce‖rand) iff PIN set.
3. Keys:
   - `session = SHA-256(ECDH(appPriv, devicePub))` (AES-256 key)
   - scratch[32]: `scratch[0:14]=appNonce`, `scratch[16:28]=devicePubDER[0:12]`
   - `d = SHA-256(scratch)`; GCM IV = `d[0:12]`, CBC IV = `d[0:16]`
4. Config-wifi (type 1, frame unencrypted, payload TLVs):
   TLV 1=`{0,1}`; TLV 2=AES-GCM-256(session, gcmIV, tag128)(json) (or CBC
   variant); TLV 3=HMAC iff PIN. Await reply type 2 (ok) / 255 (error).

## V1_RSA ("1.2") — legacy fallback

- Default key = `MD5(BLE adv name)` (AES-128-ECB-PKCS5).
- App hello: JSON `{"id":1,"method":"hello","params":{"app_ver":"1.0",
  "key":"<app-generated RSA-1024 pub as PEM>"}}` AES-ECB(default key).
- Device replies RSA-encrypted (app's pub) with `params.key` session string.
- SSID-list request is always V1: frame `("1.2", 1, 32, 2, {0,1})`
  AES-ECB(MD5(name)); response type 33 (JSON base64 list in V1, TLVs in V2).

## Config JSON (kotlinx serialization, field names exact)

```json
{"ssid": "...", "passwd": "...", "u": "<user id>", "token": {"s","t","r","tz","cst"}, "tokens": [{"s","t","r"}], "tz": "...", "cst": "...", "ts": <epoch seconds>}
```
`token`/`tokens` are cloud-issued (ActivationTokenBean). Nullable in the
schema — open experiment: does the vacuum accept null and join WiFi anyway?

## Cloud endpoints (CN region, for later local redirection)

| Purpose | Endpoint |
|---|---|
| IoT API | `https://cniot.roborock.com` (also `cn-iot.roborock.com`, test: `test-cniot`) |
| MQTT | `mqtt-cn.roborock.com` (us/eu/ru analogues) |
| Product catalog | `https://productcn.roborock.com` — `/api/v5/product`, `/api/v1/product/category/details`, `/api/v2/product/resetwifiinfo?model=...` (all require app auth headers; 404 without) |

## Files

- `rr_provision.py` — standalone client (uv + bleak + cryptography); `--probe`
  runs hello/ECDH/ssid-list without credentials.
- `ble_scan.py` — find the vacuum's BLE address (`rr-a184…`).
- Decompiled-app research material (jadx output, the APK, jadx tooling)
  lives outside this repo, at `~/homelab/roborock-re/`; key classes:
  `data/repo/netconfig/o0OOO0o.java` (BluetoothConfigManager),
  `o00oO0o.java` (GATT callback), `com/facebook/internal/OooOO0O.java`
  (frame codec), `internal/common/activator/*` (TLV/payload codecs).

## Status / open questions

- [x] Protocol fully extracted (bytecode-verified, two independent passes)
- [x] Client implemented + self-tested (frame/CRC, chunked reassembly, ECDH,
      GCM/ECB construction)
- [ ] Probe against live device (needs pairing mode re-triggered)
- [ ] Null-token acceptance experiment → decides whether redroid/CN-app
      token minting is needed at all
- [ ] `a184` marketing-name mapping (needs sticker; catalog API is auth-gated)
