'use strict';
// De tv-pagina. Haalt elke paar seconden `data` op (zie kkaddon/tvhub.py) en tekent alles opnieuw; er is niets te bedienen.
// `?demo=1` toont voorbeeldgegevens, om aan het ontwerp te werken zonder add-on.

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const POLL = 10000;
const STALE = 90; // s: zo oud mag de data zijn voor er een waarschuwing komt

// ---------------------------------------------------------------- pictogrammen (24x24, lijnen)
const ICONS = {
  dot: '<circle cx="12" cy="12" r="3"/>',
  sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.6M12 18.9v2.6M2.5 12h2.6M18.9 12h2.6M5.3 5.3l1.9 1.9M16.8 16.8l1.9 1.9M18.7 5.3l-1.9 1.9M7.2 16.8l-1.9 1.9"/>',
  moon: '<path d="M20 14.2A8.2 8.2 0 0 1 9.8 4 8.2 8.2 0 1 0 20 14.2z"/>',
  cloud: '<path d="M7 18.5h10.2a4 4 0 0 0 .6-7.95A5.6 5.6 0 0 0 7.1 9.6 4.5 4.5 0 0 0 7 18.5z"/>',
  partly: '<circle cx="8.6" cy="8.4" r="3.2"/><path d="M8.6 2.3v1.4M2.5 8.4h1.4M4.3 4.1l1 1M13 4.1l-1 1"/><path d="M9.5 20h8a3.4 3.4 0 0 0 .4-6.8 4.7 4.7 0 0 0-9-.9A3.9 3.9 0 0 0 9.5 20z"/>',
  rain: '<path d="M7 14.5h10.2a4 4 0 0 0 .6-7.95A5.6 5.6 0 0 0 7.1 5.6 4.5 4.5 0 0 0 7 14.5z"/><path d="M8.5 17.5l-1 2.5M12.5 17.5l-1 2.5M16.5 17.5l-1 2.5"/>',
  pour: '<path d="M7 13.5h10.2a4 4 0 0 0 .6-7.95A5.6 5.6 0 0 0 7.1 4.6 4.5 4.5 0 0 0 7 13.5z"/><path d="M7.5 16.5l-1.2 3.5M11.5 16.5l-1.2 3.5M15.5 16.5l-1.2 3.5M19 16.5l-1 3"/>',
  snow: '<path d="M7 14.5h10.2a4 4 0 0 0 .6-7.95A5.6 5.6 0 0 0 7.1 5.6 4.5 4.5 0 0 0 7 14.5z"/><path d="M8.5 18v.01M12 19.5v.01M15.5 18v.01M10.2 21v.01M13.8 21v.01"/>',
  bolt: '<path d="M13.4 2.5 5.5 13.2h5.6l-1 8.3 8.4-11.4h-5.8z"/>',
  fog: '<path d="M6.5 13h11a3.6 3.6 0 0 0 .4-7.2A5 5 0 0 0 8 5.3 4 4 0 0 0 6.5 13z"/><path d="M4 16.5h16M6.5 20h11"/>',
  wind: '<path d="M3 9h10.5a2.7 2.7 0 1 0-2.6-3.4M3 13.5h15a2.8 2.8 0 1 1-2.7 3.5M3 18h6"/>',
  gauge: '<path d="M4.5 18a8.5 8.5 0 1 1 15 0"/><path d="M12 14l3.6-5.2"/><circle cx="12" cy="14" r="1.3"/>',
  drop: '<path d="M12 3.2s6 6.1 6 10.6a6 6 0 0 1-12 0C6 9.3 12 3.2 12 3.2z"/>',
  flask: '<path d="M9.5 3h5M10.2 3v6.3L4.8 18.2A2 2 0 0 0 6.5 21h11a2 2 0 0 0 1.7-2.8l-5.4-8.9V3M7.5 15.2h9"/>',
  power: '<path d="M12 3v8.5M7 6.2a8 8 0 1 0 10 0"/>',
  pump: '<rect x="3.5" y="8.5" width="11" height="9" rx="2"/><path d="M14.5 12h6M20.5 12v-3M7 8.5V5.5h4v3"/>',
  home: '<path d="M3.5 11.2 12 3.8l8.5 7.4M5.5 9.8V20h13V9.8M10 20v-5.5h4V20"/>',
  thermo: '<path d="M10 14.6V5.2a2 2 0 1 1 4 0v9.4a4 4 0 1 1-4 0zM12 9v7"/>',
  flake: '<path d="M12 2.8v18.4M4 7.4l16 9.2M4 16.6l16-9.2M9.5 4.8 12 7l2.5-2.2M9.5 19.2 12 17l2.5 2.2"/>',
  fan: '<circle cx="12" cy="12" r="1.6"/><path d="M12 10.4C12 6 14.5 3.4 17 4.5c2 .9 1.2 4.2-1.6 5.4M13.6 12c4.4 0 7 2.5 5.9 5-.9 2-4.2 1.2-5.4-1.6M12 13.6C12 18 9.5 20.6 7 19.5c-2-.9-1.2-4.2 1.6-5.4M10.4 12C6 12 3.4 9.5 4.5 7c.9-2 4.2-1.2 5.4 1.6"/>',
  cal: '<rect x="3.5" y="5" width="17" height="15.5" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4M8 14h2M12 14h2M16 14h.01M8 17.2h2M12 17.2h2"/>',
  bin: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 11v5.5M14 11v5.5"/>',
  leaf: '<path d="M5 19.5C4 11 9 5 19.5 4.5c.5 10.5-5 15-12 14.5M5 19.5c2.5-5 5.5-8 9-10"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5M12 14.5v2.5"/>',
  unlock: '<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V8a4 4 0 0 1 7.6-1.7M12 14.5v2.5"/>',
  bell: '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15zM10 20.5a2.2 2.2 0 0 0 4 0"/>',
  door: '<rect x="5.5" y="3.5" width="12" height="17" rx="1.5"/><path d="M14.5 12v.01M17.5 5.5l3 1.5v11l-3 1.5"/>',
  check: '<path d="M4.5 12.5l4.8 4.8L19.5 7"/>',
  warn: '<path d="M12 3.5 2.8 19.5h18.4zM12 10v4.5M12 17v.01"/>',
  solar: '<path d="M3.5 19.5 6 9h12l2.5 10.5zM4.8 14.2h14.4M9.7 9l-.9 10.5M14.3 9l.9 10.5"/><path d="M12 2.5v2.6M5.2 4.6l1.8 1.8M18.8 4.6 17 6.4"/>',
  battery: '<rect x="3" y="7" width="16" height="10" rx="2.5"/><path d="M21.5 10.5v3M7 10.5v3M11 10.5v3M15 10.5v3"/>',
  zap: '<path d="M13.4 2.5 5.5 13.2h5.6l-1 8.3 8.4-11.4h-5.8z"/>',
  waves: '<path d="M2.5 9c2.2-2 4.4-2 6.5 0s4.3 2 6.5 0 4.3-2 6 0M2.5 14c2.2-2 4.4-2 6.5 0s4.3 2 6.5 0 4.3-2 6 0M2.5 19c2.2-2 4.4-2 6.5 0s4.3 2 6.5 0 4.3-2 6 0"/>',
  moonz: '<path d="M20 14.2A8.2 8.2 0 0 1 9.8 4 8.2 8.2 0 1 0 20 14.2z"/>',
  flame: '<path d="M12 21a6.5 6.5 0 0 0 6.5-6.5c0-4-3-5.5-3.5-9.5-2.5 1.5-4 4-4.2 6.2-1.2-.5-1.8-1.6-2-2.8C6.800 10 5.500 12 5.500 14.500A6.500 6.500 0 0 0 12 21z"/>',
  car: '<path d="M4 15.5v-3.2l2-4.3h12l2 4.3v3.2zM4 15.5v2.5M20 15.5v2.5M7.5 12.5h.01M16.5 12.5h.01M4.5 12.3h15"/>',
  water: '<path d="M12 3.2s6 6.1 6 10.6a6 6 0 0 1-12 0C6 9.3 12 3.2 12 3.2z"/><path d="M9.5 14.2a2.6 2.6 0 0 0 2.2 2.6"/>',
};
const ic = (n, s = 32) => `<svg class="ic" viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[n] || ICONS.dot}</svg>`;

const COND = { // weer-entiteit van HA -> [pictogram, tekst]
  sunny: ['sun', 'Zonnig'], 'clear-night': ['moon', 'Helder'], partlycloudy: ['partly', 'Licht bewolkt'], cloudy: ['cloud', 'Bewolkt'], rainy: ['rain', 'Regen'],
  pouring: ['pour', 'Zware regen'], snowy: ['snow', 'Sneeuw'], 'snowy-rainy': ['snow', 'Natte sneeuw'], lightning: ['bolt', 'Onweer'], 'lightning-rainy': ['bolt', 'Onweer met regen'],
  fog: ['fog', 'Mist'], hail: ['snow', 'Hagel'], windy: ['wind', 'Winderig'], 'windy-variant': ['wind', 'Winderig'], exceptional: ['warn', 'Uitzonderlijk'],
};
const WASTE = [ // trefwoord (in de titel van de afspraak) -> kleur
  [/gft|groen|organisch|tuin/i, '#7bd88f'], [/pmd|plastic|verpakking|drankkarton|blik/i, '#6cb6ff'], [/papier|karton/i, '#f2b84b'], [/glas/i, '#4fd1c5'],
  [/rest|grof|huisvuil/i, '#a8b3c7'], [/kerst/i, '#ff8a8a'],
];
const wasteColor = (t, fixed) => fixed || (WASTE.find(([re]) => re.test(t)) || [0, '#a58bff'])[1];

// ---------------------------------------------------------------- opmaak
const num = (v, d = 1) => (v == null || Number.isNaN(Number(v)) ? '–' : Number(v).toLocaleString('nl-BE', { minimumFractionDigits: d, maximumFractionDigits: d }));
const val = (v, unit, d) => `<span class="val">${num(v, d)}${unit ? `<span class="u">${esc(unit)}</span>` : ''}</span>`;
const hhmm = (ts, tz) => new Date(ts * 1000).toLocaleTimeString('nl-BE', { hour: '2-digit', minute: '2-digit', timeZone: tz });

function chart(series, o = {}) { // [{color, points: [[ts, waarde]], fill}] -> svg met 3 rasterlijnen en tijdlabels
  const W = 520, H = o.h || 150, L = 52, B = 26, T = 8, R = 10;
  const t1 = o.now || Date.now() / 1000, t0 = t1 - (o.hours || 12) * 3600;
  const all = series.flatMap((s) => s.points.filter((p) => p[0] >= t0).map((p) => p[1]));
  if (!all.length) return '';
  const mn = Math.min(...all), mx = Math.max(...all), span = Math.max(mx - mn, 2);
  const lo = o.auto ? Math.floor(mn - span * 0.25) : o.min != null ? o.min : Math.min(0, mn), hi0 = mx, hi = o.auto ? Math.ceil(mx + span * 0.25) : o.max != null ? o.max : (hi0 <= lo ? lo + 1 : lo + (hi0 - lo) * 1.15);
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R), y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const grid = [0, 0.5, 1].map((f) => { const v = lo + (hi - lo) * f; return `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="rgba(140,170,230,.16)"/><text x="${L - 10}" y="${y(v) + 7}" text-anchor="end" fill="#8aa0c2" font-size="20">${num(v, o.dec ?? 0)}${o.unit || ''}</text>`; }).join('');
  const hourOf = (t) => Number(new Date(t * 1000).toLocaleString('nl-BE', { hour: 'numeric', hour12: false, timeZone: o.tz })) % 24;
  const ticks = []; // een label op elke vierde hele uur
  for (let t = Math.ceil(t0 / 3600) * 3600; t <= t1; t += 3600) if (hourOf(t) % 4 === 0 && x(t) > L + 30 && x(t) < W - R - 30) ticks.push(`<text x="${x(t).toFixed(1)}" y="${H - 4}" text-anchor="middle" fill="#8aa0c2" font-size="20">${String(hourOf(t)).padStart(2, '0')}u</text>`);
  const lines = series.map((s, i) => {
    const pts = s.points.filter((p) => p[0] >= t0).map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`);
    if (!pts.length) return '';
    const id = `g${i}${Math.abs((o.tag || '').length)}${s.color.replace('#', '')}`;
    return `<defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${s.color}" stop-opacity=".32"/><stop offset="1" stop-color="${s.color}" stop-opacity="0"/></linearGradient></defs>
      ${s.fill ? `<polygon points="${x(s.points.filter((p) => p[0] >= t0)[0][0]).toFixed(1)},${y(lo)} ${pts.join(' ')} ${x(s.points[s.points.length - 1][0]).toFixed(1)},${y(lo)}" fill="url(#${id})"/>` : ''}
      <polyline points="${pts.join(' ')}" fill="none" stroke="${s.color}" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round"/>`;
  }).join('');
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${grid}${ticks.join('')}${lines}</svg>`;
}

function gauge(v, max) { // halve cirkel met een kleurverloop; v/max bepaalt hoever hij gevuld is
  const f = v == null ? 0 : Math.max(0, Math.min(1, v / (max || 5))), len = Math.PI * 128;
  return `<svg viewBox="0 0 300 196"><defs><linearGradient id="gg" x1="0" x2="1"><stop offset="0" stop-color="#45d08a"/><stop offset=".55" stop-color="#f2b84b"/><stop offset="1" stop-color="#ff8a4b"/></linearGradient></defs>
    <path d="M22 172A128 128 0 0 1 278 172" fill="none" stroke="rgba(140,170,230,.16)" stroke-width="22" stroke-linecap="round"/>
    <path d="M22 172A128 128 0 0 1 278 172" fill="none" stroke="url(#gg)" stroke-width="22" stroke-linecap="round" stroke-dasharray="${(len * f).toFixed(1)} ${len.toFixed(1)}"/></svg>`;
}

// ---------------------------------------------------------------- tegels
function tileEnergy(e, now, tz) {
  if (!e) return '';
  const c = e.consumption, s = e.solar, b = e.battery;
  const side = [s && `<div class="stat">${ic('solar', 46)}<div>${val(s.value, s.unit || 'kW')}<div class="lbl">Zonnepanelen</div></div></div>`,
    b && `<div class="stat b">${ic('battery', 46)}<div>${val(b.pct, '%', 0)}<div class="lbl">Batterij${b.detail ? ` · ${esc(b.detail)}` : ''}</div></div></div>`,
    e.grid && `<div class="stat">${ic('zap', 46)}<div>${val(e.grid.value, e.grid.unit || 'kW')}<div class="lbl">${esc(e.grid.label || 'Net')}</div></div></div>`].filter(Boolean).join('');
  const ser = [e.series?.consumption && { color: '#4da3ff', points: e.series.consumption, fill: true }, e.series?.solar && { color: '#f2b84b', points: e.series.solar, fill: true }].filter(Boolean);
  const tag = e.trend != null ? `<span class="tag ${e.trend <= 0 ? 'up' : 'down'}">${e.trend <= 0 ? '↓' : '↑'} ${num(Math.abs(e.trend), 0)}%</span>` : '';
  return `<section class="card energy"><h2>${ic('zap', 40).replace('currentColor', '#f2b84b')}Energie${tag}</h2>
    <div class="body"><div class="gauge">${gauge(c?.value, e.max)}<div class="ico">${ic('home', 40)}</div><div class="mid">${val(c?.value, c?.unit || 'kW', 2)}<div class="lbl">Actueel verbruik</div></div></div>
      ${side ? `<div class="sep"></div><div class="side">${side}</div>` : ''}</div>
    ${ser.length ? `<div class="chart">${chart(ser, { now, tz, unit: '', dec: 0, h: 118, tag: 'en', min: 0 })}</div>
    <div class="legend"><span><i style="background:#4da3ff"></i>Verbruik</span>${ser.length > 1 ? '<span><i style="background:#f2b84b"></i>Productie</span>' : ''}</div>` : ''}</section>`;
}

// Camera's: een momentopname elke 2 seconden (de add-on haalt ze bij de camera; de tv ziet geen inloggegevens). Het laatste beeld blijft staan tijdens het verversen.
const camFrames = {};
let camBusy = false;
function tileCameras(d) {
  const cams = d.cameras || [];
  const demo = new URLSearchParams(location.search).get('demo');
  const cells = cams.map((c) => `<div class="cam ${demo ? 'demo' : ''}" data-n="${c.n}">${camFrames[c.n] ? `<img alt="" src="${camFrames[c.n]}">` : '<img alt="">'}<div class="cl">${esc(c.label)}</div><div class="cb">Camera niet bereikbaar</div></div>`).join('');
  return `<section class="card hero cams n${Math.min(cams.length, 4)}">${cells}</section>`;
}
async function camTick() {
  if (camBusy || document.hidden || new URLSearchParams(location.search).get('demo')) return;
  camBusy = true;
  try {
    for (const el of document.querySelectorAll('.cam')) {
      const n = el.dataset.n;
      try {
        const r = await fetch(`cam/${n}.jpg?t=${Date.now()}`, { cache: 'no-store' });
        if (!r.ok) throw new Error(r.status);
        const url = URL.createObjectURL(await r.blob()), old = camFrames[n];
        camFrames[n] = url;
        document.querySelectorAll(`.cam[data-n="${n}"] img`).forEach((i) => { i.src = url; });
        if (old) URL.revokeObjectURL(old);
        document.querySelectorAll(`.cam[data-n="${n}"]`).forEach((c) => c.classList.remove('bad'));
      } catch { document.querySelectorAll(`.cam[data-n="${n}"]`).forEach((c) => c.classList.add('bad')); }
    }
  } finally { camBusy = false; }
}

function tileHero(d) {
  if ((d.cameras || []).length) return tileCameras(d);
  const st = d.status || { ok: true };
  const note = `<div class="note ${st.ok ? '' : 'warn'}">${ic(st.ok ? 'check' : 'warn', 36)}<div><b>${st.ok ? 'Alles in orde' : esc(st.text || 'Let op')}</b><span>${st.ok ? 'Geen meldingen' : esc((st.alerts || []).join(' · '))}</span></div></div>`;
  if (d.photo) return `<section class="card hero" style="background-image:url('${esc(d.photo)}')"><div class="shade"></div>${note}</section>`;
  const hr = Number(new Date((d.now || Date.now() / 1000) * 1000).toLocaleString('nl-BE', { hour: 'numeric', hour12: false, timeZone: d.tz || undefined }));
  const hello = hr < 6 ? 'Goedenacht' : hr < 12 ? 'Goedemorgen' : hr < 18 ? 'Goedemiddag' : 'Goedenavond';
  return `<section class="card hero plain"><div class="hello">${hello}</div><div class="when">${esc(d.title || '')}</div>${note}</section>`;
}

function tileWeather(w, d) {
  if (!w) return '';
  const [icon, text] = w.state ? (COND[w.state] || ['cloud', w.text || '']) : ['thermo', ''];
  const hours = (w.hours || []).map((h) => `<div class="h"><div class="t">${esc(h.t)}</div>${ic((COND[h.state] || ['cloud'])[0], 38)}<div class="d">${num(h.temp, 0)}°</div></div>`).join('');
  const stats = [w.pressure != null && ['gauge', `${num(w.pressure, 0)}<small>hPa</small>`, 'Luchtdruk'], w.rain != null && ['rain', `${num(w.rain, 1)}<small>mm</small>`, 'Neerslag'],
    w.humidity != null && ['drop', `${num(w.humidity, 0)}<small>%</small>`, 'Vocht']].filter(Boolean)
    .map(([i, v, l]) => `<div class="ws">${ic(i, 34)}<div><b>${v}</b><span>${l}</span></div></div>`).join('');
  return `<section class="card weather"><h2>${ic(icon, 40)}Weer</h2>
    <div class="top"><div><div class="big">${num(w.temp, 1)}<span class="u">°C</span></div><div class="cond">${esc(w.text || text)}</div></div>
      ${w.max != null || w.min != null ? `<div class="mm"><div>Max <b>${num(w.max, 0)}°C</b></div><div>Min <b>${num(w.min, 0)}°C</b></div></div>` : ''}</div>
    ${stats ? `<div class="wstats">${stats}</div>` : ''}
    ${hours ? `<div class="hours">${hours}</div>` : ''}</section>`;
}

function metric(r) {
  return `<div class="metric ${r.tone || ''}">${ic(r.icon || 'dot', 38)}<div style="min-width:0">${r.text != null ? `<span class="val">${esc(r.text)}</span>` : val(r.value, r.unit, r.dec ?? 1)}<div class="lbl">${esc(r.label)}</div></div></div>`;
}

function tileSensors(t, now, tz) {
  const rows = t.rows || [], ser = t.series?.length ? [{ color: '#3fa0ff', points: t.series, fill: true }] : [];
  return `<section class="card"><h2>${ic(t.icon || 'waves', 40)}${esc(t.title)}${t.tag ? `<span class="tag">${esc(t.tag)}</span>` : ''}</h2>
    <div class="metrics">${rows.slice(0, 4).map(metric).join('')}</div>
    ${ser.length ? `<div class="chart" style="margin-top:12px">${chart(ser, { now, tz, unit: t.unit || '', dec: 0, h: 118, tag: t.id, hours: 12, auto: true })}</div>` : ''}</section>`;
}

function tileClimate(c, now, tz) {
  if (!c || !(c.rows || []).length) return '';
  const big = c.rows.slice(0, 2), small = c.rows.slice(2, 4);
  const ser = (c.series || []).map((s, i) => ({ color: ['#4da3ff', '#f2b84b'][i % 2], points: s, fill: false }));
  return `<section class="card"><h2>${ic('thermo', 40)}${esc(c.title || 'Temperaturen')}</h2>
    <div class="bigtwo">${big.map((r, i) => `${i ? '<div class="sep"></div>' : ''}<div class="b">${ic(r.icon || 'thermo', 34)}${val(r.value, r.unit || '°C', 1)}<div class="lbl">${esc(r.label)}</div></div>`).join('')}</div>
    ${small.length ? `<div class="subrow">${small.map((r) => `<div class="s">${ic(r.icon || 'flake', 34)}<div><b>${r.text != null ? esc(r.text) : `${num(r.value, 1)}${esc(r.unit || '°C')}`}</b><span>${esc(r.label)}</span></div></div>`).join('')}</div>` : ''}
    ${ser.length ? `<div class="chart" style="margin-top:8px">${chart(ser, { now, tz, unit: '°', dec: 0, h: 92, tag: 'kl', auto: true })}</div>` : ''}</section>`;
}

const dayText = (iso, today) => {
  const d = new Date(`${iso}T12:00:00`), t = new Date(`${today}T12:00:00`), n = Math.round((d - t) / 864e5);
  const abs = d.toLocaleDateString('nl-BE', { weekday: 'short', day: 'numeric', month: 'short' });
  return n === 0 ? ['Vandaag', abs] : n === 1 ? ['Morgen', abs] : [abs, `over ${n} dagen`];
};

function tileWaste(w, d) {
  if (!w) return '';
  const bins = ['#7bd88f', '#6cb6ff', '#f2b84b', '#a8b3c7'].map((c) => `<i style="background:${c}">${ic('bin', 30)}</i>`).join('');
  if (!w.items || !w.items.length) {
    return `<section class="card waste empty"><h2>${ic('bin', 40)}Ophaalkalender</h2><div class="ghost"><div class="bins">${bins}</div><div>${w.configured ? 'Geen ophaling de komende dagen' : 'Nog niet gekoppeld'}</div>${w.configured ? '' : '<div style="font-size:21px;color:var(--dim)">Kies een kalender in Huisconfig → TV</div>'}</div></section>`;
  }
  const today = w.today || new Date().toISOString().slice(0, 10);
  const byDay = []; // twee ophalingen op één dag (PMD en restafval) horen op één regel
  for (const it of w.items) {
    const last = byDay[byDay.length - 1];
    if (last && last.date === it.date) last.items.push(it);
    else byDay.push({ date: it.date, days: it.days, items: [it] });
  }
  const rows = byDay.slice(0, 3).map((g) => {
    const [a, b] = dayText(g.date, today), cols = g.items.map((it) => wasteColor(it.label, it.color));
    const dots = g.items.length > 1 ? `<div class="dots">${g.items.map((it, i) => `<i style="background:${cols[i]}"></i>`).join('')}</div>` : `<div class="dot" style="background:${cols[0]}">${ic('bin', 34)}</div>`;
    return `<div class="pick ${g.days <= 1 ? 'soon' : ''}">${dots}<div class="nm ${g.items.length > 1 ? 'many' : ''}">${g.items.map((it) => esc(it.label)).join(' · ')}</div><div class="dt"><b>${esc(a)}</b><span>${esc(b)}</span></div></div>`;
  }).join('');
  return `<section class="card waste"><h2>${ic('bin', 40)}Ophaalkalender</h2><div class="list">${rows}</div></section>`;
}

const chip = (c) => `<div class="chip ${c.tone || (c.on ? 'on' : '')}"><div class="ico">${ic(c.icon || 'dot', 34)}</div><div><b>${esc(c.label)}</b><span>${esc(c.text)}</span></div></div>`;

// ---------------------------------------------------------------- pagina
function render(d) {
  const tz = d.tz || undefined, now = d.now, w = d.weather;
  const [wi] = w && w.state ? (COND[w.state] || ['cloud']) : [];
  const st = d.status || { ok: true };
  const date = new Date(now * 1000).toLocaleDateString('nl-BE', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: tz }).replace(/\.$/, '');
  const head = `<div class="head"><h1>${esc(d.title || 'Mijn huis')}</h1><div class="pill ${st.ok ? '' : 'warn'}"><i></i>${st.ok ? 'Alles OK' : esc(st.text || 'Let op')}</div><div class="sp"></div>
    <div class="date">${esc(date.charAt(0).toUpperCase() + date.slice(1))}</div><div class="clock" id="clock">${hhmm(now, tz)}</div>
    ${w ? `<div class="now">${ic(wi || 'thermo', 52)}<div><div class="t">${num(w.temp, 0)}°C</div>${w.feels != null ? `<div class="f">Voelt aan: ${num(w.feels, 0)}°C</div>` : ''}</div></div>` : ''}</div>`;
  const r1 = [tileEnergy(d.energy, now, tz), tileHero(d), tileWeather(w, d)].filter(Boolean);
  const tiles = (d.tiles || []).map((t) => tileSensors(t, now, tz));
  const r2 = [tiles[0], tileClimate(d.climate, now, tz), tileWaste(d.waste, d), ...tiles.slice(1)].filter(Boolean).slice(0, 3);
  const cols = (n) => (n === 3 ? '1.05fr 1fr 0.95fr' : `repeat(${n}, 1fr)`);
  const chips = (d.chips || []).slice(0, 5).map(chip).join('');
  const next = d.next ? `<div class="chip next"><div class="ico">${ic('cal', 34)}</div><div><b>${esc(d.next.label || 'Volgende')}</b><span>${esc(d.next.text)}</span></div></div>` : '';
  const stale = d.age != null && d.age > STALE ? `<div class="stale">gegevens van ${Math.round(d.age / 60)} min geleden</div>` : '';
  return `${head}<div class="row r1" style="grid-template-columns:${cols(r1.length)}">${r1.join('')}</div>
    ${r2.length ? `<div class="row r2" style="grid-template-columns:${cols(r2.length)}">${r2.join('')}</div>` : ''}
    <div class="foot">${chips}${next}</div>${stale}`;
}

function fit() { // de stage van 1920x1080 past in elk venster, met balken waar de verhouding niet klopt
  const s = Math.min(innerWidth / 1920, innerHeight / 1080), st = $('#stage');
  st.style.transform = `scale(${s}) translate(-50%, -50%)`;
  st.style.transformOrigin = '0 0';
  st.style.left = '50%'; st.style.top = '50%';
  st.style.marginLeft = '0';
}
addEventListener('resize', fit);

// ---------------------------------------------------------------- gegevens
const DEMO = (() => {
  const now = Math.floor(Date.now() / 1000), series = (f, n = 72) => Array.from({ length: n }, (_, i) => [now - (n - i) * 600, f(i / n)]);
  return {
    title: 'Mijn Huis', now, tz: 'Europe/Brussels', age: 0, status: { ok: true }, photo: '',
    weather: { state: 'partlycloudy', temp: 8.4, feels: 6, text: 'Licht bewolkt', max: 10, min: 4, pressure: 1022, humidity: 83, rain: 0.4, hours: [{ t: '21u', state: 'clear-night', temp: 6 }, { t: '22u', state: 'clear-night', temp: 5 }, { t: '23u', state: 'clear-night', temp: 5 }, { t: '00u', state: 'cloudy', temp: 5 }, { t: '01u', state: 'cloudy', temp: 4 }] },
    energy: { consumption: { value: 1.24, unit: 'kW' }, max: 5, solar: { value: 3.8, unit: 'kW' }, battery: { pct: 68, detail: '2,1 kWh' }, trend: -12,
      series: { consumption: series((x) => 0.8 + 0.6 * Math.sin(x * 9) ** 2 + 0.3 * Math.sin(x * 31)), solar: series((x) => Math.max(0, 3.6 * Math.sin(Math.PI * Math.min(1, x * 1.3)))) } },
    tiles: [{ id: 'zwembad', title: 'Zwembad', icon: 'waves', tag: '24,6°C', rows: [{ label: 'pH', value: 7.2, icon: 'drop' }, { label: 'Chloor', value: 1.1, unit: 'ppm', icon: 'flask' }, { label: 'Pomp', text: 'Aan', icon: 'pump', tone: 'on' }], series: series((x) => 25 - 1.2 * Math.sin(x * 5)), unit: '°' }],
    climate: { title: 'Temperaturen', rows: [{ label: 'Woonkamer', value: 20.5, unit: '°C', icon: 'home' }, { label: 'Slaapkamer', value: 18, unit: '°C', icon: 'thermo' }, { label: 'Gewenst', value: 22, unit: '°C', icon: 'flake' }, { label: 'Ventilatie', text: 'Auto', icon: 'fan' }],
      series: [series((x) => 19 + 1.5 * Math.sin(x * 6)), series((x) => 7 + 5 * Math.sin(x * 4))] },
    waste: { configured: true, today: new Date().toISOString().slice(0, 10), items: [{ date: new Date(Date.now() + 864e5).toISOString().slice(0, 10), label: 'PMD', days: 1 }, { date: new Date(Date.now() + 4 * 864e5).toISOString().slice(0, 10), label: 'Restafval', days: 4 }, { date: new Date(Date.now() + 9 * 864e5).toISOString().slice(0, 10), label: 'GFT', days: 9 }] },
    chips: [{ label: 'Eco modus', text: 'Actief', icon: 'leaf', on: true }, { label: 'Nachtmodus', text: 'Uit', icon: 'moon' }, { label: 'Alarm', text: 'Uit', icon: 'lock' }, { label: 'Deurbel', text: 'Geen meldingen', icon: 'bell' }],
    next: { label: 'Volgende activiteit:', text: 'Zwembadpomp 22:00' },
    cameras: new URLSearchParams(location.search).get('cam') ? [{ n: 0, label: 'Voortuin' }] : [],
  };
})();

let last = null, fetchedAt = 0;
async function load() {
  const q = new URLSearchParams(location.search);
  if (q.get('demo')) return DEMO;
  const r = await fetch('data', { cache: 'no-store' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
async function tick() {
  try {
    last = await load();
    fetchedAt = Date.now();
    $('#stage').innerHTML = render(last);
  } catch (e) {
    if (!last) $('#stage').innerHTML = `<div class="msg">Geen gegevens (${esc(e.message)})</div>`;
    else if (!$('.stale')) $('#stage').insertAdjacentHTML('beforeend', '<div class="stale">geen verbinding met Huisconfig</div>');
  }
}
setInterval(() => { // de klok loopt zelf, tussen twee ophaalrondes
  if (!last) return;
  const el = $('#clock');
  if (el) el.textContent = hhmm(last.now + (Date.now() - fetchedAt) / 1000, last.tz);
}, 1000);
// Volledig scherm. De browser van de tv houdt zijn adresbalk over de pagina tot de pagina volledig scherm krijgt, en dat mag alleen na een klik
// met de aanwijzer (op een echte LG gemeten: een druk op OK bereikt de pagina niet, een klik wel). De add-on stuurt die klik zelf nadat hij de
// pagina opende (lghub.py); wie de pagina met de hand opent, klikt eenmaal. In de voorvertoning (een iframe) doen we dit niet.
const isFrame = window.self !== window.top;
const fsElement = () => document.fullscreenElement || document.webkitFullscreenElement;
function goFull() {
  if (isFrame || fsElement()) return;
  const el = document.documentElement, f = el.requestFullscreen || el.webkitRequestFullscreen;
  if (f) try { Promise.resolve(f.call(el)).catch(() => {}); } catch { /* geen volledig scherm mogelijk */ }
}
for (const ev of ['pointerdown', 'click', 'keydown']) addEventListener(ev, goFull, true);
const hint = document.createElement('div');
hint.className = 'fshint';
hint.textContent = 'Klik met de aanwijzer voor volledig scherm';
document.body.appendChild(hint);
const showHint = () => { hint.style.display = isFrame || fsElement() || new URLSearchParams(location.search).get('demo') ? 'none' : 'block'; };
['fullscreenchange', 'webkitfullscreenchange'].forEach((ev) => document.addEventListener(ev, () => { showHint(); fit(); }));
showHint();

fit();
tick();
setInterval(tick, POLL);
setInterval(camTick, 2000);
setTimeout(camTick, 400);
