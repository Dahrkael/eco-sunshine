/**
 * ecovacs-control-card — full robot control: clean/pause/dock/locate,
 * suction + water selectors, per-room cleaning buttons, consumables.
 *
 * Vacuum actions go through native HA services (no MQTT arithmetic);
 * water/room commands publish to the bridge topics using the slug the
 * bridge reports in the state doc attributes.
 *
 * Config:
 *   type: custom:ecovacs-control-card
 *   entity: vacuum.<slug>
 *   base_topic: deebot   (optional, must match the bridge config)
 */
import { tr, css } from './ecovacs-card-common.js';

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

  _vac(action) {
    this._hass.callService('vacuum', action,
      { entity_id: this._config.entity });
  }

  _publish(topic, payload) {
    this._hass.callService('mqtt', 'publish',
      { topic, payload: String(payload) });
  }

  _update() {
    const t = tr(this._hass);
    const st = this._state || {};
    const slug = st.slug || '';
    const base = this._config.base_topic || 'deebot';
    const fanList = (st.fan_speed_list || ['silent', 'standard', 'strong']);
    const fan = st.fan_speed || 'standard';
    const rooms = ((st.map_sets && st.map_sets.ar && st.map_sets.ar.entries) || [])
      .filter((r) => r.mssid);
    const cons = st.consumables || {};
    const hasWater = !!st.has_water && !!slug;

    this.querySelector('.content').innerHTML = `
      <div class="row" style="gap:6px">
        <button class="btn primary" data-a="start">▶ ${t('start')}</button>
        <button class="btn warn" data-a="pause">⏸ ${t('pause')}</button>
        <button class="btn dock" data-a="return_to_base">🏠 ${t('dock')}</button>
        <button class="btn purple" data-a="locate">🔊 ${t('locate')}</button>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="muted">${t('suction')}</label>
        <select class="fan">
          ${fanList.map((f) => `<option value="${f}" ${f === fan ? 'selected' : ''}>${t(f)}</option>`).join('')}
        </select>
        ${hasWater ? `
        <label class="muted" style="margin-left:10px">${t('water')}</label>
        <select class="water">
          ${Object.keys(WATER_LEVELS).map((w) => `<option value="${w}">${t(w)}</option>`).join('')}
        </select>` : ''}
      </div>
      ${rooms.length && slug ? `<h3 class="section">${t('rooms')}</h3>
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
      b.addEventListener('click', () => this._vac(b.dataset.a)));
    this.querySelector('.fan').addEventListener('change', (e) =>
      this._hass.callService('vacuum', 'set_fan_speed',
        { entity_id: this._config.entity, fan_speed: e.target.value }));
    const water = this.querySelector('.water');
    if (water) water.addEventListener('change', (e) =>
      this._publish(`${base}/${slug}/water/set`, e.target.value));
    this.querySelectorAll('[data-room]').forEach((b) =>
      b.addEventListener('click', () =>
        this._publish(`${base}/${slug}/room`, b.dataset.room)));
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
