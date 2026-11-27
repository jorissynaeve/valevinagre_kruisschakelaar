'use strict';
// Pictogrammen als inline SVG: geen lettertype of afbeelding om te laden, ze nemen de kleur van de tekst over.
// Eigen tekeningen op een raster van 24 x 24 (lijnen van 1,8), bedoeld om klein te staan.

const ICONS = {
  // navigatie
  schema: '<rect x="3" y="3.5" width="7" height="6" rx="1.5"/><rect x="14" y="14.5" width="7" height="6" rx="1.5"/><path d="M10 6.5h3a3 3 0 0 1 3 3v5"/>',
  nodes: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2.5v3.5M15 2.5v3.5M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>',
  energy: '<path d="M13 2.5 5 13.5h6l-1 8 8-11h-6z"/>',
  charge: '<path d="M9 2.5v4.5M15 2.5v4.5M6 7h12v3.5a6 6 0 0 1-12 0z"/><path d="M12 16.5v5"/>',
  log: '<path d="M8.5 6h12M8.5 12h12M8.5 18h12"/><circle cx="4" cy="6" r=".6"/><circle cx="4" cy="12" r=".6"/><circle cx="4" cy="18" r=".6"/>',
  settings: '<path d="M4 6h9M19 6h1M4 12h3M13 12h7M4 18h11M21 18h-1"/><circle cx="16" cy="6" r="2.2"/><circle cx="10" cy="12" r="2.2"/><circle cx="18" cy="18" r="2.2"/>',
  home: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10"/><path d="M10 20v-5h4v5"/>',
  // toestellen
  lamp: '<path d="M9.5 18h5M10.5 21h3"/><path d="M12 3a6 6 0 0 0-3.4 10.9c.6.5 1 1.2 1 2.1h4.8c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/>',
  switch: '<rect x="2.5" y="7" width="19" height="10" rx="5"/><circle cx="16.5" cy="12" r="2.4"/>',
  panel: '<rect x="5" y="2.5" width="14" height="19" rx="2.5"/><path d="M10.5 18h3"/>',
  screen: '<rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8 20.5h8M12 16.5v4"/>',
  tuya: '<rect x="3.5" y="3.5" width="17" height="17" rx="4.5"/><path d="M9 9v2.5M15 9v2.5M9.5 15.5h5"/>',
  airco: '<path d="M12 2.5v19M3.8 7.2l16.4 9.6M3.8 16.8 20.2 7.2"/><path d="m9.5 4.5 2.5 2 2.5-2M9.5 19.5l2.5-2 2.5 2"/>',
  tv: '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8 20.5h8M12 17v3.5"/>',
  solar: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  // tijd
  timer: '<circle cx="12" cy="13.5" r="7.5"/><path d="M12 9.5v4l2.5 1.5M9.5 2.5h5"/>',
  sunrise: '<path d="M2.5 19h19M6.5 19a5.5 5.5 0 0 1 11 0M12 3v7M9 6l3-3 3 3M4.5 11.5l1.4 1.4M19.5 11.5l-1.4 1.4"/>',
  sunset: '<path d="M2.5 19h19M6.5 19a5.5 5.5 0 0 1 11 0M12 10V3M9 7l3 3 3-3M4.5 11.5l1.4 1.4M19.5 11.5l-1.4 1.4"/>',
  time: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
  // waarden, rekenen, vergelijken, logica
  number: '<path d="M4.5 9h15M4.5 15h15M10.5 3.5 8.5 20.5M15.5 3.5l-2 17"/>',
  select: '<path d="M4 6h5l6 12h5M4 18h5l2-4M13 10l2-4h5M17 3l3 3-3 3M17 15l3 3-3 3"/>',
  dp: '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v6.5c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V5.5M4.5 12v6.5c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V12"/>',
  add: '<path d="M12 5v14M5 12h14"/>',
  sub: '<path d="M5 12h14"/>',
  between: '<path d="M4 4.5v15M20 4.5v15M8 12h8M10.5 9l-3 3 3 3M13.5 9l3 3-3 3"/>',
  after: '<path d="M4 4.5v15M8 12h12M15.5 7l5 5-5 5"/>',
  before: '<path d="M20 4.5v15M16 12H4M8.5 7l-5 5 5 5"/>',
  above: '<path d="m8 5 9 7-9 7"/>',
  below: '<path d="m16 5-9 7 9 7"/>',
  and: '<path d="M5.5 20.5V11a6.5 6.5 0 0 1 13 0v9.5z"/>',
  or: '<path d="M4.5 4c5 0 12 .5 15 8-3 7.5-10 8-15 8 3-4 3-12 0-16z"/>',
  // routines
  sprinkler: '<path d="M12 3s6.5 6.5 6.5 11a6.5 6.5 0 0 1-13 0C5.5 9.5 12 3 12 3z"/><path d="M9 14.5a3 3 0 0 0 3 3"/>',
  action: '<path d="M7 4.5 19 12 7 19.5z"/>',
  scenes: '<path d="M12 3a9 9 0 1 0 0 18c1.6 0 2.2-1 2.2-2s-.8-1.7-.8-2.7S14.2 14 15.3 14H17a4 4 0 0 0 4-4c0-3.9-4-7-9-7z"/><circle cx="7.5" cy="11.5" r="1"/><circle cx="10" cy="7.5" r="1"/><circle cx="15" cy="7.5" r="1"/>',
  // kaarten
  cards: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
  qr: '<rect x="3.5" y="3.5" width="6" height="6" rx="1"/><rect x="14.5" y="3.5" width="6" height="6" rx="1"/><rect x="3.5" y="14.5" width="6" height="6" rx="1"/><path d="M14.5 14.5h2.5v2.5h-2.5zM20.5 14.5v.01M14.5 20.5h.01M17.5 20.5h3v-3"/>',
  wifi: '<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.5" r=".8"/>',
  // koppelingen
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/><path d="M12 15v2"/>',
  door: '<rect x="5" y="3" width="14" height="18" rx="1.5"/><circle cx="15" cy="12" r=".9"/><path d="M3 21h18"/>',
  wind: '<path d="M3 8.5h11a3 3 0 1 0-3-3M3 12.5h15a3 3 0 1 1-3 3M3 16.5h7a3 3 0 1 1-3 3"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx=".5"/><path d="M9 2.5v3.5M15 2.5v3.5M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>',
  camera: '<path d="M4 7.5h3l1.5-2.5h7L17 7.5h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.5"/>',
  // bediening
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20.5 20.5-4.8-4.8"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  right: '<path d="m9 5.5 6.5 6.5L9 18.5"/>',
  down: '<path d="m5.5 9 6.5 6.5L18.5 9"/>',
  more: '<circle cx="5.5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="18.5" cy="12" r="1.3"/>',
  edit: '<path d="M4 20h4L19.5 8.5a2.8 2.8 0 0 0-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  wand: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 17v4M17 19h4"/>',
  expand: '<path d="M4 9.5V4h5.5M20 9.5V4h-5.5M4 14.5V20h5.5M20 14.5V20h-5.5"/>',
  shrink: '<path d="M9.5 4v5.5H4M14.5 4v5.5H20M9.5 20v-5.5H4M14.5 20v-5.5H20"/>',
  layout: '<rect x="3" y="3.5" width="18" height="17" rx="2"/><path d="M3 9h18M9.5 9v11.5"/>',
  tray: '<rect x="3" y="3.5" width="18" height="17" rx="2"/><path d="M9 3.5v17"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  fit: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M9 12h6M12 9v6"/>',
  room: '<path d="M4 20V8.5L12 4l8 4.5V20z"/><path d="M10 20v-6h4v6"/>',
  bolt: '<path d="M13 2.5 5 13.5h6l-1 8 8-11h-6z"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5v.01"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 .9-1 1.7M12 17v.01"/>',
};

// Welk pictogram bij welke module in het menu "+ Module" hoort (zelfde sleutels als SC_MENU in schema.js)
const MENU_ICON = {
  lamp: 'lamp', switch: 'switch', panel: 'panel', screen: 'screen', tuya: 'tuya', airco: 'airco', lg: 'tv', charge: 'charge', solar: 'solar',
  timer: 'timer', sunrise: 'sunrise', sunset: 'sunset', time: 'time', number: 'number', select: 'select', dp: 'dp', add: 'add', sub: 'sub',
  between: 'between', after: 'after', before: 'before', above: 'above', below: 'below', and: 'and', or: 'or',
  sprinkler: 'sprinkler', action: 'action', scenes: 'scenes',
  'card-grid': 'cards', 'card-charger': 'charge', 'card-saver': 'moon', 'card-airco': 'airco', 'card-power': 'energy', 'card-qr': 'qr',
};

function ico(name, size) {
  const d = ICONS[name] || ICONS.cpu;
  return `<svg class="ico" viewBox="0 0 24 24" width="${size || 18}" height="${size || 18}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${d}</svg>`;
}
