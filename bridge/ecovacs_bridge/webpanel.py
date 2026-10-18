"""Bridge control web panel — canvas + WebSocket for real time.

Serves at http://<host>:8181 — standalone, no HA needed.

Architecture:
  - Canvas renders the map (walls/floor) from grid data
  - WebSocket pushes the robot position in real time (fluid)
  - WebSocket pushes the full map only when pieces change
  - The robot moves smoothly (interpolation between positions)
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web, WSMsgType

log = logging.getLogger(__name__)

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deebot — Local Control</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;--ok:#3fb950;--warn:#d29922;--err:#f85149;--accent:#58a6ff}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;min-height:100vh}
h1{font-size:1.3rem;padding:.6rem;color:var(--ok)}
#status-bar{display:flex;gap:1.2rem;padding:.5rem 1rem;background:var(--card);border:1px solid var(--border);border-radius:8px;margin-bottom:.5rem;font-size:.85rem;flex-wrap:wrap}
.status-item{display:flex;align-items:center;gap:.3rem}
.status-val{font-weight:600}
#canvas-wrap{position:relative;background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;width:min(92vw,650px)}
#map-canvas{display:block;width:100%;cursor:crosshair}
#map-loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#666;font-size:.9rem}
#controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.5rem;padding:.8rem;width:min(92vw,650px)}
.btn{background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:8px;padding:.7rem;font-size:.95rem;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:.35rem;transition:all .15s}
.btn:hover{background:var(--border);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn.start{border-color:#238636;color:#3fb950}
.btn.pause{border-color:#9e6a03;color:#d29922}
.btn.dock{border-color:#1f6feb;color:#58a6ff}
.btn.locate{border-color:#8957e5;color:#bc8cff}
.btn.danger{border-color:#da3633;color:#f85149}
select{background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:.5rem;font-size:.85rem}
select:focus{outline:none;border-color:var(--accent)}
.section{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.8rem;margin:.4rem 0;width:min(92vw,650px)}
.section h3{font-size:.8rem;color:var(--accent);margin-bottom:.5rem;text-transform:uppercase;letter-spacing:.08em}
.consumables{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:.4rem}
.consumable{background:#21262d;border-radius:6px;padding:.5rem;text-align:center}
.consumable .pct{font-size:1.2rem;font-weight:700}
.consumable .label{font-size:.7rem;opacity:.7}
.bar{height:3px;border-radius:2px;background:#30363d;margin-top:.3rem}
.bar-fill{height:100%;border-radius:2px;transition:width .5s}
#log{font-family:'Cascadia Code',monospace;font-size:.7rem;opacity:.6;max-height:120px;overflow-y:auto;padding:.4rem;background:var(--card);border:1px solid var(--border);border-radius:8px;width:min(92vw,650px);margin-top:.4rem}
</style>
</head>
<body>
<h1 id="title">🤖 Deebot</h1>
<select id="robot-sel" onchange="switchRobot(this.value)" style="background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:.3rem .6rem;font-size:.9rem;margin-bottom:.4rem"></select>
<div id="status-bar">
  <div class="status-item">🔋 <span id="battery" style="font-weight:700">--</span></div>
  <div class="status-item">📍 <span id="status" style="font-weight:700">--</span></div>
  <div class="status-item">💨 <span id="fan" style="font-weight:700">--</span></div>
  <div class="status-item">⚠ <span id="error" style="font-weight:700">--</span></div>
</div>

<div id="canvas-wrap">
  <canvas id="map-canvas" width="600" height="450"></canvas>
  <div id="map-loading" data-i18n="connecting">Connecting to robot…</div>
</div>

<div id="controls">
  <button class="btn start" onclick="cmd('start')" data-i18n="start">▶ Start</button>
  <button class="btn pause" onclick="cmd('pause')" data-i18n="pause">⏸ Pause</button>
  <button class="btn dock" onclick="cmd('dock')" data-i18n="dock">🏠 Dock</button>
  <button class="btn locate" onclick="cmd('locate')" data-i18n="locate">🔊 Locate</button>
</div>

<div class="section">
  <h3 data-i18n="settings">Settings</h3>
  <div style="display:flex;gap:1rem;flex-wrap:wrap;align-items:end">
    <div><label style="font-size:.8rem;opacity:.7" data-i18n="power">Suction</label><br>
      <select id="fan-sel" onchange="cmd('fan_speed',this.value)">
        <option value="silent" data-i18n="fan_silent">Quiet</option><option value="standard" selected data-i18n="fan_standard">Normal</option><option value="strong" data-i18n="fan_strong">Max</option>
      </select></div>
    <div><label style="font-size:.8rem;opacity:.7" data-i18n="water">Water</label><br>
      <select onchange="cmd('water_level',this.value)">
        <option value="1" data-i18n="water_low">Low</option><option value="2" selected data-i18n="water_med">Medium</option><option value="3" data-i18n="water_high">High</option>
      </select></div>
    <div><label style="font-size:.8rem;opacity:.7" data-i18n="continuous">Continuous</label><br>
      <select onchange="cmd('set_on_off',{'t':'g','on':this.value})">
        <option value="1" data-i18n="yes">Yes</option><option value="0" data-i18n="no">No</option>
      </select></div>
  </div>
</div>

<div class="section">
  <h3 data-i18n="consumables">Consumables</h3>
  <div class="consumables" id="consumables"></div>
</div>

<div class="section">
  <h3 data-i18n="rooms">Rooms</h3>
  <div id="rooms" style="display:flex;gap:.4rem;flex-wrap:wrap"></div>
</div>

<div class="section">
  <h3 data-i18n="stats">Statistics</h3>
  <div id="stats" style="display:flex;gap:1.2rem;flex-wrap:wrap;font-size:.9rem"></div>
</div>

<div class="section">
  <h3 data-i18n="map">Map</h3>
  <div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center">
    <button class="btn danger" onclick="if(confirm(t('confirm_clear')))cmd('clear_map')" data-i18n="delete">🗑 Delete</button>
    <button class="btn" onclick="cmd('recover_map')" data-i18n="restore">♻ Restore</button>
    <button class="btn" id="btn-wall" onclick="startDraw('wall')" data-i18n="wall">🧱 Wall</button>
    <button class="btn" id="btn-zone" onclick="startDraw('zone')" data-i18n="zone">⬜ Zone</button>
    <span id="draw-hint" style="font-size:.75rem;opacity:.7"></span>
  </div>
  <div id="walls" style="margin-top:.5rem;font-size:.8rem"></div>
</div>

<div id="log"></div>

<script>
const COLORS = {0:'#0d1117', 1:'#30363d', 2:'#8b949e', 3:'#6b5b3e', 4:'#f85149'};
let CELL_MM = 50, ORIGIN_R = 401, ORIGIN_C = 401, Y_DOWN = true;

// ---- i18n: browser language decides (Spanish browsers → es, rest → en) ----
const I18N = {
  es: {
    doc_title: 'Deebot — Control local',
    connecting: 'Conectando al robot…',
    start: '▶ Iniciar', pause: '⏸ Pausa', dock: '🏠 Base', locate: '🔊 Localizar',
    settings: 'Configuración', power: 'Potencia', water: 'Agua', continuous: 'Continuo',
    fan_silent: 'Silencioso', fan_standard: 'Normal', fan_strong: 'Potente',
    water_low: 'Bajo', water_med: 'Medio', water_high: 'Alto', yes: 'Sí', no: 'No',
    consumables: 'Consumibles', rooms: 'Habitaciones', stats: 'Estadísticas',
    no_rooms: 'sin habitaciones mapeadas',
    stat_total: 'total', stat_cleaned: 'limpiados', stat_cleans: 'limpiezas',
    map: 'Mapa', confirm_clear: '¿Borrar el mapa?',
    delete: '🗑 Borrar', restore: '♻ Recuperar', wall: '🧱 Muro', zone: '⬜ Zona',
    hint_wall: 'clic en 2 puntos para el muro',
    hint_zone: 'clic en 2 esquinas de la zona',
    hint_wall2: 'muro: falta el segundo punto',
    hint_zone2: 'zona: falta la segunda esquina',
    item_wall: 'muro', item_nomop: 'no-fregar', item_zone: 'zona',
    no_walls: 'sin muros ni zonas'
  },
  en: {
    doc_title: 'Deebot — Local Control',
    connecting: 'Connecting to robot…',
    start: '▶ Start', pause: '⏸ Pause', dock: '🏠 Dock', locate: '🔊 Locate',
    settings: 'Settings', power: 'Suction', water: 'Water', continuous: 'Continuous',
    fan_silent: 'Quiet', fan_standard: 'Standard', fan_strong: 'Max',
    water_low: 'Low', water_med: 'Medium', water_high: 'High', yes: 'Yes', no: 'No',
    consumables: 'Consumables', rooms: 'Rooms', stats: 'Statistics',
    no_rooms: 'no mapped rooms yet',
    stat_total: 'total', stat_cleaned: 'cleaned', stat_cleans: 'cleans',
    map: 'Map', confirm_clear: 'Delete the map?',
    delete: '🗑 Delete', restore: '♻ Restore', wall: '🧱 Wall', zone: '⬜ Zone',
    hint_wall: 'click 2 points to place the wall',
    hint_zone: 'click 2 corners of the zone',
    hint_wall2: 'wall: waiting for the second point',
    hint_zone2: 'zone: waiting for the second corner',
    item_wall: 'wall', item_nomop: 'no-mop', item_zone: 'zone',
    no_walls: 'no walls or zones'
  }
};
const LANG = (navigator.languages || [navigator.language || 'en'])
  .some(l => (l || '').toLowerCase().startsWith('es')) ? 'es' : 'en';
function t(k) { return I18N[LANG][k] ?? I18N.en[k] ?? k; }
document.documentElement.lang = LANG;
document.title = t('doc_title');
document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });

let ws, gridData = null, currentRobot = null, robots = {}, gridW = 0, gridH = 0, cropR = 0, cropC = 0;
let robot = null, charger = null, trace = [], animRobot = null, lastPos = null;
let mapCache = {};   // slug → last map payload (restored when switching robots)
let lastState = {};  // last state doc (rooms/walls/stats for overlays)
let drawMode = null, drawPts = [];  // 'wall' | 'zone' | null
const canvas = document.getElementById('map-canvas');
const ctx = canvas.getContext('2d');

function cellToMm(c, r) {
  // cropped-canvas cell → robot-frame mm (inverse of mmToCell)
  const mmX = (c + cropC - ORIGIN_C) * CELL_MM;
  const drow = r + cropR - ORIGIN_R;
  const mmY = (Y_DOWN ? drow : -drow) * CELL_MM;
  return [mmX, mmY];
}

function startDraw(mode) {
  if (drawMode === mode) { drawMode = null; drawPts = []; }
  else { drawMode = mode; drawPts = []; }
  document.getElementById('btn-wall').style.outline = drawMode === 'wall' ? '2px solid #f85149' : 'none';
  document.getElementById('btn-zone').style.outline = drawMode === 'zone' ? '2px solid #58a6ff' : 'none';
  document.getElementById('draw-hint').textContent = drawMode
    ? (drawMode === 'wall' ? t('hint_wall') : t('hint_zone')) : '';
  draw();
}

canvas.addEventListener('click', e => {
  if (!drawMode || !gridData) return;
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width, scaleY = canvas.height / rect.height;
  const cellSize = Math.max(1, Math.floor(Math.min(canvas.width / gridW, canvas.height / gridH)));
  const offX = Math.floor((canvas.width - gridW * cellSize) / 2);
  const offY = Math.floor((canvas.height - gridH * cellSize) / 2);
  const c = Math.floor(((e.clientX - rect.left) * scaleX - offX) / cellSize);
  const r = Math.floor(((e.clientY - rect.top) * scaleY - offY) / cellSize);
  if (c < 0 || c >= gridW || r < 0 || r >= gridH) return;
  drawPts.push(cellToMm(c, r));
  if (drawPts.length === 2) {
    const coords = [Math.round(drawPts[0][0]), Math.round(drawPts[0][1]),
                    Math.round(drawPts[1][0]), Math.round(drawPts[1][1])];
    cmd(drawMode === 'wall' ? 'add_wall' : 'add_zone', coords);
    startDraw(drawMode); // clears the mode (and disables it)
    drawMode = null; drawPts = [];
    document.getElementById('btn-wall').style.outline = 'none';
    document.getElementById('btn-zone').style.outline = 'none';
    document.getElementById('draw-hint').textContent = '';
  } else {
    document.getElementById('draw-hint').textContent =
      drawMode === 'wall' ? t('hint_wall2') : t('hint_zone2');
    draw();
  }
});

function applyMap(d) {
  gridData = d.rows.map(r => r.split('').map(Number));
  gridW = d.w; gridH = d.h; cropR = d.crop_r; cropC = d.crop_c;
  CELL_MM = d.cell_mm ?? 50; ORIGIN_R = d.origin_r ?? 401; ORIGIN_C = d.origin_c ?? 401; Y_DOWN = d.y_down ?? true;
  document.getElementById('map-loading').style.display = 'none';
  draw();
}

function switchRobot(slug) { currentRobot = slug; gridData = null; trace = []; lastPos = null; robot = null; charger = null; animRobot = null; lastState = {}; drawMode = null; drawPts = []; document.getElementById('map-loading').style.display='flex'; if (mapCache[slug]) applyMap(mapCache[slug]); if(ws) ws.close(); }
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/api/ws`);
  ws.onmessage = (e) => { const d = JSON.parse(e.data); handle(d); };
  ws.onclose = () => { setTimeout(connect, 3000); log('ws disconnected, retrying…'); };
  ws.onopen = () => { log('ws connected'); };
}

function handle(d) {
  if (d.type === 'robots') { updateRobotList(d.robots); return; }
  if (d.type === 'map') {
    if (d.slug) {
      mapCache[d.slug] = d;
      if (currentRobot && d.slug !== currentRobot) return;
    }
    applyMap(d);
    return;
  }
  if (currentRobot && d.slug && d.slug !== currentRobot) return;
  if (d.type === 'state') {
    updateStatus(d);
    if (d.position) {
      const np = mmToCell(d.position.x, d.position.y);
      if (np) {
        if (lastPos) {
          // interpolate smoothly
          animRobot = {from: lastPos, to: np, t: 0, start: performance.now()};
        } else { lastPos = np; animRobot = null; }
        trace.push([np[0], np[1]]);
        if (trace.length > 5000) trace.shift();
      }
    }
    if (d.charger) {
      charger = mmToCell(d.charger.x, d.charger.y);
    }
    draw();
  }
}

function mmToCell(x, y) {
  if (!gridData) return null;
  const dr = Math.floor(y / CELL_MM);
  const r = (Y_DOWN ? ORIGIN_R + dr : ORIGIN_R - dr) - cropR;
  const c = Math.floor(x / CELL_MM) + ORIGIN_C - cropC;
  if (r >= 0 && r < gridH && c >= 0 && c < gridW) return [r, c];
  return null;
}

function draw() {
  if (!gridData) return;
  const cw = canvas.width, ch = canvas.height;
  const cellSize = Math.max(1, Math.floor(Math.min(cw / gridW, ch / gridH)));
  const offX = Math.floor((cw - gridW * cellSize) / 2);
  const offY = Math.floor((ch - gridH * cellSize) / 2);
  ctx.fillStyle = COLORS[0];
  ctx.fillRect(0, 0, cw, ch);
  // map
  for (let r = 0; r < gridH; r++) {
    const row = gridData[r];
    for (let c = 0; c < gridW; c++) {
      const v = row[c];
      if (v === 0) continue;
      ctx.fillStyle = COLORS[v] || COLORS[0];
      ctx.fillRect(offX + c * cellSize, offY + r * cellSize, cellSize, cellSize);
    }
  }
  // overlays: walls / zones / rooms
  const sets = lastState.map_sets || {};
  const mmToPx = (x, y) => {
    const cell = mmToCell(x, y);
    if (!cell) return null;
    return [offX + cell[1] * cellSize + cellSize / 2, offY + cell[0] * cellSize + cellSize / 2];
  };
  for (const z of (sets.mw?.entries || []).concat(sets.sa?.entries || [])) {
    if (z.x1 === undefined) continue;
    const p1 = mmToPx(z.x1, z.y1), p2 = mmToPx(z.x2, z.y2);
    if (p1 && p2) {
      ctx.fillStyle = 'rgba(88,166,255,0.25)';
      ctx.strokeStyle = 'rgba(88,166,255,0.8)'; ctx.lineWidth = 1.5;
      const x = Math.min(p1[0], p2[0]), y = Math.min(p1[1], p2[1]);
      ctx.fillRect(x, y, Math.abs(p2[0] - p1[0]) || cellSize, Math.abs(p2[1] - p1[1]) || cellSize);
      ctx.strokeRect(x, y, Math.abs(p2[0] - p1[0]) || cellSize, Math.abs(p2[1] - p1[1]) || cellSize);
    }
  }
  for (const w of (sets.vw?.entries || [])) {
    if (w.x1 === undefined || w.kind) continue; // kind=mw travels in vw on the 930
    const p1 = mmToPx(w.x1, w.y1), p2 = mmToPx(w.x2, w.y2);
    if (p1 && p2) {
      ctx.strokeStyle = '#f85149'; ctx.lineWidth = Math.max(2, cellSize * 0.4);
      ctx.beginPath(); ctx.moveTo(p1[0], p1[1]); ctx.lineTo(p2[0], p2[1]); ctx.stroke();
    }
  }
  ctx.fillStyle = '#c9d1d9'; ctx.font = `${Math.max(10, cellSize + 4)}px sans-serif`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  for (const room of (sets.ar?.entries || [])) {
    if (room.center) {
      const p = mmToPx(room.center[0], room.center[1]);
      if (p) ctx.fillText(room.name || room.mssid, p[0], p[1]);
    }
  }
  // editor pending points
  for (const pt of drawPts) {
    const p = mmToPx(pt[0], pt[1]);
    if (p) {
      ctx.fillStyle = drawMode === 'wall' ? '#f85149' : '#58a6ff';
      ctx.beginPath(); ctx.arc(p[0], p[1], 6, 0, Math.PI * 2); ctx.fill();
    }
  }
  // trace (cleaned)
  if (trace.length > 1) {
    ctx.strokeStyle = 'rgba(63,185,80,0.3)';
    ctx.lineWidth = Math.max(1, cellSize * 0.3);
    ctx.beginPath();
    ctx.moveTo(offX + trace[0][1]*cellSize + cellSize/2, offY + trace[0][0]*cellSize + cellSize/2);
    for (let i = 1; i < trace.length; i++) {
      ctx.lineTo(offX + trace[i][1]*cellSize + cellSize/2, offY + trace[i][0]*cellSize + cellSize/2);
    }
    ctx.stroke();
  }
  // charging dock
  if (charger) {
    ctx.fillStyle = '#d29922';
    const s = cellSize * 2;
    ctx.beginPath();
    ctx.roundRect(offX + charger[1]*cellSize - s/4, offY + charger[0]*cellSize - s/4, s, s, 2);
    ctx.fill();
    ctx.fillStyle = '#000'; ctx.font = `${cellSize+2}px sans-serif`; ctx.textAlign='center'; ctx.textBaseline='middle';
    ctx.fillText('⚡', offX + charger[1]*cellSize + cellSize/2, offY + charger[0]*cellSize + cellSize/2);
  }
  // robot
  let rp = lastPos;
  if (animRobot) {
    const t = Math.min(1, (performance.now() - animRobot.start) / 500);
    const e = t * t * (3 - 2 * t);
    rp = [animRobot.from[0] + (animRobot.to[0] - animRobot.from[0]) * e,
          animRobot.from[1] + (animRobot.to[1] - animRobot.from[1]) * e];
    if (t >= 1) { lastPos = animRobot.to; animRobot = null; }
  }
  if (rp) {
    const cx = offX + rp[1]*cellSize + cellSize/2;
    const cy = offY + rp[0]*cellSize + cellSize/2;
    const rad = Math.max(3, cellSize * 1.2);
    ctx.fillStyle = '#3fb950';
    ctx.beginPath(); ctx.arc(cx, cy, rad, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
    // heading
    if (robot && robot.angle !== undefined) {
      const a = robot.angle * Math.PI / 180;
      ctx.strokeStyle = '#3fb950'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(a) * rad * 1.8, cy + Math.sin(a) * rad * 1.8);
      ctx.stroke();
    }
  }
  if (animRobot) requestAnimationFrame(draw);
}

function updateRobotList(list) {
  robots = list;
  const sel = document.getElementById('robot-sel');
  if (Object.keys(list).length > 1) { sel.style.display='block'; } else { sel.style.display='none'; }
  const slugs = Object.keys(list);
  sel.innerHTML = slugs.map(s => `<option value="${s}" ${s===currentRobot?'selected':''}>${list[s]}</option>`).join('');
  if (!currentRobot && slugs.length) currentRobot = slugs[0];
  const name = list[currentRobot] || 'Deebot';
  document.getElementById('title').textContent = '🤖 ' + name;
}
function updateStatus(d) {
  lastState = d;
  document.getElementById('battery').textContent = (d.battery ?? '--') + '%';
  document.getElementById('battery').style.color = d.battery < 20 ? '#f85149' : d.battery < 50 ? '#d29922' : '#3fb950';
  document.getElementById('status').textContent = d.status_raw || d.status || '--';
  document.getElementById('fan').textContent = d.fan_speed || '--';
  document.getElementById('error').textContent = d.error || 'OK';
  robot = d.position;
  let c = '';
  for (const [k, v] of Object.entries(d.consumables || {})) {
    const pct = Math.round(v.left / v.total * 100);
    const col = pct < 15 ? '#f85149' : pct < 30 ? '#d29922' : '#3fb950';
    c += `<div class="consumable"><div class="pct" style="color:${col}">${pct}%</div><div class="label">${k}</div><div class="bar"><div class="bar-fill" style="width:${pct}%;background:${col}"></div></div></div>`;
  }
  document.getElementById('consumables').innerHTML = c;
  renderRooms(d);
  renderStats(d);
  renderWalls(d);
}

function renderRooms(d) {
  const el = document.getElementById('rooms');
  const rooms = d.map_sets?.ar?.entries || [];
  if (!rooms.length) { el.innerHTML = `<span style="opacity:.5;font-size:.8rem">${t('no_rooms')}</span>`; return; }
  el.innerHTML = rooms.map(r =>
    `<button class="btn" style="padding:.4rem .7rem;font-size:.85rem" onclick="cmd('clean_room','${r.mssid}')">🚪 ${r.name || r.mssid}</button>`
  ).join('');
}

function renderStats(d) {
  const el = document.getElementById('stats');
  const s = d.stats || {};
  const h = Math.floor((s.total_time || 0) / 3600), m = Math.floor(((s.total_time || 0) % 3600) / 60);
  const m2 = ((s.total_area || 0) / 1e6).toFixed(0);
  el.innerHTML =
    `<div>⏱ <b>${h}h ${m}m</b> <span style="opacity:.6">${t('stat_total')}</span></div>` +
    `<div>📐 <b>${m2} m²</b> <span style="opacity:.6">${t('stat_cleaned')}</span></div>` +
    `<div>🔁 <b>${s.total_count ?? '—'}</b> <span style="opacity:.6">${t('stat_cleans')}</span></div>`;
}

function renderWalls(d) {
  const el = document.getElementById('walls');
  const sets = d.map_sets || {};
  const items = [];
  for (const w of (sets.vw?.entries || [])) {
    if (w.x1 === undefined) continue;
    items.push(`<span style="display:inline-flex;gap:.3rem;align-items:center;margin:.15rem">
      <span style="color:#f85149">🧱</span> ${t('item_wall')}
      <button class="btn" style="padding:.1rem .4rem;font-size:.7rem" onclick="cmd('del_wall','${w.mid}')">✕</button></span>`);
  }
  for (const z of (sets.mw?.entries || []).concat(sets.sa?.entries || [])) {
    if (z.x1 === undefined) continue;
    items.push(`<span style="display:inline-flex;gap:.3rem;align-items:center;margin:.15rem">
      <span style="color:#58a6ff">⬜</span> ${z.kind === 'mw' ? t('item_nomop') : t('item_zone')}
      <button class="btn" style="padding:.1rem .4rem;font-size:.7rem" onclick="cmd('del_zone','${z.mid}')">✕</button></span>`);
  }
  el.innerHTML = items.length ? items.join('') : `<span style="opacity:.5">${t('no_walls')}</span>`;
}

async function cmd(action, value) {
  log(`> ${action} ${value ?? ''}`);
  await fetch('/api/cmd', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, value, slug: currentRobot})});
}
function log(msg) {
  const el = document.getElementById('log');
  el.innerHTML = `[${new Date().toLocaleTimeString()}] ${msg}<br>` + el.innerHTML;
  if (el.children.length > 15) el.lastChild.remove();
}
connect();
</script>
</body>
</html>"""


class NullPanel:
    """Disabled panel (servers.panel=false) — same interface, no-op.

    Saves `if panel` checks all over serve: broadcasts and map sends
    simply do nothing.
    """

    port = 0

    async def start(self) -> None:
        pass

    def build_and_broadcast_map(self, grids=None) -> None:
        pass

    def _build_map_data(self, *args, **kwargs) -> dict | None:
        return None  # no panel → no map payload (map_data topic stays empty)

    def broadcast_raw(self, msg: str) -> None:
        pass

    async def broadcast(self, data: dict, slug: str = "") -> None:
        pass


class WebPanel:
    def __init__(self, service, port: int = 8181, host: str = "0.0.0.0"):
        self.service = service
        self.port = port
        self.host = host
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/api/state", self.api_state)
        self.app.router.add_post("/api/cmd", self.api_cmd)
        self.app.router.add_get("/api/ws", self.api_ws)
        self._ws_clients: list[web.WebSocketResponse] = []
        self._last_maps: dict[str, str] = {}  # slug → last map JSON
        self._send_tasks: set[asyncio.Task] = set()  # anti-GC refs for sends

    async def start(self) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        log.info("web panel at http://%s:%d", self.host, self.port)

    async def index(self, _: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    def _build_map_data(self, slug: str | None = None,
                        map_id: str | None = None,
                        grid: list[list[int]] | None = None) -> dict | None:
        """Assembles the grid and serializes it as rows of characters.

        slug/map_id/grid optional: if serve already resolved the robot's
        map (and assembled it) they are reused; otherwise they are
        resolved here.
        """
        svc = self.service
        if not svc.asm.maps:
            return None
        if map_id is None:
            if slug:
                for key, r in svc.robots.items():
                    if r.slug == slug:
                        map_id = svc._robot_map_id(key)
                        break
            if not map_id:
                map_id = max(svc.asm.maps, key=lambda m: svc.asm.piece_count(m))
        grid = grid if grid is not None else svc.asm.assemble(map_id)
        if not grid:
            return None
        # bounding box
        rows = [r for r in range(len(grid)) if any(grid[r])]
        if not rows:
            return None
        cols = [c for c in range(len(grid[0])) if any(grid[rr][c] for rr in range(len(grid)))]
        rmin, rmax, cmin, cmax = min(rows), max(rows), min(cols), max(cols)
        h = rmax - rmin + 1
        w = cmax - cmin + 1
        # serialize: each cell = 1 character ('0'-'3')
        row_strs = ["".join(str(grid[r][c]) for c in range(cmin, cmax + 1)) for r in range(rmin, rmax + 1)]
        geo = svc.asm.geo(map_id)
        return {
            "type": "map",
            "slug": slug or "",
            "w": w, "h": h,
            "crop_r": rmin, "crop_c": cmin,
            "rows": row_strs,
            "map_id": map_id,
            # map mm→cell transform (930: 50mm/origin 401/y-down;
            # MQTT: variable pixel, centered origin, y-up)
            "cell_mm": geo.cell_mm,
            "origin_r": geo.origin_row,
            "origin_c": geo.origin_col,
            "y_down": geo.y_down,
        }

    async def api_state(self, _: web.Request) -> web.Response:
        svc = self.service
        docs = {}
        for jid, r in svc.robots.items():
            docs[r.slug] = {
                "status": r.status, "battery": r.battery,
                "fan_speed": svc.fan_speeds.get(r.slug, "standard"),
                "error": r.error, "fw": r.version,
                "clean_seconds": r.clean_seconds,
                "position": {"x": r.position.x, "y": r.position.y, "angle": r.position.angle} if r.position else None,
                "charger": {"x": r.charger.x, "y": r.charger.y} if r.charger else None,
                "consumables": r.lifespans,
                "map_sets": {tp: ms.to_doc() for tp, ms in r.map_sets.items()},
                "stats": r.stats,
            }
        # list for the panel selector
        robot_list = {r.slug: r.model.display for r in svc.robots.values()}
        docs["_robots"] = robot_list
        return web.json_response(docs)

    async def api_cmd(self, request: web.Request) -> web.Response:
        data = await request.json()
        action = data.get("action", "")
        value = data.get("value")
        svc = self.service
        target_slug = data.get("slug") or None
        for jid, r in svc.robots.items():
            if target_slug and r.slug != target_slug:
                continue
            c = svc.commanders[jid]
            log.info("web cmd [%s]: %s %s", r.slug, action, value or "")
            if action == "start": c.start_clean(speed=svc.fan_speeds.get(r.slug, "standard"))
            elif action == "pause": c.pause()
            elif action == "stop": c.stop()
            elif action == "dock": c.dock()
            elif action == "locate": c.play_sound(30)
            elif action == "fan_speed":
                svc.fan_speeds[r.slug] = value or "standard"
                self._call(c, "set_clean_speed", value or "standard")
            elif action == "water_level": self._call(c, "set_water_permeability", int(value or 2))
            elif action == "set_lang": self._call(c, "set_active_lang", value or "es-ES")
            elif action == "clear_map": self._call(c, "clear_map")
            elif action == "recover_map": self._call(c, "recover_map")
            elif action == "set_on_off":
                if isinstance(value, dict): self._call(c, "set_on_off", value.get("t", "g"), value.get("on", "1") == "1")
                else: self._call(c, "set_on_off", "g", str(value) == "1")
            elif action.startswith("reset_"): self._call(c, "reset_life_span", value or "Brush")
            elif action in ("clean_room", "add_wall", "del_wall",
                            "add_zone", "del_zone"):
                # delegates to serve.on_command (resolves the robot's mid, etc.)
                await svc.on_command(r.slug, action, value)
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "no robot"}, status=404)

    @staticmethod
    def _call(commander, method: str, *args) -> None:
        """Lenient duck typing: XMPP and MQTT do not share 100% of the vocabulary."""
        fn = getattr(commander, method, None)
        if fn is None:
            log.info("command %s not available for this robot", method)
            return
        fn(*args)

    async def api_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.append(ws)
        # send robot list
        svc = self.service
        robot_list = {r.slug: r.model.display for r in svc.robots.values()}
        if robot_list:
            try: await ws.send_str(json.dumps({"type": "robots", "robots": robot_list}))
            except ConnectionResetError: pass
        # send each robot's cached map on connect
        for msg in self._last_maps.values():
            try: await ws.send_str(msg)
            except ConnectionResetError: pass
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR: break
        finally:
            if ws in self._ws_clients:
                self._ws_clients.remove(ws)
        return ws

    def build_and_broadcast_map(self, grids: dict[str, list[list[int]]] | None = None) -> None:
        """Called by serve when pieces change — builds and broadcasts each
        robot's map (with its slug) to every WebSocket.

        grids: {map_id: grid} cache already assembled by serve (avoids
        re-assembling per robot); if None they are assembled here (compat).
        """
        svc = self.service
        for r in svc.robots.values():
            map_id = svc._robot_map_id_by_slug(r.slug)
            if not map_id:
                continue
            grid = (grids or {}).get(map_id)
            data = self._build_map_data(r.slug, map_id, grid)
            if data:
                msg = json.dumps(data)
                self._last_maps[r.slug] = msg
                self.broadcast_raw(msg)
        # robots without their own map yet → the most populated one (single-robot compat)
        if not self._last_maps:
            data = self._build_map_data(None, None, None)
            if data:
                self._last_maps[""] = json.dumps(data)
                self.broadcast_raw(self._last_maps[""])

    def broadcast_raw(self, msg: str) -> None:
        """Non-blocking send to all WS. A broken client's exception lands
        INSIDE its task (not in create_task): it is caught in the wrapper
        and the client is dropped from the list."""

        async def _send_to(ws) -> None:
            try:
                await ws.send_str(msg)
            except Exception:  # noqa: BLE001
                if ws in self._ws_clients:
                    self._ws_clients.remove(ws)

        for ws in self._ws_clients[:]:
            try:
                task = asyncio.get_running_loop().create_task(_send_to(ws))
                self._send_tasks.add(task)  # (S7) anti-GC reference
                task.add_done_callback(self._send_tasks.discard)
            except RuntimeError:
                log.warning("broadcast_raw without event loop — message lost")

    async def broadcast(self, data: dict, slug: str = "") -> None:
        """Sends state to every connected WebSocket (no ordering guarantee)."""
        data.setdefault("type", "state")
        if slug:
            data["slug"] = slug
        msg = json.dumps(data)
        if not self._ws_clients:
            return
        results = await asyncio.gather(
            *(ws.send_str(msg) for ws in self._ws_clients[:]), return_exceptions=True
        )
        for ws, res in zip(self._ws_clients[:], results):
            if isinstance(res, Exception) and ws in self._ws_clients:
                self._ws_clients.remove(ws)
