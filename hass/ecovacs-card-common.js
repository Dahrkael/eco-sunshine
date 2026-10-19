/**
 * eco-sunshine shared card utilities.
 * MQTT subscriptions over the HA websocket API, i18n and small helpers
 * shared by the three cards (map / control / status).
 */

// Minimal Lit-free base: cards extend HTMLElement and get hass wired by HA.
export const I18N = {
  es: {
    start: 'Iniciar', pause: 'Pausa', dock: 'Base', locate: 'Localizar',
    suction: 'Aspiración', water: 'Agua', rooms: 'Habitaciones',
    consumables: 'Consumables', stats: 'Estadísticas', battery: 'Batería',
    status: 'Estado', error: 'Error', no_error: 'Sin errores',
    total: 'total', cleaned: 'limpiado', cleans: 'limpiezas',
    waiting_map: 'Esperando mapa…', no_map: 'Sin mapa (inicia una limpieza)',
    fw: 'Firmware', low: 'Baja', medium: 'Media', high: 'Alta',
    silent: 'Silencio', standard: 'Normal', strong: 'Potente',
  },
  en: {
    start: 'Start', pause: 'Pause', dock: 'Dock', locate: 'Locate',
    suction: 'Suction', water: 'Water', rooms: 'Rooms',
    consumables: 'Consumables', stats: 'Statistics', battery: 'Battery',
    status: 'Status', error: 'Error', no_error: 'No errors',
    total: 'total', cleaned: 'cleaned', cleans: 'cleans',
    waiting_map: 'Waiting for map…', no_map: 'No map yet (start a clean)',
    fw: 'Firmware', low: 'Low', medium: 'Medium', high: 'High',
    silent: 'Quiet', standard: 'Standard', strong: 'Max',
  },
};

export function detectLang(hass) {
  const lang = (hass && hass.language) || 'en';
  return lang.toLowerCase().startsWith('es') ? 'es' : 'en';
}

export function tr(hass) {
  const l = detectLang(hass);
  return (k) => (I18N[l] && I18N[l][k]) || I18N.en[k] || k;
}

/** Subscribe to an MQTT topic through the HA websocket connection.
 * The callback receives the event object {topic, payload, qos, retain}.
 * Requires an admin user (HA gates mqtt/subscribe). */
export function subscribeMqtt(hass, topic, cb) {
  if (!hass || !hass.connection) return () => {};
  let unsub = () => {};
  let closed = false;
  hass.connection.subscribeMessage(
    (msg) => cb(msg),
    { type: 'mqtt/subscribe', topic },
  ).then((u) => { if (closed) u(); else unsub = u; })
    .catch((e) => console.error('mqtt subscribe failed', topic, e));
  return () => { closed = true; unsub(); };
}

export const css = `
  :host { display: block; }
  .card { background: var(--card-bg-color, #1c1c1c); border-radius: var(--ha-card-border-radius, 12px);
          padding: 16px; color: var(--primary-text-color); }
  .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .btn { background: var(--secondary-background-color, #333); color: var(--primary-text-color);
         border: none; border-radius: 8px; padding: 8px 14px; font-size: 14px; cursor: pointer; }
  .btn:hover { filter: brightness(1.2); }
  .btn.primary { color: #3fb950; } .btn.warn { color: #d29922; }
  .btn.dock { color: #58a6ff; } .btn.purple { color: #bc8cff; }
  select { background: var(--secondary-background-color, #333); color: var(--primary-text-color);
           border: none; border-radius: 6px; padding: 6px 8px; font-size: 13px; }
  .muted { opacity: .6; font-size: .85em; }
  .title { font-weight: 600; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }
  h3.section { font-size: .8em; text-transform: uppercase; letter-spacing: .08em;
               opacity: .7; margin: 12px 0 6px; }
`;
