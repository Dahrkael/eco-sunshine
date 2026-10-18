/**
 * ecovacs-map-card — live canvas map with robot, dock, trace, rooms,
 * virtual walls and no-mop zones.
 *
 * Config:
 *   type: custom:ecovacs-map-card
 *   entity: vacuum.<slug>        (state doc: position/charger/map_sets)
 *   map_topic: deebot/<slug>/map_data   (optional, derived from entity if omitted)
 *   height: 400                  (optional, canvas height in px)
 */
import { subscribeMqtt, tr, css } from './ecovacs-card-common.js';

const COLORS = { 0: '#0d1117', 1: '#30363d', 2: '#8b949e', 3: '#6b5b3e', 4: '#f85149' };

class EcovacsMapCard extends HTMLElement {
  static getStubConfig() { return { type: 'custom:ecovacs-map-card', entity: '' }; }

  setConfig(config) {
    if (!config.entity) throw new Error('entity is required');
    if (this._unsubMap) { this._unsubMap(); this._unsubMap = null; } // re-config: drop old subscription
    this._config = config;
    this._map = null;       // last map_data payload
    this._robot = null;     // {r,c} animated
    this._anim = null;
    this._drawFp = null;    // last drawn fingerprint (skip redundant redraws)
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config.entity];
    if (!st) return;
    this._state = st.attributes || {};
    // slug + base come from the state doc the bridge publishes — no
    // entity-id arithmetic (HA derives entity ids from the NAME)
    const slug = this._state.slug || (this._config.entity.split('.')[1] || '');
    const base = this._config.base_topic || 'deebot';
    const topic = this._config.map_topic || `${base}/${slug}/map_data`;
    if (topic && !this._unsubMap) this._unsubMap = subscribeMqtt(hass, topic, (m) => {
      try { this._map = JSON.parse(m.payload); } catch (e) { return; }
      this._drawFp = null;  // new map → force redraw
      this._draw();
    });
    const pos = this._state.position;
    if (pos && this._map) {
      const cell = this._mmToCell(pos.x, pos.y);
      if (cell) {
        if (this._robot) {
          this._anim = { from: this._robot, to: cell, start: performance.now() };
        } else { this._robot = cell; this._anim = null; }
        this._draw();
      }
    } else { this._draw(); }
  }

  getCardSize() { return 5; }

  disconnectedCallback() {
    if (this._unsubMap) { this._unsubMap(); this._unsubMap = null; }
  }

  _mmToCell(x, y) {
    const m = this._map; if (!m) return null;
    const r = (m.y_down ? Math.floor(y / m.cell_mm) + m.origin_r - m.crop_r
                        : m.origin_r - Math.floor(y / m.cell_mm) - m.crop_r);
    const c = Math.floor(x / m.cell_mm) + m.origin_c - m.crop_c;
    if (r >= 0 && r < m.h && c >= 0 && c < m.w) return [r, c];
    return null;
  }

  _render() {
    this.innerHTML = `
      <style>${css}</style>
      <ha-card class="card">
        <canvas width="600" height="${this._config.height || 400}"
                style="width:100%;cursor:crosshair;display:block"></canvas>
      </ha-card>`;
    this._canvas = this.querySelector('canvas');
    this._ctx = this._canvas.getContext('2d');
  }

  _draw() {
    const ctx = this._ctx, cv = this._canvas, m = this._map;
    const t = tr(this._hass);
    // skip redundant full redraws (same map + same rounded position)
    const fp = m ? `${m.map_id}|${this._state.position &&
      Math.round(this._state.position.x)}|${this._state.position &&
      Math.round(this._state.position.y)}` : 'none';
    if (!this._anim && fp === this._drawFp) return;
    this._drawFp = fp;
    ctx.fillStyle = '#0d1117'; ctx.fillRect(0, 0, cv.width, cv.height);
    if (!m) {
      ctx.fillStyle = '#666'; ctx.font = '14px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText(t('waiting_map'), cv.width / 2, cv.height / 2);
      return;
    }
    const cell = Math.max(1, Math.floor(Math.min(cv.width / m.w, cv.height / m.h)));
    const offX = Math.floor((cv.width - m.w * cell) / 2);
    const offY = Math.floor((cv.height - m.h * cell) / 2);
    const px = (r, c) => [offY + r * cell + cell / 2, offX + c * cell + cell / 2];

    // grid
    for (let r = 0; r < m.h; r++) {
      const row = m.rows[r] || '';
      for (let c = 0; c < m.w; c++) {
        const v = +row[c];
        if (!v) continue;
        ctx.fillStyle = COLORS[v] || COLORS[0];
        ctx.fillRect(offX + c * cell, offY + r * cell, cell, cell);
      }
    }

    const sets = (this._state && this._state.map_sets) || {};
    const mm = (x, y) => { const cl = this._mmToCell(x, y); return cl ? px(cl[0], cl[1]) : null; };

    // no-mop / custom zones
    for (const z of [...((sets.mw && sets.mw.entries) || []), ...((sets.sa && sets.sa.entries) || [])]) {
      if (z.x1 === undefined) continue;
      const p1 = mm(z.x1, z.y1), p2 = mm(z.x2, z.y2);
      if (p1 && p2) {
        ctx.fillStyle = 'rgba(88,166,255,0.25)'; ctx.strokeStyle = 'rgba(88,166,255,0.8)';
        const x = Math.min(p1[1], p2[1]), y = Math.min(p1[0], p2[0]);
        const w = Math.abs(p2[1] - p1[1]) || cell, h = Math.abs(p2[0] - p1[0]) || cell;
        ctx.fillRect(x, y, w, h); ctx.strokeRect(x, y, w, h);
      }
    }
    // virtual walls
    ctx.lineWidth = Math.max(2, cell * 0.4); ctx.strokeStyle = '#f85149';
    for (const w of (sets.vw && sets.vw.entries) || []) {
      if (w.x1 === undefined || w.kind) continue;
      const p1 = mm(w.x1, w.y1), p2 = mm(w.x2, w.y2);
      if (p1 && p2) { ctx.beginPath(); ctx.moveTo(p1[1], p1[0]); ctx.lineTo(p2[1], p2[0]); ctx.stroke(); }
    }
    // room labels
    ctx.fillStyle = '#c9d1d9'; ctx.font = `${Math.max(10, cell + 3)}px sans-serif`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (const room of (sets.ar && sets.ar.entries) || []) {
      if (room.center) { const p = mm(room.center[0], room.center[1]); if (p) ctx.fillText(room.name || room.mssid, p[1], p[0]); }
    }
    // dock
    const ch = this._state && this._state.charger;
    if (ch) { const p = mm(ch.x, ch.y);
      if (p) { ctx.fillStyle = '#d29922'; const s = cell * 2;
        if (ctx.roundRect) {
          ctx.beginPath(); ctx.roundRect(p[1] - s / 4, p[0] - s / 4, s, s, 3); ctx.fill();
        } else { ctx.fillRect(p[1] - s / 4, p[0] - s / 4, s, s); } // Safari <16.4
        ctx.fillStyle = '#000'; ctx.font = `${cell + 3}px sans-serif`;
        ctx.fillText('⚡', p[1], p[0]); } }
    // animated robot
    let rp = this._robot;
    if (this._anim) {
      const k = Math.min(1, (performance.now() - this._anim.start) / 600);
      const e = k * k * (3 - 2 * k);
      rp = [this._anim.from[0] + (this._anim.to[0] - this._anim.from[0]) * e,
            this._anim.from[1] + (this._anim.to[1] - this._anim.from[1]) * e];
      if (k >= 1) { this._robot = this._anim.to; this._anim = null; }
    }
    if (rp) {
      const [cy, cx] = px(rp[0], rp[1]);
      const rad = Math.max(4, cell * 1.2);
      ctx.fillStyle = '#3fb950'; ctx.beginPath(); ctx.arc(cx, cy, rad, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke();
      const a = (this._state.position && this._state.position.angle || 0) * Math.PI / 180;
      ctx.strokeStyle = '#3fb950'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(a) * rad * 1.8, cy + Math.sin(a) * rad * 1.8); ctx.stroke();
    }
    if (this._anim) requestAnimationFrame(() => this._draw());
  }
}

customElements.define('ecovacs-map-card', EcovacsMapCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: 'ecovacs-map-card', name: 'Ecovacs Map' });
