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
  panelDraft: {}, // paneel-id -> wat je typt en nog niet hebt opgeslagen
  force: false, // de zijbalk vervangen, ook als er nog een veld de focus heeft
  reveal: null, // blok dat bij de volgende tekening in beeld moet komen
};
const SC_HEAD = 42, SC_ROW = 24, SC_FOOT = 10; // dezelfde maten als de automatische indeling in schema.py
const SW_W = 250, LAMP_W = 340, TUYA_W = 420;
const SC_GRID = 10;
const clampN = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const SC_WIRES = false; // voorlopig geen lijnen: elke verbinding is een verwijzing, ook binnen een ruimte
const SC_MARGIN = 200; // links van x=0: de verwijzingen en draden aan de touch-kant

const SC_GANGED = ['switch', 'panel', 'tuya']; // blokken met gangen: per gang een touch en een relais
const scBlocks = () => S.schema.blocks;
const scBlock = (k) => scBlocks().find((b) => b.key === k);
const scW = (b) => (b.kind === 'lamp' ? LAMP_W : b.kind === 'tuya' ? TUYA_W : SW_W);
const scRows = (b) => (b.kind === 'timer' ? 1 : b.kind === 'sprinkler' ? Math.max(b.ports.length, b.zones.length) : b.kind === 'module' ? Math.max(b.ports.length, 1) : b.kind === 'lamp' ? Math.max(b.inputs.length, b.outputs.length) + (b.source === 'ha' ? 3 : 1) : b.gangs.length);
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
  if (b.kind === 'module') return b.out === 'bool' ? 'logic' : 'time';
  if (b.kind === 'sprinkler') return 'spr';
  if (b.kind === 'panel' || b.kind === 'screen' || b.kind === 'tuya') return b.kind;
  if (b.source === 'node') return 'node';
  if (b.source === 'virtual') return 'ext';
  const conns = scConns().filter((x) => x.sw && x.sw.key === b.key);
  return conns.length && conns.every((x) => x.dir === 'out') ? 'box' : 'sw'; // alleen geschakeld, nooit bediend: een relaisbox
}
const SC_TYPE_NL = { panel: 'NSPanel', screen: 'Scherm · openHASP', tuya: 'Tuya · lokaal', timer: 'Timer', node: 'Node', ext: 'Extern apparaat', box: 'Relaisbox · HA', sw: 'Schakelaar · HA' };
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
    return `<line class="bkrule" x1="${b.x}" x2="${b.x + SW_W}" y1="${y - SC_ROW / 2}" y2="${y - SC_ROW / 2}"/>
      <text class="bkt" x="${b.x + SW_W / 2}" y="${y + 4}" text-anchor="middle">${esc(scCut(g.label, g.touch || g.relay ? 18 : 34))}</text>
      ${scGangPorts(b, g, SW_W, tags)}`;
  }).join('');
  return `<g class="bk sw t-${type} ${sel ? 'sel' : ''} ${b.online ? '' : 'off'}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 28))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">${b.alias ? `${esc(scCut(b.tech, 20))} · ` : ''}${sub}${b.kind === 'panel' && b.online && b.temperature != null ? ` · ${b.temperature.toFixed(1)} °C` : ''}${b.online ? '' : ' · offline'}</text>${rows}</g>`;
}

function scRowText(c, name) { // de gang is het belangrijkste: inkorten gaat ten koste van de toestelnaam
  if (!c.sw || name.length <= 23) return scCut(name, 23);
  return `${scCut(c.swName, Math.max(6, 20 - c.label.length))} · ${scCut(c.label, 16)}`;
}

function scCtlRow(b) { // de aan/uit-ingang, net boven de disable
  if (b.source !== 'ha') return '';
  const y = rowY(b, scRows(b) - 2), src = b.control && scBlock(scDisKey(b.control));
  return `<circle class="pt ctl ${src ? '' : 'free'} ${b.held === true ? 'on' : ''}" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="control" data-i="0"><title>Aan/uit: op 1 staat de lamp vast aan, op 0 vast uit</title></circle>
    ${src ? `<text class="bkt away" x="${b.x + 11}" y="${y + 4}" data-sc="go" data-k="${esc(src.key)}">↗ aan/uit: ${esc(scCut(src.name, 16))} (${scOut(src)})<title>${esc(src.name)} geeft nu ${scOut(src)}</title></text>`
    : `<text class="ptl" x="${b.x + 11}" y="${y + 4}">+ aan/uit</text>`}`;
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
  const badge = b.source === 'node' ? 'netwerk' : b.held === true ? 'vast aan' : b.held === false ? 'vast uit' : b.blocked ? 'geblokkeerd' : b.enabled ? 'actief' : 'niet actief';
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
    ${b.outputs.map((c, i) => row(c, i, 'out')).join('')}${free('out', b.outputs.length)}${scCtlRow(b)}${disRow}</g>`;
}

const scTyValue = (p) => `${p.value === true ? 'aan' : p.value === false ? 'uit' : p.value}${p.unit ? ` ${p.unit}` : ''}`;

function scGangPorts(b, g, w, tags) { // touch links, relais rechts, en de verwijzingen naar de lampen ernaast
  const y = rowY(b, g.i);
  const st = g.on === true ? 'on' : g.on === null ? 'unknown' : '';
  return `${g.touch ? `<circle class="pt tou" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="touch" data-g="${g.i}"><title>Touch: geeft de druk door (ingang)</title></circle><text class="ptl" x="${b.x + 11}" y="${y + 4}">touch</text>` : ''}
    ${g.relay ? `<circle class="pt rel ${st}" cx="${b.x + w}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="relay" data-g="${g.i}"><title>Relais: wordt geschakeld (uitgang)</title></circle><text class="ptl" x="${b.x + w - 11}" y="${y + 4}" text-anchor="end">relais</text>` : ''}
    ${(tags.get(`${b.key}|${g.i}|in`) || []).map((t, n) => `<text class="stub" x="${b.x - 11}" y="${y + 4 + n * 13}" text-anchor="end" data-sc="go" data-k="${esc(t.key)}">${esc(scCut(t.text, 28))} ←<title>${esc(t.text)}</title></text>`).join('')}
    ${(tags.get(`${b.key}|${g.i}|out`) || []).map((t, n) => `<text class="stub" x="${b.x + w + 11}" y="${y + 4 + n * 13}" data-sc="go" data-k="${esc(t.key)}">→ ${esc(scCut(t.text, 28))}<title>${esc(t.text)}</title></text>`).join('')}`;
}

function scTuya(b, tags) { // bovenaan de relais (touch en relais, zoals een schakelaar); daaronder links wat je kunt instellen, rechts wat het toestel meldt
  const relays = b.gangs.slice(0, b.nrelay).map((g) => {
    const y = rowY(b, g.i);
    return `<line class="bkrule" x1="${b.x}" x2="${b.x + TUYA_W}" y1="${y - SC_ROW / 2}" y2="${y - SC_ROW / 2}"/>
      <text class="bkt" x="${b.x + TUYA_W / 2}" y="${y + 4}" text-anchor="middle">${esc(scCut(g.label, 30))}</text>${scGangPorts(b, g, TUYA_W, tags)}`;
  }).join('');
  const row = (p, n, left) => {
    const i = n + b.nrelay, y = rowY(b, i), x = left ? b.x + 11 : b.x + TUYA_W - 11;
    const tip = `${p.name}: ${scTyValue(p)}${left ? ' (instellen in de zijbalk)' : ''}`;
    return `<circle class="ptv ${left ? 'tou' : 'rel'}" cx="${left ? b.x : b.x + TUYA_W}" cy="${y}" r="5"><title>${esc(tip)}</title></circle>
      <text class="bkt" x="${x}" y="${y + 4}" ${left ? '' : 'text-anchor="end"'}><title>${esc(tip)}</title>${esc(scCut(p.name, left ? 17 : 21))} <tspan class="tyv">${esc(scCut(scTyValue(p), left ? 14 : 12))}</tspan></text>`;
  };
  return `<g class="bk ty t-tuya ${SC.sel === b.key ? 'sel' : ''} ${b.online ? '' : 'off'}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${TUYA_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${TUYA_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 44))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">${esc(SC_TYPE_NL.tuya)}${b.online ? '' : ' · offline'}</text>
    ${b.note ? `<text class="bkt" x="${b.x + TUYA_W / 2}" y="${rowY(b, 0) + 4}" text-anchor="middle">${esc(b.note)}</text>` : ''}
    ${relays}${b.pin.map((p, i) => row(p, i, true)).join('')}${b.pout.map((p, i) => row(p, i, false)).join('')}</g>`;
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
  if (b.out === 'number') return String(b.value);
  return b.out === 'time' ? scClock(b.value) : b.value ? '1' : '0';
}
function scUsers(b) { // wie deze uitgang gebruikt
  return scBlocks().filter((u) => (u.kind === 'lamp' && [u.disable, u.control].some((s) => s && scDisKey(s) === b.key)) || ((u.kind === 'module' || u.kind === 'sprinkler') && u.ports.some((p) => p.ref === b.key)));
}

const SC_HELP = {
  sunrise: 'De zonsopgang van vandaag, op de locatie van Home Assistant.',
  sunset: 'De zonsondergang van vandaag, op de locatie van Home Assistant.',
  time: 'Een vaste tijd of duur, bv. 02:30.',
  number: 'Een vast getal, bv. voor het aantal cycli of de wachttijd van een sprinkler.',
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
    <circle class="pt v-${b.out} ${b.value === true ? 'on' : ''}" cx="${b.x + SW_W}" cy="${y0}" r="6" data-b="${esc(b.key)}" data-role="mout" data-g="0"><title>Uitgang: sleep naar een ingang van een module${b.out === 'bool' ? ' of een ingang van een lamp' : ''}</title></circle>
    ${users.map((u, n) => `<text class="stub" x="${b.x + SW_W + 11}" y="${y0 + 4 + n * 13}" data-sc="go" data-k="${esc(u.key)}">→ ${esc(scCut(u.name, 28))}<title>${esc(u.name)}</title></text>`).join('')}</g>`;
}

// ---------------------------------------------------------------- sprinkler

function scGangOf(entity) { // welk relais van welk schakelaar-blok hoort bij deze entiteit
  for (const s of scBlocks()) {
    if (!SC_GANGED.includes(s.kind)) continue;
    const g = s.gangs.find((x) => x.entity === entity);
    if (g) return { s, g };
  }
  return null;
}

function scSprText(b) {
  const st = b.status;
  if (st.running) return st.zone !== null ? `loopt: ${b.zones[st.zone].name}, cyclus ${st.cycle}/${st.cycles}` : `wacht op ${b.zones[st.next].name}, cyclus ${st.cycle}/${st.cycles}`;
  return b.enabled ? 'actief' : 'uit';
}

function scSprinkler(b) {
  const paramText = { start: b.start, cycles: b.cycles, wait: `${b.wait} min` };
  const left = b.ports.map((p, i) => {
    const y = rowY(b, i), src = p.ref && scBlock(p.ref);
    return `<circle class="pt v-${p.type} ${src ? '' : 'free'}" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="min" data-i="${i}"><title>Ingang ${esc(p.label)}</title></circle>
      ${src ? `<text class="bkt away" x="${b.x + 11}" y="${y + 4}" data-sc="go" data-k="${esc(src.key)}">${esc(scCut(p.label, 6))}: ${esc(scCut(src.name, 9))} (${scOut(src)})<title>${esc(p.label)}: ${esc(src.name)} geeft nu ${scOut(src)}</title></text>`
    : `<text class="ptl" x="${b.x + 11}" y="${y + 4}">${esc(p.label)}${paramText[p.name] !== undefined ? `: ${esc(paramText[p.name])}` : ''}</text>`}`;
  }).join('');
  const right = b.zones.map((z, i) => {
    const y = rowY(b, i), on = b.status.zone === i, gang = z.entity && scGangOf(z.entity);
    return `<text class="bkt ${on ? 'zon' : ''}" x="${b.x + SW_W - 11}" y="${y + 4}" text-anchor="end">${esc(scCut(z.name, 9))} ${z.seconds}s</text>
      <circle class="pt rel ${on ? 'on' : ''} ${z.entity ? '' : 'free'}" cx="${b.x + SW_W}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="zout" data-i="${i}"><title>Zone ${i + 1}: sleep naar het relais van een schakelaar</title></circle>
      ${z.entity ? `<text class="stub" x="${b.x + SW_W + 11}" y="${y + 4}" ${gang ? `data-sc="go" data-k="${esc(gang.s.key)}"` : ''}>→ ${esc(scCut(gang ? `${gang.s.name} ${gang.g.label}` : z.entity, 28))}<title>${esc(z.entity)}</title></text>` : ''}`;
  }).join('');
  return `<g class="bk spr t-spr ${SC.sel === b.key ? 'sel' : ''}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 28))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">Sprinkler · ${esc(scCut(scSprText(b), 30))}</text>${left}${right}</g>`;
}

function scPanelState(b) {
  if (!b.host) return 'nog geen IP-adres';
  if (!b.online) return 'offline';
  return [b.greeted ? 'online' : 'online, wacht op HA', b.temperature != null ? `${b.temperature.toFixed(1)} °C` : ''].filter(Boolean).join(' · ');
}

function scPanelPanel(b, props) {
  const draft = SC.panelDraft[b.id] || {};
  const hub = location.hostname;
  const relays = b.gangs.map((g) => `<button class="btn small" type="button" data-sc="pnrelay" data-id="${esc(b.id)}" data-n="${g.i + 1}" data-on="${g.on ? 0 : 1}" ${g.on === null ? 'disabled' : ''}>${esc(g.label)}: ${g.on === null ? '?' : g.on ? 'aan' : 'uit'}</button>`).join('');
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.online ? 'ok' : ''}">${esc(scPanelState(b))}</span>${b.rssi != null ? `<span class="sub">${b.rssi} dBm</span>` : ''}</div>
    ${props}
    <label>IP-adres van de panel</label><input type="text" data-pnfield="host" data-id="${esc(b.id)}" placeholder="192.168.0.105" value="${esc(draft.host ?? b.host)}">
    <label>Configuratie (YAML van nspanel-lovelace-ui)</label><textarea rows="14" style="flex:none;min-height:260px" spellcheck="false" data-pnfield="yaml" data-id="${esc(b.id)}" placeholder="cards:&#10;  - type: cardGrid&#10;    title: Licht&#10;    entities: ...">${esc(draft.yaml ?? b.yaml)}</textarea>
    ${b.error ? `<div class="sub">Config niet te gebruiken: ${esc(b.error)}</div>` : ''}
    <div class="row" style="margin-top:6px"><button class="btn small primary" type="button" data-sc="pnsave" data-id="${esc(b.id)}">Opslaan</button>${Object.keys(draft).length ? '<span class="sub">niet opgeslagen</span>' : ''}</div>
    <h2>Relais</h2><div class="row">${relays}</div>
    <div class="sub">Sleep een <b>touch</b> (de knop op de panel) naar de ingang van een lamp, of een <b>relais</b> naar de uitgang. In HA heet het <code>${esc(b.gangs[0].entity)}</code>. Een knop schakelt altijd zijn eigen relais, ook zonder HA; staat de touch op een lamp, dan volgt dat relais ook die lamp.</div>
    <h2>Op de panel zetten</h2>
    <ol class="sub" style="padding-left:18px;margin:4px 0">
      <li>Zet de AppDaemon-app <code>nspanel-lovelace-ui</code> uit.</li>
      <li>IP-adres van Home Assistant: <input type="text" id="pnhub" value="${esc(hub)}"> <a id="pndl" class="btn small" href="api/panels/script?hub=${encodeURIComponent(hub)}">huisconfig.be downloaden</a></li>
      <li>Webpagina van de panel → <b>Consoles → Manage File system</b>: upload het bestand.</li>
      <li>Zet als laatste regel in <code>autoexec.be</code>: <code>load("huisconfig.be")</code> en herstart de panel.</li>
    </ol>
    <div class="sub">Terugdraaien: die regel weghalen, herstarten en AppDaemon weer aanzetten.</div>
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delpanel" data-id="${esc(b.id)}">Paneel verwijderen</button></div>`;
}

function scScreenState(b) {
  if (!b.plate) return 'nog niet gekoppeld';
  if (!b.online) return 'offline';
  return { query: 'online, controleert ontwerp', push: 'online, stuurt ontwerp', ready: 'online' }[b.phase] || 'online';
}

function scScreenPanel(b, props) {
  scLoadEnts();
  const pages = b.pages.map((p, pi) => `<div class="tmd"><div class="row sp"><b>Pagina ${pi + 1}</b>
      ${b.pages.length > 1 ? `<button class="btn small danger" type="button" data-sc="hsdelpage" data-k="${esc(b.key)}" data-p="${pi}" title="Pagina weghalen">✕</button>` : ''}</div>
    <label>Titel</label><input type="text" maxlength="60" data-hstitle="${pi}" value="${esc(p.title)}">
    ${p.buttons.map((x, i) => `<div class="row sp" style="margin-top:6px"><b>Knop ${i + 1}</b><button class="btn small danger" type="button" data-sc="hsdelbtn" data-k="${esc(b.key)}" data-p="${pi}" data-i="${i}" title="Knop weghalen">✕</button></div>
      <label>Tekst</label><input type="text" maxlength="60" data-hslabel="${pi}" data-i="${i}" value="${esc(x.label)}">
      <label>Bedient</label>${scEntSelect(`data-hsent="${pi}" data-i="${i}"`, x.entity, 'niets')}`).join('')}
    ${p.buttons.length < 4 ? `<div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="hsaddbtn" data-k="${esc(b.key)}" data-p="${pi}">+ Knop</button></div>` : ''}</div>`).join('');
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.online ? 'ok' : ''}">${esc(scScreenState(b))}</span>${b.uptime ? `<span class="sub">aan sinds ${esc(b.uptime)}</span>` : ''}</div>
    ${props}
    <label>Naam op de firmware</label><input type="text" maxlength="32" data-hsfield="plate" list="hsplates" spellcheck="false" autocomplete="off" placeholder="bv. scherm01" value="${esc(b.plate)}">
    <datalist id="hsplates">${b.plates.map((p) => `<option value="${esc(p)}"></option>`).join('')}</datalist>
    <div class="sub">Een scherm heeft één eigenaar. Haal het eerst uit de pyscript-config, anders sturen beide tegelijk.</div>
    <label>Scherm donker na (seconden, 0 = nooit)</label><input type="number" min="0" max="3600" step="1" data-hsfield="timeout" value="${b.timeout}">
    ${b.missing.length ? `<div class="sub"><b>Niet in Home Assistant:</b> ${b.missing.map((e) => `<code>${esc(e)}</code>`).join(', ')}. Deze knoppen blijven grijs.</div>` : ''}
    <h2>Pagina's</h2>${pages}
    <div class="row" style="margin-top:6px">${b.pages.length < 12 ? `<button class="btn small" type="button" data-sc="hsaddpage" data-k="${esc(b.key)}">+ Pagina</button>` : ''}<button class="btn small danger" type="button" data-sc="delscreen" data-id="${esc(b.id)}">Scherm verwijderen</button></div>`;
}

async function scSaveScreen(b, patch) {
  try {
    await api('PUT', `screens/${b.id}`, patch);
  } finally {
    SC.force = true;
    await refresh();
  }
}

function scTuyaState(b) {
  if (!b.available) return 'niet beschikbaar op armv7';
  if (!b.configured) return 'nog niet ingesteld';
  return b.online ? `verbonden, protocol ${b.version}` : b.error || 'verbinden…';
}

function scTuyaPanel(b, props) {
  const opt = (v, cur, text) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(text)}</option>`;
  const rows = b.points.filter((p) => !p.hide && !p.rw).map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(scTyValue(p))}</td><td class="sub">${esc(p.dp)}</td></tr>`).join('');
  const control = (p) => {
    const attr = `data-tyset="${esc(p.dp)}"`;
    if (p.type === 'bool') return `<select ${attr}>${opt('ON', p.value ? 'ON' : 'OFF', 'aan')}${opt('OFF', p.value ? 'ON' : 'OFF', 'uit')}</select>`;
    if (p.type === 'enum') return `<select ${attr}>${p.options.map((o) => opt(o, p.value, o)).join('')}</select>`;
    return `<input type="number" ${attr} min="${p.min}" max="${p.max}" step="${p.step}" value="${esc(p.value)}">`;
  };
  const controls = b.points.filter((p) => p.rw && !p.hide).map((p) => `<label>${esc(p.name)}${p.unit ? ` (${esc(p.unit)})` : ''}</label>${control(p)}`).join('');
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.online ? 'ok' : ''}">${esc(scTuyaState(b))}</span></div>
    ${props}
    ${b.nrelay ? `<h2>Relais</h2>${scGangRows(b)}<div class="sub">Sleep een <b>touch</b> naar de ingang van een lamp, of een <b>relais</b> naar de uitgang. De knop op het toestel schakelt altijd zijn eigen relais; staat de touch op een lamp, dan volgt de rest van die lamp mee.</div>` : ''}
    ${controls ? `<h2>Bedienen</h2>${controls}<div class="sub">Een wijziging gaat meteen naar het toestel.</div>` : ''}
    <h2>Toestel</h2>
    <label>Device-id</label><input type="text" maxlength="40" data-tyfield="device_id" spellcheck="false" autocomplete="off" value="${esc(b.device_id)}">
    <label>IP-adres</label><input type="text" maxlength="253" data-tyfield="host" spellcheck="false" autocomplete="off" placeholder="192.168.8.26" value="${esc(b.host)}">
    <label>Local key</label><input type="password" maxlength="16" data-tyfield="key" autocomplete="new-password" placeholder="${b.has_key ? 'ingesteld, leeg laten om te behouden' : '16 tekens'}">
    <div class="sub">Uit de Tuya API Explorer (<code>local_key</code>). Blijft in de add-on en gaat niet mee in de export.</div>
    <label>Protocol</label><select data-tyfield="version">${opt('auto', b.version, 'automatisch zoeken')}${['3.5', '3.4', '3.3'].map((v) => opt(v, b.version, v)).join('')}</select>
    <label>Profiel</label><select data-tyfield="profile">${opt('', b.profile, b.has_model ? 'eigen datamodel' : 'geen')}${Object.keys(b.profiles).map((k) => opt(k, b.profile, b.profiles[k])).join('')}</select>
    <label>Datamodel plakken</label><textarea rows="8" style="flex:none;min-height:150px" spellcheck="false" data-tyfield="model" placeholder="Antwoord van Query Things Data Model uit de Tuya API Explorer"></textarea>
    <div class="sub">Het datamodel zegt wat elk datapunt betekent en wat instelbaar is. Een profiel is hetzelfde, maar al ingebouwd en met nette namen. Zonder een van beide zijn de datapunten alleen te lezen, onder hun nummer. Veel toestellen laten maar één verbinding toe: haal het toestel eerst uit LocalTuya en sluit de SmartLife-app.</div>
    ${rows ? `<h2>Waarden</h2><div class="scroll"><table><tbody>${rows}</tbody></table></div>` : ''}
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="deltuya" data-id="${esc(b.id)}">Toestel verwijderen</button></div>`;
}

async function scSaveTuya(b, patch) {
  try {
    await api('PUT', `tuya/${b.id}`, patch);
  } finally {
    SC.force = true;
    await refresh();
  }
}

const SC_WEEK = ['ma', 'di', 'wo', 'do', 'vr', 'za', 'zo'];

function scSprinklerPanel(b, props) {
  const sel = (p) => {
    const srcs = scBlocks().filter((s) => (s.kind === 'timer' && p.type === 'bool') || (s.kind === 'module' && s.out === p.type));
    return `<select data-spinput="${esc(p.name)}"><option value="">${p.name === 'block' ? 'niet aangesloten' : 'waarde hierboven gebruiken'}</option>${srcs.map((s) => `<option value="${esc(s.key)}" ${s.key === p.ref ? 'selected' : ''}>${esc(s.name)} (nu ${scOut(s)})</option>`).join('')}</select>`;
  };
  const port = (n) => b.ports.find((p) => p.name === n);
  scLoadEnts();
  const zones = b.zones.map((z, i) => `<div class="tmd"><div class="row sp"><b>Zone ${i + 1}</b>${b.status.zone === i ? '<span class="badge ok">aan</span>' : ''}
      ${b.zones.length > 1 ? `<button class="btn small danger" type="button" data-sc="spdelzone" data-k="${esc(b.key)}" data-i="${i}" title="Zone weghalen">✕</button>` : ''}</div>
    <label>Naam</label><input type="text" maxlength="60" data-spzone="name" data-i="${i}" value="${esc(z.name)}">
    <label>Aan-tijd in seconden</label><input type="number" min="0" max="86400" step="1" data-spzone="seconds" data-i="${i}" value="${z.seconds}">
    <label>Relais</label>${scEntSelect(`data-spzone="entity" data-i="${i}"`, z.entity, 'niet aangesloten')}</div>`).join('');
  const st = b.status;
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${st.running ? 'ok' : ''}">${esc(scSprText(b))}</span><span class="sub">${esc(S.clock.time)} · ${esc(S.clock.tz)}</span></div>
    ${props}
    <label style="margin:6px 0;color:var(--ink);cursor:pointer"><input type="checkbox" data-spfield="enabled" ${b.enabled ? 'checked' : ''}> ${b.enabled ? 'Actief: stuurt de relais aan' : 'Uit: raakt de relais niet aan'}</label>
    <div class="row">${st.running ? `<button class="btn small danger" type="button" data-sc="sprun" data-id="${esc(b.id)}" data-op="stop">Stoppen</button><span class="sub">nog ${Math.ceil(st.left / 60)} min</span>` : `<button class="btn small primary" type="button" data-sc="sprun" data-id="${esc(b.id)}" data-op="start" ${b.enabled ? '' : 'disabled'}>Nu starten</button>`}</div>
    <h2>Planning</h2>
    <label>Startuur</label><input type="time" data-spfield="start" value="${esc(b.start)}">${sel(port('start'))}
    <label>Dagen</label><div class="row">${SC_WEEK.map((d, i) => `<label style="margin:0;color:var(--ink)"><input type="checkbox" data-spday="${i}" ${b.days.includes(i) ? 'checked' : ''}> ${d}</label>`).join('')}</div>
    <label>Aantal cycli</label><input type="number" min="1" max="100" step="1" data-spfield="cycles" value="${b.cycles}">${sel(port('cycles'))}
    <label>Wachttijd tussen zones (minuten)</label><input type="number" min="0" max="1440" step="1" data-spfield="wait" value="${b.wait}">${sel(port('wait'))}
    <label>Blokkeren (bv. bij regen)</label>${sel(port('block'))}<div class="sub">Op 1 start er niets en stopt een lopende beurt.</div>
    <label>Relais opnieuw aansturen elke (seconden)</label><input type="number" min="5" max="600" step="1" data-spfield="refresh" value="${b.refresh}">
    <div class="sub">Herstelt een gemist bevel, ook "uit". Valt de add-on, HA of de wifi weg, dan helpt alleen een limiet in het toestel zelf.</div>
    <h2>Zones</h2>
    <div class="row"><button class="btn small" type="button" data-sc="spbox" data-k="${esc(b.key)}">Zones uit relaisbox…</button></div>
    <div class="sub">Een relais hoeft geen schakelaar-blok te zijn: kies een relaisbox of kies het relais bij de zone.</div>
    ${zones}
    <div class="row" style="margin-top:6px">${b.zones.length < 16 ? `<button class="btn small" type="button" data-sc="spaddzone" data-k="${esc(b.key)}">+ Zone</button>` : ''}<button class="btn small danger" type="button" data-sc="delsprinkler" data-id="${esc(b.id)}">Sprinkler verwijderen</button></div>`;
}

function scBoxRelays(d) { // de relais van een toestel om aan te vinken; wat al een zone is en gewone gangen staan aan
  const b = scBlock(SC.boxFor), used = new Set(b.zones.map((z) => z.entity));
  const ents = importEntities(d);
  const anyUsed = ents.some((e) => used.has(e.entity_id));
  return ents.map((e, i) => `<label style="display:block;margin:2px 0;color:var(--ink)" title="${esc(e.entity_id)}"><input type="checkbox" data-boxent="${i}" ${(anyUsed ? used.has(e.entity_id) : e.gang) ? 'checked' : ''}> ${esc(e.short)}</label>`).join('');
}

function scBoxDialog(b) {
  api('GET', 'ha/devices').then((r) => r, () => ({ registry: false, devices: [] })).then((r) => {
    if (!r.registry || !r.devices.length) {
      smallDialog('<h3>Zones uit relaisbox</h3><p class="sub">Home Assistant geeft geen apparatenlijst. Typ de entiteit dan bij elke zone.</p>', async () => {});
      return;
    }
    SC.boxFor = b.key;
    SC.boxDevs = [...r.devices].sort((x, y) => x.name.localeCompare(y.name));
    const used = new Set(b.zones.map((z) => z.entity));
    const cur = SC.boxDevs.findIndex((d) => d.entities.some((e) => used.has(e.entity_id)));
    smallDialog(`<h3>Zones uit relaisbox</h3><p class="sub">Elk aangevinkt relais wordt een zone, in deze volgorde. Een relais dat al een zone is houdt zijn naam en aan-tijd; de andere zones vervallen. Er kunnen maximaal 16 zones.</p>
      <label for="spdev">Relaisbox</label><select id="spdev"><option value="">Kies een apparaat…</option>${SC.boxDevs.map((d, i) => `<option value="${i}" ${i === cur ? 'selected' : ''}>${esc(d.name)}${d.area ? ` (${esc(d.area)})` : ''}</option>`).join('')}</select>
      <div id="spboxlist" style="max-height:45vh;overflow:auto;margin-top:8px">${cur >= 0 ? scBoxRelays(SC.boxDevs[cur]) : ''}</div>`, async () => {
      const i = $('#spdev').value;
      if (i === '') throw new Error('Kies een relaisbox.');
      const ents = importEntities(SC.boxDevs[+i]);
      const picked = [...document.querySelectorAll('#spboxlist input[data-boxent]:checked')].map((x) => ents[+x.dataset.boxent]);
      if (!picked.length) throw new Error('Vink minstens één relais aan.');
      const now = scBlock(SC.boxFor);
      const zones = picked.map((e) => now.zones.find((z) => z.entity === e.entity_id) || { name: e.short, entity: e.entity_id, seconds: 60 });
      await api('PUT', `sprinklers/${now.id}`, { zones });
      SC.force = true;
    });
  });
}

async function scSaveSprinkler(b, patch) {
  try {
    await api('PUT', `sprinklers/${b.id}`, patch);
  } finally {
    SC.force = true;
    await refresh();
  }
}

function scModulePanel(b, props) {
  const sources = (p) => scBlocks().filter((s) => s.key !== b.key && ((s.kind === 'timer' && p.type === 'bool') || (s.kind === 'module' && s.out === p.type)));
  const ports = b.ports.map((p) => `<label>${esc(p.label)}</label><select data-mport="${esc(p.name)}" data-k="${esc(b.key)}"><option value="">niet aangesloten</option>${sources(p).map((s) => `<option value="${esc(s.key)}" ${s.key === p.ref ? 'selected' : ''}>${esc(s.name)} (nu ${scOut(s)})</option>`).join('')}</select>`).join('');
  const users = scUsers(b);
  const nosun = (b.type === 'sunrise' || b.type === 'sunset') && b.value === null ? '<div class="sub">Nog onbekend: de add-on heeft de locatie van Home Assistant nog niet, of de zon komt vandaag niet op of gaat niet onder.</div>' : '';
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge">uitgang nu ${scOut(b)}</span><span class="sub">${esc(S.clock.time)} · ${esc(S.clock.tz)}</span></div>
    <div class="sub">${esc(SC_HELP[b.type])}</div>${nosun}${props}
    ${b.type === 'number' ? `<label for="scmnum">Waarde</label><input type="number" id="scmnum" min="0" max="100000" step="1" data-k="${esc(b.key)}" value="${esc(b.param)}">` : ''}
    ${b.type === 'time' ? `<label for="scmtime">Tijd</label><input type="time" id="scmtime" data-k="${esc(b.key)}" value="${esc(b.param)}">` : ''}
    ${ports ? `<h2>Ingangen</h2>${ports}` : ''}
    <h2>Uitgang gaat naar</h2>${users.map((u) => `<div class="sub"><a href="#" data-sc="go" data-k="${esc(u.key)}">${esc(u.name)}</a></div>`).join('') || '<div class="sub">Nog nergens.</div>'}
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delmodule" data-id="${esc(b.id)}">Module verwijderen</button></div>`;
}

// Het menu "+ Module": alles wat je op een pagina kunt zetten, per soort
const SC_MENU = [
  ['Toestellen', [['lamp', 'Lamp', 'met touches en relais'], ['switch', 'Schakelaar', 'een toestel uit Home Assistant'], ['panel', 'NSPanel', 'scherm met twee relais'], ['screen', 'Scherm', 'aanraakscherm met openHASP'], ['tuya', 'Tuya', 'toestel rechtstreeks over het LAN']]],
  ['Tijd', [['timer', 'Timer', 'intervallen per dag, geeft 0 of 1'], ['sunrise', 'Zonsopgang', 'tijd van vandaag'], ['sunset', 'Zonsondergang', 'tijd van vandaag'], ['time', 'Vaste tijd', 'bv. 02:30']]],
  ['Waarden', [['number', 'Getal', 'bv. aantal cycli']]],
  ['Rekenen', [['add', 'Optellen', 'tijd + tijd'], ['sub', 'Aftrekken', 'tijd − tijd']]],
  ['Vergelijken', [['between', 'Tussen', '1 tussen twee tijden'], ['after', 'Na', '1 na een tijd'], ['before', 'Voor', '1 voor een tijd']]],
  ['Logica', [['and', 'En', '1 als beide 1 zijn'], ['or', 'Of', '1 als een van beide 1 is']]],
  ['Routines', [['sprinkler', 'Sprinkler', 'zones na elkaar, in cycli']]],
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
  for (const sp of scBlocks().filter((b) => b.kind === 'sprinkler')) { // een relais toont welke sprinkler het aanstuurt
    sp.zones.forEach((z) => {
      const hit = z.entity && scGangOf(z.entity);
      if (!hit) return;
      const k = `${hit.s.key}|${hit.g.i}|out`;
      if (!tags.has(k)) tags.set(k, []);
      tags.get(k).push({ key: sp.key, text: `${sp.name}: ${z.name}` });
    });
  }
  const shown = new Set(list.map((b) => b.key));
  const wires = !SC_WIRES ? '' : conns.filter((x) => scSame(x) && shown.has(x.lamp.key) && x.c.gang < x.sw.gangs.length).map(scWire).join('');
  const blocks = list.map((b) => (b.kind === 'lamp' ? scLamp(b) : b.kind === 'timer' ? scTimer(b) : b.kind === 'module' ? scModule(b) : b.kind === 'sprinkler' ? scSprinkler(b) : b.kind === 'tuya' ? scTuya(b, tags) : scSwitch(b, tags))).join('');
  return `<g class="wires">${wires}</g>${blocks}<path id="sctemp" class="wire temp"/>`; // draden onder de blokken: anders liggen ze over de poorten
}

function scDrawTemp() { // alleen dit ene pad: de poort onder de cursor moet blijven bestaan, anders mist de volgende klik hem
  const b = SC.link && scBlock(SC.link.b), el = $('#sctemp');
  const a = !b ? null : b.page === SC.page ? scPortPos(SC.link) : [SC.link.x, 0]; // uit een andere ruimte: de draad komt van bij de tabbladen
  if (el) el.setAttribute('d', a ? `M ${a[0]} ${a[1]} L ${SC.link.x} ${SC.link.y}` : '');
}

function scLinkBar() { // zolang een draad wacht op zijn andere kant: waar hij vandaan komt en hoe je stopt
  const el = $('#sclink');
  if (!el) return;
  const l = SC.link && SC.link.armed ? SC.link : null, b = l && scBlock(l.b);
  if (l && !b) SC.link = null; // het blok is intussen weg
  document.body.classList.toggle('linking', !!b);
  if (!b) { el.innerHTML = ''; return; }
  const gang = b.gangs && l.g !== undefined ? b.gangs[+l.g] : null;
  el.innerHTML = `<b>Draad vanaf ${esc(b.name)}${gang ? ` · ${esc(gang.label)}` : ''}</b> <span>Kies eventueel een andere ruimte en klik op de poort of de lamp waar hij heen moet.</span> <button class="btn small" type="button" data-sc="linkstop">Stoppen (Esc)</button>`;
}

function scLinkStop() {
  SC.link = null;
  scDraw();
  scLinkBar();
}

function scPortPos(p) {
  const b = scBlock(p.b);
  if (!b) return null;
  if (b.kind === 'timer') return [b.x + SW_W, rowY(b, 0)];
  if (b.kind === 'sprinkler') return p.role === 'min' ? [b.x, rowY(b, +p.i)] : [b.x + SW_W, rowY(b, +p.i)];
  if (b.kind === 'module') return p.role === 'min' ? [b.x, rowY(b, +p.i)] : [b.x + SW_W, rowY(b, 0)];
  if (p.role === 'disable') return [b.x, rowY(b, scRows(b) - 1)];
  if (p.role === 'control') return [b.x, rowY(b, scRows(b) - 2)];
  if (SC_GANGED.includes(b.kind)) return [p.role === 'relay' ? b.x + scW(b) : b.x, rowY(b, +p.g)];
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
  return part('Lampen', 'lamp') + part('Schakelaars', 'switch') + part('Timers', 'timer') + part('Tijd en logica', 'module') + part('Routines', 'sprinkler') + part('Panelen', 'panel') + part('Schermen', 'screen') + part('Tuya', 'tuya');
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
  return b.kind === 'lamp' ? scLampPanel(b, props) : b.kind === 'timer' ? scTimerPanel(b, props) : b.kind === 'module' ? scModulePanel(b, props) : b.kind === 'sprinkler' ? scSprinklerPanel(b, props) : b.kind === 'panel' ? scPanelPanel(b, props) : b.kind === 'screen' ? scScreenPanel(b, props) : b.kind === 'tuya' ? scTuyaPanel(b, props) : scSwitchPanel(b, props);
}

function scConnRow(b, c, i, dir) {
  const name = c.sw ? `${c.swName} · ${c.label}` : c.text;
  return `<div class="scc"><a href="#" data-sc="wiresel" data-w="${esc(b.key)}|${dir}|${i}">${esc(name)}</a>${c.mode && c.mode !== 'toggle' ? ` <span class="badge">${MODE_NL[c.mode]}</span>` : ''}
    ${c.ref.entity ? `<button class="btn small" type="button" data-sc="member" data-id="${esc(b.id)}" data-e="${esc(c.ref.entity)}" title="Alleen dit relais wisselen (nu ${c.on === true ? 'aan' : c.on === false ? 'uit' : 'onbekend'})">⇄</button>` : ''}
    ${c.fixed ? '<span class="badge">vast</span>' : `<button class="btn small danger" type="button" data-sc="unwire" data-w="${esc(b.key)}|${dir}|${i}" title="Verbinding weghalen">✕</button>`}</div>`;
}

// Alle schakelaars en lampen uit HA, ook die zonder apparaat in HA (groepen, templates, helpers), om in te zoeken
function scFreshEnts() {
  return api('GET', 'ha/entities').then((r) => { SC.haEnts = r.entities; }, () => { SC.haEnts = SC.haEnts || []; });
}
function scLoadEnts() { // eenmalig voor de zijbalk
  if (SC.haEnts) return;
  SC.haEnts = [];
  scFreshEnts().then(() => { SC.force = true; render(); });
}
const SC_DOMAINS = [['light', 'Lampen'], ['switch', 'Schakelaars'], ['input_boolean', 'Helpers'], ['fan', 'Ventilatoren']];

// Een keuzelijst met alles uit HA, per soort en op naam. Kent HA (nog) niets, dan kun je de entiteit typen.
function scEntSelect(attrs, cur, first, hide = []) {
  const ents = SC.haEnts || [];
  if (!ents.length) return `<input type="text" ${attrs} spellcheck="false" autocomplete="off" value="${esc(cur || '')}" placeholder="bv. light.lamp_oprit">`;
  const label = (e) => (e.name && e.name !== e.entity_id ? `${e.name} (${e.entity_id})` : e.entity_id);
  const groups = SC_DOMAINS.map(([dom, title]) => {
    const list = ents.filter((e) => e.entity_id.startsWith(`${dom}.`) && !hide.includes(e.entity_id)).sort((x, y) => label(x).localeCompare(label(y), 'nl'));
    return list.length ? `<optgroup label="${title}">${list.map((e) => `<option value="${esc(e.entity_id)}" ${e.entity_id === cur ? 'selected' : ''}>${esc(label(e))}</option>`).join('')}</optgroup>` : '';
  }).join('');
  const gone = cur && !ents.some((e) => e.entity_id === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)} (niet in HA)</option>` : ''; // nooit stil een keuze kwijtraken
  return `<select ${attrs}><option value="">${esc(first)}</option>${gone}${groups}</select>`;
}

function scAddEntity(b, dir) { // aansluiten zonder schakelaar-blok
  if (b.source !== 'ha') return '';
  return scEntSelect(`data-scent="${dir}" data-k="${esc(b.key)}"`, null, `+ ${dir === 'in' ? 'ingang' : 'uitgang'} uit Home Assistant…`, (dir === 'in' ? b.inputs : b.outputs).map((c) => c.ref.entity));
}

function scAddSelect(b, dir) {
  if (dir === 'out' && b.source === 'node') return '';
  const taken = new Set((dir === 'in' ? b.inputs : b.outputs).map((c) => `${c.sw}|${c.gang}`));
  const groups = scBlocks().filter((s) => s.kind === 'switch' || s.kind === 'tuya').map((s) => {
    const gangs = s.gangs.filter((g) => !taken.has(`${s.key}|${g.i}`) && (dir === 'in' ? g.touch : g.relay) && (b.source !== 'ha' || g.entity));
    return gangs.length ? `<optgroup label="${esc(s.name)}">${gangs.map((g) => `<option value="${esc(s.key)}|${g.i}">${esc(g.label)}</option>`).join('')}</optgroup>` : '';
  }).join('');
  return groups ? `<select data-scadd="${dir}" data-k="${esc(b.key)}"><option value="">+ ${dir === 'in' ? 'ingang' : 'uitgang'} toevoegen, ook uit een andere ruimte…</option>${groups}</select>` : '';
}

function scLampPanel(b, pages) {
  scLoadEnts();
  const onoff = b.on === true ? 'Aan' : b.on === false ? 'Uit' : 'Onbekend';
  const active = b.source === 'ha'
    ? `<label style="margin:0;color:var(--ink);cursor:pointer"><input type="checkbox" data-grp-toggle="${esc(b.id)}" ${b.enabled ? 'checked' : ''}> Actief</label>
      <div class="sub">${b.enabled ? 'De add-on stuurt deze lamp: touches en ingangen werken.' : 'De add-on stuurt deze lamp niet: touches en de ingangen hieronder doen niets. Alleen de knop hieronder werkt.'}</div>
      ${!b.enabled && b.control ? '<div class="sub" style="color:var(--warn)">De aan/uit-ingang is aangesloten, maar doet niets zolang Actief uit staat.</div>' : ''}` : '';
  const tail = b.source === 'ha'
    ? `<button class="btn small danger" data-sc="dellamp" data-id="${esc(b.id)}">Verwijderen</button>`
    : `<button class="btn small primary" data-act="learn" data-n="${b.node}" data-r="${b.relay}">Schakelaars leren</button><button class="btn small" data-act="edit" data-n="${b.node}" data-r="${b.relay}">Bewerken</button>`;
  const sources = scBlocks().filter((t) => t.kind === 'timer' || (t.kind === 'module' && t.out === 'bool'));
  const srcOpts = (cur) => sources.map((t) => { const v = t.kind === 'timer' ? t.id : t.key; return `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${t.kind === 'timer' ? 'Timer ' : t.tech === t.name ? '' : `${esc(t.tech)} `}${esc(t.name)} (nu ${scOut(t)})</option>`; }).join('');
  const ctl = b.source !== 'ha' ? '' : `<label for="scctl">Aan/uit-ingang${b.held === null || b.held === undefined ? '' : ` <span class="badge ok">nu vast ${b.held ? 'aan' : 'uit'}</span>`}</label><select id="scctl" data-k="${esc(b.key)}"><option value="">geen</option>${srcOpts(b.control)}</select>
    <div class="sub">Op 1 staat de lamp vast aan, op 0 vast uit. Touches en HA kunnen hem dan niet veranderen. Gaat voor op de disable-ingang.</div>`;
  const dis = b.source !== 'ha' ? '' : `<label for="scdis">Disable-ingang${b.blocked ? ' <span class="badge bad">nu geblokkeerd</span>' : ''}</label><select id="scdis" data-k="${esc(b.key)}"><option value="">geen</option>${srcOpts(b.disable)}</select>
    <div class="sub">Op 1 doen de touches niets voor deze lamp. De knop hieronder en het lamp-relais via HA werken dan nog wel.</div>`;
  const known = [...b.inputs, ...b.outputs].filter((c) => c.on === true || c.on === false);
  const mixed = b.source === 'ha' && known.some((c) => c.on !== known[0].on) ? '<span class="badge warn">relais staan niet gelijk</span>' : '';
  return `<h3>${esc(b.name)}</h3>${pages}${active}${ctl}${dis}${mixed}
    <button class="power ${b.on === true ? 'on' : b.on === null ? 'unknown' : ''} ${b.pending ? 'pending' : ''}" data-sc="toggle" data-k="${esc(b.key)}">${onoff}</button>
    <h2>Ingangen</h2>${b.inputs.map((c, i) => scConnRow(b, c, i, 'in')).join('') || '<div class="sub">Nog geen touches.</div>'}${scAddSelect(b, 'in')}${scAddEntity(b, 'in')}
    <h2>Uitgangen</h2>${b.outputs.map((c, i) => scConnRow(b, c, i, 'out')).join('') || '<div class="sub">Nog geen relais: de lamp weet niet welk relais hem schakelt.</div>'}${scAddSelect(b, 'out')}${scAddEntity(b, 'out')}
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

function scGangRows(b) { // per gang: waar hij op aangesloten is, en een keuze om er een lamp bij te zetten
  const conns = scConns().filter((x) => x.sw && x.sw.key === b.key);
  const free = b.source === 'node' ? S.free_relays.filter((f) => f.node === b.node) : [];
  return b.gangs.filter((g) => g.touch || g.relay || b.kind !== 'tuya').map((g) => {
    const to = conns.filter((x) => x.c.gang === g.i);
    return `<div class="scc"><div><b>${esc(g.label)}</b>${g.entity ? `<div class="sub"><code>${esc(g.entity)}</code></div>` : ''}
      ${to.map((x) => `<div class="sub"><a href="#" data-sc="go" data-k="${esc(x.lamp.key)}">${x.dir === 'in' ? 'touch →' : 'relais ←'} ${esc(x.lamp.name)}</a></div>`).join('') || '<div class="sub">niet aangesloten</div>'}
      ${scConnSelect(b, g)}
      ${free.some((f) => f.relay === g.i) ? `<button class="btn small" data-act="addlight" data-n="${b.node}" data-r="${g.i}">Als lamp toevoegen</button>` : ''}</div></div>`;
  }).join('');
}

function scSwitchPanel(b, pages) {
  const rows = scGangRows(b);
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
    <div id="sclink"></div>
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
  scLinkBar();
  const panel = $('#scpanel'), html = scPanel(), active = document.activeElement;
  // een lijst die net open staat of een veld waarin je typt niet onder je handen vervangen
  if (SC.force || (html !== SC.lastPanel && !(active && panel.contains(active) && /SELECT|INPUT|TEXTAREA/.test(active.tagName)))) {
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
  if (to.kind === 'lamp' && sink.role === 'control') await api('POST', 'schema/control', { lamp: to.key, source: from.kind === 'timer' ? from.id : from.key });
  else if (to.kind === 'lamp' && (!sink.role || sink.role === 'disable')) await api('POST', 'schema/disable', { lamp: to.key, timer: from.kind === 'timer' ? from.id : from.key });
  else if (to.kind === 'sprinkler' && sink.role === 'min') await api('PUT', `sprinklers/${to.id}`, { inputs: { [to.ports[+sink.i].name]: from.key } });
  else if (to.kind === 'module' && sink.role === 'min') await api('PUT', `modules/${to.id}`, { inputs: { [to.ports[+sink.i].name]: from.key } });
  else throw new Error('Een uitgang sluit je aan op een ingang van een module of een ingang (aan/uit of disable) van een lamp.');
  SC.sel = to.key;
  SC.force = true;
  await refresh();
}

async function scWireZone(a, b) { // van een zone van een sprinkler naar een relais
  const zone = a.role === 'zout' ? a : b, rel = zone === a ? b : a;
  const sp = scBlock(zone.b), sw = scBlock(rel.b);
  if (!SC_GANGED.includes(sw.kind) || rel.role !== 'relay') throw new Error('Een zone sluit je aan op het relais van een schakelaar.');
  const gang = sw.gangs[+rel.g];
  if (!gang.entity) throw new Error('Dit relais loopt niet via Home Assistant; een sprinkler kan het nog niet aansturen.');
  const zones = sp.zones.map((z, i) => (i === +zone.i ? { ...z, entity: gang.entity } : z));
  await api('PUT', `sprinklers/${sp.id}`, { zones });
  SC.sel = sp.key;
  SC.force = true;
  await refresh();
}

const SC_LOGIC_ROLES = ['tout', 'mout', 'min', 'disable', 'control'];
const SC_HELD = 'Deze lamp wordt vastgehouden door zijn aan/uit-ingang.';

async function scWireUp(a, b) { // a en b zijn poorten; één hoort bij een lamp, de ander bij een schakelaar, of het zijn modules
  const pa = scBlock(a.b), pb = scBlock(b.b);
  if (a.role === 'zout' || b.role === 'zout') return scWireZone(a, b);
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
      <textarea id="swents" rows="5" spellcheck="false" placeholder="switch.touch_keuken_relay_1 = Links">${esc(lines)}</textarea>
      <label for="swadd">Losse entiteit toevoegen (ook zonder apparaat in HA)</label>${scEntSelect('id="swadd"', null, 'Kies een entiteit…')}`, async () => {
    const body = { name: $('#swname').value.trim(), gangs: parse() };
    if (b) {
      await api('PUT', `schema/switch/${b.id}`, body);
    } else {
      const r = await api('POST', 'schema/switch', { ...body, page: SC.page, ...scFreeSpot('switch') });
      SC.sel = SC.reveal = r.key;
    }
  });
  if (b) { scFreshEnts().then(() => open([])); return; }
  Promise.all([api('GET', 'ha/devices').then((r) => r.devices, () => []), scFreshEnts()]).then(([devs]) => {
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
    const r = d.type === 'sprinkler' ? await api('POST', 'sprinklers', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'panel' ? await api('POST', 'panels', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'screen' ? await api('POST', 'screens', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'tuya' ? await api('POST', 'tuya', { page: SC.page, ...scFreeSpot('module') })
      : await api('POST', 'modules', { type: d.type, page: SC.page, ...scFreeSpot('module') });
    SC.sel = SC.reveal = r.key;
    SC.force = true;
    await refresh();
  },
  sprun: async (d) => {
    await api('POST', `sprinklers/${d.id}/run`, { op: d.op });
    SC.force = true;
    await refresh();
  },
  spbox: (d) => scBoxDialog(scBlock(d.k)),
  spaddzone: (d) => {
    const b = scBlock(d.k);
    return scSaveSprinkler(b, { zones: [...b.zones, { name: `Zone ${b.zones.length + 1}`, entity: null, seconds: 60 }] });
  },
  spdelzone: (d) => {
    const b = scBlock(d.k);
    return scSaveSprinkler(b, { zones: b.zones.filter((_, i) => i !== +d.i) });
  },
  pnsave: async (d) => {
    const pid = d.id, draft = SC.panelDraft[pid] || {};
    await api('PUT', `panels/${pid}`, draft);
    delete SC.panelDraft[pid]; // bij een weigering blijft wat je getypt hebt staan
    SC.force = true;
    toast('Opgeslagen');
    await refresh();
  },
  pnrelay: async (d) => {
    await api('POST', `panels/${d.id}/relay/${d.n}`, { on: d.on === '1' });
  },
  delpanel: async (d) => {
    if (!confirm('Dit paneel verwijderen? Zijn relais en temperatuur verdwijnen ook uit Home Assistant.')) return;
    await api('DELETE', `panels/${d.id}`);
    delete SC.panelDraft[d.id];
    SC.sel = null;
    await refresh();
  },
  hsaddpage: (d) => {
    const b = scBlock(d.k);
    return scSaveScreen(b, { pages: [...b.pages, { title: `Pagina ${b.pages.length + 1}`, buttons: [] }] });
  },
  hsdelpage: (d) => {
    const b = scBlock(d.k);
    return scSaveScreen(b, { pages: b.pages.filter((_, i) => i !== +d.p) });
  },
  hsaddbtn: (d) => {
    const b = scBlock(d.k);
    return scSaveScreen(b, { pages: b.pages.map((p, i) => (i === +d.p ? { ...p, buttons: [...p.buttons, { label: '', entity: null }] } : p)) });
  },
  hsdelbtn: (d) => {
    const b = scBlock(d.k);
    return scSaveScreen(b, { pages: b.pages.map((p, i) => (i === +d.p ? { ...p, buttons: p.buttons.filter((_, j) => j !== +d.i) } : p)) });
  },
  delscreen: async (d) => {
    if (!confirm('Dit scherm verwijderen? Op het scherm zelf blijft het ontwerp staan tot het opnieuw opstart.')) return;
    await api('DELETE', `screens/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  deltuya: async (d) => {
    if (!confirm('Dit Tuya-toestel verwijderen? Zijn entiteiten verdwijnen uit Home Assistant.')) return;
    await api('DELETE', `tuya/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  delsprinkler: async (d) => {
    if (!confirm('Deze sprinkler verwijderen? Zijn relais krijgen nog één keer "uit".')) return;
    await api('DELETE', `sprinklers/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  delmodule: async (d) => {
    if (!confirm('Deze module verwijderen? Wat ze gebruikte is daarna niet meer aangesloten.')) return;
    await api('DELETE', `modules/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  page: (d) => { SC.page = d.id; SC.sel = null; if (SC.link && !SC.link.armed) SC.link = null; render(); }, // een draad die wacht gaat mee
  linkstop: () => scLinkStop(),
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
  newlamp: async () => {
    if (!SC.page) throw new Error('Maak eerst een ruimte met de + bij de tabbladen.');
    await scFreshEnts();
    smallDialog(`<h3>Nieuwe lamp</h3><p class="sub">Een lamp via Home Assistant, eerst uitgeschakeld. Kies wat de lamp schakelt, of laat het leeg en trek later een draad.</p>
      <label for="lpent">Lamp of relais uit Home Assistant</label>${scEntSelect('id="lpent"', null, 'Later aansluiten')}
      <label for="lpname">Naam</label><input type="text" id="lpname" maxlength="60" placeholder="bv. Spots keuken">`, async () => {
      const entity = $('#lpent').value.trim();
      const r = await api('POST', 'schema/lamp', { name: $('#lpname').value.trim(), ...(entity ? { entity } : {}), page: SC.page, ...scFreeSpot('lamp') });
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
    if (b.held !== null && b.held !== undefined) throw new Error(SC_HELD);
    if (b.source === 'ha') await api('POST', `groups/${b.id}/cmd`, { op: 'toggle' }).catch((e) => { throw new Error(`${e.message}: geen enkel relais bereikbaar?`); });
    else await api('POST', `light/${b.node}/${b.relay}/cmd`, { op: 'toggle' });
  },
  wiresel: (d) => { const [lamp, dir, i] = d.w.split('|'); SC.sel = { lamp, dir, i: +i }; render(); },
  unwire: (d) => scUnwire(d.w),
  member: async (d) => {
    const b = scBlock(`g:${d.id}`);
    if (b.held !== null && b.held !== undefined) throw new Error(SC_HELD);
    return api('POST', `groups/${d.id}/cmd`, { op: 'toggle', entity: d.e }).catch((e) => { throw new Error(`${e.message}: ${d.e} is niet bereikbaar?`); });
  },
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
  if (dest && dest.b === from.b && dest.g === from.g && dest.i === from.i && dest.swRole === from.swRole) dest = null;
  if (!dest) { // nergens op beland: de draad blijft wachten, zodat je nog naar een andere ruimte kunt
    SC.link.armed = true;
    scLinkBar();
    scDrawTemp();
    return;
  }
  SC.link = null;
  scDrawTemp(); // ook als de verbinding zo meteen geweigerd wordt, mag de draad niet blijven hangen
  scLinkBar();
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
      scLinkBar();
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
document.addEventListener('pointercancel', () => { if (SC.link && !SC.link.armed) SC.link = null; SC.drag = null; if (tab === 'schema') render(); });

document.addEventListener('keydown', (ev) => {
  if (tab !== 'schema' || dlg().open || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if (ev.key === 'Escape' && SC.link) scLinkStop();
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

document.addEventListener('input', (ev) => {
  const t = ev.target, f = t.dataset && t.dataset.pnfield;
  if (f) (SC.panelDraft[t.dataset.id] = SC.panelDraft[t.dataset.id] || {})[f] = t.value; // een statuswijziging tekent de zijbalk opnieuw: dit blijft staan
  if (t.id === 'pnhub') {
    const a = $('#pndl');
    if (a) a.href = `api/panels/script?hub=${encodeURIComponent(t.value.trim())}`;
  }
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (t.id === 'scname') {
    const b = scBlock(t.dataset.k), name = t.value.trim();
    act(async () => {
      try {
        if (b.kind === 'switch') await api('POST', 'schema/name', { key: b.key, name });
        else if (b.kind === 'module') await api('PUT', `modules/${b.id}`, { name });
        else if (b.kind === 'sprinkler') await api('PUT', `sprinklers/${b.id}`, { name });
        else if (b.kind === 'panel') await api('PUT', `panels/${b.id}`, { name });
        else if (b.kind === 'screen') await api('PUT', `screens/${b.id}`, { name });
        else if (b.kind === 'tuya') await api('PUT', `tuya/${b.id}`, { name });
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
  if (t.id === 'lpent' && !$('#lpname').value.trim()) {
    const hit = (SC.haEnts || []).find((e) => e.entity_id === t.value.trim());
    if (hit) $('#lpname').value = hit.name.slice(0, 60);
  }
  if (t.id === 'swadd' && t.value.trim()) {
    const box = $('#swents'), hit = (SC.haEnts || []).find((e) => e.entity_id === t.value.trim());
    box.value = `${box.value.trim() ? `${box.value.trim()}\n` : ''}${t.value.trim()}${hit ? ` = ${hit.name}` : ''}`;
    if (hit && !$('#swname').value.trim()) $('#swname').value = hit.name.slice(0, 60);
    t.value = '';
  }
  if (t.dataset && t.dataset.scent && t.value.trim()) {
    const lamp = t.dataset.k, dir = t.dataset.scent, entity = t.value.trim();
    t.blur();
    act(async () => {
      try { await api('POST', 'schema/wire', { lamp, dir, ref: { entity } }); } finally { SC.force = true; await refresh(); }
    })();
  }
  if (t.id === 'spdev') $('#spboxlist').innerHTML = t.value === '' ? '' : scBoxRelays(SC.boxDevs[+t.value]);
  if (t.id === 'scmnum' && t.value !== '') {
    const b = scBlock(t.dataset.k), value = +t.value;
    t.blur();
    act(async () => {
      try { await api('PUT', `modules/${b.id}`, { value }); } finally { SC.force = true; await refresh(); }
    })();
  }
  if (t.dataset && (t.dataset.spfield || t.dataset.spinput || t.dataset.spzone || t.dataset.spday !== undefined) && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'sprinkler') {
    const b = scBlock(SC.sel), d = t.dataset;
    let patch;
    if (d.spinput) patch = { inputs: { [d.spinput]: t.value || null } };
    else if (d.spday !== undefined) patch = { days: b.days.filter((x) => x !== +d.spday).concat(t.checked ? [+d.spday] : []) };
    else if (d.spzone) {
      const v = d.spzone === 'seconds' ? +t.value : d.spzone === 'entity' ? (t.value.trim() || null) : t.value;
      patch = { zones: b.zones.map((z, i) => (i === +d.i ? { ...z, [d.spzone]: v } : z)) };
    } else patch = { [d.spfield]: d.spfield === 'enabled' ? t.checked : d.spfield === 'start' ? t.value : +t.value };
    t.blur();
    act(() => scSaveSprinkler(b, patch))();
  }
  if (t.dataset && t.dataset.tyset && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'tuya') {
    const b = scBlock(SC.sel), dp = t.dataset.tyset, value = t.value;
    t.blur();
    act(async () => {
      try {
        await api('POST', `tuya/${b.id}/set`, { dp, value });
      } finally {
        SC.force = true;
        await refresh();
      }
    })();
  }
  if (t.dataset && t.dataset.tyfield && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'tuya') {
    const b = scBlock(SC.sel);
    const value = t.dataset.tyfield === 'key' ? t.value : t.value.trim();
    t.blur();
    if (value || t.dataset.tyfield !== 'model') act(() => scSaveTuya(b, { [t.dataset.tyfield]: value }))();
  }
  if (t.dataset && (t.dataset.hsfield || t.dataset.hstitle !== undefined || t.dataset.hslabel !== undefined || t.dataset.hsent !== undefined) && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'screen') {
    const b = scBlock(SC.sel), d = t.dataset;
    const button = (pi, patch) => b.pages.map((p, i) => (i === +pi ? { ...p, buttons: p.buttons.map((x, j) => (j === +d.i ? { ...x, ...patch(x) } : x)) } : p));
    let patch;
    if (d.hsfield) patch = { [d.hsfield]: d.hsfield === 'timeout' ? +t.value : t.value.trim() };
    else if (d.hstitle !== undefined) patch = { pages: b.pages.map((p, i) => (i === +d.hstitle ? { ...p, title: t.value } : p)) };
    else if (d.hslabel !== undefined) patch = { pages: button(d.hslabel, () => ({ label: t.value })) };
    else {
      const entity = t.value.trim() || null;
      const known = (SC.haEnts || []).find((e) => e.entity_id === entity);
      patch = { pages: button(d.hsent, (x) => ({ entity, label: x.label || (known ? known.name : '') })) }; // een lege knop krijgt de naam uit HA
    }
    t.blur();
    act(() => scSaveScreen(b, patch))();
  }
  if (t.id === 'scctl') {
    const k = t.dataset.k, source = t.value || null;
    t.blur();
    act(async () => {
      try { await api('POST', 'schema/control', { lamp: k, source }); } finally { SC.force = true; await refresh(); }
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
