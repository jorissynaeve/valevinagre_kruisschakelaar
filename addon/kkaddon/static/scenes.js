'use strict';
// HUISDEV-scènes in de zijbalk van een apparaat dat ze kent (kkaddon/scenehub.py en scenesweb.py, protocol/huisdev/HUISDEV.md §16).
// Wordt na schema.js geladen: `scEspdevPanel` roept `scnSection` en `scnHint` aan. De bibliotheek (naam, soort, parameters) staat op de server in
// /data/scenes.json; hier staat alleen het formulier. De voorbeeldstrip wordt niet hier berekend: de renderer bestaat maar een keer, in Python
// (POST /api/scenes/preview), en dit bestand tekent alleen de kleuren die terugkomen. Zo ook de tijdschuif: het beeld op tijdstip t komt van POST /api/scenes/frame, en
// "ook op de strip van dit apparaat" stuurt dezelfde t naar het apparaat (POST espdev/<id>/scene/seek, HUISDEV 1.2); de server voegt die samen.

const SCN = {
  lib: [], // de bibliotheek zoals de server hem gaf
  json: '', // idem als tekst, om een verandering te zien
  at: 0, // wanneer hij voor het laatst werd opgehaald
  busy: false,
  ed: null, // het formulier: {id (null = nieuw), name, release, kind, ...parameters als tekst}
  prev: '', // de voorbeeldstrip als afbeelding (data-URL)
  prevInfo: '', // en wat erbij hoort (duur, of een fout)
  timer: null,
  tpos: {}, // de stand van elke tijdschuif in ms, per sleutel: 'ed' (het formulier) of een scène-id (de bibliotheek)
  frames: {}, // het laatste beeld per sleutel (de strip onder de schuif), zodat een nieuwe opbouw van de zijbalk het meteen weer toont
  win: 0, // de duur die de schuif van het formulier beslaat (window_ms van het voorbeeld)
  live: false, // het vinkje "ook op de strip van dit apparaat"
  dev: null, // het apparaat dat dat vinkje bedient (id van het blok)
  held: false, // er ging een seek naar het apparaat, dat nog vasthoudt tot een stop (of 300 s)
  skey: null, // waar de laatste beweging van een schuif bij hoorde
  skinfo: '', // de status van de seek, of een melding
};
const SCN_FRAME_GAP = 100; // ms: hoogstens 10 beelden per seconde ophalen
const SCN_SEEK_GAP = 125; // ms: hoogstens 8 seeks per seconde naar het apparaat (de server voegt ze ook samen)

const SCN_KINDS = [['static', 'Statisch (een kleur)'], ['fade', 'Kleurverloop'], ['sunrise', 'Zonsopkomst'], ['wake', 'Wekker (zonsopkomst, dan een kleur)'], ['rainbow', 'Regenboog'], ['raw', 'Eigen definitie (JSON)']];
const SCN_STATE = { ok: 'staat op het apparaat', wait: 'wordt opgeslagen…', noslot: 'geen vrij slot', toolarge: 'te groot voor dit apparaat', broken: 'ongeldig' };

const scnDefault = (kind) => ({
  static: { color: '#41260a', hold_min: 0, brightness: 100 },
  fade: { c1: '#000000', c2: '#ffa040', minutes: 10, hold_min: 0, brightness: 100 },
  sunrise: { minutes: 20, sun_pct: 20, hold_min: 0, brightness: 100 },
  wake: { sun_min: 20, wake_min: 20, wake_color: '#006400', sun_pct: 20, brightness: 100 },
  rainbow: { period_s: 5, brightness: 100 },
  raw: { json: '{\n  "header": {"loops": 1, "end": 1},\n  "frames": [{"dt": 0, "spans": [{"c1": "#41260a"}]}]\n}' },
})[kind] || {};

const scnHex = (c, dflt) => (typeof c === 'string' ? c : Array.isArray(c) ? `#${c.map((v) => Number(v).toString(16).padStart(2, '0')).join('')}` : dflt);

function scnDraft(s) { // een bibliotheekscène als formulier
  const d = s.def || {}, k = SCN_KINDS.some((x) => x[0] === d.kind) ? d.kind : 'raw', base = { id: s.id, name: s.name, release: s.release, kind: k, ...scnDefault(k) };
  const pick = (names) => names.forEach((n) => { if (d[n] !== undefined) base[n] = d[n]; });
  if (k !== 'raw') pick(['brightness']); // helderheid in % licht, ontbreekt = 100
  if (k === 'static') Object.assign(base, { color: scnHex(d.color, base.color), hold_min: (d.hold_s || 0) / 60 });
  else if (k === 'fade') Object.assign(base, { c1: scnHex(d.c1, base.c1), c2: scnHex(d.c2, base.c2), hold_min: (d.hold_s || 0) / 60 }, d.minutes !== undefined ? { minutes: d.minutes } : {});
  else if (k === 'sunrise') { pick(['minutes', 'sun_pct']); base.hold_min = (d.hold_s || 0) / 60; }
  else if (k === 'wake') { pick(['sun_min', 'wake_min', 'sun_pct']); base.wake_color = scnHex(d.wake_color, base.wake_color); }
  else if (k === 'rainbow') pick(['period_s']);
  else { const { kind, ...rest } = d; base.json = JSON.stringify(rest, null, 2); }
  return base;
}

function scnDef(e) { // het formulier als definitie voor de server
  const n = (v, name) => {
    if (v === '' || v === null || v === undefined || Number.isNaN(Number(v))) throw new Error(`${name}: geef een getal`);
    return Number(v);
  };
  const hold = (v) => Math.round(n(v, 'Vasthouden') * 60);
  const br = { brightness: n(e.brightness === undefined ? 100 : e.brightness, 'Helderheid') }; // procent licht (de server eist 1 tot 100)
  switch (e.kind) {
    case 'static': return { kind: 'static', color: e.color, hold_s: hold(e.hold_min), ...br };
    case 'fade': return { kind: 'fade', c1: e.c1, c2: e.c2, minutes: n(e.minutes, 'Duur'), hold_s: hold(e.hold_min), ...br };
    case 'sunrise': return { kind: 'sunrise', minutes: n(e.minutes, 'Duur'), sun_pct: n(e.sun_pct, 'Zon'), hold_s: hold(e.hold_min), ...br };
    case 'wake': return { kind: 'wake', sun_min: n(e.sun_min, 'Zonsopkomst'), wake_min: n(e.wake_min, 'Wekker'), wake_color: e.wake_color, sun_pct: n(e.sun_pct, 'Zon'), ...br };
    case 'rainbow': return { kind: 'rainbow', period_s: n(e.period_s, 'Seconden per ronde'), ...br };
    default: {
      let j;
      try { j = JSON.parse(e.json); } catch (err) { throw new Error(`JSON: ${err.message}`); }
      if (!j || typeof j !== 'object' || Array.isArray(j)) throw new Error('JSON: verwacht een object met header en frames');
      return { ...j, kind: 'raw' };
    }
  }
}

const scnDur = (ms) => {
  if (ms === null || ms === undefined) return '';
  const m = ms / 60000;
  return m >= 120 ? `${(m / 60).toFixed(1).replace('.0', '')} u` : m >= 1 ? `${Math.round(m)} min` : `${Math.round(ms / 1000)} s`;
};
const scnTime = (t) => new Date(t * 1000).toLocaleTimeString('nl-NL', { hour: '2-digit', minute: '2-digit' }); // lokale tijd
const scnDay = (t) => new Date(t * 1000).toLocaleString('nl-NL', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

const scnClock = (ms) => { // mm:ss, of u:mm:ss vanaf een uur
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000)), p = (v) => String(v).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}` : `${p(Math.floor(s / 60))}:${p(s % 60)}`;
};

const scnStripCss = (frame) => { // een rij leds als verloop met harde randen (een tekenreeks, dus ook na een nieuwe opbouw van de zijbalk meteen weer te tonen)
  if (!Array.isArray(frame) || !frame.length) return 'background:#000';
  const n = frame.length;
  return `background:linear-gradient(to right,${frame.map((c, i) => `rgb(${c[0]},${c[1]},${c[2]}) ${(i / n * 100).toFixed(2)}% ${((i + 1) / n * 100).toFixed(2)}%`).join(',')})`;
};

function scnLatest(gap, send) { // `push(x)`: roept `send(x)` hoogstens eens per `gap` ms aan, een tegelijk, altijd met het nieuwste; wat tussendoor komt vervangt wat wacht
  let want, busy = false, at = 0, cur = Promise.resolve();
  const run = async () => {
    busy = true;
    try {
      while (want !== undefined) {
        const w = at + gap - Date.now();
        if (w > 0) await new Promise((r) => setTimeout(r, w));
        const x = want;
        want = undefined;
        at = Date.now();
        try { await send(x); } catch (err) { /* send meldt zelf */ }
      }
    } finally { busy = false; }
  };
  return { push: (x) => { want = x; if (!busy) cur = run(); }, drop: () => { want = undefined; }, wait: () => cur };
}

async function scnLoad(force) { // de bibliotheek ophalen (hoogstens eens per 8 s, tenzij het moet)
  if (SCN.busy || (!force && Date.now() - SCN.at < 8000)) return;
  SCN.busy = true;
  try {
    const lib = await api('GET', 'scenes'), j = JSON.stringify(lib);
    SCN.at = Date.now();
    if (j !== SCN.json) {
      SCN.json = j;
      SCN.lib = lib;
      render();
    }
  } catch (err) { /* de zijbalk blijft werken zonder lijst */ } finally { SCN.busy = false; }
}

function scnPreviewSoon() { // na elke wijziging van het formulier: de server rekent een voorbeeld uit
  clearTimeout(SCN.timer);
  SCN.timer = setTimeout(async () => {
    const img = document.getElementById('scnprev'), info = document.getElementById('scnprevinfo');
    let def;
    try { def = scnDef(SCN.ed); } catch (err) { SCN.prev = ''; SCN.prevInfo = err.message; if (img) img.removeAttribute('src'); if (info) info.textContent = SCN.prevInfo; return; }
    try {
      const p = await api('POST', 'scenes/preview', { def, n: 24, samples: 60 });
      const cw = 12, ch = 3, cv = document.createElement('canvas');
      cv.width = p.n * cw;
      cv.height = p.frames.length * ch;
      const g = cv.getContext('2d');
      p.frames.forEach((row, y) => row.forEach((c, x) => { g.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`; g.fillRect(x * cw, y * ch, cw, ch); }));
      SCN.prev = cv.toDataURL('image/png');
      SCN.win = p.window_ms || 0;
      SCN.prevInfo = `de strip van links naar rechts, de tijd van boven naar beneden (0 tot ${scnDur(p.window_ms) || '0 s'}); ${p.bounded ? `duurt ${scnDur(p.total_ms)}` : 'loopt tot stop (het apparaat begrenst hem zelf)'}`;
    } catch (err) { SCN.prev = ''; SCN.prevInfo = err.message; }
    const img2 = document.getElementById('scnprev'), info2 = document.getElementById('scnprevinfo');
    if (img2) { if (SCN.prev) img2.src = SCN.prev; else img2.removeAttribute('src'); }
    if (info2) info2.textContent = SCN.prevInfo;
    if (SCN.ed) scnSyncSlider('ed', SCN.win);
    if (SCN.ed && SCN.prev && SCN.win > 0 && SCN.skey === 'ed') scnScrub('ed', SCN.tpos.ed || 0); // de definitie is veranderd: het beeld (en bij het vinkje: de strip) volgt
  }, 350);
}

function scnSlider(key, win) { // de tijdschuif met zijn tijd en de strip met het beeld op dat tijdstip
  const t = Math.min(SCN.tpos[key] || 0, win);
  return `<div class="scnsk"><input type="range" class="scnrange" data-sksl="${esc(key)}" min="0" max="${win}" step="${win >= 10000 ? 100 : 10}" value="${t}" ${win > 0 ? '' : 'disabled'} title="Tijdschuif: het beeld op dat tijdstip">
    <span class="sub scnclock" data-sktime="${esc(key)}">${scnClock(t)}</span></div><div class="scnstrip" data-skstrip="${esc(key)}" style="${scnStripCss(SCN.frames[key])}"></div>`;
}

function scnSyncSlider(key, win) { // na een nieuw voorbeeld: de lengte van de schuif van het formulier volgt de duur
  document.querySelectorAll(`[data-sksl="${key}"]`).forEach((el) => {
    el.max = win;
    el.step = win >= 10000 ? 100 : 10;
    el.disabled = !(win > 0);
    if (Number(el.value) > win) el.value = win;
  });
  if ((SCN.tpos[key] || 0) > win) SCN.tpos[key] = win;
  scnPaintSlider(key);
}

function scnPaintSlider(key) { // tijd, strip en streepje in de voorbeeldstrip bij de stand van de schuif
  const t = SCN.tpos[key] || 0;
  document.querySelectorAll(`[data-sktime="${key}"]`).forEach((el) => { el.textContent = scnClock(t); });
  document.querySelectorAll(`[data-skstrip="${key}"]`).forEach((el) => { el.setAttribute('style', scnStripCss(SCN.frames[key])); });
  const mark = document.getElementById('scnmark');
  if (mark && key === 'ed') {
    mark.style.display = SCN.win > 0 && SCN.prev ? 'block' : 'none';
    mark.style.top = `${SCN.win > 0 ? Math.min(100, t / SCN.win * 100) : 0}%`;
  }
}

function scnSkInfo(msg) { // de status onder het vinkje
  SCN.skinfo = msg;
  const el = document.getElementById('scnskinfo');
  if (el) el.textContent = msg;
}

const scnFrames = scnLatest(SCN_FRAME_GAP, async ({ key, t }) => { // het beeld op tijdstip t van de server (dezelfde render als het apparaat)
  let body;
  try { body = { ...(key === 'ed' ? { def: scnDef(SCN.ed) } : { id: key }), t_ms: t, n: 24 }; } catch (err) { scnSkInfo(err.message); return; }
  const f = await api('POST', 'scenes/frame', body);
  if (SCN.tpos[key] !== t) return; // alweer verder geschoven (de volgende ronde haalt dat beeld): dit is te laat
  SCN.frames[key] = f.frame;
  scnPaintSlider(key);
});

const scnSeeks = scnLatest(SCN_SEEK_GAP, async ({ key, t }) => { // hetzelfde tijdstip naar het apparaat; de server voegt samen en stuurt alleen de nieuwste
  if (!SCN.live || !SCN.dev) return;
  let body;
  try { body = { ...(key === 'ed' ? { def: scnDef(SCN.ed) } : { id: key }), t_ms: t }; } catch (err) { scnSkInfo(err.message); return; }
  SCN.held = true;
  try {
    const r = await api('POST', `espdev/${SCN.dev}/scene/seek`, body);
    if (!r.superseded && SCN.live) scnSkInfo(`apparaat toont: ${scnClock(t)}`);
  } catch (err) { scnSkInfo(`${err.message}`); }
});

function scnScrub(key, t) { // de schuif is verplaatst (of de definitie veranderde): beeld ophalen en, bij het vinkje, naar het apparaat
  SCN.tpos[key] = t;
  SCN.skey = key;
  scnPaintSlider(key);
  scnFrames.push({ key, t });
  if (SCN.live) scnSeeks.push({ key, t });
}

async function scnLiveOff(note) { // het vinkje uit, de editor dicht of de zijbalk weg: een eventueel vastgehouden beeld stopt (alleen als het apparaat een seek toont)
  const was = SCN.live, dev = SCN.dev;
  SCN.live = false;
  scnSeeks.drop();
  const box = document.getElementById('scnlive');
  if (box) box.checked = false;
  if (!SCN.held || !dev) { if (was && note !== false) scnSkInfo(''); return; }
  SCN.held = false;
  await scnSeeks.wait(); // een seek die al onderweg is, komt eerst aan
  try {
    const r = await api('POST', `espdev/${dev}/scene/seek/stop`, {});
    scnSkInfo(note === false ? '' : r.message || '');
  } catch (err) { scnSkInfo(err.message); }
}

function scnForm() {
  const e = SCN.ed;
  if (!e) return '';
  const inp = (key, label, attrs = 'type="number" step="any" min="0"') => `<label>${label}</label><div class="row"><input ${attrs} data-scf="${key}" value="${esc(e[key])}"></div>`;
  const col = (key, label) => inp(key, label, 'type="color"');
  const hold = inp('hold_min', 'Daarna vasthouden (minuten, 0 = tot stop)', 'type="number" step="any" min="0" max="720"');
  const bright = inp('brightness', 'Helderheid (%)', 'type="number" step="any" min="1" max="100"'); // % licht, bovenop de helderheid van het apparaat zelf
  const parts = {
    static: () => col('color', 'Kleur') + hold + bright,
    fade: () => col('c1', 'Van') + col('c2', 'Naar') + inp('minutes', 'Duur (minuten)', 'type="number" step="any" min="0.1" max="1440"') + hold + bright,
    sunrise: () => inp('minutes', 'Duur (minuten)', 'type="number" step="any" min="0.1" max="1440"') + inp('sun_pct', 'Grootte van de zon (%)', 'type="number" min="0" max="100"') + hold + bright,
    wake: () => inp('sun_min', 'Zonsopkomst (minuten)', 'type="number" step="any" min="0.1" max="1440"') + inp('wake_min', 'Daarna de wekkerkleur (minuten)', 'type="number" step="any" min="0.1" max="720"') + col('wake_color', 'Wekkerkleur')
      + inp('sun_pct', 'Grootte van de zon (%)', 'type="number" min="0" max="100"') + bright,
    rainbow: () => inp('period_s', 'Seconden per ronde', 'type="number" step="any" min="0.02" max="1300"') + bright,
    raw: () => `<label>Definitie (header en frames, zie protocol/ledscene/LEDSCENE.md; de helderheid zet je zelf per frame met <code>lum</code>)</label><textarea rows="8" data-scf="json" spellcheck="false">${esc(e.json)}</textarea>`,
  };
  return `<div class="scnform">
    <label>Naam</label><div class="row"><input type="text" maxlength="60" data-scf="name" value="${esc(e.name)}" placeholder="bv. Wekker schooldag"></div>
    <label>Soort</label><div class="row"><select data-scf="kind">${SCN_KINDS.map((k) => `<option value="${k[0]}" ${k[0] === e.kind ? 'selected' : ''}>${esc(k[1])}</option>`).join('')}</select></div>
    ${(parts[e.kind] || parts.raw)()}
    <label>Als de ingang naar 0 gaat</label><div class="row"><select data-scf="release"><option value="stop" ${e.release === 'stop' ? 'selected' : ''}>stoppen</option><option value="finish" ${e.release === 'finish' ? 'selected' : ''}>laten uitlopen</option></select></div>
    <label>Voorbeeld</label><div class="scnprevwrap"><img id="scnprev" class="scnprev" alt="voorbeeld" ${SCN.prev ? `src="${SCN.prev}"` : ''}><div id="scnmark" class="scnmark" style="display:${SCN.prev && SCN.win > 0 && SCN.skey === 'ed' ? 'block' : 'none'};top:${SCN.win > 0 ? Math.min(100, (SCN.tpos.ed || 0) / SCN.win * 100) : 0}%"></div></div>
    <div class="sub" id="scnprevinfo">${esc(SCN.prevInfo)}</div>${scnSlider('ed', SCN.win)}
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="scnsave">${e.id ? 'Bewaren' : 'Maken'}</button><button class="btn small" type="button" data-sc="scncancel">Annuleren</button>
      ${e.id ? '<button class="btn small danger" type="button" data-sc="scndel">Verwijderen</button>' : ''}</div></div>`;
}

function scnSection(b) { // de sectie "Scènes" in de zijbalk van een apparaat met scènes
  const sc = b.scn;
  if (!sc || !sc.cap) return '';
  scnLoad(false);
  const sel = new Map(sc.sel.map((s) => [s.id, s]));
  const run = sc.run;
  const held = run && run.seek !== null && run.seek !== undefined ? run.seek : null; // het apparaat toont een beeld uit de tijdschuif
  const status = !b.online ? 'het apparaat is offline' : held !== null ? `toont een beeld uit de tijdschuif${run.name ? ` (${esc(run.name)})` : ''} op ${scnClock(held)}` : run ? `speelt: ${esc(run.name || (run.slot ? `slot ${run.slot}` : 'een scène'))}${run.until ? `, tot ${scnTime(run.until)}` : ''}` : 'speelt niets';
  if (sc.cap.seek) SCN.dev = b.id;
  const liveBox = sc.cap.seek
    ? `<label class="chk" title="Elke beweging van een tijdschuif toont dat beeld ook op de echte strip en houdt het vast (hoogstens 5 minuten, of tot je dit uitzet)"><input type="checkbox" id="scnlive" data-sklive="1" ${SCN.live ? 'checked' : ''} ${b.online ? '' : 'disabled'}> ook op de strip van dit apparaat</label>
      <div class="sub" id="scnskinfo">${esc(SCN.skinfo || (held !== null ? `apparaat toont: ${scnClock(held)}` : ''))}</div>`
    : '<div class="sub">Dit apparaat kan niet door een scène scrollen (firmware ouder dan 1.2): de tijdschuif toont alleen het beeld hier op de pagina.</div>';
  const rows = SCN.lib.map((s) => {
    const on = sel.get(s.id), extra = [on ? (SCN_STATE[on.state] || '') + (on.slot ? ` (slot ${on.slot})` : '') : 'niet op dit apparaat', s.bytes ? `${s.bytes} bytes` : 'compileert niet',
      s.bounded && s.total_ms ? scnDur(s.total_ms) : s.bounded === false ? 'loopt tot stop' : ''].filter(Boolean).join(' · ');
    const slide = s.bytes ? `<div class="scnscrub">${scnSlider(s.id, s.window_ms || 0)}</div>` : '';
    return `<div class="scnrow"><label class="chk" title="Zet deze scène op dit apparaat: een slot en een rij met een ingang op het blok"><input type="checkbox" data-scsel="${esc(s.id)}" ${on ? 'checked' : ''}> <b>${esc(s.name)}</b></label>
      <span class="sub">${esc(extra)}</span>
      <span class="scnbtns"><button class="btn small" type="button" data-sc="scnplay" data-id="${esc(b.id)}" data-s="${esc(s.id)}" title="Nu spelen" ${b.online ? '' : 'disabled'}>▶</button><button class="btn small" type="button" data-sc="scnstop" data-id="${esc(b.id)}" data-s="${esc(s.id)}" title="Stoppen (alleen als deze scène speelt)" ${b.online ? '' : 'disabled'}>■</button><button class="btn small" type="button" data-sc="scnedit" data-s="${esc(s.id)}">Wijzig</button></span>
      ${slide}${on && on.note ? `<div class="sub scnnote">${esc(on.note)}</div>` : ''}${on && on.sched_note ? `<div class="sub scnnote">${esc(on.sched_note)}</div>` : ''}</div>`;
  }).join('');
  const sched = sc.sched.map((e) => `<div class="sub">${esc(scnDay(e.at))} · ${esc(e.name || `slot ${e.slot}`)}</div>`).join('');
  return `<h2>Scènes</h2><div class="sub">${status}. ${sc.cap.slots} slots, scènes tot ${sc.cap.max_bytes} bytes.${sc.note ? ` ${esc(sc.note)}` : ''}</div>${liveBox}
    ${rows || '<div class="sub">Nog geen scènes. Maak er een: kies een soort, geef hem een naam en zet hem op dit apparaat.</div>'}
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="scnnew">Nieuwe scène</button></div>
    ${scnForm()}
    <div class="sub" style="margin-top:6px"><b>Gepland op het apparaat</b> (lokale tijd)</div>${sched || '<div class="sub">Geen geplande starts. Sluit op de ingang van een scène een timer aan: het apparaat krijgt dan de eerstvolgende starts vooruit en start ook zonder verbinding.</div>'}`;
}

function scnHint(b) { // onder de ingangen
  return b.scn && b.scn.cap ? '<div class="sub">Een <b>scène</b> (▶) start als de bron van <b>0 naar 1</b> gaat en stopt als ze naar 0 gaat, of loopt uit (naar keuze per scène). Een start wacht hoogstens 60 seconden op een apparaat dat niet bereikbaar is; een stop blijft staan tot het apparaat terug is. Is de bron een <b>timer</b>, dan krijgt het apparaat de komende starts vooraf en doet de wekker het ook als de add-on of het netwerk weg is.</div>' : '';
}

function scnEditorOpens() { // een nieuwe of andere scène in het formulier: de schuif begint weer bij 0
  if (SCN.live && SCN.skey === 'ed') scnLiveOff();
  SCN.tpos.ed = 0;
  SCN.frames.ed = null;
  SCN.win = 0;
  if (SCN.skey === 'ed') SCN.skey = null;
}

function scnEditorCloses() { // de editor dicht: een beeld dat hij op de strip hield, stopt
  if (SCN.live && SCN.skey === 'ed') scnLiveOff();
  scnFrames.drop();
  SCN.skey = SCN.skey === 'ed' ? null : SCN.skey;
}

async function scnReload() { SCN.at = 0; await scnLoad(true); SC.force = true; await refresh(); }

Object.assign(SC_ACTIONS, {
  scnnew: () => { scnEditorOpens(); SCN.ed = { id: null, name: '', release: 'stop', kind: 'static', ...scnDefault('static') }; SCN.prev = ''; SCN.prevInfo = ''; SC.force = true; render(); scnPreviewSoon(); },
  scnedit: (d) => {
    const s = SCN.lib.find((x) => x.id === d.s);
    if (!s) return;
    scnEditorOpens();
    SCN.ed = scnDraft(s);
    SCN.prev = '';
    SCN.prevInfo = '';
    SC.force = true;
    render();
    scnPreviewSoon();
  },
  scncancel: () => { scnEditorCloses(); SCN.ed = null; SC.force = true; render(); },
  scnsave: async () => {
    const e = SCN.ed, b = scBlock(SC.sel);
    if (!e || !b) return;
    const body = { name: e.name, def: scnDef(e), release: e.release };
    if (e.id) await api('PUT', `scenes/${e.id}`, body);
    else {
      const r = await api('POST', 'scenes', body);
      if (b.scn && b.scn.cap) await api('PUT', `espdev/${b.id}/scenes`, { scenes: [...b.scn.sel.map((s) => s.id), r.id] }); // een nieuwe scène staat meteen op dit apparaat
    }
    scnEditorCloses();
    SCN.ed = null;
    toast(e.id ? 'Scène bewaard.' : 'Scène gemaakt en op dit apparaat gezet.', 3000);
    await scnReload();
  },
  scndel: async () => {
    const e = SCN.ed;
    if (!e || !e.id || !confirm(`De scène "${e.name}" verwijderen? Ze verdwijnt van elk apparaat en haar ingang wordt losgekoppeld.`)) return;
    await api('DELETE', `scenes/${e.id}`);
    scnEditorCloses();
    SCN.ed = null;
    await scnReload();
  },
  scnplay: async (d) => {
    try {
      const r = await api('POST', `espdev/${d.id}/scene/play`, { id: d.s });
      toast(r.message || 'Gestart.', 3000);
    } finally { SC.force = true; await refresh(); }
  },
  scnstop: async (d) => {
    try {
      const r = await api('POST', `espdev/${d.id}/scene/stop`, { id: d.s });
      toast(r.message || 'Gestopt.', 3000);
    } finally { SC.force = true; await refresh(); }
  },
});

document.addEventListener('input', (ev) => { // het formulier houdt zijn stand in SCN.ed: de zijbalk wordt bij een verversing opnieuw opgebouwd
  const t = ev.target;
  if (t.dataset && t.dataset.sksl !== undefined) { scnScrub(t.dataset.sksl, Math.round(Number(t.value)) || 0); return; } // een tijdschuif
  if (!SCN.ed || !t.dataset || !t.dataset.scf || t.dataset.scf === 'kind') return;
  SCN.ed[t.dataset.scf] = t.value;
  if (t.dataset.scf !== 'name') scnPreviewSoon();
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (!t.dataset) return;
  if (t.dataset.sklive !== undefined) { // het vinkje "ook op de strip van dit apparaat"
    if (t.checked) {
      SCN.live = true;
      scnSkInfo('sleep de tijdschuif om het beeld op de strip te tonen');
      if (SCN.skey && SCN.tpos[SCN.skey] !== undefined) scnSeeks.push({ key: SCN.skey, t: SCN.tpos[SCN.skey] });
    } else scnLiveOff();
    return;
  }
  if (t.dataset.scf === 'kind' && SCN.ed) {
    SCN.ed = { id: SCN.ed.id, name: SCN.ed.name, release: SCN.ed.release, kind: t.value, ...scnDefault(t.value), ...(t.value !== 'raw' && SCN.ed.brightness !== undefined ? { brightness: SCN.ed.brightness } : {}) };
    SC.force = true;
    render();
    scnPreviewSoon();
  } else if (t.dataset.scsel !== undefined && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'espdev') {
    const b = scBlock(SC.sel), checked = [...document.querySelectorAll('[data-scsel]')].filter((x) => x.checked).map((x) => x.dataset.scsel);
    const have = (b.scn ? b.scn.sel : []).map((s) => s.id), scenes = [...have.filter((i) => checked.includes(i)), ...checked.filter((i) => !have.includes(i))];
    act(async () => {
      try { await api('PUT', `espdev/${b.id}/scenes`, { scenes }); } finally { SC.force = true; await refresh(); }
    })();
  }
});

if (typeof window !== 'undefined' && window.addEventListener) {
  window.addEventListener('pagehide', () => { // de pagina verlaten: het vasthouden stopt (anders na 5 minuten vanzelf)
    if (SCN.held && SCN.dev) fetch(`api/espdev/${SCN.dev}/scene/seek/stop`, { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {});
  });
}
setInterval(() => { if (SCN.live && !document.getElementById('scnlive')) scnLiveOff(false); }, 2000); // de zijbalk van dit apparaat is weg (ander blok gekozen): ook dan stoppen

(() => { // een paar regels stijl, hier zodat style.css niet verandert
  const st = document.createElement('style');
  st.textContent = '.scnrow { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; padding: 3px 0; border-bottom: 1px solid var(--line, #ddd); } .scnrow .scnbtns { margin-left: auto; display: flex; gap: 4px; } .scnrow .scnnote { flex-basis: 100%; }'
    + ' .scnform { margin-top: 6px; padding: 6px 0; } .scnform textarea { width: 100%; font-family: monospace; font-size: 12px; } .scnprev { width: 100%; max-height: 190px; image-rendering: pixelated; background: #000; display: block; border-radius: 4px; }'
    + ' .scnprevwrap { position: relative; } .scnmark { position: absolute; left: 0; right: 0; height: 0; border-top: 2px solid #fff; mix-blend-mode: difference; pointer-events: none; }'
    + ' .scnscrub { flex-basis: 100%; } .scnsk { display: flex; align-items: center; gap: 8px; margin-top: 4px; } .scnrange { flex: 1; width: auto; } .scnclock { min-width: 44px; text-align: right; font-variant-numeric: tabular-nums; }'
    + ' .scnstrip { height: 12px; border-radius: 3px; background: #000; margin-top: 3px; }';
  document.head.appendChild(st);
})();
