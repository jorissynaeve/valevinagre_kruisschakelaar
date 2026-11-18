'use strict';
// HUISDEV-scènes in de zijbalk van een blok Scènes (kkaddon/scenehub.py, scenerules.py en scenesweb.py, protocol/huisdev/HUISDEV.md §16): de ingangen en regels die
// kiezen welke scène een ledstrip speelt, en de bibliotheek met het formulier, de voorbeeldstrip en de tijdschuif.
// Wordt na schema.js geladen: `scPanel` roept `scScenesPanel` aan en het apparaatblok `scnPointer`. De bibliotheek (naam, soort, parameters) staat op de server in
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

function scnSection(q, dev) { // de bibliotheek met formulier, voorbeeld en tijdschuif; `dev` is het apparaat van het blok (mag ontbreken)
  scnLoad(false);
  const sc = dev && dev.scn && dev.scn.cap ? dev.scn : null;
  const sel = new Map((sc ? sc.sel : []).map((s) => [s.id, s]));
  const used = new Map();
  q.rules.forEach((r, n) => used.set(r.scene, [...(used.get(r.scene) || []), n + 1]));
  const run = sc ? sc.run : null;
  const held = run && run.seek !== null && run.seek !== undefined ? run.seek : null; // het apparaat toont een beeld uit de tijdschuif
  const status = !sc ? 'kies hierboven een apparaat met scènes om ze te spelen' : !dev.online ? 'het apparaat is offline' : held !== null ? `toont een beeld uit de tijdschuif${run.name ? ` (${esc(run.name)})` : ''} op ${scnClock(held)}` : run ? `speelt: ${esc(run.name || (run.slot ? `slot ${run.slot}` : 'een scène'))}${run.until ? `, tot ${scnTime(run.until)}` : ''}` : 'speelt niets';
  if (sc && sc.cap.seek) SCN.dev = dev.id;
  const liveBox = sc && sc.cap.seek
    ? `<label class="chk" title="Elke beweging van een tijdschuif toont dat beeld ook op de echte strip en houdt het vast (hoogstens 5 minuten, of tot je dit uitzet)"><input type="checkbox" id="scnlive" data-sklive="1" ${SCN.live ? 'checked' : ''} ${dev.online ? '' : 'disabled'}> ook op de strip van dit apparaat</label>
      <div class="sub" id="scnskinfo">${esc(SCN.skinfo || (held !== null ? `apparaat toont: ${scnClock(held)}` : ''))}</div>`
    : sc ? '<div class="sub">Dit apparaat kan niet door een scène scrollen (firmware ouder dan 1.2): de tijdschuif toont alleen het beeld hier op de pagina.</div>' : '';
  const online = sc && dev.online;
  const rows = SCN.lib.map((s) => {
    const on = sel.get(s.id), where = used.get(s.id);
    const extra = [where ? `regel ${where.join(', ')}` : 'in geen enkele regel', on ? (SCN_STATE[on.state] || '') + (on.slot ? ` (slot ${on.slot})` : '') : '', s.bytes ? `${s.bytes} bytes` : 'compileert niet',
      s.bounded && s.total_ms ? scnDur(s.total_ms) : s.bounded === false ? 'loopt tot stop' : ''].filter(Boolean).join(' · ');
    const slide = s.bytes ? `<div class="scnscrub">${scnSlider(s.id, s.window_ms || 0)}</div>` : '';
    return `<div class="scnrow"><b>${esc(s.name)}</b>
      <span class="sub">${esc(extra)}</span>
      <span class="scnbtns"><button class="btn small" type="button" data-sc="scnplay" data-id="${esc(dev ? dev.id : '')}" data-s="${esc(s.id)}" title="Nu spelen" ${online ? '' : 'disabled'}>▶</button><button class="btn small" type="button" data-sc="scnstop" data-id="${esc(dev ? dev.id : '')}" data-s="${esc(s.id)}" title="Stoppen (alleen als deze scène speelt)" ${online ? '' : 'disabled'}>■</button><button class="btn small" type="button" data-sc="scnedit" data-s="${esc(s.id)}">Wijzig</button></span>
      ${slide}${on && on.note ? `<div class="sub scnnote">${esc(on.note)}</div>` : ''}${on && on.sched_note ? `<div class="sub scnnote">${esc(on.sched_note)}</div>` : ''}</div>`;
  }).join('');
  const sched = (sc ? sc.sched : []).map((e) => `<div class="sub">${esc(scnDay(e.at))} · ${esc(e.name || `slot ${e.slot}`)}</div>`).join('');
  return `<h2>Scènes</h2><div class="sub">${status}.${sc ? ` ${sc.cap.slots} slots, scènes tot ${sc.cap.max_bytes} bytes.${sc.note ? ` ${esc(sc.note)}` : ''}` : ''}</div>${liveBox}
    ${rows || '<div class="sub">Nog geen scènes. Maak er een: kies een soort en geef hem een naam. Daarna zet je hem in een regel hierboven.</div>'}
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="scnnew">Nieuwe scène</button></div>
    ${scnForm()}
    ${sc ? `<div class="sub" style="margin-top:6px"><b>Gepland op het apparaat</b> (lokale tijd)</div>${sched || '<div class="sub">Geen geplande starts. Een regel met alleen een timer (of een wekker op een timer) wordt vooruit naar het apparaat gestuurd: het start dan ook zonder verbinding.</div>'}` : ''}`;
}

function scnPointer(b) { // in de zijbalk van het apparaat: waar de scènes nu staan
  if (!b.scn || !b.scn.cap) return '';
  const q = scBlocks().find((x) => x.kind === 'scenes' && x.device === b.id);
  return `<h2>Scènes</h2><div class="sub">${q ? `Dit apparaat kan scènes afspelen. Welke scène wanneer start, staat in het blok <a href="#" data-sc="go" data-k="${esc(q.key)}">${esc(q.name)}</a>.`
    : 'Dit apparaat kan scènes afspelen. Maak een blok <b>Scènes</b> (+ Module → Routines) om te kiezen welke scène wanneer start.'}</div>`;
}

// ---------------------------------------------------------------- de ingangen en regels van een blok Scènes

const SCQ_OPS = [['on', 'is 1'], ['off', 'is 0'], ['between', 'tijd tussen'], ['after', 'tijd na'], ['before', 'tijd voor'], ['wake', 'wekker']];
const SCQ_NEED = { on: ['in:bool'], off: ['in:bool'], after: ['in:time'], before: ['in:time'], between: ['from:time', 'to:time'], wake: ['in:any'] };

const scqBlock = () => { const b = SC.sel && scBlock(SC.sel); return b && b.kind === 'scenes' ? b : null; };
const scqRules = (b) => b.rules.map((r) => ({ scene: r.scene, when: r.when.map((c) => ({ ...c })) }));
const scqInputs = (b) => b.defs.map((i) => ({ id: i.id, name: i.name, kind: i.kind, ref: i.ref }));

function scqSave(b, patch) { // elke wijziging gaat meteen naar de server; een weigering komt als melding en de zijbalk toont weer wat er echt staat
  return act(async () => {
    try { await api('PUT', `scenerules/${b.id}`, patch); } finally { SC.force = true; await refresh(); }
  })();
}

function scqSources(kind) { // de bronnen die op een ingang van deze soort passen
  const same = (s) => ({ key: s.key, name: s.name, out: scOut(s) });
  return kind === 'bool' ? [...scBlocks().filter((s) => s.kind === 'timer' || (s.kind === 'module' && s.out === 'bool')).map(same), ...scAcOuts('bool')]
    : [...scBlocks().filter((s) => s.kind === 'module' && s.out === 'time').map(same), ...scAcOuts('time')];
}

function scqCond(b, r, n, c, k) { // een voorwaarde als regel met keuzelijsten
  const at = `data-qr="${n}" data-qc="${k}"`;
  const pick = (field, want) => {
    const list = b.defs.filter((i) => want === 'any' || i.kind === want);
    return `<select ${at} data-qf="${field}"><option value="">kies een ingang</option>${list.map((i) => `<option value="${esc(i.id)}" ${i.id === c[field] ? 'selected' : ''}>${esc(i.name)}</option>`).join('')}</select>`;
  };
  const num = (field, label, min, max) => `<label class="chk">${label} <input type="number" ${at} data-qf="${field}" min="${min}" max="${max}" step="1" value="${esc(c[field])}" style="width:64px"></label>`;
  const inp = b.defs.find((i) => i.id === c.in);
  let body = '';
  if (c.op === 'between') body = `${pick('from', 'time')} en ${pick('to', 'time')}`;
  else if (c.op === 'wake') body = `${pick('in', 'any')} ${num('before', 'min ervoor', 0, 720)}${inp && inp.kind === 'time' ? ` ${num('hold', 'min erna', 1, 720)}` : ''}`;
  else body = pick('in', c.op === 'on' || c.op === 'off' ? 'bool' : 'time');
  return `<div class="row qcond"><select ${at} data-qf="op">${SCQ_OPS.map((o) => `<option value="${o[0]}" ${o[0] === c.op ? 'selected' : ''}>${esc(o[1])}</option>`).join('')}</select> ${body}
    <button class="btn small" type="button" data-sc="qdelcond" data-r="${n}" data-c="${k}" title="Voorwaarde weghalen">✕</button></div>`;
}

function scqRule(b, r, n) {
  const sceneOpt = SCN.lib.map((s) => `<option value="${esc(s.id)}" ${s.id === r.scene ? 'selected' : ''}>${esc(s.name)}</option>`).join('');
  const missing = SCN.lib.some((s) => s.id === r.scene) ? '' : `<option value="${esc(r.scene)}" selected>${esc(r.scene_name)}</option>`;
  const on = b.rules[n] && b.rules[n].on;
  return `<div class="qrule ${on ? 'qon' : ''}"><div class="row"><b>${n + 1}.</b> dan scène <select data-qr="${n}" data-qf="scene">${missing}${sceneOpt}</select>${on ? '<span class="badge ok">klopt nu</span>' : ''}
      <span class="scnbtns"><button class="btn small" type="button" data-sc="qup" data-r="${n}" ${n ? '' : 'disabled'} title="Eerder">↑</button><button class="btn small" type="button" data-sc="qdown" data-r="${n}" ${n < b.rules.length - 1 ? '' : 'disabled'} title="Later">↓</button><button class="btn small danger" type="button" data-sc="qdelrule" data-r="${n}" title="Regel weghalen">✕</button></span></div>
    <div class="sub">als</div>${r.when.map((c, k) => scqCond(b, r, n, c, k)).join('')}
    <div class="row"><button class="btn small" type="button" data-sc="qaddcond" data-r="${n}">+ voorwaarde</button></div></div>`;
}

function scScenesPanel(b, props) {
  scnLoad(false);
  const dev = scBlocks().find((x) => x.kind === 'espdev' && x.id === b.device) || null;
  const taken = new Set(scBlocks().filter((x) => x.kind === 'scenes' && x.key !== b.key).map((x) => x.device));
  const devs = scBlocks().filter((x) => x.kind === 'espdev' && x.driver === 'huisdev' && x.scn && (x.scn.cap || x.id === b.device));
  const devSel = `<label>Ledstrip (apparaat met scènes)</label><select data-qdev="1"><option value="">geen apparaat</option>${devs.map((x) => `<option value="${esc(x.id)}" ${x.id === b.device ? 'selected' : ''} ${taken.has(x.id) ? 'disabled' : ''}>${esc(x.name)}${taken.has(x.id) ? ' (heeft al een blok)' : ''}${x.scn.cap ? '' : ' (meldt geen scènes)'}</option>`).join('')}</select>`;
  const ins = b.defs.map((i, k) => {
    const list = scqSources(i.kind), cur = i.ref && !list.some((x) => x.key === i.ref) ? scSrc(i.ref) : null;
    const all = cur ? [{ ...cur, key: i.ref }, ...list] : list;
    return `<div class="row qin"><input type="text" maxlength="60" data-qin="name" data-i="${k}" value="${esc(i.name)}" style="width:110px">
      <select data-qin="kind" data-i="${k}"><option value="bool" ${i.kind === 'bool' ? 'selected' : ''}>0/1</option><option value="time" ${i.kind === 'time' ? 'selected' : ''}>tijd</option></select>
      <select data-qin="ref" data-i="${k}"><option value="">niet aangesloten</option>${all.map((x) => `<option value="${esc(x.key)}" ${x.key === i.ref ? 'selected' : ''}>${esc(x.name)} (nu ${esc(x.out)})</option>`).join('')}</select>
      <button class="btn small danger" type="button" data-sc="qdelin" data-i="${k}" title="Ingang weghalen (mag niet als een regel hem gebruikt)">✕</button></div>`;
  }).join('');
  const status = !b.device ? 'Kies een apparaat.' : !b.ready ? 'Het apparaat meldt (nog) geen scènes: het blok wacht.' : b.active ? `Kiest nu: <b>${esc(b.active_name)}</b> (regel ${b.active_rule + 1}).` : b.unsure ? 'Een ingang heeft geen waarde: er klopt geen regel en het blok laat alles zoals het was.' : 'Geen regel klopt: geen scène.';
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.ready ? 'ok' : ''}">${b.ready ? (b.online ? 'verbonden' : 'offline') : 'wacht'}</span></div>
    <div class="sub">Kiest welke scène een ledstrip speelt. De regels gaan van boven naar onder: de <b>eerste regel waarvan alle voorwaarden kloppen</b> bepaalt de scène. ${status}</div>${props}
    ${devSel}
    <h2>Ingangen</h2>${ins || '<div class="sub">Nog geen ingangen. Een ingang is een 0/1 (een timer of module) of een tijd (een module, bv. Vaste tijd of Zonsopgang). Je kunt ze ook op het blok zelf aansluiten door te slepen.</div>'}
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="qaddin">+ Ingang</button></div>
    <h2>Regels</h2>${b.rules.map((r, n) => scqRule(b, r, n)).join('') || '<div class="sub">Nog geen regels.</div>'}
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="qaddrule" ${b.defs.length && SCN.lib.length ? '' : 'disabled'}>+ Regel</button>${b.defs.length ? '' : '<span class="sub">maak eerst een ingang</span>'}</div>
    <div class="sub"><b>Een wekker</b> start de scène een aantal minuten <i>voor</i> de wekkertijd en laat hem lopen tot een aantal minuten erna. Is de ingang een <b>timer</b>, dan telt de timer die minuten eerder (en stopt hij ook eerder); alleen dan en alleen als de regel verder niets vraagt, krijgt het apparaat de komende starts vooraf en start de wekker ook als de add-on of het netwerk weg is. Een scène start meteen als de keuze verandert; een scène die niet meer geldt stopt alleen als ze zo is ingesteld (anders loopt ze uit). Een start wacht hoogstens 60 seconden op een apparaat dat niet bereikbaar is. Na een herstart of een wijziging van het blok onthoudt het alleen de stand en start het niets.</div>
    ${scnSection(b, dev)}
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delscenes" data-id="${esc(b.id)}">Blok verwijderen</button></div>`;
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
    const e = SCN.ed;
    if (!e) return;
    const body = { name: e.name, def: scnDef(e), release: e.release };
    if (e.id) await api('PUT', `scenes/${e.id}`, body);
    else await api('POST', 'scenes', body);
    scnEditorCloses();
    SCN.ed = null;
    toast(e.id ? 'Scène bewaard.' : 'Scène gemaakt. Zet hem in een regel om hem te laten starten.', 4000);
    await scnReload();
  },
  scndel: async () => {
    const e = SCN.ed;
    if (!e || !e.id || !confirm(`De scène "${e.name}" verwijderen? Ze verdwijnt van elk apparaat en haar regels worden weggehaald.`)) return;
    await api('DELETE', `scenes/${e.id}`);
    scnEditorCloses();
    SCN.ed = null;
    await scnReload();
  },
  qaddin: () => { const b = scqBlock(); if (b) return scqSave(b, { inputs: [...scqInputs(b), { name: `Ingang ${b.defs.length + 1}`, kind: 'bool', ref: null }] }); },
  qdelin: (d) => { const b = scqBlock(); if (b) return scqSave(b, { inputs: scqInputs(b).filter((_, k) => k !== +d.i) }); },
  qaddrule: () => {
    const b = scqBlock();
    if (!b || !b.defs.length || !SCN.lib.length) return;
    const i = b.defs[0];
    return scqSave(b, { rules: [...scqRules(b), { scene: SCN.lib[0].id, when: [i.kind === 'bool' ? { op: 'on', in: i.id } : { op: 'after', in: i.id }] }] });
  },
  qdelrule: (d) => { const b = scqBlock(); if (b) return scqSave(b, { rules: scqRules(b).filter((_, n) => n !== +d.r) }); },
  qup: (d) => { const b = scqBlock(), n = +d.r, rules = b && scqRules(b); if (b && n > 0) { [rules[n - 1], rules[n]] = [rules[n], rules[n - 1]]; return scqSave(b, { rules }); } },
  qdown: (d) => { const b = scqBlock(), n = +d.r, rules = b && scqRules(b); if (b && n < rules.length - 1) { [rules[n + 1], rules[n]] = [rules[n], rules[n + 1]]; return scqSave(b, { rules }); } },
  qaddcond: (d) => {
    const b = scqBlock();
    if (!b || !b.defs.length) return;
    const rules = scqRules(b), i = b.defs[0];
    rules[+d.r].when.push(i.kind === 'bool' ? { op: 'on', in: i.id } : { op: 'after', in: i.id });
    return scqSave(b, { rules });
  },
  qdelcond: (d) => {
    const b = scqBlock();
    if (!b) return;
    const rules = scqRules(b);
    rules[+d.r].when.splice(+d.c, 1);
    return scqSave(b, { rules }); // een regel zonder voorwaarden weigert de server: haal dan de regel weg
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
  } else if (t.dataset.qdev !== undefined && scqBlock()) {
    scqSave(scqBlock(), { device: t.value });
  } else if (t.dataset.qin && scqBlock()) { // een ingang: naam, soort of bron
    const q = scqBlock(), ins = scqInputs(q), i = ins[+t.dataset.i], f = t.dataset.qin;
    if (!i) return;
    if (f === 'name') i.name = t.value.trim() || i.name;
    else if (f === 'kind') { i.kind = t.value; i.ref = null; } // een bron van de andere soort past niet
    else i.ref = t.value || null;
    scqSave(q, { inputs: ins });
  } else if (t.dataset.qr !== undefined && t.dataset.qf && scqBlock()) { // een regel of een voorwaarde
    const q = scqBlock(), rules = scqRules(q), r = rules[+t.dataset.qr], f = t.dataset.qf;
    if (!r) return;
    if (f === 'scene') r.scene = t.value;
    else {
      const c = r.when[+t.dataset.qc];
      if (!c) return;
      if (f === 'op') {
        const first = (kind) => (q.defs.find((i) => i.kind === kind) || q.defs[0] || {}).id || '';
        const need = SCQ_NEED[t.value];
        r.when[+t.dataset.qc] = t.value === 'between' ? { op: 'between', from: first('time'), to: first('time') } : t.value === 'wake' ? { op: 'wake', in: first('time'), before: 0, hold: 60 }
          : { op: t.value, in: first(need[0].split(':')[1]) };
      } else if (f === 'before' || f === 'hold') c[f] = Math.round(Number(t.value));
      else c[f] = t.value;
    }
    scqSave(q, { rules });
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
    + ' .scnstrip { height: 12px; border-radius: 3px; background: #000; margin-top: 3px; }'
    + ' .qin, .qcond, .qrule > .row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin: 3px 0; } .qin select, .qin input[type=text], .qcond select { width: auto; flex: 1 1 110px; min-width: 0; }'
    + ' .qrule { border: 1px solid var(--line, #ddd); border-radius: 6px; padding: 4px 8px; margin: 6px 0; } .qrule.qon { border-color: #2a8; } .qrule select[data-qf=scene] { width: auto; flex: 1 1 140px; } .qrule .scnbtns { margin-left: auto; display: flex; gap: 4px; }';
  document.head.appendChild(st);
})();
