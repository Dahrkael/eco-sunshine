"""Panel web de control del bridge — imita la app oficial del 930.

Sirve en http://<host>:8080 — sin HA, standalone.

Endpoints:
  GET /            → SPA (HTML+JS+CSS inline)
  GET /api/state   → estado JSON consolidado
  GET /api/map     → PNG del mapa en vivo
  POST /api/cmd    → {action: "...", value: "..."}
  GET /api/ws      → WebSocket para updates en tiempo real
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web, WSMsgType

from .robot import RobotCommander, RobotState
from .map_pieces import render_png

log = logging.getLogger(__name__)

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deebot 930 — Control Local</title>
<style>
:root { --bg:#1a1a2e; --card:#16213e; --accent:#0f3460; --text:#e4e6eb; --ok:#4caf50; --warn:#ff9800; --err:#f44336; }
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif; min-height:100vh; display:flex; flex-direction:column; align-items:center; }
h1 { font-size:1.4rem; padding:.8rem; color:var(--ok); }
#status-bar { display:flex; gap:1rem; padding:.5rem 1rem; background:var(--card); border-radius:8px; margin-bottom:.8rem; font-size:.9rem; flex-wrap:wrap; }
.status-item { display:flex; align-items:center; gap:.3rem; }
.status-val { font-weight:bold; }
#map-container { position:relative; width:min(90vw,600px); }
#map-img { width:100%; border-radius:8px; background:var(--card); min-height:200px; }
#controls { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:.6rem; padding:1rem; width:min(90vw,600px); }
.btn { background:var(--accent); color:var(--text); border:none; border-radius:8px; padding:.8rem 1rem; font-size:1rem; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:.4rem; transition:background .2s; }
.btn:hover { filter:brightness(1.2); }
.btn:active { filter:brightness(.8); }
.btn.start { background:#2e7d32; }
.btn.pause { background:#e65100; }
.btn.dock { background:#1565c0; }
.btn.locate { background:#6a1b9a; }
.btn.danger { background:#b71c1c; }
.btn:disabled { opacity:.4; cursor:default; }
select { background:var(--accent); color:var(--text); border:none; border-radius:6px; padding:.6rem; font-size:.9rem; }
.section { background:var(--card); border-radius:10px; padding:.8rem; margin:.5rem 0; width:min(90vw,600px); }
.section h3 { font-size:.95rem; color:var(--accent); margin-bottom:.5rem; text-transform:uppercase; letter-spacing:.05em; }
.consumables { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:.4rem; }
.consumable { background:var(--accent); border-radius:6px; padding:.5rem; text-align:center; }
.consumable .pct { font-size:1.3rem; font-weight:bold; }
.consumable .label { font-size:.75rem; opacity:.8; }
.bar { height:4px; border-radius:2px; background:#333; margin-top:.3rem; }
.bar-fill { height:100%; border-radius:2px; transition:width .3s; }
#log { font-family:monospace; font-size:.75rem; opacity:.7; max-height:150px; overflow-y:auto; padding:.5rem; background:var(--card); border-radius:8px; width:min(90vw,600px); }
</style>
</head>
<body>
<h1>🤖 Deebot OZMO 930</h1>
<div id="status-bar">
  <div class="status-item">🔋 <span class="status-val" id="battery">--%</span></div>
  <div class="status-item">📍 <span class="status-val" id="status">--</span></div>
  <div class="status-item">⚡ <span class="status-val" id="fan">--</span></div>
  <div class="status-item">💧 <span class="status-val" id="water">--</span></div>
  <div class="status-item">⚠ <span class="status-val" id="error">--</span></div>
</div>

<div id="map-container">
  <img id="map-img" src="/api/map" alt="Mapa" style="display:none">
  <div id="map-loading" style="padding:3rem;text-align:center;color:#666">Esperando datos del mapa...</div>
</div>

<div id="controls">
  <button class="btn start" onclick="cmd('start')">▶️ Iniciar</button>
  <button class="btn pause" onclick="cmd('pause')">⏸️ Pausa</button>
  <button class="btn dock" onclick="cmd('dock')">🏠 Base</button>
  <button class="btn locate" onclick="cmd('locate')">🔊 Localizar</button>
</div>

<div class="section">
  <h3>⚙️ Configuración</h3>
  <div style="display:flex;gap:1rem;flex-wrap:wrap">
    <div>
      <label>Potencia</label>
      <select id="fan-select" onchange="cmd('fan_speed',this.value)">
        <option value="silent">Silencioso</option>
        <option value="standard" selected>Normal</option>
        <option value="strong">Potente</option>
      </select>
    </div>
    <div>
      <label>Nivel agua</label>
      <select id="water-select" onchange="cmd('water_level',this.value)">
        <option value="1">Bajo</option>
        <option value="2" selected>Medio</option>
        <option value="3">Alto</option>
      </select>
    </div>
    <div>
      <label>Idioma</label>
      <select id="lang-select" onchange="cmd('set_lang',this.value)">
        <option value="es-ES">Español</option>
        <option value="en-US">English</option>
      </select>
    </div>
  </div>
</div>

<div class="section">
  <h3>🔧 Consumibles</h3>
  <div class="consumables" id="consumables"></div>
</div>

<div class="section">
  <h3>📊 Estadísticas</h3>
  <div id="stats" style="font-size:.9rem"></div>
</div>

<div class="section">
  <h3>🗺️ Mapa</h3>
  <div style="display:flex;gap:.5rem;flex-wrap:wrap">
    <button class="btn" onclick="cmd('get_map_wifi')">📶 Mapa WiFi</button>
    <button class="btn danger" onclick="if(confirm('¿Borrar mapa?'))cmd('clear_map')">🗑️ Borrar mapa</button>
  </div>
</div>

<div id="log"></div>

<script>
let ws;
function connect() {
  ws = new WebSocket(`ws://${location.host}/api/ws`);
  ws.onmessage = (e) => update(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 3000);
  ws.onopen = () => log('WebSocket conectado');
}
function update(d) {
  document.getElementById('battery').textContent = (d.battery ?? '--') + '%';
  document.getElementById('status').textContent = d.status_raw || d.status || '--';
  document.getElementById('fan').textContent = d.fan_speed || '--';
  document.getElementById('error').textContent = d.error || 'OK';
  document.getElementById('water').textContent = d.water_level || '--';
  if (d.battery != null) document.getElementById('battery').parentElement.style.color = d.battery < 20 ? '#f44336' : '#4caf50';
  let c = '';
  for (const [k,v] of Object.entries(d.consumables || {})) {
    const pct = Math.round(v.left / v.total * 100);
    const color = pct < 15 ? '#f44336' : pct < 30 ? '#ff9800' : '#4caf50';
    c += `<div class="consumable"><div class="pct" style="color:${color}">${pct}%</div><div class="label">${k}</div><div class="bar"><div class="bar-fill" style="width:${pct}%;background:${color}"></div></div></div>`;
  }
  document.getElementById('consumables').innerHTML = c;
  let s = '';
  if (d.clean_seconds) s += `⏱️ ${(d.clean_seconds/60).toFixed(1)} min`;
  if (d.position) s += ` 📍(${d.position.x.toFixed(0)}, ${d.position.y.toFixed(0)})@${d.position.angle.toFixed(0)}°`;
  document.getElementById('stats').textContent = s || 'Sin datos';
  if (d.has_map) {
    document.getElementById('map-img').style.display = 'block';
    document.getElementById('map-loading').style.display = 'none';
  }
}
async function cmd(action, value) {
  log(`> ${action} ${value || ''}`);
  const r = await fetch('/api/cmd', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, value})});
  if (!r.ok) log(`ERROR: ${await r.text()}`);
}
function log(msg) {
  const el = document.getElementById('log');
  el.innerHTML = `[${new Date().toLocaleTimeString()}] ${msg}<br>` + el.innerHTML;
  if (el.children.length > 20) el.lastChild.remove();
}
document.getElementById('map-img').onload = function() {
  this.style.display='block';
  document.getElementById('map-loading').style.display='none';
};
connect();
setInterval(() => { document.getElementById('map-img').src = '/api/map?' + Date.now(); }, 5000);
</script>
</body>
</html>"""


class WebPanel:
    def __init__(self, service, port: int = 8080):
        self.service = service
        self.port = port
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/api/state", self.api_state)
        self.app.router.add_get("/api/map", self.api_map)
        self.app.router.add_post("/api/cmd", self.api_cmd)
        self.app.router.add_get("/api/ws", self.api_ws)
        self._ws_clients: list[web.WebSocketResponse] = []
        self._last_png: bytes | None = None

    async def start(self) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        log.info("panel web en http://0.0.0.0:%d", self.port)

    async def index(self, _: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")

    async def api_state(self, _: web.Request) -> web.Response:
        svc = self.service
        docs = {}
        for jid, r in svc.robots.items():
            docs[r.slug] = {
                "status": r.status,
                "status_raw": r.status,
                "battery": r.battery,
                "fan_speed": svc.fan_speeds.get(r.slug, "standard"),
                "error": r.error,
                "fw": r.version,
                "clean_seconds": r.clean_seconds,
                "position": {"x": r.position.x, "y": r.position.y, "angle": r.position.angle} if r.position else None,
                "charger": {"x": r.charger.x, "y": r.charger.y} if r.charger else None,
                "consumables": r.lifespans,
                "has_map": bool(svc.asm.maps),
            }
        return web.json_response(docs)

    async def api_map(self, _: web.Request) -> web.Response:
        svc = self.service
        # intentar render en vivo
        if svc.asm.maps:
            map_id = max(svc.asm.maps, key=lambda m: svc.asm.piece_count(m))
            grid = svc.asm.assemble(map_id)
            log.debug("api_map: map=%s piezas=%d grid=%s", map_id, svc.asm.piece_count(map_id), "OK" if grid else "None")
            if grid:
                try:
                    robot = next(iter(svc.robots.values()), None)
                    png = render_png(
                        grid,
                        trace=[(p.x, p.y) for p in ([robot.position] if robot and robot.position else [])],
                        position=(robot.position.x, robot.position.y) if robot and robot.position else None,
                        dock=(robot.charger.x, robot.charger.y) if robot and robot.charger else None,
                    )
                    self._save_cache(png)
                    log.debug("api_map: render vivo OK (%d bytes)", len(png))
                    return web.Response(body=png, content_type="image/png")
                except Exception:  # noqa: BLE001
                    log.exception("api_map: render falló")
        # fallback: cache en disco
        cached = self._load_cache()
        if cached:
            log.debug("api_map: sirviendo cache disco (%d bytes)", len(cached))
            return web.Response(body=cached, content_type="image/png")
        # último recurso: fichero del corpus
        try:
            from pathlib import Path
            p = Path(__file__).parent.parent.parent / "data" / "captures" / "session-02" / "map-final.png"
            if p.exists():
                log.debug("api_map: sirviendo corpus (%d bytes)", p.stat().st_size)
                return web.Response(body=p.read_bytes(), content_type="image/png")
        except OSError:
            pass
        log.warning("api_map: sin mapa disponible")
        return web.Response(status=404, text="Sin mapa")

    def _cache_path(self) -> "Path":
        from pathlib import Path
        d = Path(__file__).parent.parent.parent / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d / "last-map.png"

    def _save_cache(self, png: bytes) -> None:
        try:
            self._cache_path().write_bytes(png)
        except OSError:
            pass

    def _load_cache(self) -> bytes | None:
        try:
            p = self._cache_path()
            if p.exists():
                return p.read_bytes()
        except OSError:
            pass
        return None

    async def api_cmd(self, request: web.Request) -> web.Response:
        data = await request.json()
        action = data.get("action", "")
        value = data.get("value")
        svc = self.service
        for jid, r in svc.robots.items():
            c = svc.commanders[jid]
            log.info("web cmd: %s %s", action, value or "")
            if action == "start":
                c.start_clean(speed=svc.fan_speeds.get(r.slug, "standard"))
            elif action == "pause":
                c.pause()
            elif action == "stop":
                c.stop()
            elif action == "dock":
                c.dock()
            elif action == "locate":
                c.play_sound(30)
            elif action == "fan_speed":
                svc.fan_speeds[r.slug] = value or "standard"
            elif action == "water_level":
                c.set_water_permeability(int(value or 2))
            elif action == "set_lang":
                c.set_active_lang(value or "es-ES")
            elif action == "clear_map":
                c.clear_map()
            elif action == "get_map_wifi":
                c.get_map_wifi()
            elif action == "recover_map":
                c.recover_map()
            elif action.startswith("reset_"):
                c.reset_life_span(value or "Brush")
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "no robot"}, status=404)

    async def api_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.append(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._ws_clients.remove(ws)
        return ws

    async def broadcast(self, data: dict) -> None:
        """Envía estado a todos los clientes WebSocket conectados."""
        msg = json.dumps(data)
        for ws in self._ws_clients[:]:
            try:
                await ws.send_str(msg)
            except ConnectionResetError:
                self._ws_clients.remove(ws)
