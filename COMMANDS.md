# Roborock P20 (a184) command reference

Everything here was tested on this vacuum on 2026-10-29: firmware
02.22.56, BOM `A.03.0619`, China (PRC) locale, running fully local
against `local_roborock_server`. Value meanings marked *(lib)* come from
python-roborock (`roborock/data/v1/`) and weren't confirmed by changing
state. Formats marked *(LocalRock)* or *(Valetudo)* came from those
projects' code and were then confirmed on this vacuum.

## At a glance

| | Count |
|---|---|
| **Working and confirmed** | ~175 |
| Format known from research, not yet tried | ~15 |
| Exists, but no working format found | ~12 |
| Live-video session commands, not yet tried individually (video itself works via go2rtc) | ~12 |
| Not on this model | ~27 |
| Out of scope (cloud, firmware, logs) or destructive | ~15 |

Per-command details are in [Not available](#not-available).

## How to send commands

| What | Tool | Transport |
|---|---|---|
| Normal command | `uv run vacuum.py cmd <method> '<json params>'` | Direct TCP to port 58867 |
| Map, saved map, photo | `uv run vacuum_map.py …` (`CloudVacuum` in Python) | MQTT through the local stack; the reply comes back as an encrypted protocol-301 push |

Errors:

| Reply | Meaning |
|---|---|
| "method not recognized" | Not on this model |
| `-10005 …` | Wrong param **shape**. The message often says what it wanted ("Param is not an array", "data should be json") |
| `-10007 …` | Right shape, bad **value**, or blocked ("invalid params", "The Lab Status is disable", "Not FCC robot", "robot is in motion") |
| `["retry"]` | Map-edit command accepted and being applied (see [Map editing](#map-and-room-editing)) |

Units: times in **seconds**, areas in **mm²** (÷1,000,000 for m²), map
coordinates in **mm**, timestamps in **Unix seconds** (these are also the
cleaning-record IDs).

### Things that bite

- **Don't probe with junk params.** A batch of commands sent with `"x"` hung
  the vacuum's command handler. A power cycle didn't fix it; a Wi-Fi reset
  plus `provisioning/rr_provision.py` did (see [Recovery](#recovery-when-the-vacuum-stops-answering)).
  Test one command at a time with realistic values.
- **Some setters don't validate.** `set_dnd_timer_actions` accepts anything
  and stores garbage (`{"resume": "error"}`). Always read back.
- **The map channel drops error replies.** Map, photo and saved-map
  commands that fail look like a 10 s timeout. The real reply is in
  `docker compose logs` (`rpc_response … method=…`), or resend the command
  on `v1._mqtt_rpc_channel` to see it.
- **Camera settings can't change while the robot moves** ("robot is in motion").
- **Before the first run, the dock washes the mop** for about 2.5 minutes
  (state 23/26). After docking, it empties the bin (state 22).

---

## Cleaning

| Command | Params | Notes |
|---|---|---|
| `app_start` | none | Whole home (state 5) |
| `start_clean` | none | Same as `app_start` (pre-wash, then clean) |
| `do_scenes_app_start` | `[{"repeat": 1}]` | Whole home via the routine path (state 23 pre-wash) |
| `app_segment_clean` | `[{"segments": [2], "repeat": 1}]` | Rooms by segment ID (state 18) |
| `app_zoned_clean` | `[[x0, y0, x1, y1, repeat], …]` | Rectangles in map mm (state 17) |
| `app_spot` | none | Spot clean where it stands (state 11) |
| `app_goto_target` | `[x, y]` | Drive to a point (state 16) |
| `app_pause` | none | State 10 (3 after a spot clean) |
| `resume_segment_clean` / `resume_zoned_clean` | none | Back to 18 / 17 |
| `stop_segment_clean` / `stop_zoned_clean` / `stop_goto_target` / `app_stop` | none | State 3 idle |
| `app_charge` | none | Return to dock (6 → 8) |
| `app_rc_start` / `app_rc_end` | none | Remote-control mode (state 7) / exit. From the dock, the first move adds ~25 cm of undocking. Ends by itself after a while without moves (>16 s, <2 min); stops mop drying. `vacuum.py drive …` |
| `app_rc_move` | `[{"omega": 0.5, "velocity": 0.0, "duration": 1000, "seqnum": 1}]` | rad/s (+ = left), m/s (accurate off the dock), ms. Always answers `ok`, but **silently ignores** a move when not in state 7, or when `seqnum` isn't above every seqnum seen so far, even across `app_rc_end`/`app_rc_start`. `vacuum.py drive` uses tenths of a second since 2026-01-01. A new move while one runs extends it. Won't drive onto a rug with the mop attached |
| `app_rc_stop` | none | |
| `app_start_build_map` | none | Mapping-only run (state 29). Fails with "error max map count" if a map is already saved; `del_map` first |
| `find_me` | none | Locator sound |
| `set_clean_sequence` | `[2, 1]` | Room order (needs map saving on). `get_clean_sequence` reads it; `[]` resets |

Coordinates here: dock at (26303, 25598).

## Modes and settings

Every setter below was confirmed by writing the current value and
reading the getter again. To change something, send the same shape with
a new value.

| Getter → current | Setter (current value) | Meaning |
|---|---|---|
| `get_custom_mode` → `[102]` | `set_custom_mode [102]` | Suction: 101 quiet · 102 balanced · 103 turbo · 104 max · 105 off (mop only) · 106 custom · 108 max+ · 110 smart *(lib)* |
| `get_water_box_custom_mode` → `{water_box_mode: 202, distance_off: 0}` | `set_water_box_custom_mode {"water_box_mode": 202, "distance_off": 0}` | Water: 200 off · 201 low · 202 medium · 203 high · 204 custom · 209 smart *(lib)* |
| `get_clean_motor_mode` → `[{fan_power, water_box_mode, mop_mode, distance_off}]` | `set_clean_motor_mode [{"fan_power": 102, "water_box_mode": 202, "mop_mode": 300}]` | All three at once. `{106, 204, 302}` = "use per-room settings" |
| (`mop_mode` in status) → 300 | `set_mop_mode [300]` | Route: 300 standard · 301 deep · 302 custom · 304 fast · 306 smart *(lib)* |
| (`repeat` in status) → 1 | `set_clean_repeat_times {"repeat": 1}` | Passes per run |
| (`distance_off` in `get_water_box_custom_mode`) → 0 | `set_water_box_distance_off {"distance_off": 150}` | Custom water-flow distance, 85–230; only used by water mode 207. **Can't be set back to 0**: 0 is rejected, and the other setters ignore it |
| `get_carpet_mode` → `[{enable: 0, current_integral: 1000, current_high: 500, current_low: 1000, stall_time: 10}]` | `set_carpet_mode` (same object, in a list) | Carpet suction boost |
| `get_carpet_clean_mode` → `[{carpet_clean_mode: 1}]` | `set_carpet_clean_mode {"carpet_clean_mode": 1}` | Carpet behaviour while mopping |
| `app_get_carpet_deep_clean_status` → `{status: 0}` | `app_set_carpet_deep_clean_status {"status": 0}` | Carpet deep clean |
| `get_collision_avoid_status` → 1 | `set_collision_avoid_status {"status": 1}` | Obstacle avoidance |
| `get_identify_furniture_status` → 1 | `set_identify_furniture_status {"status": 1}` | Furniture recognition |
| `get_identify_ground_material_status` → 1 | `set_identify_ground_material_status {"status": 1}` | Floor-type recognition |
| `get_clean_follow_ground_material_status` → 0 | `set_clean_follow_ground_material_status {"status": 0}` | Clean along the floor direction |
| `get_dirty_object_detect_status` → 0 | `set_dirty_object_detect_status {"status": 0}` | Dirt detection |
| `get_handle_leak_water_status` → 0 | `set_handle_leak_water_status {"status": 0}` | Spill handling |
| `get_pet_supplies_deep_clean_status` → 0 | `set_pet_supplies_deep_clean_status {"status": 0}` | Extra care round pet bowls |
| `get_gap_deep_clean_status` → 0 | `set_gap_deep_clean_status {"status": 0}` | Deep clean along gaps |
| `get_right_brush_stretch_status` → 1 | `set_right_brush_stretch_status {"status": 1}` | Extending side brush |
| `get_stretch_tag_status` → 1 | `set_stretch_tag_status {"status": 1}` | Extending mop |
| `get_child_lock_status` → `{lock_status: 0}` | `set_child_lock_status {"lock_status": 0}` | Child lock |
| `get_led_status` → `[1]` | `set_led_status [1]` | Button LEDs |
| `get_sound_volume` → `[90]` | `change_sound_volume [90]` | Volume %. `test_sound_volume` plays a tone |
| `get_offline_map_status` → `{common_switch_status: 0}` | `set_offline_map_status {"status": 0}` | |
| (`switch_map_mode` in status) → 0 | `set_switch_map_mode {"mode": 0}` | Auto map switching |
| `get_timezone` → `["Asia/Shanghai"]` | `set_timezone ["Asia/Shanghai"]`, `set_app_timezone ["Asia/Shanghai"]` | |
| `get_camera_status` → `[641]` | `set_camera_status [641]` | Camera bits, see [Photos](#photos-and-camera) |
| (`lab_status` in status) → 1 | `set_lab_status [{"lab_status": 1}]` | **Map saving.** See [Maps](#maps) |
| — | `app_set_cross_carpet_cleaning_status {"status": 0}`, `app_set_dirty_replenish_clean_status {"status": 0}` | Accepted; no getter, so the effect isn't visible |

## Dock

| Command | Params | Notes |
|---|---|---|
| `app_start_collect_dust` / `app_stop_collect_dust` | none | Empty the bin (state 22, ~30 s). Answers `ok` but refuses when asked again soon after an emptying: the robot announces that emptying was requested too frequently and ignores it (seen ~10 min after the previous one) |
| `app_start_wash` / `app_stop_wash` | none | Wash the mop (state 23). Stopping starts drying |
| `app_set_dryer_status` | `{"status": 0}` | Stop or start drying now |
| `start_wash_then_charge` | none | Accepted; no visible effect while docked (probably for mid-run) |
| `get_dust_collection_switch_status` / `set_…` | `{"status": 1}` | Auto-empty after runs |
| `get_dust_collection_mode` / `set_…` | `{"mode": 0}` | 0 smart · 1 light · 2 balanced · 4 max *(lib)* |
| `get_wash_towel_mode` / `set_…` | `{"wash_mode": 10}` | Mop wash: 0 light · 1 balanced · 2 deep · 10 smart |
| `get_smart_wash_params` / `set_…` | `{"smart_wash": 0, "wash_interval": 1200}` | Wash during runs |
| `get_wash_water_temperature` / `set_wash_water_temperature` | getter `{wash_water_temperature: 2}`, setter `{"values": 2}` *(LocalRock)* | Hot-wash level |
| `app_get_dryer_setting` / `app_set_dryer_setting` | `{"status": 1}` | Drying on; 3 h total, 1 h heated |
| `get_auto_delivery_cleaning_fluid` / `set_…` | `{"status": 1}` | Detergent dosing |
| `get_dock_info` | none | `{sn: "", version: ""}` |

## Status and info

`get_status` (also `get_prop ["get_status"]`) returns one object:

| Field | Meaning |
|---|---|
| `state` | 1 starting · 2 off dock (also seen while charging) · 3 idle · 5 cleaning · 6 returning · 7 remote control · 8 charging · 10 paused · 11 spot · 12 error · 15 docking · 16 going to target · 17 zone · 18 room · 22 emptying bin · 23/25 washing mop · 26 going to wash mop · 29 mapping · 100 charged |
| `battery`, `charge_status` | %, and 1 = charging |
| `error_code`, `dock_error_status` | 0 = none. Robot: 1 lidar blocked · 2 bumper · 3 wheels lifted · 4 cliff sensor · 5/6 brush jammed · 8 trapped · 9 no bin. Dock: 32 no bin/filter · 34 duct blocked · 38 water empty · 39 waste tank full · 44 tank latch open *(lib)* |
| `in_cleaning` | 0 none · 1 whole home · 2 zone · 3 room unfinished |
| `clean_time`, `clean_area`, `clean_percent` | Current or last run |
| `fan_power`, `water_box_mode`, `mop_mode`, `repeat`, `cleaning_info` | Active settings; `cleaning_info.target_segment_id` = room being cleaned |
| `map_present`, `map_status` | `map_status >> 2` = active map (63 = none). 3 = saved map loaded |
| `unsave_map_reason` | Why the last map wasn't saved (0 = saved; 4 and 7 seen with map saving off) |
| `lab_status` | Map saving on/off |
| `camera_status` | Camera bitfield |
| `dss` | Dock sensors: `>>2&3` clean tank · `>>4&3` dirty tank · `>>6&3` dust bag (0 = OK) *(lib)* |
| `wash_phase`, `wash_status`, `dry_status`, `dust_collection_status` | Dock activity |
| `dock_type` | 20 (`k1s_dock` *(lib)*) |

Other info getters (all working): `get_network_info`, `app_get_wifi_list`,
`get_serial_number`, `get_fw_features`, `app_get_init_status`,
`app_get_locale`, `app_get_clean_estimate_info` (live progress),
`get_log_upload_status`, `get_testid`, `get_random_pkey`,
`app_wakeup_robot`.

## Consumables and history

| Command | Params | Notes |
|---|---|---|
| `get_consumable` | none | Seconds used: main brush (300 h life), side brush (200 h), filter (150 h), `sensor_dirty_time` (30 h; was at 195%); counts: `strainer_work_times`, `dust_collection_work_times` *(lib lifetimes)* |
| `reset_consumable` | `["sensor_dirty_time"]` *(Valetudo)* | **Works**: 210358 → 0. Other names: `main_brush_work_time`, `side_brush_work_time`, `filter_work_time`, `strainer_work_times`, `dust_collection_work_times` |
| `get_clean_summary` | none | Totals plus the 20 most recent record IDs |
| `get_clean_record` | `[record_id]` | One run: duration, area, `finish_reason` (52 finished · 21/29/37 interrupted *(lib)*), `start_type`, `avoid_count`, `wash_count` |
| `get_clean_record_map` | `{"start_time": record_id}` (map channel) | Map of that run, with path and obstacles. `uv run vacuum_map.py record-all maps/` |

## Maps

**Map saving must be on** (`set_lab_status [{"lab_status": 1}]`, **on
now**). With it off, nothing is saved, rooms don't persist, the
clean-order and per-room commands answer "Lab Status is disable", and
obstacle photos are thrown away.

| Command | Params | Notes |
|---|---|---|
| `get_map_v1` | `{"nonce": -1}` (map channel) | Live or current map, including mid-run. `uv run vacuum_map.py current map.png` |
| `get_multi_map` | `{"map_index": 0}`, add `"is_bak": 1` for the backup (map channel) *(LocalRock)* | The saved map |
| `get_multi_maps_list` | none | 1 map + 1 backup max |
| `get_map_status`, `get_segment_status`, `get_dynamic_map_diff` | none | |
| `get_room_mapping` | none | `[[segment, cloud_room_id, tag], …]`; on 30 Oct `[[1,"",1],[2,"",1],[3,"",6],[4,"",15]]` (room 1 joined after a run; earlier only 2–4) |
| `del_map` | `[0]` | Deletes the saved map |
| `manual_segment_map` | `[{"map_flag": 0}]` *(LocalRock)* | Re-runs automatic room division on the saved map → `[1]`. Clears room mapping and per-room settings; re-run `name_segment` afterwards |
| `load_multi_map` | `[0]` | `["ok"]` (only one slot) |
| `app_resume_build_map` | none | `["ok"]` when idle on the dock, with no effect |

Map data parses with python-roborock's `MapParser`: floor image, rooms
with bounds, path, dock, no-go zones and walls, and obstacles with AI
labels (shoes, cable, weighing scale, clothes, …) and photo IDs.

### Map and room editing

Room edits must be **wrapped and bracketed**, as the official app does
*(LocalRock)*:

```
start_edit_map []
<command> {"data": <params>, "need_retry": 1}   → ["retry"], then applied
end_edit_map
```

Plain params give `-10005 Param error`.

| Command | `data` | Confirmed |
|---|---|---|
| `name_segment` | `[{"iotRoomId": "1", "robotRoomId": 1, "robotTagId": 0}, …every room]` | Links segments to the server's rooms (`iotRoomId` = room ID in the server's home data; room 1 = "Living Room"). Names live in the server, not on the robot |
| `set_customize_clean_mode` | `[{"segment": 2, "fan_power": 102, "water_box_mode": 202, "mop_mode": 300, "repeat": 1, "seq_type": 0}, …]` | Per-room settings; `get_customize_clean_mode` reads them. `mop_mode` is required. Probably replaces the whole table, so send every room |
| `merge_segment` | `[1, 2]` | Two rooms into one |
| `split_segment` | `[seg, x1, y1, x2, y2]` | The line must cross the room from wall to wall; swap the ends if refused |
| `set_segment_ground_material` | `[[seg, material, dir]]` (dir -1 unless wood) | `["retry"]`, accepted; no getter to read it back. Material codes unknown |

These take plain params (no wrapper) and **replace the whole set** each time:

| Command | Params | Confirmed |
|---|---|---|
| `save_map` | `[[0, x1,y1, x2,y2, x3,y3, x4,y4]]` no-go zone · `[[1, x0,y0, x1,y1]]` virtual wall · `[[2, …4 corners]]` no-mop zone; mix freely | Checked on the map; `[]` clears all |
| `set_carpet_area` | `{"map_index": 0, "type": 1, "zone_data": [[…4 corners], …]}` *(LocalRock)* | Accepted; `zone_data: []` clears |
| `app_set_smart_door_sill` | `{"map_index": 0, "sides_infos": [[0,0,0,0]], "zones": [[2, …4 corners]]}` *(LocalRock)* | Accepted; empty lists clear |
| `set_ignore_identify_area` | `[[…4 corners]]` | Accepted. Clearing it with `[]` timed out |

## Photos and camera

Obstacle photos need **map saving on** and **photo saving on**
(`camera_status` bits 10 and 11 together: `set_camera_status [641 | 3072]`
= 3713; write 641 back to turn it off). Bit 10 alone is rejected.
With remote view authorized the base is 8869, so photo saving on is
`set_camera_status [11941]` (on since 30 Oct). `vacuum.py map` / `record`
list each obstacle with its photo ID; `vacuum.py photo <id>` downloads it
(small square crops, ~200 px). Obstacle types 18 and 42 have no library
label (on 30 Oct, 18 was brown fur and a green bowl- or pot-like container;
42 was a backpack).

| Command | Notes |
|---|---|
| `get_photo` | `uv run vacuum_map.py photo <id> out.jpg`, using python-roborock's `v1.obstacle_photos.get_photo(id)`. IDs come from `obstacles_with_photo[].details.photo_name` in the map. Photo saving must be on when downloading too. Replies `not support` (saving off) or `["get data failed!!!"]` (no such photo) |
| `get_camera_status` / `set_camera_status` | Bits 2, 8, 10 (alone), 13 and 14 are validated; 14 = pet mode (conflicts with a carpet setting) |
| `get_device_sdp`, `get_device_ice`, `get_turn_server`, `get_homesec_connect_status` | Live-video handshake getters. They answer "retry" or "none" because no video session is open |

## Schedules, do-not-disturb, voice

| Command | Params | Notes |
|---|---|---|
| `set_timer` | `[[id_ms, [cron, ["start_clean", {"fan_power": 102, "water_box_mode": 202, "mop_mode": 300, "repeat": 1, "segments": "0"}]]]]` | `id_ms` = string of the current time in ms; `segments` is required (`"0"` = whole home) |
| `get_timer`, `get_timer_summary`, `get_timer_detail [id]` | | |
| `upd_timer` | `[id, "on" \| "off"]` | |
| `del_timer` | `[id]` | |
| `get_dnd_timer` / `set_dnd_timer` | `[22, 0, 8, 0]` | Do-not-disturb hours (on, 22:00–08:00) |
| `close_dnd_timer` | none | Do-not-disturb off |
| `set_dnd_timer_actions` | `{"resume": 1, "vol": 1, "led": 1, "dust": 0, "dry": 0}` | **No validation**; read back |
| `set_valley_electricity_timer` | `[0, 0, 6, 0]` | Off-peak charging window; switches it on. Read back with `get_valley_electricity_timer` |
| `close_valley_electricity_timer` | none | Switches it off (keeps the window) |
| Routines | `do_scenes_segments` / `do_scenes_zones` `{"data": [{"tid", "segs": [{"sid"}] \| "zones": [{"zid", "repeat", "range"}], …settings}], "source": 101}`; `set_scenes_*` same shape | **"no such tid"**: the robot only accepts routine IDs it already knows (`get_scenes_valid_tids` is empty). Create routines on the local server first; it runs them by sending these commands |
| `get_scenes_valid_tids` | none | Routines (none) |
| `get_current_sound`, `get_sound_progress` | | Voice pack 1 (Chinese) |
| `dnld_install_sound` | `{"url": "http://<AP_IP>:<port>/en.pkg", "md5": "<hex>", "sid": 3}` | **Works**: installed the Qrevo Slim's English pack (`coral`), now `sid_in_use` 3. The `{"sid": N, "default": "en" \| "prc"}` form fails (error 2) on this firmware. See README "Voice"; `vacuum.py voice install` |

---

## Not available

### Exists, but no working format found

| Command | What was tried / reply |
|---|---|
| `set_ignore_identify_area` (clearing) | `[]` timed out; `{"areas": []}` → "Param is not an array" (param must be an array). In the app it goes through the map-edit wrapper (`{"data": …, "need_retry": 1}`) |
| `set_map_beautification_status` | Writing back 65535 → "bad request". `{"status": 0/1}` *(ioBroker)* not tried |
| `resolve_error` | No params → invalid params; `{"error_code": N}` *(python-roborock)* needs an active error |
| `app_update_unsave_map` | Exists; format unknown |
| `get_map` | No reply (sent during the junk-param batch, so untrustworthy) |

### Recovered from the decompiled app plugin (30 Oct)

The a184's device UI ships as a React Native (Hermes) plugin. Decompiling the
current-generation vacuum plugin
(`files.roborock.com/iot/plugin/ea53983b82e948638904d9154bb7f474.zip` with
`hermes-dec`) gives the exact RPC payloads. **Verified live** where noted.

| Command | Params | Status |
|---|---|---|
| `app_get_robot_setting` | `{"name": "<setting>"}` (batch: `[{"name": …}, …]`) | **Works.** Valid on a184: `lds_lifting_switch` (1), `long_haired_carpet_clean_status` (0), `common_obs_avoidance_status` (1), `object_detect_avoidance_status` (1), `identify_room_status` (1), `clean_direction_status` (0). Unknown names → `-10007 invalid name`. Setter `app_set_robot_setting` untested (presumably `{"name": …, "status": N}`) |
| `name_multi_map` | `[{"multi_map": <mapFlag>, "name": "<str>", "length": <int>}]` | **Works:** → `["ok"]` (map 0 renamed "Lab", 30 Oct) |
| `set_ignore_carpet_zone` | `{"map_index": <int>, "zone_data": [poly, …]}` | recovered, untested |
| `app_set_door_sill_blocks` | `{"map_index": <int>, "zone_data": [poly, …]}` | recovered, untested |
| `app_set_smart_cliff_forbidden` | `{"map_index": <int>, "zones": [poly, …]}` | recovered, untested |
| `app_set_ignore_stuck_point` | `{"map_index": <int>, "point_data": [[x, y], …]}` | recovered, untested |
| `app_ignore_dirty_objects` | `{"map_index": <int>, "data": […]}` | recovered, untested |
| `save_furnitures` | `{"map_flag": <int>, "data": [poly, …]}` | recovered, untested |
| `reunion_scenes` | `{"data": <scenes>}` | recovered, untested |
| `play_audio` | `{"security": {"cipher_suite": 1}, "audio": <encrypted>}` | shape recovered; the audio is encrypted for the robot's key (app "send voice"), payload format unknown |
| `get_dynamic_data` | map-diff streaming: serialized diff request + map type | app uses it for live map updates; little value locally |

`poly` is a flat 8-int rotated rectangle in map millimetres —
`[x0, y0, x1, y1, x2, y2, x3, y3]` (the app builds it from rect + slope
angle); points are `[x, y]` pairs. Probe one command at a time with realistic
coordinates read off `vacuum.py map`.

### Tried, didn't work

| Command | Result |
|---|---|
| `set_wash_towel_params` | Not tried: no getter, so the current value can't be restored |
| Room and zone routines | Need routine IDs registered through the server (see Schedules) |

### Live video (works, through go2rtc)

Works end to end since 30 Oct (README, "Camera live view"): go2rtc runs the
session; its RTSP restream `rtsp://127.0.0.1:8554/a184` carries H.264
960x540 at ~12 fps plus the robot's microphone (Opus 48 kHz stereo).
Snapshot: `curl http://127.0.0.1:1984/api/frame.jpeg?src=a184`.
Confirmed on this robot:

| Command | Params | Result |
|---|---|---|
| `set_camera_status` | `[641 \| 0x20]` then `[… \| 0x1000]`, then press Power within ~60 s | Robot grants remote view: bits 2 and 13 set, 641 → 8869 |
| `set_homesec_password` | `{"old_password": "", "new_password": md5(pin), "enable_password": 1}` | `["ok"]`; `home_sec_enable_password` → 1 |
| `check_homesec_password` | `{"password": md5(pin)}` | `["ok"]` (before a PIN was set: "password is disable") |
| `start_camera_preview` | `{"client_id": "676f32727463", "quality": "HD", "password": md5(pin)}` | `-10012` without authorization; then `-10014 request turnserver failed` until the stack answered `createca` (fixed; go2rtc now opens sessions) |
| `stop_camera_preview` | `{"client_id": …}` | `["ok"]` |
| `get_turn_server`, `get_device_sdp`, `get_device_ice`, `get_homesec_connect_status` | none | "retry" / no client until a session runs |

Not yet tried (need a running session): `send_sdp_to_robot {"app_sdp": base64}`,
`send_ice_to_robot {"app_ice": base64}`, `switch_video_quality`,
`switch_water_mark`, `start_voice_chat`, `stop_voice_chat`,
`set_voice_chat_volume`, `app_start_patrol`, `app_start_pet_patrol`,
`app_resume_patrol`, `reset_homesec_password`, `enable_homesec_voice`.

### US firmware only

`get_server_timer`, `del_server_timer` (and probably `set_server_timer`,
`upd_server_timer`): "Not FCC robot". Use `set_timer` instead.

### Not on this model ("method not recognized")

`get_map_v2`, `get_fresh_map`, `get_persist_map`, `get_map_calibration`,
`get_recover_map`, `get_recover_maps`, `app_get_amethyst_status`,
`app_set_amethyst_status`, `app_empty_rinse_tank_water`, `get_ap_mic_led_status`, `set_ap_mic_led_status`,
`get_flow_led_status`, `set_flow_led_status`, `get_fan_motor_work_timeout`,
`set_fan_motor_work_timeout`, `get_mop_motor_status`, `set_mop_motor_status`,
`get_mop_template_params_by_id`, `get_mop_template_params_summary`,
`add_mop_template_params`, `del_mop_template_params`, `mop_template_id`,
`set_mop_template_id`, `get_wash_debug_params`, `get_wash_towel_params`,
`set_airdry_hours`, `app_keep_easter_egg`, `get_lab_status`,
`app_get_lab_status`.

### Out of scope or held back

- **Cloud, firmware, logs (out of scope):** `user_upload_log`, `enable_log_upload`,
  `upload_data_for_debug_mode`, `upload_photo`, `set_fds_endpoint`,
  `app_set_dynamic_config`, `update_dock`, `app_stat` (app telemetry), `retry_request`.
- **Destructive (held back):** `app_delete_wifi` (drops it off the network),
  `reset_map`, `del_clean_record`, `del_clean_record_map_v2`, and the
  multi-map maintenance commands (`use_old_map`, `use_new_map`,
  `recover_map`, `recover_multi_map`, `manual_bak_map`). `del_map` covers
  deleting the map.

---

## Changes made to the vacuum during testing

| Setting | Was | Now |
|---|---|---|
| Map saving (`lab_status`) | 0 | **1** (needed for rooms, photos, clean order) |
| Saved map | none | 1 map from the 29 Oct mapping run |
| Obstacle-ignore area | none | Possibly a 0.5 m square at (21700, 27800)–(22200, 28300); couldn't clear it |
| Cross-carpet cleaning, dirty-water top-up | unknown | Set to 0 (no getter) |
| `camera_status` | 513 | 11941 (641 → 8869 remote view authorized → + photo saving bits 10/11) |
| Rooms | 2 | 1 (`manual_segment_map` re-division), linked to "Living Room"; per-room settings cleared |
| Water distance (`distance_off`) | 0 | 150 (can't be reset; ignored unless water mode 207) |
| Sensor-cleaning counter | 210358 s | 0 (reset) |
| Off-peak window | 00:00–00:00, off | 00:00–06:00, off |

A fresh mapping run (`del_map [0]`, then `app_start_build_map`) resets the
map, the rooms and the ignore area.

## Recovery when the vacuum stops answering

Symptoms: it pings, its port is open, its MQTT client reconnects every
~5 minutes, but no command gets a reply and no status is sent.

1. A power cycle alone did **not** fix it.
2. Reset its Wi-Fi so it enters pairing mode, then run
   `uv run provisioning/rr_provision.py <BLE addr> --name '<adv name>' --ssid "$WIFI_SSID" --passwd "$WIFI_PASS" --stack-server "$TOKEN_HOST/"` (values from lab.env).
   Retry if the hello times out. If you get "Invalid Handle", run
   `bluetoothctl remove <addr>` and retry.
3. The first cycle may join and drop back into pairing mode; run it
   again. After the second cycle it was back about 3 minutes later, with
   the map, settings and history intact.

## Sources

- python-roborock: command list, value tables, map parser, photo download.
- Official app 4.74.08 (bundled `feature_intelligent` plugin): `get_map_v1`, `get_clean_record_map` and `get_photo` params.
- [DonSidro/LocalRock](https://github.com/DonSidro/LocalRock) (captures of the official app): the map-edit wrapper, `name_segment`, `set_customize_clean_mode`, carpets, door sills, `get_multi_map`, `set_wash_water_temperature`.
- [Hypfer/Valetudo](https://github.com/Hypfer/Valetudo): `merge_segment`, `split_segment`, voice packs, `reset_consumable`.
- [copystring/ioBroker.roborock](https://github.com/copystring/ioBroker.roborock): leads for robot settings, beautification, water distance, wash towel params.
- The official app's current-gen vacuum plugin (`files.roborock.com/iot/plugin/ea53983b82e948638904d9154bb7f474.zip`, Hermes bytecode, decompiled with `hermes-dec`): exact params for the commands in "Recovered from the decompiled app plugin" (30 Oct).
