# A China-only Roborock running fully local in the US

A mainland-China Roborock P20 (`roborock.vacuum.a184`, factory firmware
02.22.56) runs in the US with no Roborock account, no Chinese phone
number, no VPN and no internet access at all. It cleans, maps, names
rooms, takes obstacle photos, keeps schedules and speaks English.

The heavy lifting is
[`local_roborock_server`](https://github.com/Python-roborock/local_roborock_server),
an open-source emulation of Roborock's cloud. This document records how
we got there, what's new compared with what's already published, what the
logs show, and what's still open.

## Status (30 Oct 2026, ~02:30)

| | |
|---|---|
| Control | Works: ~175 commands confirmed (`COMMANDS.md`), friendly CLI (`vacuum.py`) |
| Region lock (`rrcheck`) | No refusal in any log; renewals answered on both paths (`/sh` by PR #199, `/f/c` by the forged-cert front-end). Long-term proof: the 31 Oct 23:47 check |
| Isolation | Held throughout: every DNS name and every connection the vacuum made went to this machine |
| Camera | **Live view works** (30 Oct 01:18): go2rtc viewer + forged-cert createca bootstrap; first frames 960x540 while charging |
| Voice | English (another model's official pack) |
| Map | A throwaway test map; a proper mapping run is pending (clutter on the floor) |
| Scheduled check | `roborock-rrcheck.timer`: 30 Oct 18:13 and 31 Oct 23:47, results by Pushover |
| Deployment | One docker compose project owns everything vacuum-facing: server + `compose.local.yaml` overlay (443 publish + TURN ports + `apicn` forged-cert `:443` bridge, `go2rtc` viewer; consumers get standard MQTT/HTTP/RTSP/WebRTC ports) |

## Background: why China units "can't" work abroad

- **Roborock's own cloud** refuses to pair a China unit outside China
  ("not certified in the current country or region").
- **Known workarounds** keep Roborock's cloud in the loop: a VPN exiting
  in China, a paid DNS service faking the region check, or a China account
  (needs a +86 phone number).
- **Rooting** (Valetudo) isn't available for 2024–25 models.
- **`local_roborock_server`** lists China units as a known limitation:
  they onboard, then refuse ordinary commands with `-10002 rrcheck access
  denied`. Reports: G20S `a143`
  ([#81](https://github.com/Python-roborock/local_roborock_server/issues/81)),
  G30 `a146`
  ([#161](https://github.com/Python-roborock/local_roborock_server/issues/161)),
  G20S `a96`
  ([#182](https://github.com/Python-roborock/local_roborock_server/issues/182)).

## What made it work

### What `local_roborock_server` does (most of it)

- Emulates the cloud: activation, recovering the robot's keys, the MQTT
  broker, maps, photos, the camera relay.
- Its onboarding sends the robot a **made-up activation token** whose
  `token.r` field is your server's address. The robot doesn't validate the
  token; it just treats that address as its cloud. That is the core trick.
- `region = "cn"` makes the robot's location check get
  `{"country": "CN", "timezone": "Asia/Shanghai"}`.
- Forges the robot's `rrcheck` access grant
  ([#124](https://github.com/Python-roborock/local_roborock_server/pull/124));
  [#199](https://github.com/Python-roborock/local_roborock_server/pull/199)
  (open, applied locally) extends that to this model's renewal endpoint.

### What we did differently

1. **Paired over Bluetooth, not the robot's Wi-Fi hotspot.** The server's
   onboarding connects a second computer to the vacuum's temporary
   hotspot. We reverse-engineered the app's Bluetooth pairing protocol
   instead (`provisioning/PROTOCOL.md`, `provisioning/rr_provision.py`). This vacuum also offers the
   hotspot as a fallback, so the standard route would very likely have
   worked too: Bluetooth is an alternative, not the enabler. It saves the
   second computer.
2. **No internet at all.** The vacuum lives on its own Wi-Fi; DNS answers
   every name with this machine, and the firewall drops everything else.
3. **China settings everywhere:** server region `cn`, and at pairing
   `country_domain` cn, timezone Asia/Shanghai, `cst` CST-8.
4. **A robot that had never touched Roborock's cloud**, still on factory
   firmware.

Items 2–4 are choices, not new techniques. They may be why this China
unit works when others didn't; we haven't isolated which one matters.

### How it came together (29 Oct)

The first afternoon was an `omp` coding-agent session; the evening's
command work was Claude Code.

| Time | Step |
|---|---|
| 14:13 | Question: is the app really the only way to pair it? Decision: try Bluetooth without the app |
| 14:21 | Vacuum found in pairing mode: `rr-a184_TA-98B4` (settles the model), one custom service, two characteristics |
| 14:29–14:40 | App 4.74.08 decompiled; pairing is plain Java crypto (RSA hello, AES session, default key `MD5(device name)`); client written and self-tested |
| 15:05 | First contact: session key issued, Wi-Fi config accepted with no token |
| 15:42 | With a made-up token, it joins our network and looks up `api-faketoken.roborock.com`: the token names its cloud |
| 15:42–16:07 | `local_roborock_server` found and set up, with a real domain and ZeroSSL certificate (the robot checks TLS) |
| 16:28–17:17 | Re-paired with the server's token; key recovered over three pairing cycles; activated and connected |
| 17:26 | Local command port opens; `get_status` returns real data; `find_me` plays the tone |
| evening | Command bring-up, maps, photos, rooms, schedules, voice, camera authorization |

### Where the region lock lives

| Layer | Enforced here? | Evidence |
|---|---|---|
| Pairing | No | Accepted an invented token |
| Activation | No | Activated against the server the token named |
| Account binding ("not certified in your region") | Never reached | Lives in Roborock's real cloud |
| Command execution (`rrcheck`) | **Not triggered so far** | No `-10002` in any log (MQTT, HTTPS, direct link) |

## What's new compared with what's published

New, as far as our searches found:

- **A China-market unit taking commands on the local server**, where the
  published China reports are all blocked. Pending the 48-hour check.
- **Camera remote view switched on without Roborock's cloud:**
  `set_camera_status` with bit 5, then bit 12, then a press of the robot's
  Power button; the robot sets the authorization bit (2) itself. This had
  been described as possible only from the official app on the real cloud.
- **English voice on a China unit**, using the Qrevo Slim's official pack.
- **An app-less Bluetooth pairing client** for this generation's protocol.
- **Command formats for this model** (`COMMANDS.md`), including: `lab_status`
  is the map-saving switch; photo saving needs camera bits 10 and 11
  together; the PIN command needs `old_password`, `new_password` and
  `enable_password`; schedules need a `segments` field.
- Small tools: `ops/decode_local.py` (decrypts the direct command link from
  packet captures), `vacuum.py`, `check_rrcheck.py`.
- **Real-cloud probes of the robot's own endpoints (30 Oct, replayed from
  the desktop, vacuum still airgapped):** `api-cn.roborock.com` serves the
  robot bootstrap calls, and `/location` there geolocates the client IP
  (answered `countryCode: US` from here) — mechanism-level evidence for why
  internet-connected China units abroad get locked. `fwapi/checkda` is a
  signature-checked status poll whose real answer is
  `{"code":200,"msg":"success","data":null}`; `api-cn`'s
  `/devices/{did}/sh` answers with the CN statutory-holiday calendar.
- **Command formats from the app's decompiled React Native plugin**
  (`hermes-dec`): `app_get_robot_setting` and `name_multi_map` verified on
  this robot; six zone/scene setter shapes recovered (COMMANDS.md).
- **Serving the vacuum its own hardcoded endpoints with a forged
  certificate (30 Oct):** minted from the public eMudhra intermediate-CA
  key compromise via the Sectigo cross-sign to the `AAA Certificate
  Services` root — which the vacuum's frozen factory trust store still
  accepts (no revocation, no CT, no blocklist). This captured the
  startup `POST /f/c` rrcheck renewal to the real `api-cn.roborock.com`,
  let the local server answer it with a valid grant, and — by adding a
  SAN for the firmware's own 15-char-truncated hostname — unblocked the
  camera relay bootstrap (`createca`) that previously "required" a
  ≤12-character domain. Front-end: `apicn/` in the repo.

Reused, not discovered: the cloud emulation and the made-up-token trick
(`local_roborock_server`), the map-edit wrapper `{"data": …,
"need_retry": 1}` ([LocalRock](https://github.com/DonSidro/LocalRock)),
the camera's 15-character limit, grant forging (#124, #199), the local
control protocol and photo download (python-roborock).

## What the logs show (as of 30 Oct 00:30)

Coverage: decoded HTTPS and MQTT since first activation (29 Oct 17:16);
packet captures since 21:54 (earlier ones were overwritten). See README,
"Logs".

- **Destinations (complete map, 30 Oct 01:15):** only this machine —
  every TLS SNI the vacuum has ever sent: `api-rr.roborock.lab` (main
  API, :555), `rr.roborock.lab` (fwapi `checkda`/`createca`, :443),
  `api-cn.roborock.com` (the `POST /f/c` rrcheck renewal, :443 — now
  answered with a forged grant by the `apicn/` front-end),
  `conf-cn-1316693915.cos.ap-chengdu.myqcloud.com` (Roborock's Tencent
  COS config bucket — TLS probe, connect-and-close, no request sent),
  SNI `vivians` (the log-upload bucket `viviansproc-…`, hostname
  truncated the same way `createca` mangles its host; probe only), plus
  MQTT :8881 and our temporary voice-pack web servers. The conf/log
  bucket hosts come from the app's `areaServerMapNew` and are pushed to
  robots via `set_fds_endpoint`.
- **HTTPS calls:** the normal startup (`/region`, `/nc`, `/devices/…/info`,
  `/location`, `/time/now`); `checkda` in bursts of 3 per wake (answered
  with the real cloud shape since the front-end went up);
  `/devices/…/sh` (the daily rrcheck renewal, answered by `fw_check`
  since 23:20); `POST /f/c` at each startup (answered with a
  `fw_check` grant since 01:10 — the robot's first-ever successful
  renewal on that path; no retry after, unlike the 404 era).
- **MQTT:** 412 replies, 237 commands, 71 map pushes, unsolicited status
  pushes, 22 firmware-version announcements. Every error reply came from
  our own test probes.
- **Direct link:** 141 commands decoded; the only errors were two
  deliberate PIN-format probes.

## Open questions

1. **Does it keep working past 48 hours?** **Answered: yes.** The 48 h
   check (31 Oct 23:47) and every daily check since passed; the robot
   renews once a day (~19:58 UTC) on both paths and every renewal is
   answered by `fw_check`. Original question: on other models, `rrcheck`
   refuses commands ~48 h after onboarding unless the robot's access-grant
   renewals get a valid answer. The a184 renews on **two paths** (30 Oct):
   `GET /devices/{did}/sh` on its token host (~daily; answered by our
   patched `fw_check` since 23:20) and `POST /f/c` to the **real**
   `api-cn.roborock.com` at every startup (captured below) — which always
   failed on our certificate, so that path has never once succeeded.
   Decision (30 Oct ~01:15): rather than leave `/f/c` failing for a clean
   experiment, the front-end answers it with the `fw_check` grant now —
   survival over attribution, so the 48 h check becomes a pass/fail
   result rather than an isolation of `/sh`. PR #199 is verified in the
   running container and a manual `/sh` replay (23:29) got a proper
   `{k, d}` grant; the robot's own `/sh` renewals so far: one, 17:16
   pre-patch (catch-all). One caveat from the real-cloud probes:
   Roborock's `api-cn` answers the same `/devices/{did}/sh` with the
   Chinese statutory-holiday calendar, not `{k, d}` — the `{k, d}` shape
   is what unblocked a Saros 10R/a143 on the local server (#199); if the
   a184 rejects it, the captured holiday payload is the fallback.
   The 18:13 and 23:47 checks report as scheduled. Where the 48 h figure
   comes from: it is empirical, from the local_roborock_server tracker —
   a Saros 10R/a143 locked up repeatedly ~48 h after onboarding until
   `/devices/{did}/sh` was answered (PR #199, verified over weeks), and
   one EU Qrevo Edge 2/a298 failed at ~48 h three times
   ([#120](https://github.com/Python-roborock/local_roborock_server/issues/120);
   note another a298 ran 72 h+ fine, and the failing one had an inconsistent
   region/timezone answer — ours is consistent CN/Shanghai). `rrcheck` is a
   binary on the robot that enforces a time-limited local access license
   ({f, c, t}, ±300 s); 48 h is the observed grace window, not a documented
   constant.
2. **What is the `api-cn.roborock.com` call?** **Answered (30 Oct, 01:57):
   `POST /f/c` — the rrcheck access-grant renewal itself**, with body
   `cpuid`, `did`, `pid`, `platform=RR`, `sn`, `tz=Asia/Shanghai`, `uid`
   and an RSA `signature` (and an embedded Windows-IE user agent string).
   Captured by fronting the vacuum's :443 with a forged certificate for
   `api-cn.roborock.com`, minted from the public eMudhra intermediate key
   compromise (EM DV TLS CA-G3B, P-384 scalar 156) via its Sectigo
   cross-sign to the `AAA Certificate Services` root — which the robot's
   frozen factory trust store still accepts (no revocation, no CT, no
   blocklist; it also completed a handshake against a non-matching SAN on
   a second flow, so its hostname checking is inconsistent). Replayed
   startup calls also showed the real `api-cn` serves the robot bootstrap
   family: `/location` **geolocates the client IP** (answered
   `countryCode: US` from here), `/time/now`, `/nc`, `/devices/{did}/info`.
   So an internet-connected China unit renews its grant through Roborock's
   real API and is told where it is; ours gets silence and has so far kept
   working on the startup grant plus `/sh`. Bonus discovery: at startup the
   robot also opens TLS with SNI `vivians` — the analytics host
   (`viviansproc-eu-…myqcloud.com`, issue #120), hostname truncated like
   `createca` — connects, sends nothing, closes. The front-end is
   containerized (`compose.bridge.yaml` service `apicn`, key material
   in `apicn/`, gitignored) and running with `--answer-fc --camera`
   since 30 Oct ~01:15, so `POST /f/c` gets the stack's `fw_check`
   `{k,d}` grant and `createca` gets TURN credentials (both verified
   end-to-end with the robot's own requests).
3. **What is `checkda`?** Answered (30 Oct): a signature-authenticated
   firmware-API status poll (`did`, `duid`, `pid`, `uid` + RSA signature;
   the real cloud rejects a bad signature with code 1003). Replayed to the
   real `cniot.roborock.com`, our robot's exact request got
   `{"code":200,"msg":"success","data":null}` — "nothing pending for this
   device". The robot sends bursts of 3 polls (~20 s apart) at each wake;
   43 so far, all answered by the catch-all and tolerated. A faithful local
   answer is a small server patch if we ever need it.
4. **Which difference makes this China unit work?** No internet, no prior
   cloud history, factory firmware, China settings everywhere, or the
   model itself. The `/location` IP-geolocation finding above strengthens
   "no internet" as the leading candidate. Isolating it would mean giving
   the robot internet access, which conflicts with the no-escape rule, so
   it stays deliberately untested.
5. **Camera video.** **Solved (30 Oct 01:18) — live view works.** The
   firmware's mangled relay host (`rr.roborock.labiot.roborock.com`,
   from the 15-character `token.r` truncation) is covered by a SAN on
   the forged cert, and the front-end's `--camera` mode forwards
   `POST /fwapi/createca` to the stack's `fw_createca` route (the robot
   accepted the TURN credentials via its own signed request at
   01:18:25). Viewer: go2rtc v1.9.15 (`compose.bridge.yaml` service
   `go2rtc`) with a `roborock://` source — first live frames on the
   first try, 960x540, while charging. The reported snags (charging
   refusal `-10011`, strict WebRTC offers) did not bite. No short
   domain was ever needed; the candidates (`swu.dev`, `swu.io`, …)
   are moot. Setup: README "Camera live view".
6. **Command formats.** Mostly recovered (30 Oct) by decompiling the app's
   React Native vacuum plugin; see COMMANDS.md, "Recovered from the
   decompiled app plugin". Verified live: `app_get_robot_setting` (six
   valid setting names on this robot) and `name_multi_map`. Recovered but
   untested: the six zone/scene setters' shapes. Still unknown: the
   encrypted `play_audio` payload, `get_dynamic_data` streaming details,
   clearing `set_ignore_identify_area`, `app_update_unsave_map`, `get_map`.

## Bringing up the commands (29 Oct evening)

- **Map saving was off.** `lab_status` is Roborock's "persistent maps"
  switch; with it off, no map, room or obstacle photo was ever kept.
- **The map-edit wrapper** (from LocalRock's captures of the official app)
  unlocked room naming, per-room modes, merging and splitting.
- **The robot's own error messages** gave away schedule, PIN and photo-bit
  formats in a few probes each.
- **Photos** need map saving and photo saving on, both when taken and when
  downloaded.
- **English voice** from the Qrevo Slim's public pack.
- **Room names** live in the local server's home data; the robot only
  stores segment → server room ID.

## What went wrong along the way

- **The first network lockdown broke Docker** (it flushed the whole NAT
  table); rewritten to touch only the vacuum's interface.
- **Then the firewall dropped the vacuum's connections to the server**
  (Docker publishes ports through forwarding); one rule fixed it.
- **A junk-parameter probe hung the robot.** A power cycle didn't fix it;
  a Wi-Fi reset and re-pairing did, with nothing lost. Probe one command
  at a time with realistic values.
- **Some setters accept garbage** (`set_dnd_timer_actions`). Always read back.
- **The map channel hides errors** as timeouts; the real reply is in the
  server log.
- **I miscounted the real domain's length** (the firmware keeps the first
  15 characters of the `token.r` host), so the domain move didn't unblock
  the camera; everything else works on the local name.
- **A packet capture was overwritten** by re-running the AP setup; captures
  now rotate and are never overwritten.
- **A Cloudflare API token was pasted into a chat** during setup; it has
  since been rotated.

## Credits

- [local_roborock_server](https://github.com/Python-roborock/local_roborock_server):
  the cloud emulator, key recovery, grant forging, TURN relay; its issue
  tracker for camera and `rrcheck` findings.
- [python-roborock](https://github.com/Python-roborock/python-roborock):
  protocol, commands, map parser, photo download.
- [LocalRock](https://github.com/DonSidro/LocalRock): captured app
  payloads (map edits, per-room modes, camera, PIN).
- [Valetudo](https://github.com/Hypfer/Valetudo) and Dennis Giese's
  research: merge/split formats, voice packs, firmware background.
- The decompiled official app (`~/homelab/roborock-re/jadx_out/`):
  Bluetooth pairing, map and photo parameters.
