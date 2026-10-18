/**
 * ecovacs-status-card — compact glance: state, battery, error, stats, fw.
 *
 * Config:
 *   type: custom:ecovacs-status-card
 *   entity: vacuum.<slug>
 */
import { tr, css } from './ecovacs-card-common.js';

const ICONS = { cleaning: '🧹', docked: '🔋', charging: '⚡', paused: '⏸',
                returning: '🏠', idle: '💤', error: '⚠️', unknown: '❓' };

class EcovacsStatusCard extends HTMLElement {
  static getStubConfig() { return { type: 'custom:ecovacs-status-card', entity: '' }; }

  setConfig(config) {
    if (!config.entity) throw new Error('entity is required');
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config.entity];
    this._state = st ? st.state : 'unknown';
    this._attrs = (st && st.attributes) || {};
    this._update();
  }

  getCardSize() { return 2; }

  _update() {
    const t = tr(this._hass);
    const a = this._attrs;
    const stats = a.stats || {};
    const h = Math.floor((stats.total_time || 0) / 3600);
    const m = Math.floor(((stats.total_time || 0) % 3600) / 60);
    const m2 = ((stats.total_area || 0) / 1e6).toFixed(0);
    const batt = a.battery_level;
    const battCol = batt == null ? 'inherit'
      : batt < 20 ? '#f85149' : batt < 50 ? '#d29922' : '#3fb950';

    this.querySelector('.content').innerHTML = `
      <div class="row" style="justify-content:space-between">
        <div class="row" style="gap:14px">
          <span style="font-size:1.4em">${ICONS[this._state] || '❓'}</span>
          <div>
            <div style="font-weight:600;font-size:1.1em">${this._state}</div>
            <div class="muted">${a.friendly_name || ''}</div>
          </div>
        </div>
        <div style="text-align:right">
          <div style="font-size:1.3em;font-weight:700;color:${battCol}">
            ${batt != null ? batt + '%' : '--'}</div>
          ${a.fw ? `<div class="muted">${t('fw')} ${a.fw}</div>` : ''}
        </div>
      </div>
      <div class="row muted" style="margin-top:10px;gap:16px">
        <span>⏱ ${h}h ${m}m <span style="opacity:.7">${t('total')}</span></span>
        <span>📐 ${m2} m² <span style="opacity:.7">${t('cleaned')}</span></span>
        <span>🔁 ${stats.total_count ?? '—'} <span style="opacity:.7">${t('cleans')}</span></span>
      </div>
      ${a.error && a.error !== 'none' && a.error !== 'None'
        ? `<div style="margin-top:8px;color:#f85149">⚠ ${a.error}</div>`
        : `<div class="muted" style="margin-top:8px">✓ ${t('no_error')}</div>`}`;
  }

  _render() {
    this.innerHTML = `
      <style>${css}
        .muted { opacity: .6; font-size: .85em; }
      </style>
      <ha-card class="card"><div class="content"></div></ha-card>`;
  }
}

customElements.define('ecovacs-status-card', EcovacsStatusCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: 'ecovacs-status-card', name: 'Ecovacs Status' });
