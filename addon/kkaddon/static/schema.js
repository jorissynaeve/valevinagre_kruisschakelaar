'use strict';
// Schema: per ruimte een tabblad met blokken en draden, zoals een pagina in Loxone Config.
// Een draad is een lid van een kring of een trigger van een licht: de server rekent dat om (kkaddon/schema.py).
// Wordt vóór app.js geladen en gebruikt zijn globals (S, api, act, render, refresh, ...) pas bij gebruik.

const SC = {
  page: null,
  sel: null, // blok-sleutel, of {lamp, dir, i} voor een draad
  link: null, // draad in aanleg: {b, role, g, x, y, armed}
  drag: null,
  zoom: null, // null = de pagina past in de breedte
  q: '',
  lastPanel: '',
  force: false, // de zijbalk vervangen, ook als er nog een veld de focus heeft
  reveal: null, // blok dat bij de volgende tekening in beeld moet komen
};
const SC_HEAD = 42, SC_ROW = 24, SC_FOOT = 10; // dezelfde maten als de automatische indeling in schema.py
const SW_W = 250, LAMP_W = 340;
const SC_GRID = 10;
const clampN = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const SC_WIRES = false; // voorlopig geen lijnen: elke verbinding is een verwijzing, ook binnen een ruimte
const SC_MARGIN = 200; // links van x=0: de verwijzingen en draden aan de touch-kant

const scBlocks = () => S.schema.blocks;
const scBlock = (k) => scBlocks().find((b) => b.key === k);
const scW = (b) => (b.kind === 'lamp' ? LAMP_W : SW_W);
const scRows = (b) => (b.kind === 'timer' ? 1 : b.kind === 'module' ? Math.max(b.ports.length, 1) : b.kind === 'lamp' ? Math.max(b.inputs.length, b.outputs.length) + (b.source === 'ha' ? 2 : 1) : b.gangs.length);
const scH = (b) => SC_HEAD + scRows(b) * SC_ROW + SC_FOOT;
const rowY = (b, i) => b.y + SC_HEAD + i * SC_ROW + SC_ROW / 2;
const scPageName = (id) => (S.schema.pages.find((p) => p.id === id) || {}).name || '';
const scCut = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

// ---------------------------------------------------------------- welke draden lopen er

function scConns() { // alle draden: elke verbinding tussen een lamp en een gang
  const out = [];
  for (const l of scBlocks()) {
    if (l.kind !== 'lamp') continue;
    l.inputs.forEach((c, i) => out.push({ lamp: l, dir: 'in', i, c, sw: c.sw ? scBlock(c.sw) : null }));
    l.outputs.forEach((c, i) => out.push({ lamp: l, dir: 'out', i, c, sw: c.sw ? scBlock(c.sw) : null }));
  }
  return out;
}

const scSame = (x) => x.sw && x.sw.page && x.sw.page === x.lamp.page;

function scType(b) { // bepaalt de kleur van het blok
  if (b.kind === 'lamp' || b.kind === 'timer') return b.kind;
  if (b.kind === 'module') return b.out === 'time' ? 'time' : 'logic';
  if (b.source === 'node') return 'node';
  if (b.source === 'virtual') return 'ext';
  const conns = scConns().filter((x) => x.sw && x.sw.key === b.key);
  return conns.length && conns.every((x) => x.dir === 'out') ? 'box' : 'sw'; // alleen geschakeld, nooit bediend: een relaisbox
}
const SC_TYPE_NL = { timer: 'Timer', node: 'Node', ext: 'Extern apparaat', box: 'Relaisbox · HA', sw: 'Schakelaar · HA' };
const wireSel = (x) => SC.sel && SC.sel.lamp === x.lamp.key && SC.sel.dir === x.dir && SC.sel.i === x.i;

function wirePath(x0, y0, dir0, x1, y1, dir1) { // dir: naar welke kant de poort kijkt (-1 links, 1 rechts)
  const d = clampN(Math.abs(x1 - x0) / 2, 50, 160);
  return `M ${x0} ${y0} C ${x0 + dir0 * d} ${y0} ${x1 + dir1 * d} ${y1} ${x1} ${y1}`;
}

// ---------------------------------------------------------------- tekenen

function scWire(x) {
  const { lamp: l, sw } = x;
  let d;
  if (x.dir === 'in') d = wirePath(sw.x, rowY(sw, x.c.gang), -1, l.x, rowY(l, x.i), -1);
  else d = wirePath(l.x + LAMP_W, rowY(l, x.i), 1, sw.x + SW_W, rowY(sw, x.c.gang), 1);
  const on = l.on === true ? 'on' : '';
  return `<path class="wire ${on} ${wireSel(x) ? 'sel' : ''}" d="${d}"/>
    <path class="wirehit" d="${d}" data-wire="${esc(l.key)}|${x.dir}|${x.i}"><title>${esc(x.c.swName || '')} ${esc(x.c.label || '')} ${x.dir === 'in' ? '→' : '←'} ${esc(l.name)}</title></path>`;
}

function scSwitch(b, tags) {
  const sel = SC.sel === b.key;
  const type = scType(b), sub = SC_TYPE_NL[type];
  const rows = b.gangs.map((g) => {
    const y = rowY(b, g.i);
    const st = g.on === true ? 'on' : g.on === null ? 'unknown' : '';
    return `<line class="bkrule" x1="${b.x}" x2="${b.x + SW_W}" y1="${y - SC_ROW / 2}" y2="${y - SC_ROW / 2}"/>
      <text class="bkt" x="${b.x + SW_W / 2}" y="${y + 4}" text-anchor="middle">${esc(scCut(g.label, 18))}</text>
      ${g.touch ? `<circle class="pt tou" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="touch" data-g="${g.i}"><title>Touch: geeft de druk door (ingang)</title></circle><text class="ptl" x="${b.x + 11}" y="${y + 4}">touch</text>` : ''}
      ${g.relay ? `<circle class="pt rel ${st}" cx="${b.x + SW_W}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="relay" data-g="${g.i}"><title>Relais: wordt geschakeld (uitgang)</title></circle><text class="ptl" x="${b.x + SW_W - 11}" y="${y + 4}" text-anchor="end">relais</text>` : ''}
      ${(tags.get(`${b.key}|${g.i}|in`) || []).map((t, n) => `<text class="stub" x="${b.x - 11}" y="${y + 4 + n * 13}" text-anchor="end" data-sc="go" data-k="${esc(t.key)}">${esc(scCut(t.text, 28))} ←<title>${esc(t.text)}</title></text>`).join('')}
      ${(tags.get(`${b.key}|${g.i}|out`) || []).map((t, n) => `<text class="stub" x="${b.x + SW_W + 11}" y="${y + 4 + n * 13}" data-sc="go" data-k="${esc(t.key)}">→ ${esc(scCut(t.text, 28))}<title>${esc(t.text)}</title></text>`).join('')}`;
  }).join('');
  return `<g class="bk sw t-${type} ${sel ? 'sel' : ''} ${b.online ? '' : 'off'}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 28))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">${b.alias ? `${esc(scCut(b.tech, 20))} · ` : ''}${sub}${b.online ? '' : ' · offline'}</text>${rows}</g>`;
}

function scRowText(c, name) { // de gang is het belangrijkste: inkorten gaat ten koste van de toestelnaam
  if (!c.sw || name.length <= 23) return scCut(name, 23);
  return `${scCut(c.swName, Math.max(6, 20 - c.label.length))} · ${scCut(c.label, 16)}`;
}

function scLamp(b) {
  const sel = SC.sel === b.key;
  const st = b.on === true ? 'on' : b.on === null ? 'unknown' : '';
  const row = (c, i, dir) => {
    const y = rowY(b, i), left = dir === 'in';
    const same = SC_WIRES && c.sw && scBlock(c.sw) && scBlock(c.sw).page === b.page;
    const here = c.sw && scBlock(c.sw) && scBlock(c.sw).page === b.page;
    const name = c.sw ? `${c.swName} · ${c.label}` : c.text;
    const away = c.sw && !here ? (scBlock(c.sw).page ? ` (${scPageName(scBlock(c.sw).page)})` : ' (niet geplaatst)') : !c.sw ? ' (geen blok)' : '';
    return `<text class="bkt ${same ? '' : 'away'}" x="${left ? b.x + 11 : b.x + LAMP_W - 11}" y="${y + 4}" ${left ? '' : 'text-anchor="end"'} data-sc="wiresel" data-w="${esc(b.key)}|${dir}|${i}"><title>${esc(name + away)}${c.mode && c.mode !== 'toggle' ? ` (${MODE_NL[c.mode]})` : ''}</title>${same ? '' : '↗ '}${esc(scRowText(c, name))}</text>
      <circle class="pt ${left ? 'tou' : 'rel'} used" cx="${left ? b.x : b.x + LAMP_W}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="${dir}" data-i="${i}"/>`;
  };
  const free = (dir, i) => {
    const y = rowY(b, i), left = dir === 'in';
    if (!left && b.source === 'node') return '';
    return `<circle class="pt free ${left ? 'tou' : 'rel'}" cx="${left ? b.x : b.x + LAMP_W}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="${dir}" data-i="new"><title>${left ? 'Sleep hierheen vanaf een touch' : 'Sleep naar het relais dat de lamp schakelt'}</title></circle>
      <text class="ptl" x="${left ? b.x + 11 : b.x + LAMP_W - 11}" y="${y + 4}" ${left ? '' : 'text-anchor="end"'}>+ ${left ? 'ingang' : 'uitgang'}</text>`;
  };
  const badge = b.source === 'node' ? 'netwerk' : b.blocked ? 'geblokkeerd' : b.enabled ? 'actief' : 'uit';
  const dt = b.disable && scBlock(scDisKey(b.disable));
  const disRow = b.source !== 'ha' ? '' : `<circle class="pt dis ${dt ? '' : 'free'} ${b.blocked ? 'on' : ''}" cx="${b.x}" cy="${rowY(b, scRows(b) - 1)}" r="6" data-b="${esc(b.key)}" data-role="disable" data-i="0"><title>Disable: staat deze op 1, dan bedienen de touches de lamp niet</title></circle>
    ${dt ? `<text class="bkt away" x="${b.x + 11}" y="${rowY(b, scRows(b) - 1) + 4}" data-sc="go" data-k="${esc(dt.key)}">↗ disable: ${esc(scCut(dt.name, 16))} (${scOut(dt)})<title>${esc(dt.name)} geeft nu ${scOut(dt)}</title></text>`
    : `<text class="ptl" x="${b.x + 11}" y="${rowY(b, scRows(b) - 1) + 4}">+ disable</text>`}`;
  return `<g class="bk lampb t-lamp ${sel ? 'sel' : ''} ${b.online ? '' : 'off'}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${LAMP_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead lamph ${st}" x="${b.x}" y="${b.y}" width="${LAMP_W}" height="${SC_HEAD}" rx="8"/>
    <circle class="bulb ${st} ${b.pending ? 'pending' : ''}" cx="${b.x + 22}" cy="${b.y + 21}" r="12" data-sc="toggle" data-k="${esc(b.key)}"><title>Aan of uit zetten</title></circle>
    <text class="bkn" x="${b.x + 42}" y="${b.y + 18}">${esc(scCut(b.name, 30))}</text>
    <text class="bks" x="${b.x + 42}" y="${b.y + 34}">Lamp · ${b.on === true ? 'aan' : b.on === false ? 'uit' : 'onbekend'}</text>
    <text class="bkbadge ${badge === 'actief' ? 'ok' : badge === 'geblokkeerd' ? 'bad' : ''}" x="${b.x + LAMP_W - 12}" y="${b.y + 18}" text-anchor="end">${badge}</text>
    ${b.inputs.map((c, i) => row(c, i, 'in')).join('')}${free('in', b.inputs.length)}
    ${b.outputs.map((c, i) => row(c, i, 'out')).join('')}${free('out', b.outputs.length)}${disRow}</g>`;
}

function scTimer(b) {
  const y = rowY(b, 0), lamps = scUsers(b);
  return `<g class="bk tim t-timer ${SC.sel === b.key ? 'sel' : ''}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 28))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">Timer · ${scTimerText(b)}</text>
    <text class="bkt" x="${b.x + SW_W - 11}" y="${y + 4}" text-anchor="end">uitgang: ${b.on ? 1 : 0}</text>
    <circle class="pt dis ${b.on ? 'on' : ''}" cx="${b.x + SW_W}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="tout" data-g="0"><title>Uitgang van de timer: sleep naar de disable van een lamp of een ingang van een module</title></circle>
    ${lamps.map((l, n) => `<text class="stub" x="${b.x + SW_W + 11}" y="${y + 4 + n * 13}" data-sc="go" data-k="${esc(l.key)}">→ ${esc(scCut(l.name, 28))}<title>${esc(l.name)}</title></text>`).join('')}</g>`;
}

// ---------------------------------------------------------------- tijd- en logicamodules

const scDisKey = (d) => (d.startsWith('m:') ? d : `t:${d}`);
const scClock = (m) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
function scOut(b) { // wat een timer of module nu geeft, als tekst
  if (b.kind === 'timer') return b.on ? '1' : '0';
  if (b.value === null || b.value === undefined) return '?';
  return b.out === 'time' ? scClock(b.value) : b.value ? '1' : '0';
}
function scUsers(b) { // wie deze uitgang gebruikt
  return scBlocks().filter((u) => (u.kind === 'lamp' && u.disable && scDisKey(u.disable) === b.key) || (u.kind === 'module' && u.ports.some((p) => p.ref === b.key)));
}

const SC_HELP = {
  sunrise: 'De zonsopgang van vandaag, op de locatie van Home Assistant.',
  sunset: 'De zonsondergang van vandaag, op de locatie van Home Assistant.',
  time: 'Een vaste tijd of duur, bv. 02:30.',
  add: 'Telt twee tijden op, bv. zonsondergang plus 02:30. Na middernacht begint de telling opnieuw.',
  sub: 'Trekt de tweede tijd van de eerste af, bv. zonsopgang min 00:30. Voor middernacht telt het terug vanaf 24:00.',
  between: 'Geeft 1 als het nu tussen de twee tijden is. Het begin hoort erbij, het einde niet. Is "van" later dan "tot", dan loopt het over middernacht.',
  after: 'Geeft 1 vanaf deze tijd tot middernacht.',
  before: 'Geeft 1 van middernacht tot deze tijd.',
  and: 'Geeft 1 als beide ingangen 1 zijn.',
  or: 'Geeft 1 als minstens één ingang 1 is.',
};

function scModule(b) {
  const y0 = rowY(b, 0), users = scUsers(b);
  const rows = b.ports.map((p, i) => {
    const y = rowY(b, i), src = p.ref && scBlock(p.ref);
    return `<circle class="pt v-${p.type} ${src ? '' : 'free'}" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="min" data-i="${i}"><title>Ingang ${esc(p.label)}: ${p.type === 'time' ? 'een tijd' : 'een 0 of 1'}</title></circle>
      ${src ? `<text class="bkt away" x="${b.x + 11}" y="${y + 4}" data-sc="go" data-k="${esc(src.key)}">${esc(p.label)}: ${esc(scCut(src.name, 14))} (${scOut(src)})<title>${esc(src.name)} geeft nu ${scOut(src)}</title></text>`
    : `<text class="ptl" x="${b.x + 11}" y="${y + 4}">+ ${esc(p.label)}</text>`}`;
  }).join('');
  return `<g class="bk mod t-${scType(b)} ${SC.sel === b.key ? 'sel' : ''}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 28))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">${esc(b.tech)}</text>${rows}
    <text class="bkt" x="${b.x + SW_W - 11}" y="${y0 + 4}" text-anchor="end">uit: ${scOut(b)}</text>
    <circle class="pt v-${b.out} ${b.value === true ? 'on' : ''}" cx="${b.x + SW_W}" cy="${y0}" r="6" data-b="${esc(b.key)}" data-role="mout" data-g="0"><title>Uitgang: sleep naar een ingang van een module${b.out === 'bool' ? ' of de disable van een lamp' : ''}</title></circle>
    ${users.map((u, n) => `<text class="stub" x="${b.x + SW_W + 11}" y="${y0 + 4 + n * 13}" data-sc="go" data-k="${esc(u.key)}">→ ${esc(scCut(u.name, 28))}<title>${esc(u.name)}</title></text>`).join('')}</g>`;
}

function scModulePanel(b, props) {
  const sources = (p) => scBlocks().filter((s) => s.key !== b.key && ((s.kind === 'timer' && p.type === 'bool') || (s.kind === 'module' && s.out === p.type)));
  const ports = b.ports.map((p) => `<label>${esc(p.label)}</label><select data-mport="${esc(p.name)}" data-k="${esc(b.key)}"><option value="">niet aangesloten</option>${sources(p).map((s) => `<option value="${esc(s.key)}" ${s.key === p.ref ? 'selected' : ''}>${esc(s.name)} (nu ${scOut(s)})</option>`).join('')}</select>`).join('');
  const users = scUsers(b);
  const nosun = (b.type === 'sunrise' || b.type === 'sunset') && b.value === null ? '<div class="sub">Nog onbekend: de add-on heeft de locatie van Home Assistant nog niet, of de zon komt vandaag niet op of gaat niet onder.</div>' : '';
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge">uitgang nu ${scOut(b)}</span><span class="sub">${esc(S.clock.time)} · ${esc(S.clock.tz)}</span></div>
    <div class="sub">${esc(SC_HELP[b.type])}</div>${nosun}${props}
    ${b.type === 'time' ? `<label for="scmtime">Tijd</label><input type="time" id="scmtime" data-k="${esc(b.key)}" value="${esc(b.param)}">` : ''}
    ${ports ? `<h2>Ingangen</h2>${ports}` : ''}
    <h2>Uitgang gaat naar</h2>${users.map((u) => `<div class="sub"><a href="#" data-sc="go" data-k="${esc(u.key)}">${esc(u.name)}</a></div>`).join('') || '<div class="sub">Nog nergens.</div>'}
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delmodule" data-id="${esc(b.id)}">Module verwijderen</button></div>`;
}

// Het menu "+ Module": alles wat je op een pagina kunt zetten, per soort
const SC_MENU = [
  ['Toestellen', [['lamp', 'Lamp', 'met touches en relais'], ['switch', 'Schakelaar', 'een toestel uit Home Assistant']]],
  ['Tijd', [['timer', 'Timer', 'intervallen per dag, geeft 0 of 1'], ['sunrise', 'Zonsopgang', 'tijd van vandaag'], ['sunset', 'Zonsondergang', 'tijd van vandaag'], ['time', 'Vaste tijd', 'bv. 02:30']]],
  ['Rekenen', [['add', 'Optellen', 'tijd + tijd'], ['sub', 'Aftrekken', 'tijd − tijd']]],
  ['Vergelijken', [['between', 'Tussen', '1 tussen twee tijden'], ['after', 'Na', '1 na een tijd'], ['before', 'Voor', '1 voor een tijd']]],
  ['Logica', [['and', 'En', '1 als beide 1 zijn'], ['or', 'Of', '1 als een van beide 1 is']]],
];

function scMenu() {
  return `<details class="scmenu" id="scmenu"><summary class="btn small primary">+ Module</summary><div class="scmenupop">${SC_MENU.map(([title, items]) => `<div class="scmenugroup"><h2>${title}</h2>${items.map(([type, name, sub]) => `<button class="scmenuitem" type="button" data-sc="add" data-type="${type}"><b>${name}</b><span class="sub">${sub}</span></button>`).join('')}</div>`).join('')}</div></details>`;
}

const SC_DAYS = [['mon', 'Maandag'], ['tue', 'Dinsdag'], ['wed', 'Woensdag'], ['thu', 'Donderdag'], ['fri', 'Vrijdag'], ['sat', 'Zaterdag'], ['sun', 'Zondag']];

function scTimerText(b) { // korte samenvatting voor op het blok
  const all = SC_DAYS.map(([d]) => JSON.stringify(b.days[d] || []));
  if (all.every((x) => x === '[]')) return 'geen tijden';
  if (all.every((x) => x === all[0])) return `dagelijks ${b.days.mon.map((i) => `${i.from}–${i.to}`).join(', ')}`;
  return `${all.filter((x) => x !== '[]').length} dagen`;
}

function scInner(list) {
  const conns = scConns();
  const tags = new Map(); // verwijzing op een gang naar een lamp in een andere ruimte
  for (const x of conns) {
    if (!x.sw || !x.sw.page || !x.c.sw || (SC_WIRES && x.lamp.page === x.sw.page)) continue;
    const k = `${x.sw.key}|${x.c.gang}|${x.dir}`;
    if (!tags.has(k)) tags.set(k, []);
    tags.get(k).push({ key: x.lamp.key, text: `${x.lamp.name}${x.lamp.page && x.lamp.page !== x.sw.page ? ` (${scPageName(x.lamp.page)})` : ''}` });
  }
  const shown = new Set(list.map((b) => b.key));
  const wires = !SC_WIRES ? '' : conns.filter((x) => scSame(x) && shown.has(x.lamp.key) && x.c.gang < x.sw.gangs.length).map(scWire).join('');
  const blocks = list.map((b) => (b.kind === 'lamp' ? scLamp(b) : b.kind === 'timer' ? scTimer(b) : b.kind === 'module' ? scModule(b) : scSwitch(b, tags))).join('');
  return `<g class="wires">${wires}</g>${blocks}<path id="sctemp" class="wire temp"/>`; // draden onder de blokken: anders liggen ze over de poorten
}

function scDrawTemp() { // alleen dit ene pad: de poort onder de cursor moet blijven bestaan, anders mist de volgende klik hem
  const a = SC.link && scPortPos(SC.link), el = $('#sctemp');
  if (el) el.setAttribute('d', a ? `M ${a[0]} ${a[1]} L ${SC.link.x} ${SC.link.y}` : '');
}

function scPortPos(p) {
  const b = scBlock(p.b);
  if (!b) return null;
  if (b.kind === 'timer') return [b.x + SW_W, rowY(b, 0)];
  if (b.kind === 'module') return p.role === 'min' ? [b.x, rowY(b, +p.i)] : [b.x + SW_W, rowY(b, 0)];
  if (p.role === 'disable') return [b.x, rowY(b, scRows(b) - 1)];
  if (b.kind === 'switch') return [p.role === 'relay' ? b.x + SW_W : b.x, rowY(b, +p.g)];
  const n = p.role === 'in' ? b.inputs.length : b.outputs.length;
  return [p.role === 'in' ? b.x : b.x + LAMP_W, rowY(b, p.i === 'new' ? n : +p.i)];
}

// ---------------------------------------------------------------- pagina

function scTabs() {
  return `${S.schema.pages.map((p) => `<button class="sctab ${p.id === SC.page ? 'on' : ''}" data-sc="page" data-id="${esc(p.id)}">${esc(p.name)}<span class="n">${scBlocks().filter((b) => b.page === p.id).length}</span></button>`).join('')}
    <button class="sctab add" data-sc="addpage" title="Ruimte toevoegen">+</button>`;
}

function scTray() {
  const q = SC.q.trim().toLowerCase();
  const hit = (b) => !q || b.name.toLowerCase().includes(q) || b.tech.toLowerCase().includes(q);
  const item = (b) => `<div class="tri ${SC.sel === b.key ? 'on' : ''}" data-sc="trayitem" data-k="${esc(b.key)}"><span class="trd t-${scType(b)} ${b.kind === 'lamp' && b.on ? 'on' : ''}"></span>
      <span class="trn">${esc(b.name)}</span><span class="sub">${b.page ? esc(scPageName(b.page)) : '<b>+ plaatsen</b>'}</span></div>`;
  const part = (title, kind) => {
    const all = scBlocks().filter((b) => b.kind === kind && hit(b));
    all.sort((a, b) => (!!a.page - !!b.page));
    return `<h2>${title}</h2>${all.map(item).join('') || '<div class="sub">Geen.</div>'}`;
  };
  return part('Lampen', 'lamp') + part('Schakelaars', 'switch') + part('Timers', 'timer') + part('Tijd en logica', 'module');
}

function scPanel() {
  if (SC.sel && SC.sel.lamp) return scWirePanel();
  const b = SC.sel && scBlock(SC.sel);
  if (!b) {
    return `<h3>Schema</h3><p class="sub">Verbind door te slepen van een <b>touch</b> naar een <b>ingang</b> van een lamp, of van een <b>uitgang</b> naar het <b>relais</b> dat de lamp schakelt. Elke verbinding staat als verwijzing op beide blokken: klik erop om erheen te gaan. Klik op een blok om het te bewerken.</p>
      <p class="sub">Wijzigingen aan verbindingen gelden meteen. Blokken verplaatsen verandert niets aan de schakelingen.</p>`;
  }
  const pages = `<label for="scroom">Ruimte</label><select id="scroom" data-k="${esc(b.key)}"><option value="">(niet geplaatst)</option>${S.schema.pages.map((p) => `<option value="${esc(p.id)}" ${p.id === b.page ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>`;
  const props = `<label for="scname">Eigen naam</label><input type="text" id="scname" maxlength="60" data-k="${esc(b.key)}" value="${esc(b.kind === 'switch' ? b.alias : b.name)}" placeholder="${esc(b.tech)}">
    <div class="sub">Technische naam: <code>${esc(b.tech)}</code></div>${pages}`;
  return b.kind === 'lamp' ? scLampPanel(b, props) : b.kind === 'timer' ? scTimerPanel(b, props) : b.kind === 'module' ? scModulePanel(b, props) : scSwitchPanel(b, props);
}

function scConnRow(b, c, i, dir) {
  const name = c.sw ? `${c.swName} · ${c.label}` : c.text;
  return `<div class="scc"><a href="#" data-sc="wiresel" data-w="${esc(b.key)}|${dir}|${i}">${esc(name)}</a>${c.mode && c.mode !== 'toggle' ? ` <span class="badge">${MODE_NL[c.mode]}</span>` : ''}
    ${c.ref.entity ? `<button class="btn small" type="button" data-sc="member" data-id="${esc(b.id)}" data-e="${esc(c.ref.entity)}" title="Alleen dit relais wisselen (nu ${c.on === true ? 'aan' : c.on === false ? 'uit' : 'onbekend'})">⇄</button>` : ''}
    ${c.fixed ? '<span class="badge">vast</span>' : `<button class="btn small danger" type="button" data-sc="unwire" data-w="${esc(b.key)}|${dir}|${i}" title="Verbinding weghalen">✕</button>`}</div>`;
}

function scAddSelect(b, dir) {
  if (dir === 'out' && b.source === 'node') return '';
  const taken = new Set((dir === 'in' ? b.inputs : b.outputs).map((c) => `${c.sw}|${c.gang}`));
  const groups = scBlocks().filter((s) => s.kind === 'switch').map((s) => {
    const gangs = s.gangs.filter((g) => !taken.has(`${s.key}|${g.i}`) && (dir === 'in' ? g.touch : g.relay) && (b.source !== 'ha' || g.entity));
    return gangs.length ? `<optgroup label="${esc(s.name)}">${gangs.map((g) => `<option value="${esc(s.key)}|${g.i}">${esc(g.label)}</option>`).join('')}</optgroup>` : '';
  }).join('');
  return groups ? `<select data-scadd="${dir}" data-k="${esc(b.key)}"><option value="">+ ${dir === 'in' ? 'ingang' : 'uitgang'} toevoegen, ook uit een andere ruimte…</option>${groups}</select>` : '';
}

function scLampPanel(b, pages) {
  const onoff = b.on === true ? 'Aan' : b.on === false ? 'Uit' : 'Onbekend';
  const active = b.source === 'ha'
    ? `<label style="margin:0;color:var(--ink);cursor:pointer"><input type="checkbox" data-grp-toggle="${esc(b.id)}" ${b.enabled ? 'checked' : ''}> ${b.enabled ? 'Actief: volgen elkaar' : 'Uit: alleen bediening'}</label>` : '';
  const tail = b.source === 'ha'
    ? `<button class="btn small danger" data-sc="dellamp" data-id="${esc(b.id)}">Verwijderen</button>`
    : `<button class="btn small primary" data-act="learn" data-n="${b.node}" data-r="${b.relay}">Schakelaars leren</button><button class="btn small" data-act="edit" data-n="${b.node}" data-r="${b.relay}">Bewerken</button>`;
  const sources = scBlocks().filter((t) => t.kind === 'timer' || (t.kind === 'module' && t.out === 'bool'));
  const dis = b.source !== 'ha' ? '' : `<label for="scdis">Disable-ingang${b.blocked ? ' <span class="badge bad">nu geblokkeerd</span>' : ''}</label><select id="scdis" data-k="${esc(b.key)}"><option value="">geen</option>${sources.map((t) => { const v = t.kind === 'timer' ? t.id : t.key; return `<option value="${esc(v)}" ${v === b.disable ? 'selected' : ''}>${t.kind === 'timer' ? 'Timer ' : t.tech === t.name ? '' : `${esc(t.tech)} `}${esc(t.name)} (nu ${scOut(t)})</option>`; }).join('')}</select>
    <div class="sub">Op 1 bedienen de touches deze lamp niet. Bedienen hier of via HA blijft kunnen.</div>`;
  const known = [...b.inputs, ...b.outputs].filter((c) => c.on === true || c.on === false);
  const mixed = b.source === 'ha' && known.some((c) => c.on !== known[0].on) ? '<span class="badge warn">relais staan niet gelijk</span>' : '';
  return `<h3>${esc(b.name)}</h3>${pages}${dis}${active}${mixed}
    <button class="power ${b.on === true ? 'on' : b.on === null ? 'unknown' : ''} ${b.pending ? 'pending' : ''}" data-sc="toggle" data-k="${esc(b.key)}">${onoff}</button>
    <h2>Ingangen</h2>${b.inputs.map((c, i) => scConnRow(b, c, i, 'in')).join('') || '<div class="sub">Nog geen touches.</div>'}${scAddSelect(b, 'in')}
    <h2>Uitgangen</h2>${b.outputs.map((c, i) => scConnRow(b, c, i, 'out')).join('') || '<div class="sub">Nog geen relais: de lamp weet niet welk relais hem schakelt.</div>'}${scAddSelect(b, 'out')}
    <div class="row" style="margin-top:6px">${tail}</div>
    ${b.source === 'ha' ? '<p class="sub">Een relais op een uitgang reageert nog op zijn eigen touch, en een touch-relais volgt de lamp mee (daar hangt zijn LED aan).</p>' : ''}`;
}

function scTimerPanel(b, props) {
  const lamps = scUsers(b);
  const rows = SC_DAYS.map(([d, nl]) => {
    const list = b.days[d] || [];
    return `<div class="tmd"><div class="row sp"><b>${nl}</b><span class="row">
        ${list.length < 3 ? `<button class="btn small" type="button" data-sc="tadd" data-k="${esc(b.key)}" data-day="${d}" title="Interval toevoegen">+</button>` : ''}
        ${list.length ? `<button class="btn small" type="button" data-sc="tcopy" data-k="${esc(b.key)}" data-day="${d}" title="Deze tijden naar alle dagen kopiëren">naar alle dagen</button>` : ''}</span></div>
      ${list.map((i, n) => `<div class="tmi"><input type="time" data-tm data-day="${d}" data-n="${n}" data-f="from" value="${esc(i.from)}"> tot <input type="time" data-tm data-day="${d}" data-n="${n}" data-f="to" value="${esc(i.to)}">
        <button class="btn small danger" type="button" data-sc="tdel" data-k="${esc(b.key)}" data-day="${d}" data-n="${n}" title="Interval weghalen">✕</button></div>`).join('')}</div>`;
  }).join('');
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.on ? 'ok' : ''}">uitgang nu ${b.on ? 1 : 0}</span><span class="sub">${esc(S.clock.time)} · ${esc(S.clock.tz)}</span></div>${props}
    <h2>Uitgang gaat naar</h2>${lamps.map((l) => `<div class="sub"><a href="#" data-sc="go" data-k="${esc(l.key)}">${esc(l.name)}</a></div>`).join('') || '<div class="sub">Nog nergens.</div>'}
    <h2>Intervallen met uitgang 1</h2><div class="sub">Tot 3 per dag. Tot 00:00 betekent tot middernacht. Over middernacht: maak twee intervallen op twee dagen.</div>${rows}
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="deltimer" data-id="${esc(b.id)}">Timer verwijderen</button></div>`;
}

async function scSaveTimer(b, days, name = b.name) {
  try {
    await api('PUT', `timers/${b.id}`, { name, days });
  } finally {
    SC.force = true;
    await refresh();
  }
}

const scDaysCopy = (b) => JSON.parse(JSON.stringify(b.days));

function scConnSelect(s, g) { // een verwijzing maken vanaf de schakelaar: de lamp mag in elke ruimte staan
  const lamps = scBlocks().filter((l) => l.kind === 'lamp' && (l.source !== 'ha' || g.entity));
  const opt = (dir) => lamps.filter((l) => (dir === 'in' ? g.touch : g.relay && l.source === 'ha'))
    .filter((l) => !(dir === 'in' ? l.inputs : l.outputs).some((c) => c.sw === s.key && c.gang === g.i))
    .map((l) => `<option value="${esc(l.key)}|${dir}">${esc(l.name)}</option>`).join('');
  return `<select data-scconn="${esc(s.key)}|${g.i}"><option value="">+ verbinden met een lamp…</option><optgroup label="Touch als ingang">${opt('in')}</optgroup><optgroup label="Relais als uitgang">${opt('out')}</optgroup></select>`;
}

function scSwitchPanel(b, pages) {
  const conns = scConns().filter((x) => x.sw && x.sw.key === b.key);
  const free = b.source === 'node' ? S.free_relays.filter((f) => f.node === b.node) : [];
  const rows = b.gangs.map((g) => {
    const to = conns.filter((x) => x.c.gang === g.i);
    return `<div class="scc"><div><b>${esc(g.label)}</b>${g.entity ? `<div class="sub"><code>${esc(g.entity)}</code></div>` : ''}
      ${to.map((x) => `<div class="sub"><a href="#" data-sc="go" data-k="${esc(x.lamp.key)}">${x.dir === 'in' ? 'touch →' : 'relais ←'} ${esc(x.lamp.name)}</a></div>`).join('') || '<div class="sub">niet aangesloten</div>'}
      ${scConnSelect(b, g)}
      ${free.some((f) => f.relay === g.i) ? `<button class="btn small" data-act="addlight" data-n="${b.node}" data-r="${g.i}">Als lamp toevoegen</button>` : ''}</div></div>`;
  }).join('');
  const tail = b.source === 'ha'
    ? `<button class="btn small" data-sc="editswitch" data-id="${esc(b.id)}">Bewerken</button><button class="btn small danger" data-sc="delswitch" data-id="${esc(b.id)}">Blok verwijderen</button>`
    : b.source === 'virtual' ? `<button class="btn small" data-act="vedit" data-n="${b.node}">Bewerken</button>`
      : `<button class="btn small" data-act="identify" data-n="${b.node}">Knipper</button>`;
  return `<h3>${esc(b.name)}</h3><div class="sub">${b.source === 'ha' ? 'HA-apparaat' : b.source === 'virtual' ? 'Extern apparaat' : 'Node'}${b.online ? '' : ' · offline'}</div>${pages}
    <h2>Gangen</h2>${rows}<div class="row" style="margin-top:6px">${tail}</div>`;
}

function scWirePanel() {
  const l = scBlock(SC.sel.lamp);
  const c = l && (SC.sel.dir === 'in' ? l.inputs : l.outputs)[SC.sel.i];
  if (!c) { SC.sel = null; return scPanel(); }
  const name = c.sw ? `${c.swName} · ${c.label}` : c.text;
  const w = `${l.key}|${SC.sel.dir}|${SC.sel.i}`;
  return `<h3>Verbinding</h3><p>${SC.sel.dir === 'in' ? `<b>${esc(name)}</b> bedient <b>${esc(l.name)}</b>` : `<b>${esc(l.name)}</b> schakelt <b>${esc(name)}</b>`}</p>
    ${c.sw && scBlock(c.sw).page ? `<p><a href="#" data-sc="go" data-k="${esc(c.sw)}">↗ Naar ${esc(c.swName)} (${esc(scPageName(scBlock(c.sw).page))})</a></p>` : ''}
    ${c.fixed ? '<p class="sub">Deze uitgang ligt vast: het is het relais van de node zelf.</p>' : `<div class="row"><button class="btn small danger" data-sc="unwire" data-w="${esc(w)}">Verbinding weghalen</button><span class="sub">of druk op Delete</span></div>`}`;
}

function scEmpty() {
  const hasBlocks = scBlocks().length > 0;
  return `<div class="scempty"><h3>${hasBlocks ? 'Nog geen ruimtes' : 'Nog niets om te tekenen'}</h3>
    <p class="sub">${hasBlocks ? 'Zet je huidige lampen en schakelaars om: per ruimte een tabblad, met draden zoals ze nu lopen. Er verandert niets aan de schakelingen.' : 'Importeer eerst kringen (tabblad HA-kringen) of wacht tot er nodes verschijnen.'}</p>
    <button class="btn primary" data-sc="convert">Huidige configuratie omzetten</button></div>`;
}

function viewSchema() {
  return `<div class="scbar"><div id="sctabs" class="sctabs"></div><span class="row">
      <button class="btn small" data-sc="zoom" data-d="-0.1" title="Kleiner">−</button><button class="btn small" data-sc="zoom" data-d="0.1" title="Groter">+</button>
      <button class="btn small" data-sc="renamepage">Ruimte hernoemen</button><button class="btn small danger" data-sc="delpage">Ruimte verwijderen</button>
      <button class="btn small" data-sc="convert" title="Nieuwe kringen en toestellen op hun plek zetten. Wat al staat blijft staan.">Omzetten</button>
      <button class="btn small" data-sc="relayout" title="Alles opnieuw verdelen over de ruimtes">Opnieuw indelen</button>
      ${scMenu()}</span></div>
    <div class="scwrap"><aside class="card sctray"><input type="search" id="scq" placeholder="Zoek een blok" autocomplete="off" value="${esc(SC.q)}"><div id="sctraylist"></div></aside>
      <div id="sccanvas"><div id="scempty"></div><svg id="scsvg" role="img" aria-label="Schema van de ruimte"></svg></div>
      <aside class="card" id="scpanel"></aside></div>`;
}

const scOnPage = () => scBlocks().filter((b) => b.page === SC.page);

function scDraw() {
  const svg = $('#scsvg');
  if (!svg) return;
  const list = scOnPage(), box = $('#sccanvas');
  const need = scNeed(list);
  const z = scZoomNow(need);
  const w = Math.max(need, box.clientWidth / z), h = Math.max(...list.map((b) => b.y + scH(b) + 120), box.clientHeight / z);
  svg.setAttribute('viewBox', `${-SC_MARGIN} 0 ${w} ${h}`);
  svg.setAttribute('width', w * z);
  svg.setAttribute('height', h * z);
  svg.innerHTML = scInner(list);
  scDrawTemp();
  if (SC.reveal) {
    const el = svg.querySelector(`[data-block="${CSS.escape(SC.reveal)}"]`);
    SC.reveal = null;
    if (el) el.scrollIntoView({ block: 'center', inline: 'center' });
  }
}

// een schakelaar heeft rechts plaats nodig voor zijn verwijzingen
const scNeed = (list) => SC_MARGIN + Math.max(...list.map((b) => b.x + scW(b) + (b.kind === 'lamp' ? 40 : 200)), 300);

function scZoomNow(need) {
  return SC.zoom !== null ? SC.zoom : clampN($('#sccanvas').clientWidth / need, 0.55, 1);
}

function schemaUpdate() {
  if (!S.schema.pages.some((p) => p.id === SC.page)) SC.page = S.schema.pages.length ? S.schema.pages[0].id : null;
  $('#sctabs').innerHTML = scTabs();
  $('#sctraylist').innerHTML = scTray();
  const noPages = !S.schema.pages.length;
  $('#scempty').innerHTML = noPages ? scEmpty() : '';
  $('#scsvg').style.display = noPages ? 'none' : 'block';
  document.querySelectorAll('.scbar [data-sc=renamepage], .scbar [data-sc=delpage]').forEach((x) => { x.disabled = noPages; });
  scDraw();
  const panel = $('#scpanel'), html = scPanel(), active = document.activeElement;
  // een lijst die net open staat of een veld waarin je typt niet onder je handen vervangen
  if (SC.force || (html !== SC.lastPanel && !(active && panel.contains(active) && /SELECT|INPUT/.test(active.tagName)))) {
    SC.force = false;
    panel.innerHTML = html;
    SC.lastPanel = html;
  }
}

// ---------------------------------------------------------------- bewerken

function scFreeSpot(kind, page = SC.page) {
  const list = scBlocks().filter((b) => b.page === page);
  const col = kind === 'lamp' ? 460 : 40;
  const y = Math.max(30, ...list.filter((b) => Math.abs(b.x - col) < 200).map((b) => b.y + scH(b) + 24));
  return { x: col, y: Math.ceil(y / SC_GRID) * SC_GRID };
}

async function scPlace(key, page, x, y) {
  await api('POST', 'schema/place', { key, page: page || null, x, y });
  await refresh();
}

function scRef(s, g) {
  return g.entity ? { entity: g.entity } : { node: s.node, btn: g.i };
}

async function scWireLogic(a, b) { // van de uitgang van een timer of module naar een module-ingang of de disable van een lamp
  const src = [a, b].find((p) => p.role === 'tout' || p.role === 'mout');
  const sink = src === a ? b : a;
  if (!src) throw new Error('Trek de draad vanaf de uitgang van een timer of module.');
  const from = scBlock(src.b), to = scBlock(sink.b);
  if (to.kind === 'lamp' && (!sink.role || sink.role === 'disable')) await api('POST', 'schema/disable', { lamp: to.key, timer: from.kind === 'timer' ? from.id : from.key });
  else if (to.kind === 'module' && sink.role === 'min') await api('PUT', `modules/${to.id}`, { inputs: { [to.ports[+sink.i].name]: from.key } });
  else throw new Error('Een uitgang sluit je aan op een ingang van een module of de disable-ingang van een lamp.');
  SC.sel = to.key;
  SC.force = true;
  await refresh();
}

const SC_LOGIC_ROLES = ['tout', 'mout', 'min', 'disable'];

async function scWireUp(a, b) { // a en b zijn poorten; één hoort bij een lamp, de ander bij een schakelaar, of het zijn modules
  const pa = scBlock(a.b), pb = scBlock(b.b);
  if (SC_LOGIC_ROLES.includes(a.role) || SC_LOGIC_ROLES.includes(b.role)) return scWireLogic(a, b);
  if (pa.kind === pb.kind) throw new Error(pa.kind === 'lamp' ? 'Een draad loopt tussen een schakelaar en een lamp, niet tussen twee lampen.' : 'Een draad loopt tussen een schakelaar en een lamp, niet tussen twee schakelaars.');
  const [lp, sp] = pa.kind === 'lamp' ? [a, b] : [b, a];
  const lamp = scBlock(lp.b), sw = scBlock(sp.b), gang = sw.gangs[+sp.g];
  const dir = lp.role || (sp.role === 'relay' ? 'out' : 'in');
  if (dir === 'in' && !gang.touch) throw new Error('Deze gang heeft geen touch.');
  if (dir === 'out' && !gang.relay) throw new Error('Deze gang heeft geen relais.');
  await api('POST', 'schema/wire', { lamp: lamp.key, dir, ref: scRef(sw, gang) });
  SC.sel = lamp.key;
  await refresh();
}

async function scUnwire(w) {
  const [lamp, dir, i] = w.split('|');
  const l = scBlock(lamp), c = l && (dir === 'in' ? l.inputs : l.outputs)[+i];
  if (!c || c.fixed) return;
  await api('POST', 'schema/wire', { lamp, dir, ref: c.ref, remove: true });
  SC.sel = lamp;
  await refresh();
}

const scSavePages = async (pages) => { await api('PUT', 'schema/pages', { pages }); await refresh(); };

function scSwitchDialog(b) {
  const lines = b ? b.gangs.map((g) => `${g.entity} = ${g.label}`).join('\n') : '';
  const parse = () => $('#swents').value.split('\n').map((x) => x.trim()).filter(Boolean).map((x) => {
    const [entity, ...name] = x.split('=');
    return { entity: entity.trim(), name: name.join('=').trim() };
  });
  const open = (devs) => smallDialog(`<h3>${b ? 'Schakelaar bewerken' : 'Schakelaar toevoegen'}</h3>
      ${devs.length ? `<label for="swdev">Uit Home Assistant</label><select id="swdev"><option value="">Kies een apparaat…</option>${devs.map((d, i) => `<option value="${i}">${esc(d.name)}${d.area ? ` (${esc(d.area)})` : ''}</option>`).join('')}</select>` : ''}
      <label for="swname">Naam</label><input type="text" id="swname" maxlength="60" value="${esc(b ? b.name : '')}" placeholder="bv. Touch keuken">
      <label for="swents">Gangen: per regel een entiteit, eventueel met een naam</label>
      <textarea id="swents" rows="5" spellcheck="false" placeholder="switch.touch_keuken_relay_1 = Links">${esc(lines)}</textarea>`, async () => {
    const body = { name: $('#swname').value.trim(), gangs: parse() };
    if (b) {
      await api('PUT', `schema/switch/${b.id}`, body);
    } else {
      const r = await api('POST', 'schema/switch', { ...body, page: SC.page, ...scFreeSpot('switch') });
      SC.sel = SC.reveal = r.key;
    }
  });
  if (b) { open([]); return; }
  api('GET', 'ha/devices').then((r) => r.devices, () => []).then((devs) => {
    SC.devs = devs;
    open(devs);
  });
}

const SC_ACTIONS = {
  add: async (d) => {
    const menu = $('#scmenu');
    if (menu) menu.open = false;
    if (d.type === 'lamp') return SC_ACTIONS.newlamp();
    if (d.type === 'switch') return SC_ACTIONS.newswitch();
    if (d.type === 'timer') return SC_ACTIONS.newtimer();
    if (!SC.page) throw new Error('Maak eerst een ruimte met de + bij de tabbladen.');
    const r = await api('POST', 'modules', { type: d.type, page: SC.page, ...scFreeSpot('module') });
    SC.sel = SC.reveal = r.key;
    SC.force = true;
    await refresh();
  },
  delmodule: async (d) => {
    if (!confirm('Deze module verwijderen? Wat ze gebruikte is daarna niet meer aangesloten.')) return;
    await api('DELETE', `modules/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  page: (d) => { SC.page = d.id; SC.sel = null; SC.link = null; render(); },
  go: (d) => { const b = scBlock(d.k); if (b && b.page) SC.page = b.page; SC.sel = SC.reveal = d.k; render(); },
  trayitem: async (d) => {
    const b = scBlock(d.k);
    if (b.page) { SC.page = b.page; SC.sel = SC.reveal = d.k; render(); return; }
    if (!SC.page) throw new Error('Maak eerst een ruimte met de + bij de tabbladen.');
    SC.sel = d.k;
    const p = scFreeSpot(b.kind);
    await scPlace(d.k, SC.page, p.x, p.y);
  },
  zoom: (d) => {
    const now = scZoomNow(scNeed(scOnPage()));
    SC.zoom = clampN(+(now + +d.d).toFixed(1), 0.4, 1.6);
    scDraw();
  },
  addpage: () => smallDialog('<h3>Ruimte toevoegen</h3><label for="pgname">Naam</label><input type="text" id="pgname" maxlength="60" placeholder="bv. Keuken">', async () => {
    const name = $('#pgname').value.trim();
    if (!name) throw new Error('Geef de ruimte een naam');
    const ids = new Set(S.schema.pages.map((p) => p.id));
    const base = slug(name).slice(0, 34) || 'ruimte';
    let id = base, n = 2;
    while (ids.has(id)) id = `${base}_${n++}`;
    SC.page = id;
    await api('PUT', 'schema/pages', { pages: [...S.schema.pages, { id, name }] });
  }),
  renamepage: () => {
    const cur = S.schema.pages.find((p) => p.id === SC.page);
    smallDialog(`<h3>Ruimte hernoemen</h3><label for="pgname">Naam</label><input type="text" id="pgname" maxlength="60" value="${esc(cur.name)}">`, () => api('PUT', 'schema/pages', {
      pages: S.schema.pages.map((p) => (p.id === cur.id ? { ...p, name: $('#pgname').value.trim() || p.name } : p)),
    }));
  },
  delpage: async () => {
    const cur = S.schema.pages.find((p) => p.id === SC.page);
    if (!confirm(`Ruimte "${cur.name}" verwijderen? De blokken gaan terug naar de lijst links; de draden en schakelingen blijven.`)) return;
    await scSavePages(S.schema.pages.filter((p) => p.id !== cur.id));
  },
  convert: async () => {
    const r = await api('POST', 'schema/convert', {});
    await refresh();
    toast(`${r.placed} ${r.placed === 1 ? 'blok' : 'blokken'} geplaatst, ${r.pages} ${r.pages === 1 ? 'ruimte' : 'ruimtes'}${r.registry ? '' : '. Home Assistant gaf geen apparatenlijst: schakelaars staan onder "Centraal"'}.`, 6000);
  },
  relayout: async () => {
    if (!confirm('Alle ruimtes en plekken opnieuw indelen? Wat je zelf verplaatst hebt gaat verloren. De draden en schakelingen blijven.')) return;
    SC.sel = null;
    const r = await api('POST', 'schema/convert', { reset: true });
    await refresh();
    toast(`${r.placed} blokken verdeeld over ${r.pages} ruimtes.`, 6000);
  },
  newtimer: () => {
    if (!SC.page) throw new Error('Maak eerst een ruimte met de + bij de tabbladen.');
    smallDialog('<h3>Nieuwe timer</h3><p class="sub">Geeft 1 binnen de intervallen die je per dag kiest, anders 0. Sluit hem aan op de disable-ingang van een lamp of op een module.</p><label for="tmname">Naam</label><input type="text" id="tmname" maxlength="60" placeholder="bv. Nacht">', async () => {
      const r = await api('POST', 'timers', { name: $('#tmname').value.trim(), days: {}, page: SC.page, ...scFreeSpot('timer') });
      SC.sel = SC.reveal = r.key;
    });
  },
  tadd: (d) => {
    const b = scBlock(d.k), days = scDaysCopy(b);
    (days[d.day] ||= []).push({ from: '03:00', to: '07:00' });
    return scSaveTimer(b, days);
  },
  tdel: (d) => {
    const b = scBlock(d.k), days = scDaysCopy(b);
    days[d.day].splice(+d.n, 1);
    return scSaveTimer(b, days);
  },
  tcopy: (d) => {
    const b = scBlock(d.k), days = {};
    SC_DAYS.forEach(([x]) => { days[x] = b.days[d.day]; });
    return scSaveTimer(b, days);
  },
  deltimer: async (d) => {
    if (!confirm('Deze timer verwijderen? Lampen die hij blokkeert zijn daarna gewoon weer te bedienen.')) return;
    await api('DELETE', `timers/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  newlamp: () => {
    if (!SC.page) throw new Error('Maak eerst een ruimte met de + bij de tabbladen.');
    smallDialog('<h3>Nieuwe lamp</h3><p class="sub">Een lege lamp via Home Assistant, eerst uitgeschakeld. Trek daarna draden naar de touches en het relais.</p><label for="lpname">Naam</label><input type="text" id="lpname" maxlength="60" placeholder="bv. Spots keuken">', async () => {
      const r = await api('POST', 'schema/lamp', { name: $('#lpname').value.trim(), page: SC.page, ...scFreeSpot('lamp') });
      SC.sel = SC.reveal = r.key;
    });
  },
  newswitch: () => {
    if (!SC.page) throw new Error('Maak eerst een ruimte met de + bij de tabbladen.');
    scSwitchDialog(null);
  },
  editswitch: (d) => scSwitchDialog(scBlock(`d:${d.id}`)),
  delswitch: async (d) => {
    if (!confirm('Dit blok verwijderen? Zijn draden blijven gelden: de lampen tonen ze dan als "geen blok".')) return;
    await api('DELETE', `schema/switch/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  dellamp: async (d) => {
    if (!confirm(`Lamp "${groupName(d.id)}" verwijderen? De kring verdwijnt; de toestellen zelf blijven zoals ze zijn.`)) return;
    await api('DELETE', `groups/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  toggle: async (d) => {
    const b = scBlock(d.k);
    if (b.source === 'ha') await api('POST', `groups/${b.id}/cmd`, { op: 'toggle' }).catch((e) => { throw new Error(`${e.message}: geen enkel relais bereikbaar?`); });
    else await api('POST', `light/${b.node}/${b.relay}/cmd`, { op: 'toggle' });
  },
  wiresel: (d) => { const [lamp, dir, i] = d.w.split('|'); SC.sel = { lamp, dir, i: +i }; render(); },
  unwire: (d) => scUnwire(d.w),
  member: (d) => api('POST', `groups/${d.id}/cmd`, { op: 'toggle', entity: d.e }).catch((e) => { throw new Error(`${e.message}: ${d.e} is niet bereikbaar?`); }),
};

// ---------------------------------------------------------------- muis en aanraking

function scPt(ev) {
  const svg = $('#scsvg'), p = svg.createSVGPoint();
  p.x = ev.clientX; p.y = ev.clientY;
  const q = p.matrixTransform(svg.getScreenCTM().inverse());
  return { x: q.x, y: q.y };
}

const scPortOf = (el) => (el && el.dataset && el.dataset.role ? { b: el.dataset.b, role: el.dataset.role === 'touch' || el.dataset.role === 'relay' ? null : el.dataset.role, swRole: el.dataset.role, g: el.dataset.g, i: el.dataset.i } : null);

function scFinish(target) {
  const from = SC.link;
  SC.link = null;
  scDrawTemp(); // ook als de verbinding zo meteen geweigerd wordt, mag de draad niet blijven hangen
  const toPort = target && target.closest && target.closest('.pt');
  const toBlock = target && target.closest && target.closest('[data-block]');
  const norm = (p) => ({ b: p.b, role: scBlock(p.b).kind === 'lamp' ? p.lampRole : p.swRole, g: p.g, i: p.i });
  let dest = null;
  if (toPort) {
    const p = scPortOf(toPort);
    dest = { b: p.b, lampRole: p.role, swRole: p.swRole, g: p.g, i: p.i };
  } else if (toBlock && toBlock.dataset.block !== from.b && scBlock(toBlock.dataset.block).kind === 'lamp') {
    dest = { b: toBlock.dataset.block, lampRole: null }; // losgelaten op de lamp zelf: de poort waar je begon beslist
  }
  if (!dest) { scDraw(); return; }
  act(() => scWireUp(norm(from), norm(dest)))();
}

document.addEventListener('pointerdown', (ev) => {
  const svg = ev.target.closest && ev.target.closest('#scsvg');
  if (!svg || tab !== 'schema' || ev.button > 0) return;
  if (ev.target.closest('[data-sc]')) return; // eigen knop in een blok (lamp aan/uit, verwijzing)
  const port = ev.target.closest('.pt');
  const pt = scPt(ev);
  if (SC.link && SC.link.armed) { // tweede klik van klik-klik: werkt ook op een aanraakscherm
    scFinish(ev.target);
    return;
  }
  if (port) {
    const p = scPortOf(port);
    ev.preventDefault();
    SC.link = { b: p.b, lampRole: p.role, swRole: p.swRole, role: p.role || p.swRole, g: p.g, i: p.i, x: pt.x, y: pt.y, startX: ev.clientX, startY: ev.clientY };
    svg.setPointerCapture(ev.pointerId);
    return;
  }
  const hit = ev.target.closest('.wirehit');
  if (hit) {
    const [lamp, dir, i] = hit.dataset.wire.split('|');
    SC.sel = { lamp, dir, i: +i };
    render();
    return;
  }
  const bk = ev.target.closest('[data-block]');
  if (bk) {
    const b = scBlock(bk.dataset.block);
    ev.preventDefault();
    SC.sel = b.key;
    render(); // eerst tekenen: tijdens het slepen slaat render() over, en de zijbalk moet meteen open
    SC.drag = { k: b.key, dx: b.x - pt.x, dy: b.y - pt.y, moved: false };
    svg.setPointerCapture(ev.pointerId);
    return;
  }
  SC.sel = null;
  render();
});

document.addEventListener('pointermove', (ev) => {
  if (tab !== 'schema') return;
  if (SC.link && $('#scsvg')) {
    const p = scPt(ev);
    SC.link.x = p.x; SC.link.y = p.y;
    scDrawTemp();
  } else if (SC.drag && $('#scsvg')) {
    const p = scPt(ev), b = scBlock(SC.drag.k);
    if (!b) return;
    const x = Math.max(0, Math.round((p.x + SC.drag.dx) / SC_GRID) * SC_GRID), y = Math.max(0, Math.round((p.y + SC.drag.dy) / SC_GRID) * SC_GRID);
    if (x !== b.x || y !== b.y) { b.x = x; b.y = y; SC.drag.moved = true; scDraw(); }
  }
});

function scUp(ev) {
  if (tab !== 'schema') return;
  if (SC.link && !SC.link.armed) {
    const near = Math.hypot(ev.clientX - SC.link.startX, ev.clientY - SC.link.startY) < 6;
    if (near) { // alleen geklikt: de draad hangt nu aan de cursor, de volgende klik sluit hem aan
      SC.link.armed = true;
      toast('Klik nu op de poort of de lamp waar de draad heen moet (Esc om te stoppen)');
      return;
    }
    const el = document.elementFromPoint(ev.clientX, ev.clientY);
    scFinish(el);
    return;
  }
  const d = SC.drag;
  if (d) {
    SC.drag = null;
    const b = scBlock(d.k);
    if (b && d.moved) act(() => scPlace(b.key, b.page, b.x, b.y))();
  }
}
document.addEventListener('pointerup', scUp);
document.addEventListener('pointercancel', () => { SC.link = null; SC.drag = null; if (tab === 'schema') render(); });

document.addEventListener('keydown', (ev) => {
  if (tab !== 'schema' || dlg().open || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (ev.key === 'Escape' && SC.link) { SC.link = null; scDraw(); }
  if ((ev.key === 'Delete' || ev.key === 'Backspace') && SC.sel && SC.sel.lamp) {
    ev.preventDefault();
    act(scUnwire)(`${SC.sel.lamp}|${SC.sel.dir}|${SC.sel.i}`);
  }
});

document.addEventListener('click', (ev) => {
  const menu = $('#scmenu');
  if (menu && menu.open && !ev.target.closest('#scmenu')) menu.open = false; // ergens anders geklikt: menu dicht
  const el = ev.target.closest('[data-sc]');
  if (!el || !SC_ACTIONS[el.dataset.sc]) return;
  ev.preventDefault();
  act(SC_ACTIONS[el.dataset.sc])(el.dataset);
});

document.addEventListener('input', (ev) => {
  if (ev.target.id === 'scq') { SC.q = ev.target.value; $('#sctraylist').innerHTML = scTray(); }
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (t.id === 'scname') {
    const b = scBlock(t.dataset.k), name = t.value.trim();
    act(async () => {
      try {
        if (b.kind === 'switch') await api('POST', 'schema/name', { key: b.key, name });
        else if (b.kind === 'module') await api('PUT', `modules/${b.id}`, { name });
        else if (b.kind === 'timer') await api('PUT', `timers/${b.id}`, { name, days: b.days });
        else if (!name) throw new Error('Een lamp heeft een naam nodig');
        else if (b.source === 'ha') await api('PUT', `groups/${b.id}`, { name });
        else await api('PUT', `light/${b.node}/${b.relay}`, { name });
      } finally {
        t.blur();
        SC.force = true; // ook bij een weigering: het veld terug naar wat er echt staat
        await refresh();
      }
    })();
  }
  if (t.id === 'scdis') {
    const k = t.dataset.k, timer = t.value || null;
    t.blur();
    act(async () => {
      try { await api('POST', 'schema/disable', { lamp: k, timer }); } finally { SC.force = true; await refresh(); }
    })();
  }
  if (t.id === 'scmtime' && t.value) {
    const b = scBlock(t.dataset.k), value = t.value;
    t.blur();
    act(async () => {
      try { await api('PUT', `modules/${b.id}`, { value }); } finally { SC.force = true; await refresh(); }
    })();
  }
  if (t.dataset && t.dataset.mport) {
    const b = scBlock(t.dataset.k), port = t.dataset.mport, ref = t.value || null;
    t.blur();
    act(async () => {
      try { await api('PUT', `modules/${b.id}`, { inputs: { [port]: ref } }); } finally { SC.force = true; await refresh(); }
    })();
  }
  if (t.dataset && 'tm' in t.dataset) {
    const b = scBlock(SC.sel), days = scDaysCopy(b);
    days[t.dataset.day][+t.dataset.n][t.dataset.f] = t.value;
    t.blur();
    act(() => scSaveTimer(b, days))();
  }
  if (t.id === 'scroom') {
    const b = scBlock(t.dataset.k);
    const dest = t.value;
    t.blur();
    act(async () => {
      const p = scFreeSpot(b.kind, dest); // niet bovenop wat er al staat
      SC.reveal = b.key;
      await scPlace(b.key, dest, p.x, p.y);
      if (dest) SC.page = dest;
      render();
    })();
  }
  if (t.dataset && t.dataset.scadd && t.value) {
    const [sk, g] = t.value.split('|');
    const sw = scBlock(sk), lamp = t.dataset.k, dir = t.dataset.scadd;
    t.blur();
    act(async () => {
      await api('POST', 'schema/wire', { lamp, dir, ref: scRef(sw, sw.gangs[+g]) });
      await refresh();
    })();
  }
  if (t.dataset && t.dataset.scconn && t.value) {
    const [sk, g] = t.dataset.scconn.split('|'), [lamp, dir] = t.value.split('|'), sw = scBlock(sk);
    t.blur();
    act(async () => {
      await api('POST', 'schema/wire', { lamp, dir, ref: scRef(sw, sw.gangs[+g]) });
      await refresh();
    })();
  }
  if (t.id === 'swdev' && t.value !== '') {
    const d = SC.devs[+t.value];
    $('#swname').value = d.name;
    const ents = d.entities.map((e) => e.entity_id).sort(natural);
    $('#swents').value = ents.map((e, i) => `${e} = ${importEntities(d).find((x) => x.entity_id === e).short || `Gang ${i + 1}`}`).join('\n');
  }
});
