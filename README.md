# Roborock P20 (a184) — local control

Our own Roborock P20 (`roborock.vacuum.a184`, mainland-China market), running
**fully local**: no Roborock account, no vendor cloud, no WAN path from the
vacuum's network — every endpoint it reaches terminates on this machine.
The product of this repo is a small CLI over that: `vacuum.py` and friends.

How it came together, with evidence and open questions: `docs/WRITEUP.md`.

## Layout

| Path | What |
|---|---|
| `vacuum.py` | Day-to-day control over the direct TCP link (status, start, dock, any raw command) |
| `vacuum_map.py` | Maps, cleaning history, obstacle photos (through the local stack's MQTT) |
| `COMMANDS.md` | **Every command tested on this vacuum**: params, results, what's not available |
| `ops/` | Scheduled health check (`rrcheck-check.sh` → `check_rrcheck.py`), capture decoder (`decode_local.py`), hosts entry (`add_hosts.sh`), unit installer (`install-units.sh`) |
| `provisioning/` | Pairing tools, used only to (re)provision: `rr_provision.py` (BLE client), `ble_scan.py`, `PROTOCOL.md` |
| `setup_ap_concurrent.sh`, `teardown_ap_concurrent.sh`, `ap_logs/` | Isolated Wi-Fi AP, plus captures of everything the vacuum sends |
| `local_roborock_server/` | The "cloud": HTTPS API, MQTT broker, TURN relay — docker compose (nested git repo: upstream + local patches) |
| `lab.env` (gitignored; `lab.env.example` documents every key) | All install-specific values: robot host/key/duid, AP subnet, Wi-Fi, stack hostname, protocol login — read by every tool (`labconfig.py`) |
| `stack/` | Tracked copies of what the gitignored nested repo carries: compose overlay, `config.example.toml`, `go2rtc.example.yaml`, the PR #199 patch |
| `apicn/` | The forged TLS chain: minter `mint_leaf.py` + the public chain certs (`inter-cross.pem`, `aaa-root.pem` — tracked); the minted leaf/key are gitignored |
| `.tools/go2rtc*` | go2rtc camera viewer (binary + config; gitignored — config holds credentials) |
| `docs/WRITEUP.md` | Research history, evidence, open questions |
| `logs/`, `maps/`, `voice/` | Decoded-link logs; downloaded maps/photos; English voice pack |

## Daily use

One tool, no JSON. `uv run vacuum.py --help` lists everything.

```
uv run vacuum.py status                      # state, battery, modes, errors
uv run vacuum.py where                       # position, heading, room, distance to dock
uv run vacuum.py clean 3                     # returns once it has visibly started
uv run vacuum.py clean 3 --wait              # …or follow it all the way back to the dock
# Action commands confirm the robot actually started (it can say "ok" and do
# nothing) and report it if not; --no-wait returns at once. Ctrl-C only stops watching.
uv run vacuum.py clean                       # whole home
uv run vacuum.py clean "Living Room" -r 2    # rooms by name or segment id
uv run vacuum.py zone 24300 25600 25300 26600
uv run vacuum.py pause | resume | stop | dock | find | spot | goto X Y
uv run vacuum.py goto X Y --face 90            # go there, then turn to a map heading
uv run vacuum.py drive forward 0.3 | back 0.2 | left 45 | right 90 | face -90
uv run vacuum.py drive end                     # leave remote-control mode
uv run vacuum.py mode --suction turbo --water low --route deep
uv run vacuum.py volume 60
uv run vacuum.py dnd 22:00 08:00             # or: dnd off
uv run vacuum.py empty | wash | wash stop | dry off
uv run vacuum.py consumables --reset sensor_dirty_time

uv run vacuum.py rooms                       # names, order, per-room modes
uv run vacuum.py rooms name 2 Kitchen        # names live on the local server
uv run vacuum.py rooms order 2 1
uv run vacuum.py rooms mode 2 --suction max --water off
uv run vacuum.py rooms merge 1 2 | rooms split 1 X1 Y1 X2 Y2 | rooms resegment
uv run vacuum.py nogo add X0 Y0 X1 Y1 [--no-mop] | nogo wall … | nogo clear

uv run vacuum.py map                         # maps/current.png
uv run vacuum.py history [--all]             # past runs (--all adds dock-only events)
uv run vacuum.py photos on|off               # obstacle photo saving
uv run vacuum.py record <id>                 # map of a past run
uv run vacuum.py photo <photo id>            # obstacle photo (photo saving on)

uv run vacuum.py schedule add 09:00 --days weekdays
uv run vacuum.py schedule | schedule off <id> | schedule del <id>

uv run vacuum.py voice                       # active voice pack
uv run vacuum.py voice install voice/en-coral.pkg   # (re)install English

uv run vacuum.py cmd get_consumable          # any raw command
```

Coordinates are map millimetres (the dock is near 26300, 25600); read
them off `vacuum.py map` output or a room's bounds. Per-room modes apply
when the global mode is `custom`. Schedules clean the whole home; room
schedules aren't wired up yet. Naming a room with a **new** name creates
it on the server (untested so far); existing names are reused.
Formats behind every command: `COMMANDS.md`.

Library: `from vacuum import Vacuum` (async context manager, `.status()`,
`.start()`, `.cmd(method, params)` for anything else).

Device identity and every address (robot host, localKey, duid, AP subnet,
stack hostname, protocol login) live in `lab.env` — gitignored, with every
key documented in `lab.env.example`. This unit: firmware 02.22.56, model
`roborock.vacuum.a184` (mainland-CN market).

## Architecture

```
                    BLE (pairing mode)                      WAN-blocked LAN
 ┌──────────┐   provisioning/rr_provision.py    ┌────────┐   DHCP/DNS (dnsmasq)   ┌──────────────┐
 │ Vacuum   │◄────────────────────────────────►│ uap0   │◄───────────────────────►│ 192.168.77.1│
 │ a184     │  wifi creds + "cloud" token       │ AP     │  all names resolve to  │  this        │
 └────┬─────┘                                   │ ch9/11 │  .1, zero egress       │  desktop    │
      │                                         └────────┘                         └──────┬──────┘
      │ TCP :58867 (V1 local protocol)                                               │
      └───────────────────── vacuum.py ──────────────────────────────────────────────┤
                                                                                     │
                                          local_roborock_server (docker) ────────────┘
                                          api-rr.roborock.lab:555 (forged chain)
                                          MQTT TLS :8881 — the "CN cloud"
```

## Porting to your own robot

Everything install-specific lives in one file: `lab.env` (gitignored;
`lab.env.example` documents every key; `labconfig.py` loads it for the
Python tools, the ops scripts source it). A second install:

1. Prereqs: a Linux host whose Wi-Fi card can hold a concurrent AP
   (`setup_ap_concurrent.sh` builds it on a virtual interface beside
   your station link), Docker, `uv`, and a Roborock speaking the V1
   local protocol. Built and tested against the mainland-CN P20 (a184).
2. `cp lab.env.example lab.env`; fill in Wi-Fi, interfaces and AP
   address now — the robot's identity fields come out of step 6.
3. **TLS is the hard part — but the materials are in this repo.** The
   vacuum dials vendor hostnames over TLS with a frozen factory trust
   store; the stack must present a leaf those old roots accept. That
   capability ships here: the eMudhra intermediate scalar from the 2025
   disclosure is in `apicn/mint_leaf.py`, and the public chain certs it
   signs against are tracked (`apicn/inter-cross.pem`,
   `apicn/aaa-root.pem`). Mint your leaf with no arguments (SANs come
   from lab.env):
   `uv run apicn/mint_leaf.py`, then
   `cp apicn/leaf-chain.pem local_roborock_server/data/certs/fullchain.pem`
   and `cp apicn/leaf.key local_roborock_server/data/certs/privkey.pem`.
   Method and evidence: `docs/WRITEUP.md`. Two things to know: the chain
   expires end-2028 (the cross-sign and the AAA root both die 31 Dec
   2028), and minting/serving certificates for names you don't own is
   your judgment call, not this repo's.
4. Stack: clone upstream `local_roborock_server` (the templates in
   `stack/` show exactly what this repo adds on top):
   - apply `stack/patches-pr199.diff` (rrcheck renewal route; upstream
     PR #199 — re-apply after updates until it merges),
   - run its `configure` wizard for config.toml + secrets, then apply
     the deltas marked in `stack/config.example.toml` (`stack_fqdn` =
     `STACK_HOST`, ports = `STACK_PORT`/`MQTT_PORT`, `tls.mode` =
     `"provided"` with your minted chain in `data/certs/`, `[turn]`
     `host` = `AP_IP`),
   - copy `stack/compose.local.yaml` in and provide `AP_IP` (a `.env`
     next to compose.yaml or the environment),
   - `cp stack/go2rtc.example.yaml .tools/go2rtc.yaml` (fill after step 6).
5. Host names: `sudo bash ops/add_hosts.sh` (writes the /etc/hosts line;
   the vacuum's own DNS is the AP's dnsmasq).
6. Bring up AP + stack: `sudo bash setup_ap_concurrent.sh` (refuses 5 GHz —
   the a184 only joins 2.4; pin your home Wi-Fi band), then
   `sudo bash ops/install-units.sh` (boot unit) and, without sudo, again
   for the rrcheck user units.
7. Pair the robot over BLE (power-cycle it first):
   `uv run provisioning/ble_scan.py`, then
   `uv run provisioning/rr_provision.py <addr> --name … \
   --ssid "$WIFI_SSID" --passwd "$WIFI_PASS" --stack-server "$TOKEN_HOST/"`
   (protocol: `provisioning/PROTOCOL.md`). Once it joins: `VACUUM_HOST`
   is its dnsmasq lease; `VACUUM_DUID`/`VACUUM_KEY` come from the
   onboarding session (`.cloud_session.json`); the `rriot` `u`/`s`/`k`
   go into `.tools/go2rtc.yaml`.
8. Verify: the offline unit tests (`uv run --with pytest --with
   python-roborock --with aiohttp --with 'pyjwt[crypto]' pytest tests/`),
   then `uv run tests/check_local.py` — live, end-to-end, exit 1 on any
   FAIL.
9. Web page (optional): spec in `docs/WEB.md`; the deploy templates in
   `web/deploy/` carry placeholders (paths, Cloudflare Access tags) —
   fill in yours.

## Operations

**AP at boot**: the system unit (rendered from `ops/systemd/roborock-ap.service.in`) waits
for wlo1 to connect, runs `setup_ap_concurrent.sh`, then starts the stack.
The stack cannot start without the AP: it binds `${AP_IP}:443`, and
Docker does not retry a container that failed to start at boot. The AP
uses wlo1's channel, so the home Wi-Fi profile must stay pinned to 2.4 GHz
(`802-11-wireless.band bg`); the setup script refuses to start on 5 GHz.
Give the home profile a higher `connection.autoconnect-priority` (10) than
any other saved Wi-Fi (for example a phone hotspot), or NetworkManager may
pick that one at boot. If the start fails (wlo1 slow to connect, or on
5 GHz), the unit retries every minute. Stop the AP before you change the
band or reconnect wlo1: while uap0 holds a channel, wlo1 cannot join on
another one and fails with `ssid-not-found`.
Install once (and again after editing the unit template — the installer
renders `ops/systemd/*.service.in` with this repo's path and lab.env's
interface names):
```
sudo bash ops/install-units.sh         # system AP unit (+ daemon-reload)
bash ops/install-units.sh               # rrcheck user units, same script
sudo systemctl enable roborock-ap.service   # start at boot
```

**AP down/up by hand** (prefer `sudo systemctl restart|stop roborock-ap`):
```
sudo bash setup_ap_concurrent.sh       # bring up uap0 + dnsmasq + capture + lockdown
sudo bash teardown_ap_concurrent.sh   # remove it all
```

**Unit tests**: `uv run --with pytest --with python-roborock --with aiohttp --with 'pyjwt[crypto]' pytest tests/` —
`vacuum.py` against a fake robot that records every command; checks the
exact method/params for each CLI command and that bad input (times, days,
repeats, zero-area zones, consumable names, JSON) is refused before
anything is sent. No robot or server needed; run after any edit.

**Web page** (`web/`, spec `docs/WEB.md`): household control at
`vacuum.example.com` behind Cloudflare Access. Locally:
`VACUUM_WEB_DEV=1 uv run web/server.py` → http://127.0.0.1:8094.
Go-live steps are in `docs/WEB.md`.

**Local checks**: `uv run tests/check_local.py` — every piece of the
frontend, from this machine: name resolution (host + dnsmasq), the forged
leaf's SANs/expiry, TLS on `:555`/`:8881`/`:443`, the stack's routes
(including the camera relay bootstrap emulated as the firmware dials it),
admin UI, the protocol-login session, cross-file credential sync, MQTT auth,
TURN, the go2rtc viewer (leaf pinned as its trust anchor — Go rejects the
chain's SHA-1-signed 2004 root), RTSP, the systemd timer; plus, when the
vacuum is online, the direct command link and a live camera frame. Checks
that need the robot SKIP while it's offline; exit 1 on any FAIL.

**Stack** (auto-starts on boot via docker `unless-stopped`; one compose
project owns **everything** the vacuum talks to):
```
cd local_roborock_server
docker compose ps                     # health
docker compose logs -f roborock-local-server
# Always (re)create the stack with the local overlay (443 publish + TURN + bridge):
docker compose -f compose.yaml -f compose.local.yaml up -d
```
`compose.local.yaml` merges what used to be three overlays:
- `${AP_IP}:443 → :555` — **required**: `token.r` is `$TOKEN_HOST/`
  (no port), so the robot's first API calls go to `$STACK_HOST:443`;
  the stack's forged leaf (minted by `apicn/mint_leaf.py`; method:
  `docs/WRITEUP.md`) covers every name it dials.
- TURN relay ports (UDP 3478, 49160–49179) — camera only, harmless otherwise.
- **`go2rtc`** — camera viewer and the **standard consumer surface**: Web
  UI / HTTP API `:1984`, RTSP `:8554`, WebRTC `:8555` (stream `a184`),
  all bound to 127.0.0.1 since 3 Nov (no auth, and its API can edit
  streams). Watch remotely through the web page's Camera tab; for the
  go2rtc UI itself, `ssh -L 1984:127.0.0.1:1984` to this machine.
  Home Assistant consumes these; nothing outside the project needs to know
  how the vacuum is reached. Config: `.tools/go2rtc.yaml` (gitignored —
  holds `rriot` credentials).
- **Local patch: PR #199** (applied 2026-10-29, not yet merged upstream;
  tracked copy: `stack/patches-pr199.diff`). The a184 renews its
  `rrcheck` access grant with `GET /devices/{did}/sh`; unpatched, that hits
  the catch-all, and other models then refuse every command with `-10002`
  ~48 h after onboarding. With the patch the server forges a valid grant.
  Re-apply after updating the server (`git apply patches-pr199.diff`, then
  rebuild with the compose command above) until upstream merges it. Check
  that renewals land: the server log should show `GET /devices/…/sh …
  route=fw_check` (expected ~daily).
- **Scheduled checks** (systemd user timer `roborock-rrcheck.timer`, runs
  `ops/rrcheck-check.sh` → `check_rrcheck.py`): daily at 18:13, after the
  robot's daily renewal (~19:58 UTC). The 48 h check (31 Oct 23:47) passed.
  Each run checks that commands still work, that the vacuum is connected to
  the local broker (a map request, answered only as an MQTT push; exit 3 if
  not), and lists the vacuum's renewal requests and which handler answered
  them; result in
  `logs/rrcheck-check.log`, the journal (`journalctl --user -t
  roborock-rrcheck`), and, only on failure, a high-priority Pushover
  message via the `forgejo-pushover` container. Run it any time with
  `systemctl --user start roborock-rrcheck.service`.
- **Stack up but unreachable** (vacuum reconnects to Wi-Fi every ~5 min,
  `ap_logs/dnsmasq.log` shows a lookup of `api-rr.roborock.lab` every second,
  `docker port` shows nothing for the server): its container runs without
  its published ports. `bash ops/recreate-stack.sh` recreates it (seen after
  the 2026-11-13 reboot; the boot unit now uses `--force-recreate`).
- **Hostnames**: the stack is `$STACK_HOST` (lab.env; here
  `api-rr.roborock.lab`) — a portable label, not a registered domain;
  nothing resolves it off this machine. TLS on every
  robot-facing port is the forged chain minted by `apicn/mint_leaf.py`
  (method: `docs/WRITEUP.md`; this leaf is valid to 2029 — re-mint any
  time with `uv run apicn/mint_leaf.py`; the chain dies end-2028): the stack
  serves it itself on `:443`/`:555`/`:8881` (`tls.mode = "provided"` — no
  ACME, no Cloudflare, no renewal job; `:443` via the `compose.local.yaml`
  publish; the old `apicn` front-end and its iptables REDIRECT were retired
  30 Oct — `ops/drop_legacy_redirect.sh`). A real registered domain (ZeroSSL
  cert) was briefly used, then dropped 30 Oct in favour of the local
  `*.roborock.lab` labels. This machine resolves the names via
  `/etc/hosts` (`ops/add_hosts.sh`); the vacuum via dnsmasq. Earlier
  configs: `config.toml.bak-*`.

Admin: `https://$STACK_HOST:555/admin` — the admin username and the
protocol-login email/PIN are install secrets kept in `lab.env` (they're
set in the stack's config.toml at setup time; the protocol login is used
by Home Assistant/LocalRock, not by vacuum.py).

**Re-provisioning** (e.g. after a wifi change — power-cycle first, the BLE
stack needs it between sessions):
```
# hold 回充 + 局部清扫 ~3s until the (Chinese) announcement
uv run provisioning/ble_scan.py          # find rr-a184… address
uv run provisioning/rr_provision.py <addr> --name 'rr-a184…' \
    --ssid "$WIFI_SSID" --passwd "$WIFI_PASS" --stack-server "$TOKEN_HOST/"
# first BLE attempt often times out — just re-run
```

## Camera live view

Works end-to-end (brought up 30 Oct; narrative in `docs/WRITEUP.md`).

1. **TURN relay** in the server (`[turn] mode = "provided"`,
   `turn:$AP_IP:3478`); allocation test passed.
2. **Remote view authorized** on the robot: `camera_status` 641 → 8869.
   Sequence: `set_camera_status [641|0x20]`, then `[…|0x1000]` (request),
   then press **Power** on the robot within ~60 s; the robot clears bit 12
   and sets bits 2 (authorization, write-protected) and 13 itself. Without
   it: `-10012 camera status denied preview`. Survives re-pairing, and
   works while charging (this model doesn't refuse with `-10011`).
3. **Remote-view PIN** (pick one; recorded in lab.env, passed to go2rtc):
   `set_homesec_password {"old_password": "", "new_password": md5(pin), "enable_password": 1}`;
   `check_homesec_password {"password": md5(pin)}` → `["ok"]`.
4. **Relay bootstrap**, answered by the stack on `:443`: on preview start the
   firmware fetches TURN credentials from
   `https://<first 15 chars of token.r>iot.roborock.com/fwapi/createca`
   (with our `token.r` that host is `rr.roborock.labiot.roborock.com`);
   the forged leaf covers it and the stack's `fw_createca` route answers.
   Verify in the server log (`docker compose logs roborock-local-server`):
   `POST …/fwapi/createca … route=fw_createca`.
5. **Viewer: go2rtc** (`go2rtc` service, stream `a184`, config
   `.tools/go2rtc.yaml`; `u`/`s`/`k` from the `rriot` block of
   `.cloud_session.json` — **update after every re-onboarding, they
   rotate**; `tests/check_local.py` `go2rtc_creds` catches staleness):
   `roborock://$STACK_HOST:8881?u=<rriot.u>&s=<rriot.s>&k=<rriot.k>&did=$VACUUM_DUID&key=$VACUUM_KEY&pin=<remote-view PIN>` (lab.env values; template: `stack/go2rtc.example.yaml`).
   Its container pins our leaf (`SSL_CERT_FILE`, compose.local.yaml) — Go
   rejects the chain's SHA-1-signed 2004 root, so the leaf is the anchor.
   Snapshot check: `curl http://127.0.0.1:1984/api/frame.jpeg?src=a184`.
   Optional: into Scrypted (`~/homelab/scrypted`) via the RTSP plugin
   pointed at go2rtc's restream, and on to HomeKit.

## Voice (English)

The robot speaks **English** (installed 2026-10-29; it shipped with only the
Chinese voice, `sid` 1, and no English pack exists for the a184 — the
Qrevo Slim's (`coral`, 2024) official English pack installs and works):

- Source: `https://awsde0.fds.api.xiaomi.com/app/coral/voice-pkg/package/en.pkg`
  (public, 6,026,260 bytes, md5 `71a13ad02c2800227f3c6d1a3ef56436`); a
  copy is in `voice/en-coral.pkg`.
- Install: `uv run vacuum.py voice install voice/en-coral.pkg`. It serves
  the file on the AP address for the robot, sends
  `dnld_install_sound {"url", "md5", "sid": 3}` and waits for
  `get_sound_progress` state 3. `get_current_sound` → `sid_in_use: 3`.
- Other models' packs: `https://awsde0.fds.api.xiaomi.com/app/<codename>/voice-pkg/info`
  lists them (e.g. `ultron` = S8). The a184's own packs come only from
  Roborock's cloud (`GET <cn|us|eu>iot.roborock.com/api/v1/voicePackages?sn=…`).
- Back to Chinese: no command found. `{"sid": 1, "default": "prc"}` fails
  (download error). A factory reset should restore the built-in Chinese
  voice (installed packs live on the writable data partition); the
  firmware is factory 02.22.56, so a reset doesn't downgrade it. Then
  re-provision and reapply settings (map saving, camera bits, PIN).

## Quirks learned the hard way

- The a184 is **2.4 GHz-only for wifi join** (its scan sees 5 GHz names,
  but it wouldn't associate on our 5 GHz cell; softAP fallback is also 2.4).
- BLE provisioning: first connect after power-cycle often times out on
  hello; retry. Chunked writes need ~20 ms pacing.
- The dial-to-cloud timer is ~172 s after a config-apply event, not after
  boot. Activation needed 3 pairing cycles total (samples → key recovery).
- Ports 54321 (miIO) and 80 never open. **58867 opens only after the
  device is cloud-connected** — scan again after activation, not before.
- `rrcheck` (the region lock's command gate) does not gate the local
  protocol. Commands work directly.

## Logs: everything the vacuum sends and receives

| What | Where | Readable? | Retention |
|---|---|---|---|
| Every packet on its network (full size) | `ap_logs/capture-<run>.pcapNN` (tcpdump on `uap0`) | Raw; TLS and the direct link are encrypted | Ring of 20 x 100 MB per AP run; files untouched for 90 days are deleted at the next AP setup |
| Direct commands (TCP 58867), decoded | `logs/decoded_local.jsonl` via `uv run ops/decode_local.py [--since 2026-10-30T09:00] [--print]` | Yes: requests and replies, decrypted with the local key | Regenerated from the captures on demand |
| HTTPS API calls, decoded | `local_roborock_server/data/runtime/decompiled_http.jsonl` | Yes | 50 MB x 10 files (`[logging]` in config.toml) |
| MQTT messages, decoded | `local_roborock_server/data/runtime/decompiled_mqtt.jsonl` | Yes | 50 MB x 10 files |
| DNS lookups, Wi-Fi joins | `ap_logs/dnsmasq.log`, `ap_logs/hostapd.log` | Yes | Unbounded (small) |

## Background

- `docs/WRITEUP.md` — how the local stack was brought up, what the logs
  showed, what's still open; credits and prior art there.
- `provisioning/PROTOCOL.md` — the BLE pairing protocol, GATT UUIDs,
  framing, handshake, config JSON schema.
- Decompiled-app research artifacts (jadx output, APK, tooling) live
  outside the repo at `~/homelab/roborock-re/`.