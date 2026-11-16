"use strict";
// Vacuum page (docs/WEB.md). Polls /api/state; the map tab draws rooms,
// robot, dock and zones over the rendered map image.

const $ = (id) => document.getElementById(id);
const CLEANING = new Set([1, 2, 5, 11, 17, 18, 23, 25, 26]);
const PAUSED = 10;

let S = null;               // last /api/state
let M = null;               // last /api/map
let mapVersion = -1;
let tool = "rooms";
let selected = new Set();
let zone = null;            // {x0,y0,x1,y1} in map mm (x0<x1, y0<y1): survives map reloads
let labelBoxes = [];        // room label pills, image px, for tapping
let repeat = 1;
let lastJobShown = null;

// ---------------------------------------------------------------- utils

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, { credentials: "same-origin", ...opt });
  if (r.status === 403) { location.reload(); throw new Error("signed out"); }
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

function toast(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

const who = (email) => (email || "").split("@")[0] || "someone";
const roomName = (seg) => (S && S.names[seg]) || `Room ${seg}`;

// ---------------------------------------------------------------- tabs

document.querySelectorAll(".tabs button").forEach((b) => {
  b.onclick = () => showTab(b.dataset.tab);
});
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "map") { ensureZone(); updateSheet(); fitMap(); drawOverlay(); }
  if (name === "camera") startCam(); else stopCam();
}
$("btn-map").onclick = () => showTab("map");

// ---------------------------------------------------------------- actions

async function act(body, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  try {
    const job = await api("/api/action", body);
    lastJobShown = null;
    renderJob(job);
    toast(`Sent: ${job.label}`);
  } catch (e) {
    toast(`Couldn't send: ${e.message}`, 6000);
  }
}

$("btn-main").onclick = () => {
  const st = S && S.status && S.status.state;
  if (CLEANING.has(st)) act({ action: "pause" });
  else if (st === PAUSED) act({ action: "resume" });
  else act({ action: "clean" });
};
$("btn-stop").onclick = () => act({ action: "stop" });
$("btn-dock").onclick = () => act({ action: "dock" });
document.querySelectorAll("[data-action]").forEach((b) => {
  b.onclick = () => act({ action: b.dataset.action });
});
$("btn-dry").onclick = () => {
  const drying = S && S.status && S.status.dock.includes("drying");
  act({ action: "dry", on: !drying });
};

// ---------------------------------------------------------------- state

async function poll() {
  try {
    S = await api("/api/state");
    $("conn").className = S.online ? "conn ok" : "conn bad";
    $("conn").textContent = S.online ? "connected" : "robot offline";
    renderHome();
    renderActivity();
    if (S.job) renderJob(S.job);
    if (S.map_version !== mapVersion) loadMap();
  } catch (e) {
    $("conn").className = "conn bad";
    $("conn").textContent = "no connection";
  }
}

function renderHome() {
  const s = S.status;
  const banner = $("banner");
  // problems that need a person, shown above every tab: [what's wrong, what to do]
  const problems = [];
  if (!S.online) problems.push(["Can't reach the robot right now.", "It may be off or out of Wi-Fi range."]);
  else {
    if (s && s.error) problems.push([`Robot needs help: ${s.error_text}.`, ""]);
    if (s && s.dock_error) problems.push([`⚠ ${s.dock_problem}.`, s.dock_fix]);
  }
  banner.className = problems.length ? "banner bad" : "banner hidden";
  banner.replaceChildren(...problems.map(([what, fix]) => {
    const d = document.createElement("div");
    const b = document.createElement("strong");
    b.textContent = what;
    d.append(b);
    if (fix) d.append(document.createElement("br"), fix);
    return d;
  }));
  const bannerKey = problems.flat().join("|");
  if (bannerKey !== renderHome.bannerKey) { renderHome.bannerKey = bannerKey; fitMap(); drawOverlay(); }
  if (!s) return;

  $("state").textContent = s.state_text;
  const where = [];
  if (s.room) where.push(`in ${s.room_name || `room ${s.room}`}`);
  if (s.dock.length) where.push(`dock: ${s.dock.join(", ")}`);
  $("where").textContent = where.join(" · ");
  const out = CLEANING.has(s.state) || s.state === PAUSED;
  $("run").textContent = s.run_minutes || s.run_m2
    ? `${out ? "This run" : "Last run"}: ${s.run_minutes} min, ${s.run_m2} m²` : "";
  $("battery-fill").style.width = `${s.battery}%`;
  $("battery-text").textContent = `${s.battery}%`;
  $("battery").classList.toggle("low", s.battery < 20);
  $("modes").innerHTML = "";
  for (const [k, v] of [["suction", s.suction], ["water", s.water], ["route", s.route]]) {
    const c = document.createElement("span");
    c.className = "chip";
    c.textContent = `${k} ${v}`;
    $("modes").append(c);
  }
  $("btn-main").textContent = CLEANING.has(s.state) ? "Pause"
    : s.state === PAUSED ? "Resume" : "Clean everywhere";
  $("btn-dry").textContent = s.dock.includes("drying") ? "Dryer off" : "Dryer on";
  fillModeSelects();
}

function renderJob(job) {
  const card = $("job");
  const fresh = Date.now() / 1000 - job.started < 600 || !job.result;
  card.classList.toggle("hidden", !fresh);
  $("job-label").textContent = `${job.label[0].toUpperCase()}${job.label.slice(1)}`;
  const pill = $("job-result");
  pill.className = `pill ${job.result || ""}`;
  pill.textContent = job.result === "ok" ? "done" : job.result === "failed" ? "didn't happen"
    : job.result === "superseded" ? "replaced" : "working…";
  $("job-steps").innerHTML = "";
  for (const s of job.steps) {
    const li = document.createElement("li");
    li.textContent = s.replace(/^\s*\d+s\s+/, (m) => `${m.trim()} `);
    $("job-steps").append(li);
  }
  $("job-message").textContent = job.result ? job.message : "";
  if (job.result && lastJobShown !== job.id) {
    lastJobShown = job.id;
    if (job.result === "failed") toast(job.message, 8000);
  }
}

function renderActivity() {
  const ul = $("activity");
  ul.innerHTML = "";
  if (!S.activity.length) {
    ul.innerHTML = '<li class="meta">Nothing yet.</li>';
    return;
  }
  for (const a of S.activity) {
    const li = document.createElement("li");
    li.className = a.result === "failed" ? "failed" : "";
    li.innerHTML = '<div class="what"></div><div class="meta"></div>';
    li.querySelector(".what").textContent = `${a.what[0].toUpperCase()}${a.what.slice(1)}`;
    li.querySelector(".meta").textContent =
      `${who(a.who)} · ${ago(a.at)}${a.message && a.message !== "ok" ? ` · ${a.message}` : ""}`;
    ul.append(li);
  }
}

// ---------------------------------------------------------------- map

const img = $("map-img");
const cv = $("map-overlay");
const ctx = cv.getContext("2d");
let grid = null;            // Uint8Array, room per cell, rows top-down

async function loadMap() {
  try {
    const m = await api("/api/map");
    mapVersion = m.version;
    M = m;
    grid = Uint8Array.from(atob(m.grid.cells), (c) => c.charCodeAt(0));
    if (!img.src.endsWith(m.png)) {
      img.onload = () => { $("map-loading").classList.add("hidden"); $("map-wrap").classList.add("loaded"); fitMap(); drawOverlay(); };
      img.src = m.png;
    } else drawOverlay();
    for (const seg of [...selected]) if (!m.rooms.some((r) => r.segment === seg)) selected.delete(seg);
    ensureZone(); renderRoomChips(); updateSheet();
  } catch (e) { /* map not ready yet; next poll retries */ }
}

// image px <-> map mm
const toMM = (px, py) => [
  Math.round((px - M.transform.bx) / M.transform.ax),
  Math.round((py - M.transform.by) / M.transform.ay),
];

const toPx = (x, y) => [x * M.transform.ax + M.transform.bx, y * M.transform.ay + M.transform.by];
const pxPerM = () => 1000 * Math.abs(M.transform.ax);

function zonePx() {          // the zone as an image-px rect
  const [ax, ay] = toPx(zone.x0, zone.y0), [bx, by] = toPx(zone.x1, zone.y1);
  return { x: Math.min(ax, bx), y: Math.min(ay, by), w: Math.abs(bx - ax), h: Math.abs(by - ay) };
}
function setZonePx(x, y, w, h) {   // clamped: 0.5 m to 15 m a side, inside the image
  const lo = 0.5 * pxPerM(), hi = 15 * pxPerM();
  w = Math.min(Math.max(w, lo), hi, img.naturalWidth); h = Math.min(Math.max(h, lo), hi, img.naturalHeight);
  x = Math.min(Math.max(x, 0), img.naturalWidth - w); y = Math.min(Math.max(y, 0), img.naturalHeight - h);
  const [x0, y0] = toMM(x, y), [x1, y1] = toMM(x + w, y + h);
  zone = { x0: Math.min(x0, x1), y0: Math.min(y0, y1), x1: Math.max(x0, x1), y1: Math.max(y0, y1) };
}
function ensureZone() {      // zone mode always shows a box: 1.5 m square on the robot
  if (tool !== "zone" || zone || !M || !img.naturalWidth) return;
  const [cx, cy] = (M.robot || M.dock || { px: [img.naturalWidth / 2, img.naturalHeight / 2] }).px;
  const s = 1.5 * pxPerM();
  setZonePx(cx - s / 2, cy - s / 2, s, s);
}

function roomAt(px, py) {
  if (!grid) return 0;
  const cx = Math.floor(px / M.scale), cy = Math.floor(py / M.scale);
  if (cx < 0 || cy < 0 || cx >= M.grid.w || cy >= M.grid.h) return 0;
  return grid[cy * M.grid.w + cx];
}

function eventPx(e) {
  const r = cv.getBoundingClientRect();
  return [(e.clientX - r.left) * (cv.width / r.width), (e.clientY - r.top) * (cv.height / r.height)];
}

function drawOverlay() {
  if (!M || !img.naturalWidth) return;
  cv.width = img.naturalWidth;
  cv.height = img.naturalHeight;
  ctx.clearRect(0, 0, cv.width, cv.height);
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0f766e";

  // selected rooms: paint their cells
  if (selected.size && grid) {
    const off = document.createElement("canvas");
    off.width = M.grid.w; off.height = M.grid.h;
    const o = off.getContext("2d");
    const im = o.createImageData(M.grid.w, M.grid.h);
    for (let i = 0; i < grid.length; i++) {
      if (selected.has(grid[i])) { im.data[i * 4] = 255; im.data[i * 4 + 1] = 255; im.data[i * 4 + 2] = 255; im.data[i * 4 + 3] = 150; }
    }
    o.putImageData(im, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, cv.width, cv.height);
  }

  // no-go / no-mop areas, walls
  const poly = (pts, fill, stroke) => {
    ctx.beginPath(); pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath(); ctx.fillStyle = fill; ctx.fill(); ctx.strokeStyle = stroke; ctx.lineWidth = 2; ctx.stroke();
  };
  M.no_go.forEach((a) => poly(a, "rgba(220,38,38,.25)", "#dc2626"));
  M.no_mop.forEach((a) => poly(a, "rgba(147,51,234,.2)", "#9333ea"));
  ctx.strokeStyle = "#dc2626"; ctx.lineWidth = 4;
  M.walls.forEach(([[x0, y0], [x1, y1]]) => { ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); });

  // path of the current run
  if (M.path.length > 1) {
    ctx.beginPath(); M.path.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = "rgba(255,255,255,.55)"; ctx.lineWidth = 1.5; ctx.stroke();
  }

  // obstacles
  M.obstacles.forEach((o) => {
    ctx.beginPath(); ctx.arc(o.px[0], o.px[1], 6, 0, Math.PI * 2);
    ctx.fillStyle = "#f59e0b"; ctx.fill(); ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
  });

  // room labels: tappable pills in rooms mode, quiet names in zone mode
  labelBoxes = [];
  const k = cssScale();                // sizes below are in screen px, whatever the map's scale
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  M.rooms.forEach((r) => {
    const on = selected.has(r.segment);
    const [cx, y] = r.centre;
    const fit = (w) => Math.min(Math.max(cx, w / 2 + 2 * k), cv.width - w / 2 - 2 * k);  // keep inside the map
    if (tool === "rooms") {
      const label = `${on ? "✓" : "+"}  ${r.name || `Room ${r.segment}`}`;
      ctx.font = `600 ${13 * k}px system-ui, sans-serif`;
      const w = ctx.measureText(label).width + 16 * k, h = 26 * k, x = fit(w);
      ctx.fillStyle = on ? accent : "rgba(255,255,255,.95)";
      roundRect(x - w / 2, y - h / 2, w, h, h / 2); ctx.fill();
      ctx.strokeStyle = on ? "#fff" : "rgba(0,0,0,.25)"; ctx.lineWidth = 1.5 * k; ctx.stroke();
      ctx.fillStyle = on ? "#fff" : "#111"; ctx.fillText(label, x, y + 1);
      labelBoxes.push({ seg: r.segment, x: x - w / 2, y: y - h / 2, w, h });
    } else {
      const label = r.name || `Room ${r.segment}`;
      ctx.font = `500 ${12 * k}px system-ui, sans-serif`;
      const w = ctx.measureText(label).width + 10 * k, x = fit(w);
      ctx.fillStyle = "rgba(0,0,0,.45)"; roundRect(x - w / 2, y - 9 * k, w, 18 * k, 6 * k); ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,.85)"; ctx.fillText(label, x, y + 1);
    }
  });

  // dock and robot
  if (M.dock) {
    const [x, y] = M.dock.px;
    ctx.fillStyle = "#111"; ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
    roundRect(x - 10, y - 10, 20, 20, 5); ctx.fill(); ctx.stroke();
  }
  if (M.robot) {
    const [x, y] = M.robot.px;
    const a = (-M.robot.a * Math.PI) / 180;   // map y is flipped in the image
    ctx.beginPath(); ctx.arc(x, y, 14, 0, Math.PI * 2);
    ctx.fillStyle = "#fff"; ctx.fill(); ctx.strokeStyle = accent; ctx.lineWidth = 4; ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + Math.cos(a) * 22, y + Math.sin(a) * 22);
    ctx.strokeStyle = accent; ctx.lineWidth = 4; ctx.stroke();
  }

  // zone mode: dim everything but the box, with corner handles
  if (tool === "zone" && zone) {
    const z = zonePx(), r = handlePx() * 0.5;
    ctx.beginPath(); ctx.rect(0, 0, cv.width, cv.height); ctx.rect(z.x, z.y, z.w, z.h);
    ctx.fillStyle = "rgba(0,0,0,.45)"; ctx.fill("evenodd");
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 6; ctx.strokeRect(z.x, z.y, z.w, z.h);
    ctx.strokeStyle = accent; ctx.lineWidth = 3; ctx.strokeRect(z.x, z.y, z.w, z.h);
    for (const [hx, hy] of corners(z)) {
      ctx.beginPath(); ctx.arc(hx, hy, r, 0, Math.PI * 2);
      ctx.fillStyle = "#fff"; ctx.fill(); ctx.strokeStyle = accent; ctx.lineWidth = 4; ctx.stroke();
    }
    const label = zoneSize();
    const k = cssScale();
    ctx.font = `600 ${13 * k}px system-ui, sans-serif`;
    const w = ctx.measureText(label).width + 12 * k;
    const ly = z.y > 30 * k ? z.y - 18 * k : z.y + z.h + 18 * k;
    ctx.fillStyle = accent; roundRect(z.x + z.w / 2 - w / 2, ly - 11 * k, w, 22 * k, 6 * k); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.fillText(label, z.x + z.w / 2, ly + 1);
  }
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
}

// tools: each one changes the map at once (pills to tap / a box to adjust)
const HINTS = {
  rooms: "Tap rooms to pick them, on the map or here.",
  zone: "Drag the box to move it, or a corner to resize it. Tap the map to move it there.",
};
// keep the whole map above the action panel, so every room and corner is reachable
function fitMap() {
  if (!$("tab-map").classList.contains("active")) return;
  const top = $("map-wrap").getBoundingClientRect().top + window.scrollY;
  const nav = document.querySelector("nav.tabs").offsetHeight;
  const avail = window.innerHeight - top - nav - $("sheet").offsetHeight - 20;
  img.style.maxHeight = `${Math.max(240, Math.floor(avail))}px`;
}

function setTool(t) {
  tool = t;
  $("tool-rooms").classList.toggle("on", t === "rooms");
  $("tool-zone").classList.toggle("on", t === "zone");
  $("map-hint").textContent = HINTS[t];
  $("room-chips").classList.toggle("hidden", t !== "rooms");
  ensureZone(); updateSheet(); fitMap(); drawOverlay();
}
$("tool-rooms").onclick = () => setTool("rooms");
$("tool-zone").onclick = () => setTool("zone");

const cssScale = () => cv.width / (cv.getBoundingClientRect().width || cv.width);  // image px per screen px
const handlePx = () => 26 * cssScale();
const corners = (z) => [[z.x, z.y], [z.x + z.w, z.y], [z.x, z.y + z.h], [z.x + z.w, z.y + z.h]];
const zoneSize = () => `${((zone.x1 - zone.x0) / 1000).toFixed(1)} × ${((zone.y1 - zone.y0) / 1000).toFixed(1)} m`;

function toggleRoom(seg) {
  selected.has(seg) ? selected.delete(seg) : selected.add(seg);
  renderRoomChips(); updateSheet(); drawOverlay();
}

function renderRoomChips() {
  const box = $("room-chips");
  if (!M) return;
  const rooms = [...M.rooms].sort((a, b) => roomName(a.segment).localeCompare(roomName(b.segment)));
  box.replaceChildren(...rooms.map((r) => {
    const b = document.createElement("button");
    b.className = selected.has(r.segment) ? "chip on" : "chip";
    b.textContent = `${selected.has(r.segment) ? "✓ " : ""}${roomName(r.segment)}`;
    b.onclick = () => toggleRoom(r.segment);
    return b;
  }));
  fitMap();
}

let drag = null;
cv.addEventListener("pointerdown", (e) => {
  if (!M) return;
  const [x, y] = eventPx(e);
  if (tool === "rooms") { drag = { x0: x, y0: y }; return; }
  if (!zone) return;
  cv.setPointerCapture(e.pointerId);
  const z = zonePx(), h = handlePx();
  const c = corners(z).findIndex(([cx, cy]) => Math.hypot(x - cx, y - cy) <= h);
  if (c >= 0) {                       // resize from the opposite corner
    const [ox, oy] = corners(z)[3 - c];
    drag = { kind: "resize", ox, oy };
  } else if (x >= z.x && x <= z.x + z.w && y >= z.y && y <= z.y + z.h) {
    drag = { kind: "move", dx: x - z.x, dy: y - z.y, w: z.w, h: z.h };
  } else {
    drag = { kind: "new", x0: x, y0: y, moved: false };
  }
});
cv.addEventListener("pointermove", (e) => {
  if (!drag || tool !== "zone") return;
  let [x, y] = eventPx(e);   // a finger past the map's edge pins the box to that edge
  x = Math.min(Math.max(x, 0), img.naturalWidth); y = Math.min(Math.max(y, 0), img.naturalHeight);
  if (drag.kind === "resize") setZonePx(Math.min(x, drag.ox), Math.min(y, drag.oy), Math.abs(x - drag.ox), Math.abs(y - drag.oy));
  else if (drag.kind === "move") setZonePx(x - drag.dx, y - drag.dy, drag.w, drag.h);
  else {
    if (!drag.moved && Math.hypot(x - drag.x0, y - drag.y0) < 12) return;
    drag.moved = true;                // dragging on empty map draws a new box
    setZonePx(Math.min(x, drag.x0), Math.min(y, drag.y0), Math.abs(x - drag.x0), Math.abs(y - drag.y0));
  }
  updateSheet(); drawOverlay();
});
cv.addEventListener("pointerup", (e) => {
  if (!drag) return;
  const [x, y] = eventPx(e);
  if (tool === "rooms") {
    if (Math.hypot(x - drag.x0, y - drag.y0) < 12) {
      const hit = labelBoxes.find((b) => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h);
      const seg = hit ? hit.seg : roomAt(x, y);
      if (seg) toggleRoom(seg);
    }
  } else if (drag.kind === "new" && !drag.moved) {   // a tap: move the box there
    const z = zonePx();
    setZonePx(x - z.w / 2, y - z.h / 2, z.w, z.h);
  }
  drag = null;
  updateSheet(); drawOverlay();
});
cv.addEventListener("pointercancel", () => { drag = null; });

function zoneMM() {
  return [zone.x0, zone.y0, zone.x1, zone.y1];
}

function updateSheet() {
  const sheet = $("sheet");
  let title = "";
  if (tool === "rooms" && selected.size) {
    title = [...selected].map(roomName).join(", ");
    $("sheet-go").textContent = selected.size === 1 ? "Clean this room" : `Clean ${selected.size} rooms`;
  } else if (tool === "zone" && zone) {
    title = `Zone ${zoneSize()}`;
    $("sheet-go").textContent = "Clean this zone";
  }
  $("sheet-clear").textContent = tool === "zone" ? "Reset box" : "Clear";
  if (!title) {                       // keep the panel (and the layout) steady
    title = tool === "rooms" ? "Pick rooms to clean" : "";
    $("sheet-go").textContent = "Clean";
  }
  $("sheet-go").disabled = !(tool === "rooms" ? selected.size : zone);
  $("sheet-title").textContent = title;
  $("sheet-rename").classList.toggle("hidden", !(tool === "rooms" && selected.size === 1));
  $("rep").textContent = repeat;
}

$("rep-minus").onclick = () => { repeat = Math.max(1, repeat - 1); updateSheet(); };
$("rep-plus").onclick = () => { repeat = Math.min(3, repeat + 1); updateSheet(); };
$("sheet-clear").onclick = () => {
  if (tool === "zone") zone = null; else selected.clear();
  ensureZone(); renderRoomChips(); updateSheet(); drawOverlay();
};
$("mode-toggle").onclick = () => { $("mode-pickers").classList.toggle("hidden"); fitMap(); drawOverlay(); };

function fillModeSelects() {
  for (const k of ["suction", "water", "route"]) {
    const sel = $(`m-${k}`);
    if (sel.options.length) continue;
    sel.append(new Option("as now", ""));
    for (const v of S.modes[k]) if (v !== "custom") sel.append(new Option(v, v));
  }
}

$("sheet-go").onclick = () => {
  const mode = {};
  for (const k of ["suction", "water", "route"]) if ($(`m-${k}`).value) mode[k] = $(`m-${k}`).value;
  if (tool === "rooms" && selected.size) {
    act({ action: "clean", rooms: [...selected], repeat, mode });
  } else if (tool === "zone" && zone) {
    if (Object.keys(mode).length) toast("Modes apply to room cleaning; the zone uses the current mode.");
    act({ action: "zone", rect: zoneMM(), repeat });
  } else return;
  selected.clear(); zone = null; renderRoomChips(); updateSheet();
  showTab("home");
};

$("sheet-rename").onclick = () => {
  const seg = [...selected][0];
  const dlg = $("rename");
  $("rename-input").value = (S.names[seg]) || "";
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    const name = $("rename-input").value.trim();
    if (!name) return;
    try {
      await api(`/api/rooms/${seg}/name`, { name });
      toast(`Room ${seg} is now “${name}”`);
      mapVersion = -1;   // reload labels
      await poll();
      renderRoomChips(); updateSheet();
    } catch (e) { toast(`Couldn't rename: ${e.message}`, 6000); }
  };
  dlg.showModal();
};

window.addEventListener("resize", () => { fitMap(); drawOverlay(); });

// ---------------------------------------------------------------- camera
// go2rtc's MSE stream, relayed by the server (/api/camera/ws). Based on go2rtc's
// own player: ManagedMediaSource on iPhone Safari, MediaSource elsewhere; keep
// ~5 s buffered and steer playback speed to stay live. For the first minute the
// player reports what it is doing ("diag") to the server log, since an iPhone's
// player can't be watched from here.
// Video only: the robot's stream lists an audio track it doesn't fill, and the
// browser won't start playing until audio and video are both buffered.
const CAM_CODECS = ["avc1.640029", "avc1.64002A", "avc1.640033", "hvc1.1.6.L153.B0"];
let cam = null;
let camRetries = [];          // times of automatic restarts, to stop a restart loop

function camMsg(text) {
  $("cam-msg").textContent = text || "";
  $("cam-msg").classList.toggle("hidden", !text);
}

// go2rtc's errors when the robot's camera can't run, in plain words
function camError(raw) {
  if (/homesec|i\/o timeout|connection reset|EOF|not connected|not answering/i.test(raw))
    return "The robot's camera isn't answering. It can be unavailable for a minute while the robot docks, restarts or is busy.";
  if (/viewer is not running/.test(raw)) return "The camera service on the server isn't running.";
  return `The camera stream failed (${raw.slice(0, 80)}).`;
}

function startCam() {
  if (cam) return;
  if (S && !S.online) { camMsg("The robot is offline, so the camera isn't available. Tap to retry."); return; }
  const video = $("cam");
  const MS = window.ManagedMediaSource || window.MediaSource;
  if (!MS) { camMsg("This browser can't play the live stream."); return; }
  const t0 = performance.now();
  const ms = new MS();
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/camera/ws`);
  ws.binaryType = "arraybuffer";
  const stop = new AbortController();
  const c = (cam = { ws, stop, timers: [] });
  const st = { bytes: 0, chunks: 0, appended: 0, appendErr: "", codecs: "", playErr: "" };
  let sb = null;
  const queue = [];
  camMsg("Starting camera… (a few seconds)");

  const diag = (what) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    const b = video.buffered;
    const buf = b.length ? `${b.start(0).toFixed(2)}-${b.end(b.length - 1).toFixed(2)}(${b.length})` : "none";
    ws.send(JSON.stringify({ type: "diag", value: [
      `${((performance.now() - t0) / 1000).toFixed(1)}s ${what}`,
      `rs=${video.readyState} ns=${video.networkState} paused=${video.paused}`,
      `t=${video.currentTime.toFixed(2)} rate=${video.playbackRate.toFixed(2)} buf=${buf}`,
      `bytes=${st.bytes} chunks=${st.chunks} appended=${st.appended} q=${queue.length}`,
      `ms=${ms.readyState}${"streaming" in ms ? ` streaming=${ms.streaming}` : ""} mms=${!!window.ManagedMediaSource}`,
      `codecs=${st.codecs} size=${video.videoWidth}x${video.videoHeight}`,
      video.error ? `verr=${video.error.code}:${video.error.message}` : "",
      st.appendErr, st.playErr,
    ].filter(Boolean).join(" ") }));
  };
  const on = (target, events) => events.forEach((e) => target.addEventListener(e, () => diag(e), { signal: stop.signal }));
  on(video, ["loadedmetadata", "loadeddata", "canplay", "playing", "waiting", "stalled", "pause"]);
  // The robot's stream starts at 128x96 and switches to 960x540 mid-stream;
  // Safari's player dies on the switch. A fresh connection after the switch
  // starts at full size, so restart (as go2rtc's own player does).
  const restart = (why) => {
    diag(`restart: ${why}`);
    if (cam !== c) return;
    stopCam();
    const now = Date.now();
    camRetries = camRetries.filter((t) => now - t < 60000);
    if (camRetries.length >= 4) { camMsg("The camera stream keeps failing. Tap to retry."); return; }
    camRetries.push(now);
    camMsg("Reconnecting…");
    setTimeout(() => { if (!cam && $("tab-camera").classList.contains("active") && !document.hidden) startCam(); }, 800);
  };
  video.addEventListener("error", () => restart(`video error ${video.error && video.error.message}`), { signal: stop.signal });
  on(ms, ["sourceended", "sourceclose", "startstreaming", "endstreaming"]);
  ws.addEventListener("open", () => diag(`open ua=${navigator.userAgent}`), { once: true });
  let ticks = 0;
  c.timers.push(setInterval(() => {
    if (++ticks <= 20) diag("tick");
    if (ticks === 7 && !st.chunks) camMsg("Still waiting for the robot's camera…");
  }, 3000));

  const tryPlay = () => video.play().then(() => { st.playErr = ""; }).catch((e) => {
    if (cam !== c || e.name === "AbortError") return;   // stopped or restarted meanwhile
    st.playErr = `play:${e.name}`;
    diag("play rejected");
    if (e.name === "NotAllowedError") camMsg("Tap to play");
  });

  const pump = () => {
    if (!sb || sb.updating || !queue.length) return;
    try { sb.appendBuffer(queue.shift()); st.appended++; }
    catch (e) { st.appendErr = `append:${e.name}`; queue.length = 0; restart(`append ${e.name}`); }
  };
  const keepLive = () => {
    if (!sb || sb.updating || !sb.buffered.length) return;
    const end = sb.buffered.end(sb.buffered.length - 1), s0 = sb.buffered.start(0);
    const start = Math.max(end - 5, s0);
    if (start > s0 + 0.5) {
      sb.remove(s0, start);
      if (ms.setLiveSeekableRange) ms.setLiveSeekableRange(start, end);
      return;                    // remove() fires updateend again
    }
    if (video.currentTime < start) video.currentTime = start;   // never into a gap before the buffer
    const gap = end - video.currentTime;
    video.playbackRate = Math.min(Math.max(gap, 0.5), 2);
    if (video.paused && video.readyState >= 2 && !st.playErr) tryPlay();
  };
  const hello = () => ws.send(JSON.stringify({
    type: "mse", value: CAM_CODECS.filter((x) => MS.isTypeSupported(`video/mp4; codecs="${x}"`)).join(),
  }));
  ms.addEventListener("sourceopen", () => {
    diag("sourceopen");
    if (ws.readyState === WebSocket.OPEN) hello(); else ws.addEventListener("open", hello, { once: true });
  }, { once: true, signal: stop.signal });
  if (window.ManagedMediaSource) { video.disableRemotePlayback = true; video.srcObject = ms; }
  else video.src = URL.createObjectURL(ms);

  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      const m = JSON.parse(e.data);
      if (m.type === "mse") {
        st.codecs = m.value.replace(/^.*codecs="?|"$/g, "");
        try {
          sb = ms.addSourceBuffer(m.value);
          sb.mode = "segments";
          sb.addEventListener("updateend", () => { keepLive(); pump(); });
          sb.addEventListener("error", () => restart("sourcebuffer error"));
          diag("mse ready");
        } catch (err) { st.appendErr = `addSourceBuffer:${err.name}`; diag("addSourceBuffer failed"); }
      } else if (m.type === "error") {      // the robot's camera can't run: say so, don't retry by itself
        diag(`server error ${m.value}`);
        stopCam();
        camMsg(`${camError(m.value)} Tap to retry.`);
      }
      return;
    }
    st.bytes += e.data.byteLength;
    if (++st.chunks === 1) diag("first data");
    if (queue.length < 200) queue.push(e.data);
    pump();
  };
  ws.onclose = () => {
    if (cam !== c) return;       // stopped on purpose
    stopCam();
    camMsg("The camera connection closed. Tap to retry.");
  };
  video.addEventListener("playing", () => { camMsg(""); camRetries = []; }, { signal: stop.signal });
  tryPlay();
}

function stopCam() {
  if (!cam) return;
  const c = cam;
  cam = null;
  c.stop.abort();
  c.timers.forEach(clearInterval);
  c.ws.onclose = null;
  c.ws.close();
  const video = $("cam");
  video.pause(); video.removeAttribute("src"); video.srcObject = null; video.load();
}

$("cam-msg").onclick = () => {
  if (!cam) { startCam(); return; }
  const video = $("cam");
  if (video.paused) video.play().then(() => camMsg("")).catch(() => {});
};

// ---------------------------------------------------------------- loop

poll();
setInterval(() => { if (!document.hidden) poll(); }, 2000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
  if (document.hidden) stopCam();
  else if ($("tab-camera").classList.contains("active")) startCam();
});
