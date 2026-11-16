# Vacuum web page — spec

Agreed 2026-10-30. A web page for the household to control the P20 from
anywhere: `vacuum.example.com`, behind Cloudflare Access like the other
subdomains. English only. Everyone who passes Access gets every feature,
camera and driving included.

## Principles

- **The map is the main control.** Nobody types coordinates: tap rooms,
  drag zones, tap-and-drag "go here, face this way".
- **Never trust "ok".** The robot acknowledges commands it then ignores.
  Every action shows what actually happened: started, resent, refused (with
  the reason, e.g. "dock refuses to empty again so soon").
- **One robot, many viewers.** Everyone sees the same live state; commands
  are sent one at a time.
- **Rooms are named in the page** (`rooms name`), not in the CLI.

## Screens

1. **Home** — live state in words ("Washing mop → Cleaning room 3 →
   Returning"), battery, current room, minutes/m² this run, errors in plain
   words. Buttons: Clean all, Pause/Resume, Stop, Dock. Dock row: Empty,
   Wash mop, Dryer on/off.
2. **Map** — live map: robot, dock, named rooms, no-go zones, obstacles.
   Tap rooms → Clean (passes, suction/water/route). Drag a rectangle → zone
   clean. Tap a point + drag an arrow → go there and face that way. Tap an
   obstacle → its photo. Edit mode: rename rooms, cleaning order, no-go
   zones and walls, merge/split (confirmed).
3. **Camera** — live view (robot mic muted by default), snapshot, nudges
   (forward/back 20 cm, turn ±45°) that say "blocked (rug?)" when a move
   doesn't register; shortcut to the map's go-here-and-face.
4. **Photos** — obstacle photos with label, time and map pin; photo-saving
   toggle.
5. **History** — readable runs (end reason decoded, dock-only events
   hidden); tap → map of that run with path and obstacles.
6. **Settings** — default and per-room modes, schedules, do-not-disturb,
   volume, consumables with confirmed reset.

Across all screens: confirmations before destructive actions (clear no-go
zones, re-divide/merge/split rooms, consumable reset); installable on phones
(PWA); an **activity log** ("<who> started Kitchen at 10:02", identity from
the Cloudflare Access login).

**Not included:** raw commands, firmware, voice packs, new-map building,
a full joystick.

## Status and deployment

Phase 1 is built (`web/`): `server.py` (aiohttp; one robot link, status every
2 s, map every 5 s while out / 60 s docked, actions with started/ignored
confirmation, activity log in `web/data/`) and `static/` (no build step).
Tests: `tests/test_web.py`.

Run locally: `VACUUM_WEB_DEV=1 uv run web/server.py` → http://127.0.0.1:8094
(dev mode skips the Access check; the server refuses to start without
`CF_ACCESS_TEAM` + `CF_ACCESS_AUD` otherwise).

Tailnet-only (before Cloudflare): `VACUUM_WEB_TAILNET=1` behind
`sudo tailscale serve --bg --https=8443 http://127.0.0.1:8094`, giving
https://my-desktop.tailnet.ts.net:8443 (identity from
Tailscale-User-Login). Never port 443 there: that one is Funnel (public).
As a service: `web/deploy/tailnet.conf` as a drop-in for the unit below.

**Live since 3 Nov 2026** at https://vacuum.example.com: Access app
"Vacuum" (Family policy), user service `vacuum-web`, nginx block in
`~/homelab/network/cloudflare-tunnel`, ingress in
`~/homelab/forge/cloudflared-config.yml` (both installed by
`~/homelab/network/deploy.sh`), proxied DNS CNAME to the tunnel. The
server also verifies the Access JWT itself, so a request that skips
Access gets 403.

Go-live steps, for a rebuild (the page must never be reachable without Access):
1. Cloudflare Zero Trust → Access → Applications: add `vacuum.example.com`
   with the same policy as the other subdomains; copy its Audience (AUD) tag.
2. `web/deploy/vacuum-web.service` → `~/.config/systemd/user/`, set
   `CF_ACCESS_AUD`, then `systemctl --user daemon-reload && systemctl --user enable --now vacuum-web`.
3. `web/deploy/nginx-vacuum.conf` → nginx (`sudo nginx -t && sudo systemctl reload nginx`).
4. `web/deploy/cloudflared-ingress.yml` → `~/homelab/forge/cloudflared-config.yml`,
   add the DNS route, restart cloudflared.

**Camera stream:** the page plays `a184_web`, which go2rtc re-encodes from
`a184` with ffmpeg (`.tools/go2rtc.yaml`; ~4% of a core while watched). The
robot's raw H.264 mixes in slices of a second 128x96 stream whose parameter
set it never sends; WebRTC decoders (upstream's tested path) shrug that off,
but go2rtc's MSE packaging sometimes builds its header from the 128x96 set,
and Safari then fails to decode. The re-encode is one clean 960x540 stream.

**Camera (3 Nov):** the Camera tab plays go2rtc's MSE stream through
`/api/camera/ws` in `server.py`, which fixes the stream to `a184` and passes
only the browser's `mse` request on. The stream runs only while the tab is
open; viewers are noted in Activity (at most once per 10 min each). Video
only: the stream lists an AAC track the robot never fills, and MSE won't
play until every track has data. The robot's stream starts at 128x96 and switches
to 960x540 mid-stream, which kills Safari's player; the page reconnects on a
player error (a fresh connection starts at full size). For its first minute
the player logs `camera diag` lines to the vacuum-web journal.
`CameraKeeper` holds one go2rtc consumer while anyone watches (+20 s), so a
reconnect joins the running stream instead of restarting the robot's camera;
it spaces robot camera starts by 10 s, waits 2.5 s after the first video so
viewers usually skip the size switch, and restarts go2rtc (`/api/restart`)
when no video arrives in 25 s (go2rtc got stuck after a robot reboot on
3 Nov). The robot rebooted once (3 Nov 16:04) as a camera session started
right after it docked with an empty water tank; cause not certain.

Dock problems (`dock_error_status`, explained by `vacuum.DOCK_ERRORS`) show in
a banner above every tab, e.g. "Clean water tank is empty" with what to do. nginx
needs the WebSocket block in `nginx-vacuum.conf`. go2rtc itself listens on
127.0.0.1 only.

**Map tools (3 Nov):** picking Rooms or Zone changes the map at once. Rooms
shows a tappable pill per room on the map plus a row of room buttons; Zone
dims the map around a ready-made 1.5 m box on the robot, with corner handles
(drag to move or resize, tap to move it there). The map is sized to fit
above the action panel, so every room stays reachable.

## Build order

1. Home + map with room/zone cleaning + room naming + activity log.
2. Camera, go-here-and-face, nudges, photos; **Pushover notifications**
   (run finished, robot stuck/error, dock needs attention) via the existing
   `forgejo-pushover` container.
3. History, settings, schedules, no-go zone editing.
4. Later: Apple Home (Scrypted for the camera).

## Constraints learned on the robot (see COMMANDS.md)

- Room/zone/spot cleans start with a ~2.5 min mop wash; show it as progress.
- `dock` right after `stop` can be dropped: resend once.
- Manual moves need strictly increasing seqnums; remote-control mode ends
  by itself after a while; the robot won't drive onto a rug with the mop on.
- The dock refuses repeat emptying soon after the last one (it says so
  aloud only).
- Live video: WebRTC can't cross the Cloudflare tunnel; use go2rtc's
  MSE/WebSocket stream, and expose only the `a184` stream + snapshot,
  never go2rtc's full API.
