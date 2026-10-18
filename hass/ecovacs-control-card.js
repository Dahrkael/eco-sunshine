/**
 * ecovacs-control-card — full robot control: clean/pause/dock/locate,
 * suction + water selectors, per-room cleaning buttons, consumables.
 *
 * Config:
 *   type: custom:ecovacs-control-card
 *   entity: vacuum.<slug>
 */
import { tr, css } from './ecovacs-card-common.js';

const FAN_LABELS = { silent: 'quiet', standard: 'standard', strong: 'strong' };
const WATER_LEVELS = { low: 1, medium: 2, high: 3 };

class EcovacsControlCard extends HTMLElement {
  static getStubConfig() { return { type: 'custom:ecovacs-control-card', entity: '' }; }

  setConfig(config) {
    if (!config.entity) throw new Error('entity is required');
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._state = (hass.states[this._config.entity] || {}).attributes || {};
    this._update();
  }

  getCardSize() { return 4; }

  _publish(topic, payload) {
    this._hass.callService('mqtt', 'publish', { topic, payload: String(payload) });
  }

  _cmd(action) {
    const slug = this._config.entity.split('.')[1];
    this._publish(`deebot/${slug}/command`, action);
  }

  _update() {
    const t = tr(this._hass);
    const st = this._state || {};
    const slug = this._config.entity.split('.')[1];
    const fanList = (st.fan_speed_list || ['silent', 'standard', 'strong']);
    const fan = st.fan_speed || 'standard';
    const rooms = ((st.map_sets && st.map_sets.ar && st.map_sets.ar.entries) || [])
      .filter((r) => r.mssid);
    const cons = st.consumables || {};
    const hasWater = !!st.map_sets || true; // water select is published per model

    this.querySelector('.content').innerHTML = `
      <div class="row" style="gap:6px">
        <button class="btn primary" data-a="start">▶ ${t('start')}</button>
        <button class="btn warn" data-a="pause">⏸ ${t('pause')}</button>
        <button class="btn dock" data-a="dock">🏠 ${t('dock')}</button>
        <button class="btn purple" data-a="locate">🔊 ${t('locate')}</button>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="muted">${t('suction')}</label>
        <select class="fan">
          ${fanList.map((f) => `<option value="${f}" ${f === fan ? 'selected' : ''}>${t(FAN_LABELS[f] || f)}</option>`).join('')}
        </select>
        <label class="muted" style="margin-left:10px">${t('water')}</label>
        <select class="water">
          ${Object.keys(WATER_LEVELS).map((w) => `<option value="${w}">${t(w)}</option>`).join('')}
        </select>
      </div>
      ${rooms.length ? `<h3 class="section">${t('rooms')}</h3>
      <div class="row">
        ${rooms.map((r) => `<button class="btn" data-room="${r.mssid}">🚪 ${r.name || r.mssid}</button>`).join('')}
      </div>` : ''}
      <h3 class="section">${t('consumables')}</h3>
      <div class="row">
        ${Object.entries(cons).map(([k, v]) => {
          const pct = Math.round(v.left / v.total * 100);
          const col = pct < 15 ? '#f85149' : pct < 30 ? '#d29922' : '#3fb950';
          return `<span class="muted" style="min-width:90px">${k}<br>
            <b style="color:${col}">${pct}%</b></span>`;
        }).join('')}
      </div>`;

    this.querySelectorAll('[data-a]').forEach((b) =>
      b.addEventListener('click', () => this._cmd(b.dataset.a)));
    this.querySelector('.fan').addEventListener('change', (e) =>
      this._publish(`deebot/${slug}/fan_speed/set`, e.target.value));
    this.querySelector('.water').addEventListener('change', (e) =>
      this._publish(`deebot/${slug}/water/set`, e.target.value));
    this.querySelectorAll('[data-room]').forEach((b) =>
      b.addEventListener('click', () =>
        this._publish(`deebot/${slug}/room`, b.dataset.room)));
  }

  _render() {
    this.innerHTML = `
      <style>${css}
        .muted { font-size: .8em; }
      </style>
      <ha-card class="card"><div class="content"></div></ha-card>`;
  }
}

customElements.define('ecovacs-control-card', EcovacsControlCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: 'ecovacs-control-card', name: 'Ecovacs Control' });
