'use strict';
// Tabblad TV: wat er op de tv-pagina staat en uit welke entiteiten dat komt (kkaddon/tvhub.py). Rechts het echte resultaat.
// Alles wat je wijzigt, wordt meteen bewaard; de voorvertoning ververst zelf. Wordt vóór app.js geladen en gebruikt zijn globals
// ($, api, act, toast, esc, S) pas bij gebruik.

let TV = null; // {config, port, listening, ha, src: {ha: [[entiteit, naam, eenheid, stand]], addon: [...]}}
let TVERR = '';
let TVSAVING = 0;

const TV_ICONS = { dot: 'punt', sun: 'zon', moon: 'maan', cloud: 'wolk', partly: 'half bewolkt', rain: 'regen', pour: 'zware regen', snow: 'sneeuw', bolt: 'bliksem', fog: 'mist', wind: 'wind',
  drop: 'druppel', flask: 'maatkolf', power: 'aan/uit', pump: 'pomp', home: 'huis', thermo: 'thermometer', flake: 'koelen', fan: 'ventilator', cal: 'kalender', bin: 'afval', leaf: 'blad',
  lock: 'slot', unlock: 'open slot', bell: 'bel', door: 'deur', check: 'vinkje', warn: 'waarschuwing', solar: 'zonnepanelen', battery: 'batterij', zap: 'stroom', waves: 'water', flame: 'vlam',
  car: 'auto', water: 'water (druppel)', gauge: 'meter' };
const TV_ROW = () => ({ label: '', source: '', icon: '', unit: '', dec: 1, on_text: '', off_text: '' });

async function loadTv() {
  try {
    const [v, src] = await Promise.all([api('GET', 'tv'), api('GET', 'tv/sources')]);
    TV = { ...v, src };
    TVERR = '';
  } catch (e) { TV = null; TVERR = e.message; }
  const box = $('#tvbox');
  if (box) box.innerHTML = viewTvBody();
}

function viewTv() {
  if (!$('#tvbox')) setTimeout(loadTv, 0);
  return `<h2>TV</h2>
    <p class="sub">Een alleen-lezen dashboard voor de tv: klok, weer, energie, temperaturen, de ophaalkalender en de stand van het huis. Er is niets te bedienen en er staat geen menu op, want op een tv kun je niet navigeren.
    Kies hier uit welke entiteiten van Home Assistant (of uitgangen van blokken, zoals een omvormer) het komt; wat je leeg laat, verschijnt niet. Het tv-blok in het schema opent het adres hieronder.</p>
    <div id="tvbox">${viewTvBody()}</div>`;
}

// ---------------------------------------------------------------- velden

const tvVal = (path) => path.split('.').reduce((o, k) => (o == null ? o : o[k]), TV.config);
function tvSet(path, value) {
  const keys = path.split('.'), last = keys.pop();
  keys.reduce((o, k) => o[k], TV.config)[last] = value;
}

function tvState(src) { // de stand van een gekozen bron, ter controle
  if (!src) return '';
  const hit = [...TV.src.ha, ...TV.src.addon].find((x) => x[0] === src);
  if (!hit) return src.includes('.') || /^[a-z]:/.test(src) ? '<span class="sub" style="color:var(--bad)">niet gevonden</span>' : '';
  return `<span class="sub">${esc(hit[1])}${hit[3] !== '' ? ` · nu ${esc(hit[3])} ${esc(hit[2])}` : ''}</span>`;
}

function tvStatusWx() { // wat het weerstation nu geeft, of waarom niet
  if (!TV || !TV.config.weather_json.url) return '';
  if (TV.errors && TV.errors.weerstation) return `<span style="color:var(--bad)">Weerstation: ${esc(TV.errors.weerstation)}</span>`;
  const s = TV.station || {};
  if (s.ok !== true) return 'Weerstation: nog niet opgehaald…';
  const f = (v, u, d = 1) => (v == null ? '–' : `${Number(v).toLocaleString('nl-BE', { maximumFractionDigits: d })} ${u}`);
  return `Weerstation nu: ${f(s.temp, '°C')} · ${f(s.pressure, 'hPa', 0)} · ${f(s.humidity, '%', 0)} · neerslag vandaag ${f(TV.rain_today, 'mm')} (een veld dat '–' toont, bestaat niet of is te oud)`;
}
function tvStatusIcsFile() { // het geüploade bestand: wat erin zit, en of het nog lang genoeg loopt
  const f = TV && TV.ics_file;
  if (!f || !f.loaded) return 'Geen bestand geüpload.';
  const d = (iso) => (iso ? new Date(`${iso}T12:00:00`).toLocaleDateString('nl-BE', { day: 'numeric', month: 'short', year: 'numeric' }) : '–');
  const soon = f.last && (new Date(`${f.last}T12:00:00`) - Date.now()) / 864e5 < 30;
  return `Bestand${f.name ? ` “${esc(f.name)}”` : ''}: ${f.count} ophalingen, van ${d(f.first)} tot ${d(f.last)}.${soon ? ' <span style="color:var(--bad)">Het bestand loopt bijna af: upload een nieuw.</span>' : ''}${f.count ? '' : ' <span style="color:var(--bad)">Geen enkele ophaling komt nog.</span>'}`;
}
function tvStatusIcs() {
  if (!TV || !TV.config.waste.ics) return '';
  if (TV.errors && TV.errors.ophaalkalender) return `<span style="color:var(--bad)">Kalenderlink: ${esc(TV.errors.ophaalkalender)}</span>`;
  if (TV.ics_ok === null || TV.ics_ok === undefined) return 'Kalenderlink: nog niet opgehaald…';
  return `Kalenderlink: ${TV.ics_count} afspraken gelezen`;
}
async function tvStatus() { // alleen de statusregels bijwerken, zonder het formulier opnieuw te tekenen
  try {
    const v = await api('GET', 'tv');
    Object.assign(TV, { errors: v.errors, station: v.station, rain_today: v.rain_today, ics_count: v.ics_count, ics_ok: v.ics_ok, ics_file: v.ics_file });
    const a = $('#tvwx'), b = $('#tvics');
    if (a) a.innerHTML = tvStatusWx();
    if (b) b.innerHTML = tvStatusIcs();
    const c = $('#tvicsf');
    if (c) c.innerHTML = tvStatusIcsFile();
  } catch { /* de volgende keer */ }
}

function tvIn(label, path, o = {}) {
  const v = tvVal(path);
  return `<div style="min-width:${o.w || 150}px;flex:${o.flex ?? 1}"><label>${esc(label)}</label>
    <input type="${o.type || 'text'}" data-tv="${path}" ${o.list ? `list="${o.list}"` : ''} ${o.max ? `maxlength="${o.max}"` : ''} ${o.step ? `step="${o.step}" min="${o.min}" max="${o.maxv}"` : ''} spellcheck="false" autocomplete="off" value="${esc(v ?? '')}" placeholder="${esc(o.ph || '')}">
    ${o.state ? tvState(v) : ''}</div>`;
}

const tvUnit = (path) => `<div style="min-width:96px;flex:0"><label>Eenheid</label><select data-tv="${path}">${[['', 'automatisch'], ['W', 'W'], ['kW', 'kW']].map(([v, t]) => `<option value="${v}" ${(tvVal(path) || '') === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div>`;
const tvSrc = (label, path, list = 'tvsrc', ph = 'sensor.…') => tvIn(label, path, { list, ph, state: true, w: 200 });
const tvIcon = (path) => `<div style="min-width:120px"><label>Pictogram</label><select data-tv="${path}" data-re="1">${Object.entries({ '': '(geen)', ...TV_ICONS }).map(([k, t]) => `<option value="${k}" ${tvVal(path) === k ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>`;

function tvRows(base, max, o = {}) { // een lijst meetwaarden
  const rows = tvVal(base) || [];
  const list = rows.map((r, i) => `<div class="row" style="align-items:flex-start;border-top:1px dashed var(--line);padding-top:6px">
      ${tvIn('Naam', `${base}.${i}.label`, { w: 110, max: 30, ph: o.ph || 'bv. pH' })}${tvSrc('Bron', `${base}.${i}.source`)}${tvIcon(`${base}.${i}.icon`)}
      ${tvIn('Eenheid', `${base}.${i}.unit`, { w: 70, flex: 0, max: 8, ph: 'auto' })}${tvIn('Tekst bij aan', `${base}.${i}.on_text`, { w: 90, flex: 0, max: 20, ph: 'Aan' })}${tvIn('Tekst bij uit', `${base}.${i}.off_text`, { w: 90, flex: 0, max: 20, ph: 'Uit' })}
      <button class="btn small danger" type="button" data-tvact="del" data-p="${base}.${i}" title="Weghalen" style="margin-top:22px">✕</button></div>`).join('');
  return `${list}<div class="row" style="margin-top:4px"><button class="btn small" type="button" data-tvact="addrow" data-p="${base}" ${rows.length >= max ? 'disabled' : ''}>+ Waarde</button>
    <span class="sub">Een getal toont de waarde met zijn eenheid; een stand (aan/uit, open/dicht) toont een woord. Eigen teksten kies je bij <i>tekst bij aan/uit</i>. Hoogstens ${max}.</span></div>`;
}

// ---------------------------------------------------------------- het tabblad

function tvAddress() {
  const c = TV.config;
  return `http://${TV.host || location.hostname}:${TV.port}/tv/${c.token}/`; // het LAN-adres van de add-on: via de cloud van HA is de browser een ander adres
}

function viewTvBody() {
  if (!TV) return TVERR ? `<p class="empty">${esc(TVERR)}</p>` : '<p class="sub">Laden…</p>';
  const c = TV.config, srcs = (f) => TV.src.ha.filter((x) => f(x[0])).map((x) => `<option value="${esc(x[0])}">${esc(x[1])}</option>`).join('');
  const addon = TV.src.addon.map((x) => `<option value="${esc(x[0])}">${esc(x[1])}</option>`).join('');
  const lists = `<datalist id="tvsrc">${TV.src.ha.map((x) => `<option value="${esc(x[0])}">${esc(x[1])}</option>`).join('')}${addon}</datalist>
    <datalist id="tvweather">${srcs((e) => e.startsWith('weather.'))}</datalist><datalist id="tvcal">${srcs((e) => e.startsWith('calendar.'))}</datalist>
    <datalist id="tvbin">${srcs((e) => /^(binary_sensor|input_boolean|switch)\./.test(e))}</datalist>`;
  const url = tvAddress();
  const tvs = (S && S.lg) || [];
  const tvblock = tvs.length ? `<div class="row" style="margin-top:6px"><select id="tvblock">${tvs.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}${t.url === url ? ' (al ingesteld)' : ''}</option>`).join('')}</select>
      <button class="btn small" type="button" data-tvact="seturl">Zet als dashboard-adres van deze tv</button></div>` : '<div class="sub">Zet een TV-blok op het schema om dit adres daar als dashboard in te vullen.</div>';
  const warn = !TV.port ? '<p class="empty">De tv-pagina staat uit (optie <code>tv_port</code> is 0).</p>' : !TV.listening ? '<p class="empty">De tv-pagina luistert niet: de poort is waarschijnlijk in gebruik. Zie het logboek van de add-on.</p>' : '';
  const tiles = (c.tiles || []).map((t, i) => `<div class="card" style="margin-bottom:8px"><div class="row sp"><h3>Tegel ${i + 1}</h3><button class="btn small danger" type="button" data-tvact="del" data-p="tiles.${i}">Tegel weghalen</button></div>
      <div class="row">${tvIn('Titel', `tiles.${i}.title`, { w: 160, max: 30 })}${tvIcon(`tiles.${i}.icon`)}${tvSrc('Label rechtsboven', `tiles.${i}.tag`)}${tvSrc('Grafiek van', `tiles.${i}.chart`)}</div>
      ${tvRows(`tiles.${i}.rows`, 4)}</div>`).join('');
  const chips = (c.chips || []).map((r, i) => `<div class="row" style="align-items:flex-start;border-top:1px dashed var(--line);padding-top:6px">
      ${tvIn('Naam', `chips.${i}.label`, { w: 120, max: 30, ph: 'bv. Alarm' })}${tvSrc('Bron', `chips.${i}.source`)}${tvIcon(`chips.${i}.icon`)}
      ${tvIn('Tekst bij aan', `chips.${i}.on_text`, { w: 100, flex: 0, max: 20, ph: 'Aan' })}${tvIn('Tekst bij uit', `chips.${i}.off_text`, { w: 100, flex: 0, max: 20, ph: 'Uit' })}
      <button class="btn small danger" type="button" data-tvact="del" data-p="chips.${i}" title="Weghalen" style="margin-top:22px">✕</button></div>`).join('');
  const alerts = (c.alerts || []).map((a, i) => `<div class="row"><input type="text" list="tvbin" data-tv="alerts.${i}" value="${esc(a)}" spellcheck="false" autocomplete="off" placeholder="binary_sensor.rookmelder" style="flex:1;min-width:220px">${tvState(a)}
      <button class="btn small danger" type="button" data-tvact="del" data-p="alerts.${i}">✕</button></div>`).join('');
  return `${lists}
    <div class="tvgrid"><div class="tvform">
      <div class="card"><h3>Adres voor de tv</h3>
        <div class="row"><input type="text" readonly id="tvurl" value="${esc(url)}" style="flex:1;min-width:260px" onclick="this.select()">
          <button class="btn small" type="button" data-tvact="copy">Kopieer</button><button class="btn small" type="button" data-tvact="token">Nieuw adres</button></div>
        <div class="sub">Dit adres is alleen-lezen en bevat een geheim: wie het kent, ziet wat hieronder staat (niets anders). Het werkt op je thuisnetwerk, op poort ${esc(TV.port)}; het IP-adres is dat van Home Assistant, dus geef dat een vast adres in je router. Deel je het per ongeluk, kies dan <b>Nieuw adres</b>; vul het daarna opnieuw in bij het tv-blok.</div>
        ${tvblock}${warn}${TV.ha ? '' : '<p class="empty">Home Assistant is niet verbonden: er zijn nog geen entiteiten om uit te kiezen.</p>'}</div>
      <div class="card"><h3>Algemeen</h3>
        <div class="row">${tvIn('Titel', 'title', { w: 200, max: 40 })}${tvIn('Foto in het midden (adres, mag leeg)', 'photo', { w: 260, max: 300, ph: '/local/huis.jpg' })}</div>
        <div class="row">${tvSrc('Weer (voorspelling en beeld, uit HA)', 'weather', 'tvweather', 'weather.thuis')}</div>
        <div class="row">${tvIn('Weerstation (JSON-adres, mag leeg)', 'weather_json.url', { w: 360, max: 400, ph: 'https://…/current/all' })}</div>
        <div class="row">${tvIn('Temperatuur', 'weather_json.temp', { w: 110, max: 60 })}${tvIn('Luchtdruk', 'weather_json.pressure', { w: 110, max: 60 })}${tvIn('Vochtigheid', 'weather_json.humidity', { w: 110, max: 60 })}${tvIn('Regenteller', 'weather_json.rain_total', { w: 110, max: 60 })}${tvIn('Hoogstens zo oud (min)', 'weather_json.max_age', { w: 90, flex: 0, type: 'number', step: '5', min: 5, maxv: 1440 })}</div>
        <div class="sub" id="tvwx">${tvStatusWx()}</div>
        <div class="sub">Een eigen weerstation: de temperatuur, luchtdruk, luchtvochtigheid en de neerslag van vandaag komen dan van dat adres (de temperatuur gaat voor die van HA). De namen zijn de velden in de JSON; een meting is <code>{"time": …, "value": …}</code> of een kaal getal, en een meting die ouder is dan de grens telt als onbekend. De regenteller loopt op (mm sinds de installatie): de neerslag van vandaag is het verschil met middernacht. Zonder HA-weer toont de tegel alleen deze metingen.</div>
        <div><label>Meldingen (bv. rookmelder, waterlek): staat er een aan, dan toont de kop "Let op"</label>${alerts}
          <div class="row" style="margin-top:4px"><button class="btn small" type="button" data-tvact="addalert" ${(c.alerts || []).length >= 20 ? 'disabled' : ''}>+ Melding</button></div></div></div>
      <div class="card"><h3>Energie</h3>
        <div class="row">${tvSrc('Verbruik', 'energy.consumption')}${tvUnit('energy.units.consumption')}${tvSrc('Zonnepanelen', 'energy.solar')}${tvUnit('energy.units.solar')}${tvIn('Meter tot (kW)', 'energy.max', { w: 90, flex: 0, type: 'number', step: '0.5', min: 0.5, maxv: 100 })}</div>
        <div class="row">${tvSrc('Batterij (%)', 'energy.battery')}${tvSrc('Batterij (kWh, mag leeg)', 'energy.battery_kwh')}${tvSrc('Net (mag leeg)', 'energy.grid')}${tvUnit('energy.units.grid')}</div>
        <div class="sub">Een bron in W wordt omgerekend naar kW. <b>Een waarde van Loxone heeft geen eenheid</b>: kies W of kW bij de bron (de lijst bevat die als <i>Loxone · naam</i>; je maakt ze aan bij Instellingen → Loxone → <i>Een waarde van Loxone gebruiken</i>, soort <i>getal</i>). De grafiek toont de laatste 12 uur van verbruik en zonnepanelen, uit de geschiedenis van HA aangevuld met wat de add-on zelf meet; bij een waarde van Loxone bouwt ze zich op vanaf het opstarten.</div></div>
      <div class="card"><h3>Tegels</h3><div class="sub">Tot 3 eigen tegels, bv. een zwembad, een kelder of een serre: een titel, tot 4 meetwaarden en een grafiek van één waarde. De onderste rij heeft plaats voor 3 tegels: <b>tegel 1</b>, <b>Temperaturen</b> en de <b>ophaalkalender</b>, en daarna de overige tegels voor zover er plaats is. Zet Temperaturen leeg of de ophaalkalender uit om plaats te maken.</div>${tiles}
        <div class="row"><button class="btn small" type="button" data-tvact="addtile" ${(c.tiles || []).length >= 3 ? 'disabled' : ''}>+ Tegel</button></div></div>
      <div class="card"><h3>Temperaturen</h3>
        <div class="row">${tvIn('Titel', 'climate.title', { w: 200, max: 30 })}${tvSrc('Grafiek 1', 'climate.chart.0')}${tvSrc('Grafiek 2', 'climate.chart.1')}</div>
        ${tvRows('climate.rows', 4, { ph: 'bv. Woonkamer' })}<div class="sub">De eerste twee staan groot, de volgende twee kleiner.</div></div>
      <div class="card"><h3>Ophaalkalender</h3>
        <div class="row"><label style="margin:0"><input type="checkbox" data-tv="waste.on" ${c.waste.on ? 'checked' : ''}> Tonen</label>${tvSrc('Kalender in HA', 'waste.calendar', 'tvcal', 'calendar.afval')}${tvIn('Dagen vooruit', 'waste.days', { w: 90, flex: 0, type: 'number', step: '1', min: 3, maxv: 60 })}</div>
        <div class="row"><label class="btn small" style="cursor:pointer;margin:0">Kies een ICS-bestand…<input type="file" id="tvicsfile" accept=".ics,text/calendar,text/plain" hidden></label>
          <button class="btn small danger" type="button" data-tvact="icsdel" ${TV.ics_file && TV.ics_file.loaded ? '' : 'disabled'}>Bestand verwijderen</button></div>
        <div class="sub" id="tvicsf">${tvStatusIcsFile()}</div>
        <div class="row">${tvIn('Of een kalenderlink (ICS), als je er een hebt', 'waste.ics', { w: 360, max: 400, ph: 'https://…/kalender.ics' })}</div>
        <div class="sub" id="tvics">${tvStatusIcs()}</div>
        <div class="sub">Heeft je afvalophaler geen link, upload dan het ICS-bestand dat je van zijn website downloadt (bv. de agenda-export van Ivago). Het wordt in de add-on bewaard en blijft werken na een herstart; je adres staat er niet in. Elke afspraak is een ophaling; herhalingen (bv. elke twee weken) worden uitgewerkt. Een kalender loopt maar een jaar: de pagina meldt het als het bestand bijna op is.</div>
        <div class="sub">De plaats is gereserveerd, ook zonder kalender. Elke afspraak in de gekozen kalender telt als een ophaling; de titel (bv. <i>PMD</i>, <i>GFT</i>, <i>Restafval</i>, <i>Papier</i>) bepaalt de kleur. In Home Assistant komt zo'n kalender uit bv. de integratie van je gemeente of intercommunale, of uit een ICS-bestand (<i>Remote Calendar</i>).</div></div>
      <div class="card"><h3>Camera's</h3>
        <div class="sub">Een Reolink-camera in het midden van de pagina, als een opname die elke 2 seconden ververst (geen film). Kies een camera die je als apparaat hebt toegevoegd (Zelfbouw, ESPHome en Hue → Reolink). De inloggegevens blijven in de add-on: de tv krijgt alleen het beeld, via het adres met het geheim. Zonder camera staat daar een begroeting. Met één camera is het beeld het mooist; twee staan boven elkaar, drie of vier in een raster.</div>
        ${(c.cameras || []).map((cm, i) => `<div class="row" style="align-items:flex-start;border-top:1px dashed var(--line);padding-top:6px"><div style="min-width:220px;flex:1"><label>Camera</label><select data-tv="cameras.${i}.device" data-re="1"><option value="">(kies een camera)</option>${(TV.src.cameras || []).map(([id, nm]) => `<option value="${esc(id)}" ${cm.device === id ? 'selected' : ''}>${esc(nm)}</option>`).join('')}${cm.device && !(TV.src.cameras || []).some(([id]) => id === cm.device) ? `<option value="${esc(cm.device)}" selected>${esc(cm.device)} (niet gevonden)</option>` : ''}</select>${TV.errors && TV.errors[`camera ${i + 1}`] ? `<span class="sub" style="color:var(--bad)">${esc(TV.errors[`camera ${i + 1}`])}</span>` : ''}</div>${tvIn('Naam op het beeld', `cameras.${i}.label`, { w: 160, max: 30, ph: 'bv. Voortuin' })}
          <button class="btn small danger" type="button" data-tvact="del" data-p="cameras.${i}" title="Weghalen" style="margin-top:22px">✕</button></div>`).join('')}
        <div class="row" style="margin-top:4px"><button class="btn small" type="button" data-tvact="addcam" ${(c.cameras || []).length >= 4 || !(TV.src.cameras || []).length ? 'disabled' : ''}>+ Camera</button>${(TV.src.cameras || []).length ? '' : '<span class="sub">Er is nog geen Reolink-camera toegevoegd.</span>'}</div></div>
      <div class="card"><h3>Onderaan</h3><div class="sub">Tot 5 standen in een ronde knop, bv. eco-modus, nachtmodus, alarm of deurbel. Rechts staat de eerstvolgende afspraak uit een kalender.</div>${chips}
        <div class="row" style="margin-top:4px"><button class="btn small" type="button" data-tvact="addchip" ${(c.chips || []).length >= 5 ? 'disabled' : ''}>+ Knop</button></div>
        <div class="row">${tvIn('Volgende afspraak: tekst ervoor', 'next.label', { w: 220, max: 30 })}${tvSrc('Kalender', 'next.calendar', 'tvcal', 'calendar.gezin')}</div></div>
      <div id="tverr" class="sub" aria-live="polite"></div>
    </div>
    <div class="tvprev"><div class="tvframe"><iframe id="tvframe" src="tv/view/" title="Voorvertoning van de tv" loading="lazy"></iframe></div>
      <div class="sub" style="margin-top:6px">Voorvertoning: zo ziet de tv het. Ze ververst na elke wijziging en elke 10 seconden. Er is geen menu en geen bediening; de tv toont alleen wat je hier kiest.</div></div></div>`;
}

// ---------------------------------------------------------------- bewaren en acties

function tvPreview() {
  const f = $('#tvframe');
  try { if (f && f.contentWindow && f.contentWindow.tick) f.contentWindow.tick(); } catch { /* nog niet geladen */ }
}

async function tvSave() {
  const n = ++TVSAVING;
  try {
    const r = await api('PUT', 'tv', TV.config);
    if (n === TVSAVING) { TV.config.token = r.config.token; const e = $('#tverr'); if (e) e.textContent = 'Opgeslagen'; tvPreview(); setTimeout(tvStatus, 2500); setTimeout(tvStatus, 9000); }
  } catch (e) {
    const el = $('#tverr');
    if (el) el.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
    toast(`Niet bewaard: ${e.message}`, 6000); // het formulier is lang: de fout onderaan zie je niet
  }
}
const tvSoon = (() => { let t; return () => { clearTimeout(t); t = setTimeout(tvSave, 350); }; })();

const TV_ACTIONS = {
  addrow: (d) => { tvVal(d.p).push(TV_ROW()); tvDraw(); },
  addtile: () => { TV.config.tiles.push({ title: `Tegel ${TV.config.tiles.length + 1}`, icon: 'waves', tag: '', chart: '', rows: [TV_ROW()] }); tvDraw(); tvSoon(); },
  addchip: () => { TV.config.chips.push({ label: '', source: '', icon: '', on_text: '', off_text: '' }); tvDraw(); },
  addcam: () => { TV.config.cameras.push({ label: '', device: ((TV.src.cameras || [])[0] || [''])[0] }); tvDraw(); tvSoon(); },
  addalert: () => { TV.config.alerts.push(''); tvDraw(); },
  del: (d) => {
    const keys = d.p.split('.'), idx = +keys.pop();
    keys.reduce((o, k) => o[k], TV.config).splice(idx, 1);
    tvDraw();
    tvSoon();
  },
  copy: async () => {
    const el = $('#tvurl');
    el.select();
    try { await navigator.clipboard.writeText(el.value); toast('Adres gekopieerd'); } catch { document.execCommand('copy'); toast('Adres gekopieerd'); }
  },
  token: async () => {
    if (!confirm('Een nieuw adres maken? Het oude werkt dan niet meer, ook niet op de tv. Vul het daarna opnieuw in bij het tv-blok.')) return;
    const r = await api('POST', 'tv/token', {});
    TV.config.token = r.token;
    tvDraw();
    toast('Nieuw adres. Vul het opnieuw in bij het tv-blok.', 6000);
  },
  icsdel: async () => {
    if (!confirm('Het geüploade ICS-bestand verwijderen? De ophaalkalender toont dan alleen nog wat uit HA of de link komt.')) return;
    await api('DELETE', 'tv/ics');
    TV.ics_file = { loaded: false, count: 0 };
    tvDraw();
    tvSoon();
  },
  seturl: async () => {
    const id = $('#tvblock').value;
    await api('PUT', `lg/${id}`, { url: tvAddress() });
    toast('Het tv-blok opent nu deze pagina');
    tvDraw();
  },
};

function tvDraw() {
  const keep = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.tv : null;
  const y = window.scrollY;
  $('#tvbox').innerHTML = viewTvBody();
  window.scrollTo(0, y);
  if (keep) { const el = document.querySelector(`[data-tv="${keep}"]`); if (el && el.tagName !== 'SELECT') el.focus(); }
}

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-tvact]');
  if (!el || !TV_ACTIONS[el.dataset.tvact]) return;
  ev.preventDefault();
  act(() => TV_ACTIONS[el.dataset.tvact](el.dataset))();
});

async function tvUploadIcs(file) {
  if (file.size > 1024 * 1024) throw new Error('Het bestand is te groot (hoogstens 1 MB).');
  const text = await file.text();
  const r = await fetch(`api/tv/ics?name=${encodeURIComponent(file.name)}`, { method: 'PUT', headers: { 'Content-Type': 'text/calendar' }, body: text });
  let j = null;
  try { j = await r.json(); } catch { /* geen JSON */ }
  if (!r.ok) throw new Error((j && j.error) || `Fout ${r.status}`);
  TV.ics_file = j.file;
  tvDraw();
  tvPreview();
  toast(`${j.file.count} ophalingen gelezen`);
}

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (t && t.id === 'tvicsfile' && TV && t.files && t.files[0]) { act(() => tvUploadIcs(t.files[0]))(); t.value = ''; return; }
  if (!TV || !t.dataset || !t.dataset.tv) return;
  const p = t.dataset.tv;
  let v = t.type === 'checkbox' ? t.checked : t.type === 'number' ? (t.value === '' ? '' : Number(t.value)) : t.value.trim();
  if (p.startsWith('climate.chart.')) { // twee grafieken: een lijst zonder gaten
    const ch = TV.config.climate.chart, i = +p.split('.')[2];
    ch[i] = v;
    TV.config.climate.chart = ch.filter((x) => x);
  } else tvSet(p, v);
  if (t.dataset.re || p.endsWith('.source') || /^(energy|weather|waste\.calendar|next\.calendar|alerts|tiles\.\d+\.(tag|chart))/.test(p)) tvDraw(); // de stand van de gekozen bron
  tvSoon();
});
