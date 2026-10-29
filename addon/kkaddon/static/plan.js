'use strict';
// Plattegrond: kamers tekenen, schakelaars en lampen neerzetten, en per kring zien hoe die loopt.
// Alleen weergave: dit verandert nooit iets aan de schakelingen. Wordt vóór app.js geladen en gebruikt
// zijn globals (S, tab, api, act, render, ...) pas bij gebruik.

const PL = {
  floor: null, // id van de getoonde verdieping
  edit: false,
  sel: null, // 'n5' (schakelaar), 'l2.1' (lamp) of 'r:3' (kamer, alleen bij bewerken)
  placing: null, // item dat bij de volgende klik op de plattegrond wordt neergezet
  tool: 'move', // 'move' of 'room' (kamer tekenen)
  lines: 'all', // 'all' of 'sel': lijnen van niet-geselecteerde kringen tonen of verbergen
  drag: null,
  busy: 0, // aantal opslagen onderweg: de lokale plattegrond wint dan van een momentopname
  pulse: new Map(), // 'node:knop' -> tijdstip van de laatste druk
};
const GRID = 10;
const NCOL = 8; // kleuren --c0..--c7 voor de kringen
const snap = (v) => Math.round(v / GRID) * GRID;
const clampN = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const MARGIN_X = 40, MARGIN_TOP = 30, MARGIN_BOTTOM = 55; // ruimte voor symbool en naam binnen de tekening
const itemPt = (f, x, y) => [clampN(snap(x), MARGIN_X, f.w - MARGIN_X), clampN(snap(y), MARGIN_TOP, f.h - MARGIN_BOTTOM)];
const shorten = (s, n = 16) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

const planOf = () => (S.plan ||= { floors: [] });
const curFloor = () => planOf().floors.find((f) => f.id === PL.floor);
const swKey = (node) => `n${node}`;
const lampKey = (n, r) => `l${n}.${r}`;
const swName = (id) => (nodeOf(id) && nodeOf(id).name) || nodeName(id);

function planEvent(e) { // door app.js aangeroepen bij elk live event
  if (e && e.t === 'btn') PL.pulse.set(`${e.node}:${e.btn}`, Date.now());
}
const pulsing = (node, btn) => Date.now() - (PL.pulse.get(`${node}:${btn}`) || 0) < 1200;

// ---------------------------------------------------------------- kringen en catalogus

function circuits() {
  let c = 0;
  return S.lights.map((l) => {
    const byNode = new Map();
    for (const t of l.triggers) {
      if (!byNode.has(t.node)) byNode.set(t.node, []);
      byNode.get(t.node).push(t);
    }
    return { key: lampKey(l.node, l.relay), l, byNode, color: l.triggers.length ? c++ % NCOL : -1 };
  });
}

function focusOf(circ) { // welke kringen volledig getoond worden; null = alle
  const s = PL.sel;
  if (!s || s.startsWith('r:')) return null;
  if (s[0] === 'l') return new Set([s]);
  const node = +s.slice(1);
  return new Set(circ.filter((c) => c.byNode.has(node)).map((c) => c.key));
}

function catalog() { // alles wat op de plattegrond kan staan
  const trigNodes = new Set(S.lights.flatMap((l) => l.triggers.map((t) => t.node)));
  const sw = S.nodes.filter((n) => n.nbtn > 0 || trigNodes.has(n.id)).map((n) => ({ key: swKey(n.id), label: `Schakelaar ${swName(n.id)}` }));
  const lamps = S.lights.map((l) => ({ key: lampKey(l.node, l.relay), label: `Lamp ${l.name || lightName(l.node, l.relay)}` }));
  return [...sw, ...lamps];
}

function floorOfItem(key) {
  return planOf().floors.find((f) => key in f.items);
}

// ---------------------------------------------------------------- tekenen

function swSymbol(node, x, y, circ, focus, sel) {
  const n = nodeOf(node);
  const used = new Map(); // knop -> kring (bij meerdere: liefst die in beeld)
  for (const c of circ) {
    for (const t of c.byNode.get(node) || []) {
      const cur = used.get(t.btn);
      if (!cur || (focus && focus.has(c.key) && !focus.has(cur.key))) used.set(t.btn, c);
    }
  }
  const nb = Math.max(n && n.nbtn > 0 ? n.nbtn : 0, used.size ? Math.max(...used.keys()) + 1 : 0, 1);
  const w = nb * 12 + 6;
  const involved = !focus || circ.some((c) => focus.has(c.key) && c.byNode.has(node));
  const btns = [...Array(nb).keys()].map((i) => {
    const c = used.get(i);
    const lit = c && (!focus || focus.has(c.key));
    return `<rect class="swb ${pulsing(node, i) ? 'pulse' : ''}" x="${-w / 2 + 4 + i * 12}" y="-5" width="10" height="10" rx="2"${lit ? ` style="fill:var(--c${c.color})"` : ''}/>`;
  }).join('');
  return `<g class="it sw ${involved ? '' : 'dim'} ${n && !n.online ? 'off' : ''} ${sel ? 'sel' : ''}" data-it="${swKey(node)}" transform="translate(${x} ${y}) scale(1.4)">
    <title>${esc(swName(node))}</title>
    <rect class="selring" x="${-w / 2 - 4}" y="-15" width="${w + 8}" height="30" rx="9"/>
    <rect class="swbody" x="${-w / 2}" y="-11" width="${w}" height="22" rx="5"/>${btns}
    <text class="itl" y="27">${esc(shorten(swName(node)))}</text></g>`;
}

function lampSymbol(c, x, y, focus, sel) {
  const l = c.l;
  const st = l.on === true ? 'on' : l.on === null ? 'unknown' : '';
  const dim = focus && !focus.has(c.key);
  return `<g class="it lamp-g ${dim ? 'dim' : ''} ${l.online ? '' : 'off'} ${sel ? 'sel' : ''}" data-it="${c.key}" transform="translate(${x} ${y}) scale(1.4)">
    <title>${esc(l.name || lightName(l.node, l.relay))}</title>
    <circle class="selring" r="19"/>
    <circle class="lamp ${st}" r="11" style="stroke:${c.color >= 0 ? `var(--c${c.color})` : 'var(--mute)'}"/>
    <text class="itl" y="30">${esc(shorten(l.name || lightName(l.node, l.relay)))}</text></g>`;
}

function lineParts(c, f, focus) {
  const b = f.items[c.key];
  if (!b || c.color < 0) return '';
  const dim = focus && !focus.has(c.key);
  if (dim && PL.lines === 'sel') return '';
  let out = '';
  for (const [node, trigs] of c.byNode) {
    const a = f.items[swKey(node)];
    if (!a) continue;
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const t = len > 150 ? 70 / len : 0.5; // label dicht bij de schakelaar, zodat meerdere lijnen naar één lamp leesbaar blijven
    const lx = a[0] + (b[0] - a[0]) * t, ly = a[1] + (b[1] - a[1]) * t;
    const hot = trigs.some((x) => pulsing(node, x.btn));
    const label = trigs.map((x) => `K${x.btn + 1}${x.mode !== 'toggle' ? ` ⇒ ${MODE_NL[x.mode]}` : ''}`).join(' · ');
    out += `<line class="ln ${dim ? 'dim' : ''} ${hot ? 'pulse' : ''}" x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" style="stroke:var(--c${c.color})"/>`;
    if (!dim) out += `<text class="lbl" x="${lx}" y="${ly - 6}" text-anchor="middle">${esc(label)}</text>`;
  }
  return out;
}

function planInner(f) {
  const circ = circuits(), focus = focusOf(circ);
  let h = PL.edit ? `<defs><pattern id="pgrid" width="${GRID * 4}" height="${GRID * 4}" patternUnits="userSpaceOnUse"><path d="M ${GRID * 4} 0 L 0 0 0 ${GRID * 4}" class="pgridl"/></pattern></defs><rect width="${f.w}" height="${f.h}" fill="url(#pgrid)"/>` : '';
  f.rooms.forEach((r, i) => {
    const sel = PL.sel === `r:${i}`;
    h += `<g class="room ${sel ? 'sel' : ''}" data-room="${i}"><rect class="roomr" x="${r.x}" y="${r.y}" width="${r.w}" height="${r.h}" rx="6"/><text class="roomt" x="${r.x + 10}" y="${r.y + 26}">${esc(r.name)}</text>
      ${PL.edit && sel ? `<rect class="handle" data-handle="${i}" x="${r.x + r.w - 9}" y="${r.y + r.h - 9}" width="18" height="18" rx="4"/>` : ''}</g>`;
  });
  circ.forEach((c) => { h += lineParts(c, f, focus); });
  for (const c of circ) {
    const p = f.items[c.key];
    if (p) h += lampSymbol(c, p[0], p[1], focus, PL.sel === c.key);
  }
  for (const k of Object.keys(f.items)) {
    if (k[0] === 'n') h += swSymbol(+k.slice(1), f.items[k][0], f.items[k][1], circ, focus, PL.sel === k);
  }
  return h;
}

// ---------------------------------------------------------------- zijpaneel

function panelHtml(f, circ) {
  const s = PL.sel;
  const where = (key) => (f.items[key] ? '' : floorOfItem(key) ? ' <span class="badge">andere verdieping</span>' : ' <span class="badge warn">niet geplaatst</span>');
  const trig = (t) => `<b>${esc(swName(t.node))}</b> knop ${t.btn + 1}${t.mode !== 'toggle' ? ` (${MODE_NL[t.mode]})` : ''}${where(swKey(t.node))}`;
  if (s && s.startsWith('r:')) {
    const r = f.rooms[+s.slice(2)];
    return r ? `<h3>Kamer${r.name ? ` ${esc(r.name)}` : ''}</h3><p class="sub">Sleep om te verplaatsen, sleep het blokje rechtsonder om de grootte te veranderen.</p>
      <div class="row"><button class="btn small" data-plan="renameroom">Naam wijzigen</button><button class="btn small danger" data-plan="delroom">Verwijderen</button></div>` : '';
  }
  if (s && s[0] === 'l') {
    const c = circ.find((x) => x.key === s);
    if (!c) return '';
    const l = c.l;
    const st = l.on === true ? 'Aan' : l.on === false ? 'Uit' : 'Onbekend';
    return `<h3>${esc(l.name || lightName(l.node, l.relay))}</h3>
      <div class="sub"><span class="swatch" style="display:inline-block;margin:0 6px 0 0;background:${c.color >= 0 ? `var(--c${c.color})` : 'var(--mute)'}"></span>${esc(nodeName(l.node))} · relais ${l.relay + 1}${l.online ? '' : ' · offline'}</div>
      ${PL.edit ? '' : `<button class="power ${l.on === true ? 'on' : l.on === null ? 'unknown' : ''} ${l.pending ? 'pending' : ''}" data-act="toggle" data-n="${l.node}" data-r="${l.relay}" style="margin:10px 0">${st}</button>`}
      <h2 style="margin:12px 0 4px">Wordt bediend door</h2>
      ${l.triggers.length ? l.triggers.map((t) => `<div style="padding:3px 0">${trig(t)}</div>`).join('') : '<div class="sub">Nog geen schakelaars gekoppeld.</div>'}
      <div class="row" style="margin-top:10px"><button class="btn small primary" data-act="learn" data-n="${l.node}" data-r="${l.relay}">Schakelaars leren</button>
      <button class="btn small" data-act="edit" data-n="${l.node}" data-r="${l.relay}">Bewerken</button></div>
      ${PL.edit && f.items[s] ? '<div class="row" style="margin-top:8px"><button class="btn small" data-plan="unplace">Van plattegrond halen</button></div>' : ''}`;
  }
  if (s && s[0] === 'n') {
    const node = +s.slice(1), n = nodeOf(node);
    const nb = Math.max(n && n.nbtn > 0 ? n.nbtn : 0, ...circ.flatMap((c) => (c.byNode.get(node) || []).map((t) => t.btn + 1)), 1);
    const rows = [...Array(nb).keys()].map((b) => {
      const to = circ.filter((c) => (c.byNode.get(node) || []).some((t) => t.btn === b));
      return `<div style="padding:3px 0"><b>Knop ${b + 1}</b> ${to.length ? '→ ' + to.map((c) => {
        const t = c.byNode.get(node).find((x) => x.btn === b);
        return `<a href="#" data-plan="sel" data-key="${c.key}"><span class="swatch" style="display:inline-block;margin:0 4px 0 0;background:var(--c${c.color})"></span>${esc(c.l.name || lightName(c.l.node, c.l.relay))}</a>${t.mode !== 'toggle' ? ` (${MODE_NL[t.mode]})` : ''}${where(c.key)}`;
      }).join(', ') : '<span class="sub">bedient niets</span>'}</div>`;
    }).join('');
    return `<h3>${esc(swName(node))}</h3><div class="sub">${n ? `${n.online ? 'online' : 'offline'} · ${nb} ${nb === 1 ? 'knop' : 'knoppen'}` : 'nog niet gehoord'}${n && n.virtual ? ' · extern apparaat' : ''}</div>
      <h2 style="margin:12px 0 4px">Knoppen</h2>${rows}
      ${PL.edit && f.items[s] ? '<div class="row" style="margin-top:8px"><button class="btn small" data-plan="unplace">Van plattegrond halen</button></div>' : ''}`;
  }
  // niets geselecteerd: legenda met alle kringen
  const withT = circ.filter((c) => c.color >= 0), without = circ.filter((c) => c.color < 0);
  const leg = (c) => `<div class="leg" data-plan="sel" data-key="${c.key}">
      <span class="swatch" style="background:${c.color >= 0 ? `var(--c${c.color})` : 'var(--chip)'}"></span>
      <div><b>${esc(c.l.name || lightName(c.l.node, c.l.relay))}</b>${where(c.key)}<div class="sub">${c.l.triggers.length ? `← ${c.l.triggers.map((t) => `${esc(swName(t.node))} knop ${t.btn + 1}`).join(' · ')}` : 'geen schakelaars'}</div></div></div>`;
  return `<h3>Kringen</h3><p class="sub">${PL.edit ? 'Klik een kring of een item om het te selecteren.' : 'Klik op een lamp of schakelaar om te zien welke kring erbij hoort. Elke kleur is één lamp met alle schakelaars die hem bedienen; de cijfers bij de lijnen zijn de knoppen.'}</p>
    ${withT.map(leg).join('') || '<p class="sub">Nog geen lamp heeft schakelaars gekoppeld.</p>'}
    ${without.length ? `<h2 style="margin:12px 0 4px">Zonder schakelaars</h2>${without.map(leg).join('')}` : ''}`;
}

// ---------------------------------------------------------------- pagina

function viewPlan() {
  const pl = planOf();
  if (!pl.floors.some((f) => f.id === PL.floor)) PL.floor = pl.floors.length ? pl.floors[0].id : null;
  const f = curFloor();
  const tabs = pl.floors.map((x) => `<button class="btn small ${x.id === PL.floor ? 'primary' : ''}" data-plan="floor" data-id="${x.id}">${esc(x.name || 'Verdieping')}</button>`).join('');
  const bar = `<div class="row" style="margin-top:12px">${tabs}${PL.edit ? '<button class="btn small" data-plan="addfloor">+ Verdieping</button>' : ''}<span style="flex:1"></span>
    ${PL.edit ? '' : `<button class="btn small" data-plan="lines">Lijnen: ${PL.lines === 'all' ? 'alle kringen' : 'alleen selectie'}</button>`}
    <button class="btn small ${PL.edit ? 'primary' : ''}" data-plan="mode">${PL.edit ? 'Klaar' : 'Bewerken'}</button></div>`;
  if (!f) {
    return `${bar}<p class="empty">${PL.edit ? 'Voeg eerst een verdieping toe.' : 'Nog geen plattegrond. Kies <b>Bewerken</b> om kamers te tekenen en schakelaars en lampen neer te zetten.'}</p>`;
  }
  const circ = circuits();
  let tools = '';
  if (PL.edit) {
    const placed = new Set(pl.floors.flatMap((x) => Object.keys(x.items)));
    const left = catalog().filter((i) => !placed.has(i.key));
    tools = `<div class="row" style="margin-top:8px"><button class="btn small ${PL.tool === 'room' ? 'primary' : ''}" data-plan="tool">Kamer tekenen</button>
      <button class="btn small" data-plan="renamefloor">Verdieping hernoemen</button><button class="btn small danger" data-plan="delfloor">Verdieping verwijderen</button></div>
      <div style="margin-top:8px"><span class="sub">${PL.tool === 'room' ? 'Sleep op de plattegrond om een kamer te tekenen.' : PL.placing ? 'Klik op de plattegrond om het gekozen item neer te zetten.' : left.length ? 'Nog niet geplaatst (kies er een en klik daarna op de plattegrond):' : 'Alles staat op de plattegrond. Sleep om te verplaatsen.'}</span>
      <div class="tags" style="margin-top:4px">${left.map((i) => `<button class="btn small ${PL.placing === i.key ? 'primary' : ''}" data-plan="place" data-key="${i.key}">${esc(shorten(i.label, 30))}</button>`).join('')}
      ${left.length > 1 ? '<button class="btn small" data-plan="autoplace">Alles in een raster zetten</button>' : ''}</div></div>`;
  }
  return `${bar}${tools}<div class="planwrap"><svg id="plansvg" class="${PL.edit ? 'edit' : ''}" viewBox="0 0 ${f.w} ${f.h}" role="img" aria-label="Plattegrond ${esc(f.name)}">${planInner(f)}</svg>
    <div class="card" id="planpanel">${panelHtml(f, circ)}</div></div>`;
}

const planRedraw = () => { const f = curFloor(), s = $('#plansvg'); if (f && s) s.innerHTML = planInner(f); };

// ---------------------------------------------------------------- opslaan

async function planSave() {
  PL.busy++;
  try {
    await api('PUT', 'plan', S.plan);
  } catch (e) {
    PL.busy--;
    await refresh(); // de server heeft het niet aangenomen: terug naar wat daar staat
    throw e;
  }
  PL.busy--;
}

// ---------------------------------------------------------------- slepen

function svgPt(ev) {
  const svg = $('#plansvg'), p = svg.createSVGPoint();
  p.x = ev.clientX; p.y = ev.clientY;
  const q = p.matrixTransform(svg.getScreenCTM().inverse());
  return { x: q.x, y: q.y };
}

function place(f, key, x, y) {
  for (const o of planOf().floors) delete o.items[key];
  f.items[key] = itemPt(f, x, y);
}

document.addEventListener('pointerdown', (ev) => {
  const svg = ev.target.closest && ev.target.closest('#plansvg');
  const f = svg && tab === 'plan' && curFloor();
  if (!f || ev.button > 0) return;
  const it = ev.target.closest('.it'), handle = ev.target.closest('[data-handle]'), room = ev.target.closest('.room');
  if (!PL.edit) {
    PL.sel = it && PL.sel !== it.dataset.it ? it.dataset.it : null;
    render();
    return;
  }
  ev.preventDefault();
  const p = svgPt(ev);
  if (handle) {
    PL.drag = { type: 'resize', i: +handle.dataset.handle };
  } else if (it) {
    const k = it.dataset.it, pos = f.items[k];
    PL.sel = k;
    PL.drag = { type: 'item', k, dx: pos[0] - p.x, dy: pos[1] - p.y };
  } else if (PL.placing) {
    place(f, PL.placing, p.x, p.y);
    PL.sel = PL.placing;
    PL.placing = null;
    act(planSave)();
    render();
    return;
  } else if (PL.tool === 'room') {
    const x = clampN(snap(p.x), 0, f.w - 20), y = clampN(snap(p.y), 0, f.h - 20);
    f.rooms.push({ x, y, w: 20, h: 20, name: '' });
    PL.sel = `r:${f.rooms.length - 1}`;
    PL.drag = { type: 'new', i: f.rooms.length - 1, x0: x, y0: y };
  } else if (room) {
    const i = +room.dataset.room, r = f.rooms[i];
    PL.sel = `r:${i}`;
    PL.drag = { type: 'room', i, dx: r.x - p.x, dy: r.y - p.y };
  } else {
    PL.sel = null;
    render();
    return;
  }
  svg.setPointerCapture(ev.pointerId);
  planRedraw();
});

document.addEventListener('pointermove', (ev) => {
  const d = PL.drag, f = curFloor();
  if (!d || !f) return;
  const p = svgPt(ev);
  if (d.type === 'item') {
    f.items[d.k] = itemPt(f, p.x + d.dx, p.y + d.dy);
  } else if (d.type === 'room') {
    const r = f.rooms[d.i];
    r.x = clampN(snap(p.x + d.dx), 0, f.w - r.w);
    r.y = clampN(snap(p.y + d.dy), 0, f.h - r.h);
  } else if (d.type === 'resize') {
    const r = f.rooms[d.i];
    r.w = clampN(snap(p.x) - r.x, 40, f.w - r.x);
    r.h = clampN(snap(p.y) - r.y, 40, f.h - r.y);
  } else if (d.type === 'new') {
    const r = f.rooms[d.i], x = clampN(snap(p.x), 0, f.w), y = clampN(snap(p.y), 0, f.h);
    r.x = Math.min(d.x0, x); r.y = Math.min(d.y0, y);
    r.w = Math.max(20, Math.abs(x - d.x0)); r.h = Math.max(20, Math.abs(y - d.y0));
  }
  planRedraw();
});

function dragEnd() {
  const d = PL.drag, f = curFloor();
  if (!d || !f) return;
  PL.drag = null;
  let ask = false;
  if (d.type === 'new') {
    const r = f.rooms[d.i];
    if (r.w < 40 || r.h < 40) { f.rooms.splice(d.i, 1); PL.sel = null; } else ask = true;
  }
  act(planSave)();
  render();
  if (ask) PLAN_ACTIONS.renameroom();
}
document.addEventListener('pointerup', dragEnd);
document.addEventListener('pointercancel', dragEnd);

// ---------------------------------------------------------------- knoppen

const newFloorId = () => `f${Math.random().toString(36).slice(2, 7)}`;

const PLAN_ACTIONS = {
  mode: () => { PL.edit = !PL.edit; PL.sel = null; PL.placing = null; PL.tool = 'move'; render(); },
  lines: () => { PL.lines = PL.lines === 'all' ? 'sel' : 'all'; render(); },
  floor: (d) => { PL.floor = d.id; PL.sel = null; PL.placing = null; render(); },
  sel: (d) => { const fl = floorOfItem(d.key); if (fl) PL.floor = fl.id; PL.sel = d.key; render(); },
  tool: () => { PL.tool = PL.tool === 'room' ? 'move' : 'room'; PL.placing = null; render(); },
  place: (d) => {
    PL.placing = PL.placing === d.key ? null : d.key;
    PL.tool = 'move';
    render();
  },
  unplace: async () => {
    const k = PL.sel;
    if (!k || k.startsWith('r:')) return;
    for (const o of planOf().floors) delete o.items[k];
    PL.sel = null;
    render();
    await planSave();
  },
  autoplace: async () => {
    const f = curFloor(), placed = new Set(planOf().floors.flatMap((x) => Object.keys(x.items)));
    const taken = Object.values(f.items);
    let x = 60, y = 50;
    for (const i of catalog().filter((c) => !placed.has(c.key))) {
      while (taken.some((p) => Math.abs(p[0] - x) < 100 && Math.abs(p[1] - y) < 70)) {
        x += 120;
        if (x > f.w - 60) { x = 60; y += 90; }
      }
      f.items[i.key] = itemPt(f, x, y);
      taken.push(f.items[i.key]);
    }
    render();
    await planSave();
    toast('Alles staat in een raster: sleep elk item naar zijn plek.');
  },
  addfloor: () => {
    smallDialog('<h3>Verdieping toevoegen</h3><label for="flname">Naam</label><input type="text" id="flname" maxlength="60" placeholder="bv. Begane grond">', async () => {
      const f = { id: newFloorId(), name: $('#flname').value.trim(), w: 1000, h: 640, rooms: [], items: {} };
      planOf().floors.push(f);
      PL.floor = f.id;
      await planSave();
    });
  },
  renamefloor: () => {
    const f = curFloor();
    smallDialog(`<h3>Verdieping hernoemen</h3><label for="flname">Naam</label><input type="text" id="flname" maxlength="60" value="${esc(f.name)}">`, async () => {
      f.name = $('#flname').value.trim();
      await planSave();
    });
  },
  delfloor: async () => {
    const f = curFloor();
    if (!confirm(`Verdieping "${f.name || 'zonder naam'}" verwijderen? De schakelingen zelf blijven bestaan; alleen de tekening gaat weg.`)) return;
    planOf().floors = planOf().floors.filter((x) => x.id !== f.id);
    PL.sel = null;
    render();
    await planSave();
  },
  renameroom: () => {
    const f = curFloor(), i = PL.sel && PL.sel.startsWith('r:') ? +PL.sel.slice(2) : -1, r = f && f.rooms[i];
    if (!r) return;
    smallDialog(`<h3>Kamer</h3><label for="rmname">Naam</label><input type="text" id="rmname" maxlength="60" value="${esc(r.name)}" placeholder="bv. Living">`, async () => {
      r.name = $('#rmname').value.trim();
      await planSave();
    });
  },
  delroom: async () => {
    const f = curFloor();
    f.rooms.splice(+PL.sel.slice(2), 1);
    PL.sel = null;
    render();
    await planSave();
  },
};

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-plan]');
  if (!el || !PLAN_ACTIONS[el.dataset.plan]) return;
  ev.preventDefault();
  act(PLAN_ACTIONS[el.dataset.plan])(el.dataset);
});
