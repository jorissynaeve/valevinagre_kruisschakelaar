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

const SC_GANGED = ['switch', 'panel', 'tuya', 'remote']; // blokken met gangen: per gang een touch en een relais (remote: alleen een gang van een andere installatie)
const scBlocks = () => S.schema.blocks;
const scBlock = (k) => scBlocks().find((b) => b.key === k);
const scW = (b) => (b.kind === 'lamp' ? LAMP_W : b.kind === 'tuya' || (b.kind === 'remote' && b.sub === 'tuya') ? TUYA_W : SW_W);
const scRows = (b) => (b.kind === 'card' || b.kind === 'remote' || b.kind === 'action' || b.kind === 'airco' || b.kind === 'solar' ? b.rows : b.kind === 'panel' || b.kind === 'screen' ? b.gangs.length + b.cards.length + 1 : b.kind === 'timer' ? 1 : b.kind === 'sprinkler' ? Math.max(b.ports.length, b.zones.length) : b.kind === 'module' ? Math.max(b.ports.length, 1) : b.kind === 'lamp' ? Math.max(b.inputs.length, b.outputs.length) + (b.source === 'ha' ? 3 : 1) : b.gangs.length);
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
  if (b.kind === 'sprinkler' || b.kind === 'action') return 'spr';
  if (b.kind === 'panel' || b.kind === 'screen' || b.kind === 'tuya' || b.kind === 'airco' || b.kind === 'solar' || b.kind === 'card' || b.kind === 'remote') return b.kind;
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

const scPageNo = (b, n) => b.cards.slice(0, n).filter((c) => c.type !== 'saver').length + 1; // de screensaver is geen pagina

function scPanelBlock(b, tags) { // de relais zoals bij een schakelaar (een scherm heeft er geen), daaronder de kaarten in volgorde
  const rows = b.cards.map((c, n) => {
    const y = rowY(b, b.gangs.length + n);
    return `<line class="bkrule" x1="${b.x}" x2="${b.x + SW_W}" y1="${y - SC_ROW / 2}" y2="${y - SC_ROW / 2}"/>
      <circle class="pt crd" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="pcard" data-i="${n}"><title>Sleep hier een andere kaart op om ze ervoor te zetten</title></circle>
      <text class="bkt away" x="${b.x + 11}" y="${y + 4}" data-sc="go" data-k="${esc(c.key)}">${c.type === 'saver' ? '◐' : `${scPageNo(b, n)}.`} ${esc(scCut(c.name, 26))}<title>${c.type === 'saver' ? 'Screensaver: het startscherm' : `Pagina ${scPageNo(b, n)}`}</title></text>`;
  }).join('');
  const y = rowY(b, b.gangs.length + b.cards.length);
  const free = `<line class="bkrule" x1="${b.x}" x2="${b.x + SW_W}" y1="${y - SC_ROW / 2}" y2="${y - SC_ROW / 2}"/>
    <circle class="pt crd free" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="pcard" data-i="new"><title>Sleep hierheen vanaf de uitgang van een kaart</title></circle>
    <text class="ptl" x="${b.x + 11}" y="${y + 4}">+ kaart</text>`;
  return scSwitch(b, tags).replace(/<\/g>$/, `${rows}${free}</g>`);
}

const SC_CARD_NL = { grid: 'Knoppen', charger: 'Laadpaal', saver: 'Screensaver', power: 'Energie', qr: 'Wifi (QR)', airco: 'Airco' };

function scCardLines(b) { // wat een kaart zonder knoppen op haar blok laat zien
  if (b.type === 'power' && b.labels) return b.labels.length ? b.labels : ['Nog geen rijen'];
  if (b.type === 'charger') return [b.device ? `Lader: ${b.device.name}${b.device.online ? '' : ' (offline)'}` : 'Nog geen lader gekozen', `€ ${b.price} per kWh${b.price_entity ? ' of uit HA' : ''}`];
  if (b.type === 'saver') return [b.weather ? `Weer: ${b.weather}` : 'Geen weer', b.temperature ? `Binnen: ${b.temperature}` : 'Geen temperatuur', [b.status1, b.status2].filter(Boolean).length ? `${[b.status1, b.status2].filter(Boolean).length} statuspictogram(men)` : 'Klok en datum'];
  if (b.type === 'airco') return [b.device ? `Airco: ${b.device.name}${b.device.online ? '' : ' (offline)'}` : 'Nog geen airco gekozen', 'Temperatuur en stand'];
  if (b.type === 'power') return b.items.length ? b.items.map((it) => it.entity ? (it.name || it.entity) : 'leeg') : ['Nog geen rijen'];
  return [b.ssid ? `Netwerk: ${b.ssid}` : 'Nog geen netwerk', b.security === 'nopass' ? 'zonder wachtwoord' : 'met wachtwoord'];
}

const scDevKey = (dev) => (dev.startsWith('r:') ? dev : `y:${dev}`); // het blok van een toestel

function scChargerRows(b) { // de laadpaal: links Status, Laadtijd en Geladen (een waarde elk), rechts start/stop naar een instelling
  const ins = b.ins.map((x, i) => {
    const y = rowY(b, i), src = x.ref ? scSrc(x.ref) : null;
    return `<circle class="pt v-number ${x.ref ? '' : 'free'}" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="cdin" data-i="${i}"><title>${x.label}: sleep hierheen vanaf een waarde (rechts op een Tuya-blok)</title></circle>
      ${x.ref ? `<text class="bkt away" x="${b.x + 11}" y="${y + 4}" ${src && src.key ? `data-sc="go" data-k="${esc(src.key)}"` : ''}>${x.label}: ${esc(scCut(x.src || '', 17))}<title>${esc(x.src || '')} (nu ${esc(src ? src.out : '?')})</title></text>`
    : `<text class="ptl" x="${b.x + 11}" y="${y + 4}">+ ${x.label.toLowerCase()}</text>`}`;
  }).join('');
  const y1 = rowY(b, 1), dev = b.target.device;
  const out = `<circle class="pt v-bool ${b.target_label ? '' : 'free'}" cx="${b.x + SW_W}" cy="${y1}" r="6" data-b="${esc(b.key)}" data-role="cdout" data-i="1"><title>Start/stop: sleep naar een instelling (links op een Tuya-blok), bv. Laden</title></circle>
    <text class="ptl" x="${b.x + SW_W - 11}" y="${y1 + 4}" text-anchor="end">start/stop</text>
    ${b.target_label ? `<text class="stub" x="${b.x + SW_W + 11}" y="${y1 + 4}" data-sc="go" data-k="${esc(scDevKey(dev))}">→ ${esc(scCut(b.target_label, 26))}<title>${esc(b.target_label)}: starten ${esc(b.target.on || '(niets)')}, stoppen ${esc(b.target.off || '(niets)')}</title></text>` : ''}`;
  return `${ins}${out}<text class="bkt" x="${b.x + 11}" y="${rowY(b, 3) + 4}">Prijs: € ${b.price} per kWh${b.price_entity ? ' of uit HA' : ''}</text>`;
}

function scCard(b) {
  const y0 = rowY(b, 0);
  const rows = b.type === 'charger' ? scChargerRows(b) : b.type !== 'grid' ? scCardLines(b).map((t, i) => `<text class="bkt" x="${b.x + 11}" y="${rowY(b, i) + 4}">${esc(scCut(t, 26))}</text>`).join('')
    : b.items.map((it, i) => {
      const y = rowY(b, i), go = it.target && scBlock(it.target);
      return `<circle class="pt crd ${it.ref || it.entity ? '' : 'free'}" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="citem" data-i="${i}"><title>Knop ${i + 1}: sleep hierheen vanaf een lamp, een gang van een schakelaar of de uitgang van een kaart</title></circle>
        <text class="bkt ${go ? 'away' : ''}" x="${b.x + 11}" y="${y + 4}" ${go ? `data-sc="go" data-k="${esc(go.key)}"` : ''}>${i + 1}. ${esc(scCut(it.label, 24))}<title>${esc(it.label)}</title></text>`;
    }).join('') + (b.rows > b.items.length ? `<circle class="pt crd free" cx="${b.x}" cy="${rowY(b, b.items.length)}" r="6" data-b="${esc(b.key)}" data-role="citem" data-i="new"><title>Sleep hierheen vanaf een lamp of de uitgang van een kaart</title></circle>
        <text class="ptl" x="${b.x + 11}" y="${rowY(b, b.items.length) + 4}">+ knop</text>` : '');
  return `<g class="bk crdb t-card ${SC.sel === b.key ? 'sel' : ''}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 28))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">Kaart · ${SC_CARD_NL[b.type]}${b.type === 'grid' && b.layout === 'list' ? ' (lijst)' : ''}</text>${rows}
    <circle class="pt crd ${b.users.length ? '' : 'free'}" cx="${b.x + SW_W}" cy="${y0}" r="6" data-b="${esc(b.key)}" data-role="cout" data-g="0"><title>Uitgang: sleep naar een paneel of scherm (pagina) of naar een knop van een rasterkaart (subpagina)</title></circle>
    ${b.users.map((u, n) => `<text class="stub" x="${b.x + SW_W + 11}" y="${y0 + 4 + n * 13}" data-sc="go" data-k="${esc(u.key)}">→ ${esc(scCut(u.name, 22))} ${u.as === 'page' ? `(${b.type === 'saver' ? 'start' : `pagina ${u.i + 1}`})` : '(sub)'}<title>${esc(u.name)}</title></text>`).join('')}</g>`;
}

function scRemote(b, tags) { // een lamp, gang of Tuya-toestel van een andere installatie: alleen-lezen, te bedienen
  if (b.sub === 'tuya') return scRemoteTuya(b);
  const st = b.on === true ? 'on' : b.on === null || b.on === undefined ? 'unknown' : '';
  const rows = b.sub === 'gang'
    ? `<text class="bkt" x="${b.x + SW_W / 2}" y="${rowY(b, 0) + 4}" text-anchor="middle">${b.on === true ? 'aan' : b.on === false ? 'uit' : 'onbekend'}${b.busy ? ' · bezet' : ''}</text>${scGangPorts(b, b.gangs[0], SW_W, tags)}`
    : b.sub === 'lamp'
    ? `<circle class="bulb ${st}" cx="${b.x + 22}" cy="${rowY(b, 0)}" r="9" data-sc="rtoggle" data-k="${esc(b.key)}"><title>Aan of uit zetten (via ${esc(b.peerName)})</title></circle>
       <text class="bkt" x="${b.x + 40}" y="${rowY(b, 0) + 4}">${b.on === true ? 'aan' : b.on === false ? 'uit' : 'onbekend'}${b.held ? ' · vastgehouden' : ''}</text>`
    : b.points.length ? b.points.map((p, i) => `<text class="bkt" x="${b.x + 11}" y="${rowY(b, i) + 4}"><title>${esc(`${p.name}: ${scTyValue(p)}`)}</title>${esc(scCut(p.name, 20))} <tspan class="tyv">${esc(scCut(scTyValue(p), 14))}</tspan></text>`).join('')
      : `<text class="bkt" x="${b.x + 11}" y="${rowY(b, 0) + 4}">Nog geen waarden</text>`;
  return `<g class="bk rem t-remote ${SC.sel === b.key ? 'sel' : ''} ${b.online ? '' : 'off'}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 28))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">↔ ${esc(scCut(b.peerName, 16))} · ${{ lamp: 'Lamp', gang: 'Gang', tuya: 'Tuya' }[b.sub] || b.sub}${b.online ? '' : ' · offline'}</text>${rows}</g>`;
}

function scRemotePanel(b) {
  const opt = (v, cur, text) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(text)}</option>`;
  const users = scBlocks().filter((c) => c.kind === 'card' && ((c.items || []).some((it) => it.ref === b.key) || c.tuya === b.key));
  let body;
  if (b.sub === 'gang') {
    body = `<div class="row"><button class="btn small" type="button" data-sc="rcmd" data-k="${esc(b.key)}" data-op="on" ${b.linked && !b.busy ? '' : 'disabled'}>Aan</button>
      <button class="btn small" type="button" data-sc="rcmd" data-k="${esc(b.key)}" data-op="off" ${b.linked && !b.busy ? '' : 'disabled'}>Uit</button></div>
      ${b.busy ? `<div class="sub" style="color:var(--warn)">Bezet: deze gang zit bij ${esc(b.peerName)} in een actieve kring. Daar beslist die kring; een bevel van hier wordt geweigerd.</div>` : ''}
      <h2>Aansluiten</h2>${scGangRows(b)}
      <div class="sub">Sleep de <b>touch</b> naar de ingang van een lamp, of het <b>relais</b> naar de uitgang. De kring draait hier; de druk komt via ${esc(b.peerName)}. Valt de koppeling weg, dan doet deze gang niet mee; komt ze terug, dan neemt de touch de stand van de lamp over.</div>`;
  } else if (b.sub === 'lamp') {
    body = `<div class="row"><button class="btn small" type="button" data-sc="rcmd" data-k="${esc(b.key)}" data-op="on" ${b.linked ? '' : 'disabled'}>Aan</button>
      <button class="btn small" type="button" data-sc="rcmd" data-k="${esc(b.key)}" data-op="off" ${b.linked ? '' : 'disabled'}>Uit</button></div>
      ${b.held ? '<div class="sub">Deze lamp wordt bij de eigenaar vastgehouden door haar aan/uit-ingang: een bevel wordt geweigerd.</div>' : ''}`;
  } else {
    const control = (p) => (p.type === 'bool' ? `<select data-rset="${esc(p.dp)}">${opt('ON', p.value ? 'ON' : 'OFF', 'aan')}${opt('OFF', p.value ? 'ON' : 'OFF', 'uit')}</select>`
      : p.type === 'enum' ? `<select data-rset="${esc(p.dp)}">${p.options.map((o) => opt(o, p.value, o)).join('')}</select>`
        : `<input type="number" data-rset="${esc(p.dp)}" min="${p.min}" max="${p.max}" step="${p.step}" value="${esc(p.value)}">`);
    const rw = b.points.filter((p) => p.rw);
    body = rw.length ? `<h2>Bedienen</h2>${rw.map((p) => `<label>${esc(p.name)}${p.unit ? ` (${esc(p.unit)})` : ''}</label>${control(p)}`).join('')}<div class="sub">Een wijziging gaat via ${esc(b.peerName)} naar het toestel.</div>` : '';
  }
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.online ? 'ok' : ''}">${b.linked ? (b.online ? 'online' : 'toestel offline') : `${esc(b.peerName)} niet verbonden`}</span></div>
    <div class="sub">Van de installatie <b>${esc(b.peerName)}</b>. Instellen en hernoemen doe je daar; hier kun je het bedienen${b.sub === 'gang' ? ' en op een lamp aansluiten' : ` en aan een kaart hangen${b.sub === 'lamp' ? ' (sleep een knop van een rasterkaart naar dit blok)' : ' (kies het bij een laadpaalkaart)'}`}.</div>
    <label for="scroom">Ruimte</label><select id="scroom" data-k="${esc(b.key)}"><option value="">(niet geplaatst)</option>${S.schema.pages.map((p) => `<option value="${esc(p.id)}" ${p.id === b.page ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
    ${body}
    <h2>Gebruikt door</h2>${users.map((c) => `<div class="sub"><a href="#" data-sc="go" data-k="${esc(c.key)}">${esc(c.name)}</a></div>`).join('') || '<div class="sub">Nog geen kaart.</div>'}`;
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

const scDevOf = (b) => (b.kind === 'tuya' ? { id: b.id, card: `y:${b.id}`, points: b.points } : { id: b.key, card: b.key, points: b.points });

function scSrc(ref) { // de bron van een ingang: een blok, of een datapunt (v:<dp>@<toestel>) -> {key, name, out}
  if (!ref) return null;
  if (ref.startsWith('w:') || ref.startsWith('o:')) {
    const o = [...scAcOuts('bool'), ...scAcOuts('number')].find((x) => x.key === ref);
    return o ? { key: o.block, name: o.name, out: o.out } : { key: '', name: ref.startsWith('o:') ? 'omvormer (weg)' : 'airco (weg)', out: '?' };
  }
  if (ref.startsWith('v:')) {
    const [dp, dev] = ref.slice(2).split('@'), blk = scBlock(dev.startsWith('r:') ? dev : `y:${dev}`);
    const pt = blk && blk.points.find((x) => String(x.dp) === dp);
    return { key: blk ? blk.key : '', name: blk ? `${blk.name} · ${pt ? pt.name : dp}` : `datapunt ${dp} (weg)`, out: pt ? scTyValue(pt) : '?' };
  }
  const b = scBlock(ref);
  return b ? { key: b.key, name: b.name, out: scOut(b) } : null;
}

function scDpUsers(dev, dp, out) { // wie dit datapunt gebruikt: rechts (een waarde) of links (een routine die het zet)
  dp = String(dp);
  if (!out) {
    return scBlocks().filter((a) => (a.kind === 'action' && a.device === dev.id && String(a.dp) === dp)
      || (a.kind === 'card' && a.type === 'charger' && a.target.device === dev.id && String(a.target.dp) === dp)).map((a) => ({ key: a.key, text: a.name }));
  }
  const v = `v:${dp}@${dev.id}`;
  return scBlocks().filter((u) => ((u.kind === 'module' || u.kind === 'sprinkler') && u.ports.some((x) => x.ref === v)) || (u.kind === 'module' && u.type === 'dp' && u.device === dev.id && String(u.dp) === dp)
    || (u.kind === 'card' && (u.items || []).some((it) => (it.ref === dev.card || it.tuya === dev.card) && String(it.dp) === dp))
    || (u.kind === 'card' && (u.ins || []).some((x) => x.ref === v)) || (u.kind === 'airco' && u.ports.some((x) => x.ref === v))).map((u) => ({ key: u.key, text: u.name }));
}

function scTyRow(b, p, i, left, w) { // een datapunt met een poort: links instellen (een 0/1 maakt een routine), rechts een waarde
  const y = rowY(b, i), x = left ? b.x + 11 : b.x + w - 11, users = scDpUsers(scDevOf(b), p.dp, !left);
  const tip = `${p.name}: ${scTyValue(p)}${left ? ' · sleep hierheen vanaf een 0/1 (timer, Groter dan, ...) om het te laten instellen' : ' · sleep naar Groter dan, Kleiner dan of een knop van een kaart'}`;
  return `<circle class="pt ${left ? 'v-bool' : 'v-number'} ${users.length ? '' : 'free'}" cx="${left ? b.x : b.x + w}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="${left ? 'pset' : 'pval'}" data-dp="${esc(p.dp)}" data-i="${i}"><title>${esc(tip)}</title></circle>
    <text class="bkt" x="${x}" y="${y + 4}" ${left ? '' : 'text-anchor="end"'}><title>${esc(tip)}</title>${esc(scCut(p.name, left ? 17 : 21))} <tspan class="tyv">${esc(scCut(scTyValue(p), left ? 14 : 12))}</tspan></text>
    ${users.map((u, n) => `<text class="stub" x="${left ? b.x - 11 : b.x + w + 11}" y="${y + 4 + n * 13}" ${left ? 'text-anchor="end"' : ''} data-sc="go" data-k="${esc(u.key)}">${left ? `${esc(scCut(u.text, 24))} ←` : `→ ${esc(scCut(u.text, 24))}`}<title>${esc(u.text)}</title></text>`).join('')}`;
}

function scRemoteTuya(b) { // een Tuya-toestel van een andere installatie: dezelfde poorten als een Tuya-blok van hier
  return `<g class="bk ty t-remote ${SC.sel === b.key ? 'sel' : ''} ${b.online ? '' : 'off'}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${TUYA_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${TUYA_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 44))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">↔ ${esc(scCut(b.peerName, 20))} · Tuya${b.online ? '' : ' · offline'}</text>
    ${b.pin.length || b.pout.length ? '' : `<text class="bkt" x="${b.x + TUYA_W / 2}" y="${rowY(b, 0) + 4}" text-anchor="middle">Nog geen waarden</text>`}
    ${b.pin.map((x, i) => scTyRow(b, x, i, true, TUYA_W)).join('')}${b.pout.map((x, i) => scTyRow(b, x, i, false, TUYA_W)).join('')}</g>`;
}

function scTuya(b, tags) { // bovenaan de relais (touch en relais, zoals een schakelaar); daaronder links wat je kunt instellen, rechts wat het toestel meldt
  const relays = b.gangs.slice(0, b.nrelay).map((g) => {
    const y = rowY(b, g.i);
    return `<line class="bkrule" x1="${b.x}" x2="${b.x + TUYA_W}" y1="${y - SC_ROW / 2}" y2="${y - SC_ROW / 2}"/>
      <text class="bkt" x="${b.x + TUYA_W / 2}" y="${y + 4}" text-anchor="middle">${esc(scCut(g.label, 30))}</text>${scGangPorts(b, g, TUYA_W, tags)}`;
  }).join('');
  const row = (p, n, left) => scTyRow(b, p, n + b.nrelay, left, TUYA_W);
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

// ---------------------------------------------------------------- airco (Gree): een eigen blok, links wat haar stuurt, rechts wat ze meldt

const SC_AC_W = LAMP_W;
const SC_AC_MODE = { auto: 'automatisch', cool: 'koelen', dry: 'drogen', fan_only: 'ventileren', heat: 'verwarmen' };
const SC_AC_FAN = { auto: 'automatisch', low: 'laag', medium_low: 'middel-laag', medium: 'middel', medium_high: 'middel-hoog', high: 'hoog' };
const SC_AC_SWV = { default: 'standaard', swing: 'zwaaien', top: 'boven', upper_middle: 'midden-boven', middle: 'midden', lower_middle: 'midden-onder', bottom: 'onder',
  swing_top: 'zwaaien boven', swing_upper_middle: 'zwaaien midden-boven', swing_middle: 'zwaaien midden', swing_lower_middle: 'zwaaien midden-onder', swing_bottom: 'zwaaien onder' };
const SC_AC_SWH = { default: 'standaard', swing: 'zwaaien', left: 'links', left_center: 'links-midden', center: 'midden', right_center: 'rechts-midden', right: 'rechts' };
const SC_AC_SW = { turbo: 'Turbo', quiet: 'Stil', light: 'Licht', xfan: 'X-fan (nadrogen)', health: 'Health', save: 'Energiebesparing', heat8: '8 °C-verwarming', sleep: 'Slaapstand' };

const scAcOutText = (o) => (o.value === null || o.value === undefined ? '?' : o.type === 'bool' ? (o.value ? '1' : '0') : `${o.value} ${o.unit || '°C'}`);
function scAcOuts(type) { // de uitgangen van alle airco's van deze soort, als bron voor een ingang
  return scBlocks().filter((b) => b.kind === 'airco' || b.kind === 'solar').flatMap((b) => b.outs.filter((o) => o.type === type).map((o) => ({ key: o.ref, block: b.key, name: `${b.name} · ${o.label}`, out: scAcOutText(o) })));
}
function scAcUsers(ref) { // wie deze uitgang van een airco gebruikt
  return scBlocks().filter((u) => ['module', 'sprinkler', 'action', 'airco'].includes(u.kind) && u.ports.some((p) => p.ref === ref)).map((u) => ({ key: u.key, text: u.name }));
}

function scAirco(b) {
  const w = SC_AC_W, s = b.state || {};
  const ins = b.ports.map((p, i) => {
    const y = rowY(b, i), src = scSrc(p.ref);
    return `<circle class="pt v-${p.type} ${src ? '' : 'free'}" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="acin" data-i="${i}"><title>Ingang ${esc(p.label)}: ${p.type === 'bool' ? 'een 0 of 1 van een timer of module' : 'een getal, bv. van een module Getal'}</title></circle>
      ${src ? `<text class="bkt away" x="${b.x + 11}" y="${y + 4}" data-sc="go" data-k="${esc(src.key)}">${esc(p.label)}: ${esc(scCut(src.name, 14))} (${esc(src.out)})<title>${esc(src.name)} geeft nu ${esc(src.out)}</title></text>`
    : `<text class="ptl" x="${b.x + 11}" y="${y + 4}">+ ${esc(p.label)}</text>`}`;
  }).join('');
  const outs = b.outs.map((o, i) => {
    const y = rowY(b, i), users = scAcUsers(o.ref);
    return `<text class="bkt" x="${b.x + w - 11}" y="${y + 4}" text-anchor="end">${esc(o.label)} <tspan class="tyv">${esc(scAcOutText(o))}</tspan></text>
      <circle class="pt v-${o.type} ${users.length ? '' : 'free'} ${o.value === true ? 'on' : ''}" cx="${b.x + w}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="acout" data-i="${i}"><title>Uitgang ${esc(o.label)}: sleep naar een ingang van een module${o.type === 'bool' ? ' (En, Of), een routine of een andere airco' : ' (Groter dan, Kleiner dan)'}</title></circle>
      ${users.map((u, n) => `<text class="stub" x="${b.x + w + 11}" y="${y + 4 + n * 13}" data-sc="go" data-k="${esc(u.key)}">→ ${esc(scCut(u.text, 24))}<title>${esc(u.text)}</title></text>`).join('')}`;
  }).join('');
  const st = !b.online ? (b.configured ? 'offline' : 'niet ingesteld') : s.power ? (SC_AC_MODE[s.mode] || 'aan') : 'uit';
  return `<g class="bk ac t-airco ${SC.sel === b.key ? 'sel' : ''} ${b.online ? '' : 'off'}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${w}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${w}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 34))}</text>
    <text class="bkbadge ${s.power && b.online ? 'ok' : ''}" x="${b.x + w - 12}" y="${b.y + 18}" text-anchor="end">${esc(st)}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">${esc(b.tech)}${b.online && s.target != null ? ` · ingesteld ${s.target} °C` : ''}</text>${ins}${outs}</g>`;
}

function scAircoState(b) {
  if (!b.available) return 'niet beschikbaar op armv7';
  if (!b.configured) return 'nog niet ingesteld';
  return b.online ? `verbonden${b.version === '1' || b.version === '2' ? `, versleuteling v${b.version}` : ''}` : b.error || 'verbinden…';
}

function scAircoPanel(b, props) {
  const opt = (v, cur, text) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(text)}</option>`;
  const s = b.state || {};
  const sel = (field, map, cur) => `<select data-awset="${field}">${cur == null ? '<option value="" selected>?</option>' : ''}${Object.entries(map).map(([v, t]) => opt(v, cur, t)).join('')}</select>`;
  const onoff = (field, cur) => sel(field, { ON: 'aan', OFF: 'uit' }, cur == null ? null : cur ? 'ON' : 'OFF');
  const controls = !b.online ? '' : `<h2>Bedienen</h2>
    <label>Aan</label>${onoff('power', s.power)}
    <label>Stand</label>${sel('mode', SC_AC_MODE, s.mode)}
    <label>Temperatuur (°C)</label><input type="number" min="16" max="30" step="1" data-awset="target" value="${esc(s.target == null ? '' : s.target)}">
    <label>Ventilator</label>${sel('fan', SC_AC_FAN, s.fan)}
    <label>Lamellen op/neer</label>${sel('swing_v', SC_AC_SWV, s.swing_v)}
    <label>Lamellen links/rechts</label>${sel('swing_h', SC_AC_SWH, s.swing_h)}
    ${Object.entries(SC_AC_SW).filter(([k]) => s[k] != null).map(([k, t]) => `<label>${esc(t)}</label>${onoff(k, s[k])}`).join('')}
    <div class="sub">Kamer: ${s.room == null ? 'onbekend' : `${esc(s.room)} °C`}. Een wijziging gaat meteen naar de airco; de stand hier komt altijd uit haar antwoord.</div>`;
  const sources = (p) => [...scBlocks().filter((x) => (x.kind === 'timer' && p.type === 'bool') || (x.kind === 'module' && x.out === p.type)).map((x) => ({ key: x.key, name: x.name, out: scOut(x) })),
    ...scAcOuts(p.type).filter((o) => o.block !== b.key)];
  const ins = b.ports.map((p) => {
    const list = sources(p), cur = p.ref && !list.some((x) => x.key === p.ref) ? scSrc(p.ref) : null;
    const all = cur ? [{ ...cur, key: p.ref }, ...list] : list;
    return `<label>${esc(p.label)}</label><select data-awin="${esc(p.name)}"><option value="">niet aangesloten</option>${all.map((x) => `<option value="${esc(x.key)}" ${x.key === p.ref ? 'selected' : ''}>${esc(x.name)} (nu ${esc(x.out)})</option>`).join('')}</select>
      ${b.last[p.name] ? `<div class="sub">${esc(b.last[p.name])}</div>` : ''}`;
  }).join('');
  const users = b.outs.flatMap((o) => scAcUsers(o.ref).map((u) => ({ ...u, label: o.label })));
  const found = b.unclaimed.length ? `<label>Op het netwerk gevonden</label><select data-awfound="1"><option value="">${b.mac ? 'andere airco kiezen…' : 'kies een airco…'}</option>${b.unclaimed.map((f) => `<option value="${esc(f.mac)}|${esc(f.ip)}">${esc(f.ip)} · ${esc(f.mac)}${f.name ? ` · ${esc(f.name)}` : ''}</option>`).join('')}</select>` : '';
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.online ? 'ok' : ''}">${esc(scAircoState(b))}</span></div>
    <div class="sub">Een airco met een Gree-wifimodule (ook Toyotomi, Sinclair, Cooper&amp;Hunter), rechtstreeks over het LAN zonder cloud. In Home Assistant verschijnt ze als <code>climate.kk_gree_${esc(b.id)}</code>.</div>${props}
    ${controls}
    <h2>Ingangen</h2>${ins}
    <label>Ingang aan zet de airco</label><select data-awfield="on_mode">${opt('keep', b.on_mode, 'aan in de laatste stand')}${Object.entries(SC_AC_MODE).map(([v, t]) => opt(v, b.on_mode, `aan op ${t}`)).join('')}</select>
    <div class="sub">Een ingang doet pas iets als ze <b>verandert</b>: na een herstart of als de bron weer bekend wordt, onthoudt ze alleen de stand. Zo blijft wat je met de afstandsbediening deed staan tot de ingang weer wisselt. <b>Blokkeren</b> op 1 zet de airco uit en houdt de ingang aan tegen (bv. een raamcontact); terug op 0 gaat ze weer aan als aan dan 1 is.</div>
    <h2>Uitgangen gaan naar</h2>${users.map((u) => `<div class="sub">${esc(u.label)} → <a href="#" data-sc="go" data-k="${esc(u.key)}">${esc(u.text)}</a></div>`).join('') || '<div class="sub">Nog nergens. Sleep een uitgang (rechts) naar Groter dan, Kleiner dan, En, Of of een routine.</div>'}
    <h2>Toestel</h2>${found}
    <label>IP-adres</label><input type="text" maxlength="253" data-awfield="host" spellcheck="false" autocomplete="off" placeholder="192.168.1.50" value="${esc(b.host)}">
    <label>MAC-adres</label><input type="text" maxlength="17" data-awfield="mac" spellcheck="false" autocomplete="off" placeholder="wordt opgezocht als je het leeg laat" value="${esc(b.mac)}">
    <label>Versleuteling</label><select data-awfield="version">${opt('auto', b.version, 'automatisch')}${opt('1', b.version, 'v1 (ECB, oudere modules)')}${opt('2', b.version, 'v2 (GCM, nieuwere modules)')}</select>
    <label>Sleutel</label><input type="password" maxlength="16" data-awfield="key" autocomplete="new-password" placeholder="${b.has_key ? 'ingesteld, leeg laten om te behouden' : 'leeg: de add-on vraagt hem zelf aan de airco'}">
    <div class="sub">${b.firmware ? `Firmware ${esc(b.firmware)}. ` : ''}Het IP vind je in de router of met <b>Zoeken</b>. De sleutel vraagt de add-on eenmalig aan de airco (binden); de HACS-integratie mag tijdens de overstap blijven staan.</div>
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="awscan" data-id="${esc(b.id)}" ${b.scanning ? '' : 'disabled'}>Zoeken op het netwerk</button>
      <button class="btn small" type="button" data-sc="awbind" data-id="${esc(b.id)}" ${b.host ? '' : 'disabled'}>Opnieuw binden</button></div>
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delairco" data-id="${esc(b.id)}">Airco verwijderen</button></div>`;
}

async function scSaveAirco(b, patch) {
  try {
    await api('PUT', `airco/${b.id}`, patch);
  } finally {
    SC.force = true;
    await refresh();
  }
}

async function scWireAirco(a, b) { // een airco: een bron naar een ingang, of een uitgang naar een module, routine of andere airco
  const inp = [a, b].find((p) => p.role === 'acin'), out = [a, b].find((p) => p.role === 'acout');
  if (inp && out && inp.b === out.b) throw new Error('Een airco stuurt zichzelf niet.');
  if (inp) {
    const src = inp === a ? b : a, ac = scBlock(inp.b), from = scBlock(src.b), port = ac.ports[+inp.i];
    let ref;
    if (src.role === 'tout' || src.role === 'mout') ref = from.key;
    else if (src.role === 'pval') ref = `v:${src.dp}@${scDevOf(from).id}`;
    else if (src.role === 'acout') ref = from.outs[+src.i].ref;
    else throw new Error('Een ingang van een airco krijgt een 0/1 of een getal: van een timer, een module, een waarde van een Tuya-blok of een andere airco.');
    await api('PUT', `airco/${ac.id}`, { inputs: { [port.name]: ref } });
    SC.sel = ac.key;
  } else {
    const sink = out === a ? b : a, ac = scBlock(out.b), to = scBlock(sink.b), ref = ac.outs[+out.i].ref;
    if (sink.role !== 'min' || !['module', 'sprinkler', 'action'].includes(to.kind)) throw new Error('Een uitgang van een airco sluit je aan op een ingang van een module, een sprinkler, een routine of een andere airco.');
    if (to.kind === 'action') await api('PUT', `actions/${to.id}`, { inputs: { trigger: ref } });
    else await api('PUT', `${to.kind === 'module' ? 'modules' : 'sprinklers'}/${to.id}`, { inputs: { [to.ports[+sink.i].name]: ref } });
    SC.sel = to.key;
  }
  SC.force = true;
  await refresh();
}

// ---------------------------------------------------------------- omvormer (Solarman): een eigen blok met alleen uitgangen

function scSolar(b) {
  const w = SC_AC_W;
  const outs = b.outs.map((o, i) => {
    const y = rowY(b, i), users = scAcUsers(o.ref);
    return `<text class="bkt" x="${b.x + w - 11}" y="${y + 4}" text-anchor="end">${esc(o.label)} <tspan class="tyv">${esc(scAcOutText(o))}</tspan></text>
      <circle class="pt v-${o.type} ${users.length ? '' : 'free'}" cx="${b.x + w}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="acout" data-i="${i}"><title>Uitgang ${esc(o.label)}: sleep naar Groter dan of Kleiner dan, of naar een ingang van een module</title></circle>
      ${users.map((u, n) => `<text class="stub" x="${b.x + w + 11}" y="${y + 4 + n * 13}" data-sc="go" data-k="${esc(u.key)}">→ ${esc(scCut(u.text, 24))}<title>${esc(u.text)}</title></text>`).join('')}`;
  }).join('');
  const pv = b.outs.find((o) => o.name === 'pv');
  return `<g class="bk ac t-solar ${SC.sel === b.key ? 'sel' : ''} ${b.online ? '' : 'off'}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${w}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${w}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 34))}</text>
    <text class="bkbadge ${b.online ? 'ok' : ''}" x="${b.x + w - 12}" y="${b.y + 18}" text-anchor="end">${b.online ? (pv && pv.value != null ? `${pv.value} W` : 'verbonden') : 'offline'}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">${esc(b.tech)}</text>${outs}</g>`;
}

function scSolarPanel(b, props) {
  const v = b.values || {};
  const num = (x, unit, d = 0) => (x == null ? '?' : `${Number(x).toFixed(d)} ${unit}`);
  const users = b.outs.flatMap((o) => scAcUsers(o.ref).map((u) => ({ ...u, label: o.label })));
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.online ? 'ok' : ''}">${b.online ? 'verbonden' : esc(b.error || 'verbinden…')}</span></div>
    <div class="sub">Een omvormer met een Solarman-wifistick (Deye, Sunsynk, ...), rechtstreeks over het LAN zonder cloud. De add-on leest hem elke 15 s en zet de waarden in Home Assistant als sensoren <code>sensor.kk_solar_${esc(b.id)}_…</code> (ook voor het energiedashboard). Alleen lezen: de omvormer zelf stel je hier niet bij.</div>${props}
    ${b.online ? `<h2>Nu</h2>
    <div class="sub">Zon ${num(v.pv, 'W')} (PV1 ${num(v.pv1, 'W')} · ${num(v.pv1_v, 'V', 0)}, PV2 ${num(v.pv2, 'W')} · ${num(v.pv2_v, 'V', 0)})</div>
    <div class="sub">Verbruik ${num(v.load, 'W')} · net ${num(v.grid, 'W')} (+ afname, − teruglevering)</div>
    <div class="sub">Accu ${num(v.soc, '%')} · ${num(v.batt, 'W')} (+ laden, − ontladen) · ${num(v.batt_v, 'V', 1)} · ${num(v.batt_t, '°C', 1)}</div>
    <div class="sub">Vandaag: opbrengst ${num(v.today, 'kWh', 1)} · verbruik ${num(v.load_today, 'kWh', 1)} · afgenomen ${num(v.bought, 'kWh', 1)} · geleverd ${num(v.sold, 'kWh', 1)}</div>` : ''}
    <h2>Uitgangen gaan naar</h2>${users.map((u) => `<div class="sub">${esc(u.label)} → <a href="#" data-sc="go" data-k="${esc(u.key)}">${esc(u.text)}</a></div>`).join('') || '<div class="sub">Nog nergens. Sleep een uitgang (rechts) naar Groter dan of Kleiner dan, bv. om iets te starten bij veel zon.</div>'}
    <h2>Stick</h2>
    <label>IP-adres</label><input type="text" maxlength="253" data-sofield="host" spellcheck="false" autocomplete="off" placeholder="192.168.1.50" value="${esc(b.host)}">
    <label>Serienummer van de stick</label><input type="text" maxlength="10" data-sofield="serial" spellcheck="false" autocomplete="off" placeholder="10 cijfers" value="${esc(b.serial)}">
    <div class="sub">Het serienummer van de <b>stick</b> (niet van de omvormer) staat op de sticker en op zijn statuspagina. Hieronder zoekt de add-on het zelf op.</div>
    <label>Gebruikersnaam</label><input type="text" maxlength="40" id="solu" autocomplete="off" value="admin">
    <label>Wachtwoord</label><input type="password" maxlength="40" id="solp" autocomplete="new-password" value="admin">
    <div class="sub">Alleen voor het opzoeken (standaard admin en admin); ze worden niet bewaard.</div>
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="solook" data-id="${esc(b.id)}" ${b.host ? '' : 'disabled'}>Serienummer opzoeken</button></div>
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delsolar" data-id="${esc(b.id)}">Omvormer verwijderen</button></div>`;
}

async function scSaveSolar(b, patch) {
  try {
    await api('PUT', `solar/${b.id}`, patch);
  } finally {
    SC.force = true;
    await refresh();
  }
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
  return scBlocks().filter((u) => (u.kind === 'lamp' && [u.disable, u.control].some((s) => s && scDisKey(s) === b.key)) || (['module', 'sprinkler', 'action', 'airco'].includes(u.kind) && u.ports.some((p) => p.ref === b.key)));
}

// ---------------------------------------------------------------- Tuya-datapunten: kiezen bij een module, een routine of een kaart

function scDevices(local) { // Tuya-toestellen hier en gekozen bij andere installaties; `local(id)` geeft de waarde voor een toestel van hier
  const here = scBlocks().filter((t) => t.kind === 'tuya').map((t) => ({ value: local(t.id), label: `${t.name}${t.online ? '' : ' (offline)'}`, points: t.points.filter((p) => !p.hide) }));
  const far = scBlocks().filter((t) => t.kind === 'remote' && t.sub === 'tuya').map((t) => ({ value: t.key, label: `${t.name} (${t.peerName})${t.online ? '' : ' (offline)'}`, points: t.points }));
  return { here, far, all: [...here, ...far] };
}

function scDeviceSelect(attrs, cur, local, empty = 'kies een toestel…') {
  const d = scDevices(local), opt = (x) => `<option value="${esc(x.value)}" ${x.value === cur ? 'selected' : ''}>${esc(x.label)}</option>`;
  const gone = cur && !d.all.some((x) => x.value === cur) ? `<option value="${esc(cur)}" selected>${esc(cur)} (weg of niet gekozen)</option>` : '';
  return `<select ${attrs}><option value="">${esc(empty)}</option>${gone}${d.here.map(opt).join('')}${d.far.length ? `<optgroup label="Van andere installaties">${d.far.map(opt).join('')}</optgroup>` : ''}</select>`;
}

function scPointSelect(attrs, dev, cur, local, keep = () => true) {
  const d = scDevices(local).all.find((x) => x.value === dev);
  const pts = d ? d.points.filter(keep) : [];
  const gone = cur && !pts.some((p) => String(p.dp) === String(cur)) ? `<option value="${esc(cur)}" selected>datapunt ${esc(cur)}</option>` : '';
  return `<select ${attrs} ${dev ? '' : 'disabled'}><option value="">kies een datapunt…</option>${gone}${pts.map((p) => `<option value="${esc(p.dp)}" ${String(p.dp) === String(cur) ? 'selected' : ''}>${esc(p.name)} (${esc(scTyValue(p))})</option>`).join('')}</select>`;
}

const scPointOf = (dev, dp, local) => { const d = scDevices(local).all.find((x) => x.value === dev); return d && d.points.find((p) => String(p.dp) === String(dp)); };
const SC_HERE = (id) => id; // een module of routine: een toestel van hier is zijn kale id
const SC_HERE_Y = (id) => `y:${id}`; // een kaart: y:<id>

function scValueInput(attrs, p, cur) { // de waarde die een routine zet: een keuzelijst als het datapunt opties heeft
  if (p && p.type === 'bool') return `<select ${attrs}><option value="">niets</option>${['ON', 'OFF'].map((v) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${v === 'ON' ? 'aan' : 'uit'}</option>`).join('')}</select>`;
  if (p && p.type === 'enum') return `<select ${attrs}><option value="">niets</option>${p.options.map((o) => `<option value="${esc(o)}" ${o === cur ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
  return `<input type="text" ${attrs} maxlength="40" placeholder="leeg = niets" value="${esc(cur)}"${p && p.min != null ? ` title="van ${p.min} tot ${p.max}"` : ''}>`;
}

function scChargerWhen(b) { // welke statuswaarden Start en welke Stop geven (start_when / stop_when van de kaart)
  const ref = (b.ins.find((x) => x.name === 'status') || {}).ref;
  if (!ref) return '<div class="sub">Sluit eerst de ingang <b>Status</b> aan. Zonder status toont de knop Stop zolang het datapunt op de startwaarde staat, anders Start.</div>';
  const m = /^v:(\d+)@(.+)$/.exec(ref), p = m && scPointOf(m[2], m[1], SC_HERE);
  const start = b.start_when || [], stop = b.stop_when || [], low = (xs) => xs.map((x) => x.toLowerCase());
  const now = p && p.value != null && p.value !== '' ? `<div class="sub">De status is nu <code>${esc(p.value)}</code>.</div>` : '';
  const help = '<div class="sub">Staat de status op een waarde bij <b>Start</b>, dan start de knop; bij <b>Stop</b> stopt hij; bij een andere waarde toont het paneel alleen de status, zonder knop. Laat je alles leeg, dan toont de knop Stop zolang het datapunt op de startwaarde staat (of de status zegt dat hij laadt), anders Start.</div>';
  if (p && p.type === 'enum' && p.options && p.options.length) {
    const extra = [...start, ...stop].filter((x) => !low(p.options).includes(x.toLowerCase())); // een waarde die het toestel niet (meer) meldt
    const rows = [...p.options, ...extra].map((o) => {
      const cur = low(stop).includes(o.toLowerCase()) ? 'stop' : low(start).includes(o.toLowerCase()) ? 'start' : '';
      return `<div class="row sp"><code>${esc(o)}</code><select data-cdstat="${esc(o)}">${[['', 'niets'], ['start', 'Start'], ['stop', 'Stop']].map(([v, l]) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${l}</option>`).join('')}</select></div>`;
    }).join('');
    return rows + now + help;
  }
  return `<label>Start bij (waarden met komma's)</label><input type="text" spellcheck="false" data-cdwhen="start_when" placeholder="bv. connect, finish" value="${esc(start.join(', '))}">
    <label>Stop bij (waarden met komma's)</label><input type="text" spellcheck="false" data-cdwhen="stop_when" placeholder="bv. charing" value="${esc(stop.join(', '))}">${now}${help}`;
}

function scAction(b) {
  const y0 = rowY(b, 0), p = b.ports[0], src = scSrc(p.ref), st = b.status;
  return `<g class="bk act t-spr ${SC.sel === b.key ? 'sel' : ''}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 22))}</text>
    <text class="bkbadge ${b.enabled ? 'ok' : ''}" x="${b.x + SW_W - 12}" y="${b.y + 18}" text-anchor="end">${b.enabled ? 'actief' : 'uit'}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">Tuya instellen${st.ok ? '' : ' · mislukt'}</text>
    <circle class="pt v-bool ${src ? '' : 'free'} ${st.input === true ? 'on' : ''}" cx="${b.x}" cy="${y0}" r="6" data-b="${esc(b.key)}" data-role="min" data-i="0"><title>Ingang: een 0 of 1 van een timer of module</title></circle>
    ${src ? `<text class="bkt away" x="${b.x + 11}" y="${y0 + 4}" data-sc="go" data-k="${esc(src.key)}">${esc(scCut(src.name, 16))} (${src.out})<title>${esc(src.name)} geeft nu ${src.out}</title></text>` : `<text class="ptl" x="${b.x + 11}" y="${y0 + 4}">+ ingang</text>`}
    <text class="bkt" x="${b.x + 11}" y="${rowY(b, 1) + 4}">→ ${esc(scCut(b.target, 30))}<title>1: ${esc(b.on_value || 'niets')} · 0: ${esc(b.off_value || 'niets')}</title></text></g>`;
}

function scActionPanel(b, props) {
  scLoadEnts();
  const p = b.ports[0], st = b.status;
  const sources = scBlocks().filter((s) => s.kind === 'timer' || (s.kind === 'module' && s.out === 'bool'));
  const pt = scPointOf(b.device, b.dp, SC_HERE);
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${st.ok ? '' : 'bad'}">${esc(st.text)}</span></div>
    <div class="sub">Een 0/1 zet een datapunt van een Tuya-toestel, hier of bij een andere installatie. Bv. de laadpaal starten om 23:00, of de laadstroom omlaag als het verbruik hoog is.</div>${props}
    <label style="margin:6px 0;color:var(--ink);cursor:pointer"><input type="checkbox" data-acfield="enabled" ${b.enabled ? 'checked' : ''}> ${b.enabled ? 'Actief: zet het datapunt als de ingang verandert' : 'Uit: zet niets'}</label>
    <label>Ingang</label><select data-acfield="trigger"><option value="">niet aangesloten</option>${sources.map((s) => `<option value="${esc(s.key)}" ${s.key === p.ref ? 'selected' : ''}>${esc(s.name)} (nu ${scOut(s)})</option>`).join('')}</select>
    <label>Toestel</label>${scDeviceSelect('data-acfield="device"', b.device, SC_HERE)}
    <label>Datapunt</label>${scPointSelect('data-acfield="dp"', b.device, b.dp, SC_HERE, (x) => x.rw)}
    <label>Bij 1 zetten op</label>${scValueInput('data-acfield="on_value"', pt, b.on_value)}
    <label>Bij 0 zetten op</label>${scValueInput('data-acfield="off_value"', pt, b.off_value)}
    <label>Hoogstens elke (seconden)</label><input type="number" min="10" max="86400" step="1" data-acfield="interval" value="${b.interval}">
    <div class="sub">Voorzichtig met een lader: de routine doet nooit iets bij het opstarten of als de ingang weer bekend wordt; ze onthoudt die eerste stand en zet pas iets als de ingang daarna verandert. Een onbekende ingang doet niets. Een mislukt bevel wordt na de wachttijd opnieuw geprobeerd.</div>
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delaction" data-id="${esc(b.id)}">Routine verwijderen</button></div>`;
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
  dp: 'De waarde van een datapunt van een Tuya-toestel (hier of bij een andere installatie) als getal; aan/uit is 1/0. Onbekend als het toestel offline is.',
  above: 'Geeft 1 als de waarde groter is dan de grens, bv. vermogen > 3.5 kW. Onbekend als de waarde onbekend is.',
  below: 'Geeft 1 als de waarde kleiner is dan de grens.',
};

function scModule(b) {
  const y0 = rowY(b, 0), users = scUsers(b);
  const rows = b.ports.map((p, i) => {
    const y = rowY(b, i), src = scSrc(p.ref);
    return `<circle class="pt v-${p.type} ${src ? '' : 'free'}" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="min" data-i="${i}"><title>Ingang ${esc(p.label)}: ${p.type === 'time' ? 'een tijd' : 'een 0 of 1'}</title></circle>
      ${src ? `<text class="bkt away" x="${b.x + 11}" y="${y + 4}" data-sc="go" data-k="${esc(src.key)}">${esc(p.label)}: ${esc(scCut(src.name, 14))} (${src.out})<title>${esc(src.name)} geeft nu ${src.out}</title></text>`
    : `<text class="ptl" x="${b.x + 11}" y="${y + 4}">+ ${esc(p.label)}</text>`}`;
  }).join('');
  return `<g class="bk mod t-${scType(b)} ${SC.sel === b.key ? 'sel' : ''}" data-block="${esc(b.key)}">
    <rect class="bkbox" x="${b.x}" y="${b.y}" width="${SW_W}" height="${scH(b)}" rx="8"/>
    <rect class="bkhead" x="${b.x}" y="${b.y}" width="${SW_W}" height="${SC_HEAD}" rx="8"/>
    <text class="bkn" x="${b.x + 12}" y="${b.y + 18}">${esc(scCut(b.name, 28))}</text>
    <text class="bks" x="${b.x + 12}" y="${b.y + 34}">${esc(scCut(b.detail ? `${b.tech} · ${b.detail}` : b.tech, 40))}</text>${rows}
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
    const y = rowY(b, i), src = scSrc(p.ref);
    return `<circle class="pt v-${p.type} ${src ? '' : 'free'}" cx="${b.x}" cy="${y}" r="6" data-b="${esc(b.key)}" data-role="min" data-i="${i}"><title>Ingang ${esc(p.label)}</title></circle>
      ${src ? `<text class="bkt away" x="${b.x + 11}" y="${y + 4}" data-sc="go" data-k="${esc(src.key)}">${esc(scCut(p.label, 6))}: ${esc(scCut(src.name, 9))} (${src.out})<title>${esc(p.label)}: ${esc(src.name)} geeft nu ${src.out}</title></text>`
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

function scCardList(b) { // de kaarten van een paneel of een scherm in volgorde, en een keuzelijst om er een bij te hangen
  const have = new Set(b.cards.map((c) => c.id)), screen = b.kind === 'screen';
  const free = scBlocks().filter((c) => c.kind === 'card' && !have.has(c.id) && !(c.type === 'saver' && (screen || b.cards.some((x) => x.type === 'saver'))));
  const page = (c) => c && c.type !== 'saver';
  const btn = (n, op, label, title) => `<button class="btn small ${op === 'del' ? 'danger' : ''}" type="button" data-sc="pncard" data-k="${esc(b.key)}" data-n="${n}" data-op="${op}" title="${title}">${label}</button>`;
  const list = b.cards.map((c, n) => `<div class="scc"><a href="#" data-sc="go" data-k="${esc(c.key)}">${c.type === 'saver' ? 'Startscherm:' : `${scPageNo(b, n)}.`} ${esc(c.name)}</a><span class="row">
      ${page(c) && page(b.cards[n - 1]) ? btn(n, 'up', '↑', 'Naar voren') : ''}${page(c) && page(b.cards[n + 1]) ? btn(n, 'down', '↓', 'Naar achter') : ''}
      ${btn(n, 'del', '✕', `Van dit ${screen ? 'scherm' : 'paneel'} halen (de kaart blijft bestaan)`)}</span></div>`).join('');
  return `<h2>Kaarten</h2>${list || '<div class="sub">Nog geen kaarten.</div>'}
    ${free.length ? `<select data-pnadd="${esc(b.key)}"><option value="">+ kaart toevoegen…</option>${free.map((c) => `<option value="${esc(c.id)}">${esc(c.name)} (${esc(SC_CARD_NL[c.type])})</option>`).join('')}</select>` : ''}
    ${b.error ? `<div class="sub" style="color:var(--warn)">${esc(b.error)}</div>` : ''}`;
}

const scSaveCards = (b, cards) => (b.kind === 'screen' ? scSaveScreen(b, { cards }) : scSavePanel(b.id, { cards }));

function scPanelPanel(b, props) {
  const draft = SC.panelDraft[b.id] || {};
  const hub = location.hostname;
  const relays = b.gangs.map((g) => `<button class="btn small" type="button" data-sc="pnrelay" data-id="${esc(b.id)}" data-n="${g.i + 1}" data-on="${g.on ? 0 : 1}" ${g.on === null ? 'disabled' : ''}>${esc(g.label)}: ${g.on === null ? '?' : g.on ? 'aan' : 'uit'}</button>`).join('');
  const num = (f, lo, hi, label) => `<label>${label}</label><input type="number" min="${lo}" max="${hi}" step="1" data-pnset="${f}" data-id="${esc(b.id)}" value="${esc(b[f])}">`;
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.online ? 'ok' : ''}">${esc(scPanelState(b))}</span>${b.rssi != null ? `<span class="sub">${b.rssi} dBm</span>` : ''}</div>
    ${props}
    <label>IP-adres van de panel</label><input type="text" data-pnfield="host" data-id="${esc(b.id)}" placeholder="192.168.0.105" value="${esc(draft.host ?? b.host)}">
    <div class="row" style="margin-top:6px"><button class="btn small primary" type="button" data-sc="pnsave" data-id="${esc(b.id)}">Opslaan</button>${Object.keys(draft).length ? '<span class="sub">niet opgeslagen</span>' : ''}</div>
    ${b.notes.length ? `<h2>Omgezet uit de YAML</h2><ul class="sub" style="padding-left:18px;margin:4px 0">${b.notes.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      <div class="row"><button class="btn small" type="button" data-sc="pnnotes" data-id="${esc(b.id)}">Gelezen</button></div>` : ''}
    ${scCardList(b)}
    <div class="sub">Of sleep de uitgang van een kaart naar <b>+ kaart</b> op het blok. Een screensaver is het startscherm; de andere kaarten zijn de pagina's, in deze volgorde. Een kaart aan een knop van een rasterkaart wordt daar een subpagina. Nieuwe kaarten: <b>+ Module → Kaarten</b>.</div>
    <h2>Scherm</h2>
    <label>Taal</label><select data-pnset="locale" data-id="${esc(b.id)}"><option value="nl" ${b.locale === 'nl' ? 'selected' : ''}>Nederlands</option><option value="en" ${b.locale === 'en' ? 'selected' : ''}>Engels</option></select>
    ${num('timeout', 0, 3600, 'Terug naar de screensaver na (seconden, 0 = nooit)')}${num('bright', 0, 100, 'Helderheid (0–100)')}${num('dim', 0, 100, 'Helderheid van de screensaver (0–100)')}
    <details><summary class="sub">YAML van nspanel-lovelace-ui omzetten</summary>
      <textarea id="pnyaml" rows="8" style="flex:none;min-height:150px;width:100%" spellcheck="false" placeholder="cards:&#10;  - type: cardGrid&#10;    title: Licht&#10;    entities: ..."></textarea>
      <div class="row"><button class="btn small" type="button" data-sc="pnyaml" data-id="${esc(b.id)}">Omzetten naar kaarten</button></div>
      <div class="sub">Maakt kaarten en hangt ze achteraan aan dit paneel. De YAML zelf wordt niet bewaard.</div></details>
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

const SC_CARD_HELP = {
  grid: 'Tot 8 knoppen (een lijst: 4). Een knop bedient een lamp, een entiteit uit HA, of opent een subpagina.',
  charger: 'Start/stop, de status, de laadtijd, de geladen kWh en de prijs. Sluit links waarden van een Tuya-blok aan en rechts start/stop op een instelling, van welke lader ook.',
  saver: 'Het startscherm: klok, datum, het weer met 4 dagen voorspelling en een temperatuur binnen.',
  power: 'Het energieschema van nspanel-lovelace-ui: tot 6 rijen, elk een sensor uit HA.',
  qr: 'Een QR-code waarmee een gast op de wifi komt, met de naam en het wachtwoord ernaast.',
  airco: 'De thermostaatpagina van het paneel voor een airco: de temperatuur in de kamer en de ingestelde, een schuif om hem in te stellen en een knop per stand (uit, auto, koelen, drogen, ventileren, verwarmen).',
};

function scCardPanel(b, props) {
  const tx = (f, label, ph = '') => `<label>${label}</label><input type="text" maxlength="255" spellcheck="false" autocomplete="off" data-cdfield="${f}" placeholder="${esc(ph)}" value="${esc(b[f] ?? '')}">`;
  const opt = (v, cur, t) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(t)}</option>`;
  const users = b.users.map((u) => `<div class="sub"><a href="#" data-sc="go" data-k="${esc(u.key)}">${esc(u.name)}</a> · ${u.as === 'page' ? (b.type === 'saver' ? 'startscherm' : `pagina ${u.i + 1}`) : `subpagina via knop ${u.i + 1}`}</div>`).join('');
  let body = '';
  if (b.type === 'grid') {
    scLoadEnts();
    const max = b.layout === 'list' ? 4 : 8;
    const items = b.items.map((it, i) => {
      const go = it.target && scBlock(it.target);
      const what = it.dp ? (go ? `waarde <a href="#" data-sc="go" data-k="${esc(go.key)}">${esc(it.label)}</a>` : esc(it.label))
        : it.ref ? (go ? `<a href="#" data-sc="go" data-k="${esc(go.key)}">${esc(it.ref.startsWith('c:') ? `subpagina ${go.name}` : `lamp ${go.name}`)}</a>` : esc(it.label)) : it.entity ? `<code>${esc(it.entity)}</code>` : 'leeg';
      return `<div class="tmd"><div class="row sp"><b>Knop ${i + 1}</b><span class="row">
          ${i ? `<button class="btn small" type="button" data-sc="cditem" data-k="${esc(b.key)}" data-i="${i}" data-op="up" title="Naar voren">↑</button>` : ''}
          ${i < b.items.length - 1 ? `<button class="btn small" type="button" data-sc="cditem" data-k="${esc(b.key)}" data-i="${i}" data-op="down" title="Naar achter">↓</button>` : ''}
          <button class="btn small danger" type="button" data-sc="cditem" data-k="${esc(b.key)}" data-i="${i}" data-op="del" title="Knop weghalen">✕</button></span></div>
        <div class="sub" style="overflow-wrap:anywhere">${what}</div>
        <label>Naam op het scherm</label><input type="text" maxlength="60" data-cditem="name" data-i="${i}" placeholder="${esc(it.dp ? 'naam van het datapunt' : it.ref || it.entity ? 'naam van de lamp of entiteit' : '')}" value="${esc(it.name)}">
        <label>Pictogram</label><input type="text" maxlength="64" spellcheck="false" data-cditem="icon" data-i="${i}" placeholder="${it.dp ? 'gauge' : it.ref && !it.ref.startsWith('c:') ? 'lightbulb' : 'standaard'}" value="${esc(it.icon)}">
        <label>Of een entiteit uit Home Assistant</label>${scEntSelect(`data-cditem="entity" data-i="${i}"`, it.entity, it.ref ? 'zoals hierboven' : 'niets')}
        <label>Of een waarde van een Tuya-toestel</label>${scDeviceSelect(`data-cditem="dpdev" data-i="${i}"`, it.dp ? it.ref : '', SC_HERE_Y, 'geen')}
        ${it.dp ? scPointSelect(`data-cditem="dp" data-i="${i}"`, it.ref, it.dp, SC_HERE_Y) : ''}</div>`;
    }).join('');
    body = `<label>Weergave</label><select data-cdfield="layout">${opt('grid', b.layout, 'Raster (tot 8 knoppen)')}${opt('list', b.layout, 'Lijst (tot 4 rijen)')}</select>
      <h2>Knoppen</h2>${items || '<div class="sub">Nog geen knoppen.</div>'}
      ${b.items.length < max ? `<div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="cditem" data-k="${esc(b.key)}" data-op="add">+ Knop</button></div>` : ''}
      <div class="sub">Een waarde (bv. het vermogen van een stroommeter) toont de lijstweergave het best: het raster toont alleen het pictogram en de naam.</div>
      <div class="sub">Sleep naar een knop (links op het blok) vanaf een <b>lamp</b>, een <b>gang</b> van een schakelaar, of de <b>uitgang</b> van een andere kaart (dan wordt het een subpagina). Pictogrammen: de MDI-naam, bv. <code>ceiling-light</code>; wat het scherm niet kent wordt een uitroepteken.</div>`;
  } else if (b.type === 'charger') {
    const pt = scPointOf(b.target.device, b.target.dp, SC_HERE);
    body = `<h2>Ingangen</h2>${b.ins.map((x) => `<div class="scc"><div><b>${x.label}</b><div class="sub">${x.ref ? esc(x.src || x.ref) : 'niet aangesloten: sleep een waarde (rechts op een Tuya-blok) naar de poort links'}</div></div>
        ${x.ref ? `<button class="btn small danger" type="button" data-sc="cdinclear" data-k="${esc(b.key)}" data-name="${x.name}" title="Loskoppelen">✕</button>` : ''}</div>`).join('')}
      <h2>Start/stop</h2>
      <label>Toestel</label>${scDeviceSelect('data-cdtarget="device"', b.target.device, SC_HERE)}
      <label>Datapunt</label>${scPointSelect('data-cdtarget="dp"', b.target.device, b.target.dp, SC_HERE, (x) => x.rw)}
      <label>Waarde om te starten</label>${scValueInput('data-cdtarget="on"', pt, b.target.on)}
      <label>Waarde om te stoppen</label>${scValueInput('data-cdtarget="off"', pt, b.target.off)}
      <div class="sub">Of sleep de uitgang <b>start/stop</b> (rechts op het blok) naar een instelling van een Tuya-blok, hier of van een andere installatie.</div>
      <h2>Start of stop volgens de status</h2>${scChargerWhen(b)}
      <h2>Prijs</h2><label>Prijs per kWh (€)</label><input type="number" min="0" max="100" step="0.01" data-cdfield="price" value="${esc(b.price)}">
      ${tx('price_entity', 'Of de prijs uit een entiteit van HA (wint als ze een getal geeft)', 'bv. sensor.stroomprijs')}
      <div class="sub">Prijs = Geladen × de prijs per kWh.</div>`;
  } else if (b.type === 'airco') {
    const aircos = scBlocks().filter((x) => x.kind === 'airco');
    body = `<label>Airco</label><select data-cdfield="airco">${opt('', b.airco, aircos.length ? 'kies een airco' : 'nog geen airco: zet er een op het schema')}${aircos.map((x) => opt(x.id, b.airco, x.name)).join('')}</select>
      ${b.airco && !b.device ? '<div class="sub">Deze airco bestaat niet meer: kies een andere.</div>' : ''}
      <div class="sub">Het paneel toont de temperatuur in de kamer en de ingestelde; met de schuif en de knoppen stel je hem in. Alleen op een paneel, niet op een scherm met openHASP.</div>`;
  } else if (b.type === 'saver') {
    body = `${tx('weather', 'Weer', 'weather.forecast_thuis')}${tx('forecast', 'Voorspelling uit (leeg: uit het weer)', 'sensor.weather_forecast_daily')}${tx('temperature', 'Temperatuur binnen', 'sensor.temperatuur_hal')}
      ${tx('status1', 'Statuspictogram 1', 'bv. binary_sensor.voordeur')}${tx('status2', 'Statuspictogram 2', '')}`;
  } else if (b.type === 'power') {
    const rows = b.items.map((it, i) => `<div class="tmd"><div class="row sp"><b>Rij ${i + 1}</b><button class="btn small danger" type="button" data-sc="cdpow" data-k="${esc(b.key)}" data-i="${i}" data-op="del" title="Rij weghalen">✕</button></div>
        <label>Sensor uit Home Assistant (leeg: een open plek)</label><input type="text" spellcheck="false" data-cdpow="entity" data-i="${i}" placeholder="sensor.verbruik" value="${esc(it.entity || '')}">
        <label>Of een Tuya-datapunt</label>${scDeviceSelect(`data-cdpow="tuya" data-i="${i}"`, it.tuya, SC_HERE_Y, 'geen')}
        ${it.tuya ? scPointSelect(`data-cdpow="dp" data-i="${i}"`, it.tuya, it.dp, SC_HERE_Y) : ''}
        <label>Naam</label><input type="text" maxlength="60" data-cdpow="name" data-i="${i}" value="${esc(it.name)}">
        <label>Pictogram</label><input type="text" maxlength="64" data-cdpow="icon" data-i="${i}" placeholder="flash" value="${esc(it.icon)}">
        <label>Snelheid van de pijl (−120 tot 120)</label><input type="number" min="-120" max="120" step="1" data-cdpow="speed" data-i="${i}" value="${it.speed}">
        <label>Kleur: groen onder, oranje onder (leeg: geen kleur)</label><div class="row"><input type="number" step="any" data-cdpow="lo" data-i="${i}" value="${it.levels ? it.levels[0] : ''}"><input type="number" step="any" data-cdpow="hi" data-i="${i}" value="${it.levels ? it.levels[1] : ''}"></div></div>`).join('');
    body = `${rows}${b.items.length < 6 ? `<div class="row" style="margin-top:6px"><button class="btn small" type="button" data-sc="cdpow" data-k="${esc(b.key)}" data-op="add">+ Rij</button></div>` : ''}`;
  } else {
    body = `${tx('ssid', 'Netwerknaam (SSID)')}<label>Beveiliging</label><select data-cdfield="security">${opt('WPA', b.security, 'WPA/WPA2')}${opt('WEP', b.security, 'WEP')}${opt('nopass', b.security, 'Open')}</select>
      ${b.security === 'nopass' ? '' : tx('password', 'Wachtwoord')}<div class="sub">Het wachtwoord staat leesbaar op het scherm, net als in de QR-code.</div>`;
  }
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge">Kaart · ${esc(SC_CARD_NL[b.type])}</span></div><div class="sub">${esc(SC_CARD_HELP[b.type])}</div>
    ${props}<h2>Hangt aan</h2>${users || '<div class="sub">Nog nergens: sleep de uitgang (rechts) naar <b>+ kaart</b> op een paneel of scherm.</div>'}
    ${body}
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delcard" data-id="${esc(b.id)}">Kaart verwijderen</button></div>`;
}

const scItems = (b) => b.items.map(({ ref, entity, name, icon, dp }) => ({ ref, entity, name, icon, dp: dp || '' }));
const scPowItems = (b) => b.items.map(({ entity, tuya, dp, name, icon, speed, levels }) => ({ entity, tuya: tuya || '', dp: dp || '', name, icon, speed, levels }));

async function scRemoteCmd(b, cmd) { // de eigenaar voert uit en antwoordt; zijn nieuwe stand komt met de volgende melding
  if (!b.linked) throw new Error(`${b.peerName} is niet verbonden.`);
  try {
    await api('POST', 'link/cmd', { peer: b.peer, obj: b.obj, ...cmd });
  } finally {
    SC.force = true;
    await refresh();
  }
}

async function scSaveCard(b, patch) {
  try {
    await api('PUT', `cards/${b.id}`, patch);
  } finally {
    SC.force = true;
    await refresh();
  }
}

async function scSavePanel(id, patch) {
  try {
    await api('PUT', `panels/${id}`, patch);
  } finally {
    SC.force = true;
    await refresh();
  }
}

function scScreenState(b) {
  if (!b.plate) return 'nog niet gekoppeld';
  if (!b.online) return 'offline';
  return { query: 'online, controleert ontwerp', push: 'online, stuurt ontwerp', ready: 'online' }[b.phase] || 'online';
}

function scScreenPanel(b, props) {
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge ${b.online ? 'ok' : ''}">${esc(scScreenState(b))}</span>${b.uptime ? `<span class="sub">aan sinds ${esc(b.uptime)}</span>` : ''}</div>
    ${props}
    <label>Naam op de firmware</label><input type="text" maxlength="32" data-hsfield="plate" list="hsplates" spellcheck="false" autocomplete="off" placeholder="bv. scherm01" value="${esc(b.plate)}">
    <datalist id="hsplates">${b.plates.map((p) => `<option value="${esc(p)}"></option>`).join('')}</datalist>
    <div class="sub">Een scherm heeft één eigenaar. Haal het eerst uit de pyscript-config, anders sturen beide tegelijk.</div>
    ${b.missing.length ? `<div class="sub"><b>Niet in Home Assistant:</b> ${b.missing.map((e) => `<code>${esc(e)}</code>`).join(', ')}. Deze knoppen blijven grijs.</div>` : ''}
    ${scCardList(b)}
    <div class="sub">Of sleep de uitgang van een kaart naar <b>+ kaart</b> op het blok. Dezelfde kaarten als op een NSPanel: elke kaart is een pagina, in deze volgorde, en een kaart met meer dan 4 knoppen loopt door op een volgende pagina. Een kaart aan een knop van een rasterkaart wordt daar een subpagina met een knop terug. Een screensaver kan hier niet. Nieuwe kaarten: <b>+ Module → Kaarten</b>.</div>
    <h2>Scherm</h2>
    <label>Taal</label><select data-hsfield="locale"><option value="nl" ${b.locale === 'nl' ? 'selected' : ''}>Nederlands</option><option value="en" ${b.locale === 'en' ? 'selected' : ''}>Engels</option></select>
    <label>Scherm donker na (seconden, 0 = nooit)</label><input type="number" min="0" max="3600" step="1" data-hsfield="timeout" value="${b.timeout}">
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delscreen" data-id="${esc(b.id)}">Scherm verwijderen</button></div>`;
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
    ${b.unclaimed.length ? `<label>Op het netwerk gevonden</label><select data-tyfound="1"><option value="">${b.device_id ? 'ander toestel kiezen…' : 'kies een toestel…'}</option>${b.unclaimed.map((f) => `<option value="${esc(f.id)}|${esc(f.ip)}">${esc(f.ip)} · ${esc(f.id)}${f.version ? ` · ${esc(f.version)}` : ''}${f.live ? '' : ` · laatst gezien ${esc(new Date(f.seen * 1000).toLocaleDateString('nl-BE'))}`}</option>`).join('')}</select>` : ''}
    <label>Device-id</label><input type="text" maxlength="40" data-tyfield="device_id" spellcheck="false" autocomplete="off" value="${esc(b.device_id)}">
    <label>IP-adres</label><input type="text" maxlength="253" data-tyfield="host" spellcheck="false" autocomplete="off" placeholder="${b.device_id ? 'wordt ingevuld zodra het toestel zich meldt' : '192.168.8.26'}" value="${esc(b.host)}">
    ${b.found && b.found.ip !== b.host ? `<div class="sub">Het toestel meldt zich op ${esc(b.found.ip)}.</div>` : ''}
    ${b.device_id && !b.host && !b.found ? `<div class="sub">${b.listening.length
    ? `Nog niet gehoord op het netwerk. De add-on luistert op UDP ${b.listening.join(', ')} en vraagt elke minuut wie er is; ${b.heard} toestel${b.heard === 1 ? '' : 'len'} bekend. Een toestel dat al verbonden is met LocalTuya of een andere koppeling meldt zich vaak niet: koppel het daar los, of vul het IP zelf in (in de router bij de DHCP-leases).`
    : 'De add-on kan niet luisteren op UDP 6666, 6667 en 7000 (zie het logboek). Vul het IP zelf in.'}</div>` : ''}
    <div class="sub">Device-id: in de SmartLife-app → toestel → ✎ → <i>Apparaatinformatie</i> (<i>Virtuele ID</i>), of in het Tuya IoT Platform onder <i>Devices</i>.</div>
    <label>Local key</label><input type="password" maxlength="16" data-tyfield="key" autocomplete="new-password" placeholder="${b.has_key ? 'ingesteld, leeg laten om te behouden' : '16 tekens, of haal hem op uit de cloud'}">
    <label>Protocol</label><select data-tyfield="version">${opt('auto', b.version, 'automatisch zoeken')}${['3.5', '3.4', '3.3'].map((v) => opt(v, b.version, v)).join('')}</select>
    <div class="row"><span class="badge ${b.has_model ? 'ok' : ''}">${b.product ? esc(b.product) : b.has_model ? 'datamodel aanwezig' : 'geen datamodel: alles alleen-lezen'}</span></div>
    <h2>Tuya-cloud (eenmalig)</h2>
    <div class="sub">Haalt de local key en het datamodel op; daarna werkt het toestel zonder internet. Geldt voor alle Tuya-toestellen. Uit je cloudproject op iot.tuya.com → <i>Overview</i>.</div>
    <label>Access ID</label><input type="text" maxlength="64" data-tycloud="id" spellcheck="false" autocomplete="off" value="${esc(b.cloud.id)}">
    <label>Access Secret</label><input type="password" maxlength="64" data-tycloud="secret" autocomplete="new-password" placeholder="${b.cloud.has_secret ? 'ingesteld, leeg laten om te behouden' : ''}">
    <label>Datacenter</label><select data-tycloud="region">${[['eu', 'Centraal-Europa'], ['weu', 'West-Europa'], ['us', 'West-Amerika'], ['eus', 'Oost-Amerika'], ['cn', 'China'], ['in', 'India']].map(([v, t]) => opt(v, b.cloud.region, t)).join('')}</select>
    <div class="row" style="margin-top:6px"><button class="btn small primary" type="button" data-sc="tyfetch" data-id="${esc(b.id)}" ${b.device_id && b.cloud.id && b.cloud.has_secret ? '' : 'disabled'}>Ophalen uit de cloud</button></div>
    <details><summary class="sub">Of plak het datamodel zelf</summary>
    <textarea rows="8" style="flex:none;min-height:150px;width:100%" spellcheck="false" data-tyfield="model" placeholder="Antwoord van Query Things Data Model uit de Tuya API Explorer"></textarea></details>
    <div class="sub">Veel toestellen laten maar één verbinding toe: haal het toestel eerst uit LocalTuya en sluit de SmartLife-app.</div>
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
    return `<select data-spinput="${esc(p.name)}"><option value="">${p.name === 'block' ? 'niet aangesloten' : 'waarde hierboven gebruiken'}</option>${p.ref && p.ref.startsWith('v:') ? `<option value="${esc(p.ref)}" selected>${esc(scSrc(p.ref).name)}</option>` : ''}${srcs.map((s) => `<option value="${esc(s.key)}" ${s.key === p.ref ? 'selected' : ''}>${esc(s.name)} (nu ${scOut(s)})</option>`).join('')}</select>`;
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
  const ports = b.ports.map((p) => `<label>${esc(p.label)}</label><select data-mport="${esc(p.name)}" data-k="${esc(b.key)}"><option value="">niet aangesloten</option>${p.ref && p.ref.startsWith('v:') ? `<option value="${esc(p.ref)}" selected>${esc(scSrc(p.ref).name)} (nu ${esc(scSrc(p.ref).out)})</option>` : ''}${sources(p).map((s) => `<option value="${esc(s.key)}" ${s.key === p.ref ? 'selected' : ''}>${esc(s.name)} (nu ${scOut(s)})</option>`).join('')}${scAcOuts(p.type).map((o) => `<option value="${esc(o.key)}" ${o.key === p.ref ? 'selected' : ''}>${esc(o.name)} (nu ${esc(o.out)})</option>`).join('')}</select>`).join('');
  const users = scUsers(b);
  const nosun = (b.type === 'sunrise' || b.type === 'sunset') && b.value === null ? '<div class="sub">Nog onbekend: de add-on heeft de locatie van Home Assistant nog niet, of de zon komt vandaag niet op of gaat niet onder.</div>' : '';
  return `<h3>${esc(b.name)}</h3><div class="row"><span class="badge">uitgang nu ${scOut(b)}</span><span class="sub">${esc(S.clock.time)} · ${esc(S.clock.tz)}</span></div>
    <div class="sub">${esc(SC_HELP[b.type])}</div>${nosun}${props}
    ${b.type === 'number' ? `<label for="scmnum">Waarde</label><input type="number" id="scmnum" min="0" max="100000" step="1" data-k="${esc(b.key)}" value="${esc(b.param)}">` : ''}
    ${b.type === 'time' ? `<label for="scmtime">Tijd</label><input type="time" id="scmtime" data-k="${esc(b.key)}" value="${esc(b.param)}">` : ''}
    ${b.type === 'above' || b.type === 'below' ? `<label>Grens</label><input type="number" step="any" data-mfield="value" data-k="${esc(b.key)}" value="${esc(b.param)}">` : ''}
    ${b.type === 'dp' ? `<label>Toestel</label>${scDeviceSelect(`data-mfield="device" data-k="${esc(b.key)}"`, b.device, SC_HERE)}<label>Datapunt</label>${scPointSelect(`data-mfield="dp" data-k="${esc(b.key)}"`, b.device, b.dp, SC_HERE)}` : ''}
    ${ports ? `<h2>Ingangen</h2>${ports}` : ''}
    <h2>Uitgang gaat naar</h2>${users.map((u) => `<div class="sub"><a href="#" data-sc="go" data-k="${esc(u.key)}">${esc(u.name)}</a></div>`).join('') || '<div class="sub">Nog nergens.</div>'}
    <div class="row" style="margin-top:6px"><button class="btn small danger" type="button" data-sc="delmodule" data-id="${esc(b.id)}">Module verwijderen</button></div>`;
}

// Het menu "+ Module": alles wat je op een pagina kunt zetten, per soort
const SC_MENU = [
  ['Toestellen', [['lamp', 'Lamp', 'met touches en relais'], ['switch', 'Schakelaar', 'een toestel uit Home Assistant'], ['panel', 'NSPanel', 'scherm met twee relais'], ['screen', 'Scherm', 'aanraakscherm met openHASP'], ['tuya', 'Tuya', 'toestel rechtstreeks over het LAN'], ['airco', 'Airco', 'Gree, Toyotomi, ... over het LAN'], ['solar', 'Omvormer', 'Deye, Sunsynk, ... met een Solarman-stick']]],
  ['Tijd', [['timer', 'Timer', 'intervallen per dag, geeft 0 of 1'], ['sunrise', 'Zonsopgang', 'tijd van vandaag'], ['sunset', 'Zonsondergang', 'tijd van vandaag'], ['time', 'Vaste tijd', 'bv. 02:30']]],
  ['Waarden', [['number', 'Getal', 'bv. aantal cycli'], ['dp', 'Tuya-waarde', 'een datapunt als getal']]],
  ['Rekenen', [['add', 'Optellen', 'tijd + tijd'], ['sub', 'Aftrekken', 'tijd − tijd']]],
  ['Vergelijken', [['between', 'Tussen', '1 tussen twee tijden'], ['after', 'Na', '1 na een tijd'], ['before', 'Voor', '1 voor een tijd'], ['above', 'Groter dan', '1 boven een grens'], ['below', 'Kleiner dan', '1 onder een grens']]],
  ['Logica', [['and', 'En', '1 als beide 1 zijn'], ['or', 'Of', '1 als een van beide 1 is']]],
  ['Routines', [['sprinkler', 'Sprinkler', 'zones na elkaar, in cycli'], ['action', 'Tuya instellen', 'een 0/1 zet een datapunt']]],
  ['Kaarten', [['card-grid', 'Knoppen', 'lampen, schakelaars, subpagina\'s'], ['card-charger', 'Laadpaal', 'start/stop, kWh en prijs'], ['card-saver', 'Screensaver', 'klok en weer'], ['card-airco', 'Airco', 'thermostaat voor het paneel'],
    ['card-power', 'Energie', 'verbruik en opbrengst'], ['card-qr', 'Wifi (QR)', 'gastnetwerk']]],
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
  const blocks = list.map((b) => (b.kind === 'lamp' ? scLamp(b) : b.kind === 'timer' ? scTimer(b) : b.kind === 'module' ? scModule(b) : b.kind === 'sprinkler' ? scSprinkler(b)
    : b.kind === 'tuya' ? scTuya(b, tags) : b.kind === 'airco' ? scAirco(b) : b.kind === 'solar' ? scSolar(b) : b.kind === 'card' ? scCard(b) : b.kind === 'action' ? scAction(b) : b.kind === 'remote' ? scRemote(b, tags) : b.kind === 'panel' || b.kind === 'screen' ? scPanelBlock(b, tags) : scSwitch(b, tags))).join('');
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
  if (b.kind === 'action') return [b.x, rowY(b, 0)];
  if (b.kind === 'airco' || b.kind === 'solar') return [p.role === 'acout' ? b.x + SC_AC_W : b.x, rowY(b, +p.i)];
  if (p.role === 'pval' || p.role === 'pset') return [p.role === 'pval' ? b.x + TUYA_W : b.x, rowY(b, +p.i)];
  if (b.kind === 'card') return p.role === 'cout' ? [b.x + SW_W, rowY(b, 0)] : p.role === 'cdout' ? [b.x + SW_W, rowY(b, 1)] : [b.x, rowY(b, p.i === 'new' ? b.items.length : +p.i)];
  if ((b.kind === 'panel' || b.kind === 'screen') && p.role === 'pcard') return [b.x, rowY(b, b.gangs.length + (p.i === 'new' ? b.cards.length : +p.i))];
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
  return part('Lampen', 'lamp') + part('Schakelaars', 'switch') + part('Timers', 'timer') + part('Tijd en logica', 'module') + part('Routines', 'sprinkler') + part('Tuya instellen', 'action') + part('Panelen', 'panel')
    + part('Kaarten', 'card') + part('Schermen', 'screen') + part('Tuya', 'tuya') + part('Airco', 'airco') + part('Omvormer', 'solar') + part('Andere installaties', 'remote');
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
  return b.kind === 'lamp' ? scLampPanel(b, props) : b.kind === 'timer' ? scTimerPanel(b, props) : b.kind === 'module' ? scModulePanel(b, props) : b.kind === 'sprinkler' ? scSprinklerPanel(b, props) : b.kind === 'panel' ? scPanelPanel(b, props) : b.kind === 'screen' ? scScreenPanel(b, props) : b.kind === 'tuya' ? scTuyaPanel(b, props) : b.kind === 'airco' ? scAircoPanel(b, props) : b.kind === 'solar' ? scSolarPanel(b, props) : b.kind === 'card' ? scCardPanel(b, props) : b.kind === 'action' ? scActionPanel(b, props) : b.kind === 'remote' ? scRemotePanel(b) : scSwitchPanel(b, props);
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
  const groups = scBlocks().filter((s) => s.kind === 'switch' || s.kind === 'tuya' || (s.kind === 'remote' && s.sub === 'gang' && b.source === 'ha')).map((s) => {
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
  const far = g.entity && g.entity.startsWith('r:'); // een gang van een andere installatie: alleen een lamp via HA (een kring)
  const lamps = scBlocks().filter((l) => l.kind === 'lamp' && (l.source !== 'ha' || g.entity) && (!far || l.source === 'ha'));
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
  const w = kind === 'lamp' ? LAMP_W : SW_W;
  const y = Math.max(30, ...list.filter((b) => b.x < col + w + 20 && b.x + scW(b) + 20 > col).map((b) => b.y + scH(b) + 24)); // alles wat de kolom raakt
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
  else if (to.kind === 'action' && sink.role === 'min') await api('PUT', `actions/${to.id}`, { inputs: { trigger: from.key } });
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
const SC_CARD_ROLES = ['cout', 'pcard', 'citem'];
const SC_HELD = 'Deze lamp wordt vastgehouden door zijn aan/uit-ingang.';

async function scSetItem(card, i, patch) { // één knop van een rasterkaart; 'new' zet er een achteraan
  const items = scItems(card);
  if (i === 'new' || i === undefined || +i >= items.length) items.push({ ref: null, entity: null, name: '', icon: '', ...patch });
  else items[+i] = { ...items[+i], ...patch };
  await api('PUT', `cards/${card.id}`, { items });
}

async function scWireCard(a, b) { // de uitgang van een kaart naar een paneel of een knop; of een knop naar een lamp of een gang
  const out = [a, b].find((p) => p.role === 'cout');
  let sel;
  if (out) {
    const sink = out === a ? b : a, card = scBlock(out.b), to = scBlock(sink.b);
    if (sink.role === 'pcard') {
      const list = to.cards.map((c) => c.id).filter((id) => id !== card.id);
      list.splice(sink.i === 'new' ? list.length : Math.min(+sink.i, list.length), 0, card.id);
      await api('PUT', `${to.kind === 'screen' ? 'screens' : 'panels'}/${to.id}`, { cards: list });
    } else if (sink.role === 'citem') {
      await scSetItem(to, sink.i, { ref: card.key, entity: null });
    } else throw new Error('Een kaart hang je aan een paneel of een scherm (links, + kaart) of aan een knop van een rasterkaart.');
    sel = to.key;
  } else {
    const item = [a, b].find((p) => p.role === 'citem');
    if (!item) throw new Error('Trek de draad vanaf de uitgang van een kaart, of vanaf een knop van een rasterkaart.');
    const other = item === a ? b : a, card = scBlock(item.b), src = scBlock(other.b);
    const gang = SC_GANGED.includes(src.kind) && other.g !== undefined ? src.gangs[+other.g] : null;
    if (src.kind === 'lamp' || (src.kind === 'remote' && src.sub === 'lamp')) await scSetItem(card, item.i, { ref: src.key, entity: null });
    else if (src.kind === 'remote') throw new Error('Een Tuya-toestel van een andere installatie kies je bij een laadpaalkaart, in de zijbalk.');
    else if (gang && gang.entity) await scSetItem(card, item.i, { ref: null, entity: gang.entity });
    else if (gang) throw new Error('Deze gang loopt niet via Home Assistant. Sluit de knop aan op de lamp die hij schakelt.');
    else throw new Error('Een knop sluit je aan op een lamp, op een gang van een schakelaar, of op de uitgang van een kaart.');
    sel = card.key;
  }
  SC.sel = sel;
  SC.force = true;
  await refresh();
}

async function scWireDp(a, b) { // een datapunt van een Tuya-blok: een waarde naar de logica of een kaart, of een 0/1 die het instelt
  const val = [a, b].find((p) => p.role === 'pval'), set = [a, b].find((p) => p.role === 'pset');
  if (val && set) throw new Error('Een waarde sluit je aan op Groter dan of Kleiner dan, niet rechtstreeks op een instelling.');
  if (val) {
    const sink = val === a ? b : a, dev = scDevOf(scBlock(val.b)), to = scBlock(sink.b);
    if (sink.role === 'min' && (to.kind === 'module' || to.kind === 'sprinkler')) {
      const port = to.ports[+sink.i];
      if (port.type !== 'number') throw new Error(port.type === 'bool' ? 'Deze ingang verwacht een 0 of 1. Zet er een Groter dan of Kleiner dan tussen.' : 'Deze ingang verwacht een tijd, geen waarde.');
      await api('PUT', `${to.kind === 'module' ? 'modules' : 'sprinklers'}/${to.id}`, { inputs: { [port.name]: `v:${val.dp}@${dev.id}` } });
    } else if (sink.role === 'citem') await scSetItem(to, sink.i, { ref: dev.card, dp: String(val.dp), entity: null });
    else if (sink.role === 'cdin') {
      const ins = Object.fromEntries(to.ins.filter((x) => x.ref).map((x) => [x.name, x.ref]));
      ins[to.ins[+sink.i].name] = `v:${val.dp}@${dev.id}`;
      await api('PUT', `cards/${to.id}`, { inputs: ins });
    } else throw new Error('Een waarde sluit je aan op de ingang van Groter dan, Kleiner dan, een sprinkler of een laadpaal, of op een knop van een kaart.');
    SC.sel = to.key;
  } else {
    const src = set === a ? b : a, from = scBlock(src.b), tb = scBlock(set.b), dev = scDevOf(tb);
    if (src.role === 'cdout') { // de knop van een laadpaal: start en stop zetten dit datapunt
      const pt = dev.points.find((x) => String(x.dp) === String(set.dp)), opts = (pt && pt.options) || [];
      const guess = opts.includes('OpenCharging') && opts.includes('CloseCharging') ? ['OpenCharging', 'CloseCharging'] : pt && pt.type === 'bool' ? ['ON', 'OFF'] : ['', ''];
      await api('PUT', `cards/${from.id}`, { target: { device: dev.id, dp: String(set.dp), on: guess[0], off: guess[1] } });
      SC.sel = from.key;
      if (!guess[0]) toast('Kies bij de kaart de waarde om te starten en om te stoppen.', 6000);
      SC.force = true;
      await refresh();
      return;
    }
    if (!(src.role === 'tout' || (src.role === 'mout' && from.out === 'bool'))) throw new Error('Een instelling stuur je vanaf de uitgang van een timer of een 0/1-module (bv. Groter dan).');
    const pt = dev.points.find((x) => String(x.dp) === String(set.dp));
    const r = await api('POST', 'actions', { name: `${tb.name} · ${pt ? pt.name : set.dp}`.slice(0, 60), page: tb.page || SC.page, ...scFreeSpot('module', tb.page || SC.page) });
    await api('PUT', `actions/${r.key.slice(2)}`, { device: dev.id, dp: String(set.dp), inputs: { trigger: from.key } });
    SC.sel = SC.reveal = r.key;
    toast('Routine Tuya instellen gemaakt, nog uit. Kies de waarden bij 1 en 0 en zet ze aan.', 7000);
  }
  SC.force = true;
  await refresh();
}

async function scWireUp(a, b) { // a en b zijn poorten; één hoort bij een lamp, de ander bij een schakelaar, of het zijn modules
  const pa = scBlock(a.b), pb = scBlock(b.b);
  if ([a.role, b.role].some((r) => r === 'acin' || r === 'acout')) return scWireAirco(a, b);
  if ([a.role, b.role].some((r) => r === 'pval' || r === 'pset')) return scWireDp(a, b);
  if (SC_CARD_ROLES.includes(a.role) || SC_CARD_ROLES.includes(b.role)) return scWireCard(a, b);
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
    const r = d.type.startsWith('card-') ? await api('POST', 'cards', { type: d.type.slice(5), page: SC.page, ...scFreeSpot('module') })
      : d.type === 'sprinkler' ? await api('POST', 'sprinklers', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'action' ? await api('POST', 'actions', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'panel' ? await api('POST', 'panels', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'screen' ? await api('POST', 'screens', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'tuya' ? await api('POST', 'tuya', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'airco' ? await api('POST', 'airco', { page: SC.page, ...scFreeSpot('module') })
      : d.type === 'solar' ? await api('POST', 'solar', { page: SC.page, ...scFreeSpot('module') })
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
  pncard: (d) => {
    const b = scBlock(d.k), list = b.cards.map((c) => c.id), n = +d.n;
    if (d.op === 'del') list.splice(n, 1);
    else {
      const to = d.op === 'up' ? n - 1 : n + 1;
      [list[n], list[to]] = [list[to], list[n]];
    }
    return scSaveCards(b, list);
  },
  pnnotes: (d) => scSavePanel(d.id, { notes: [] }),
  pnyaml: async (d) => {
    const yaml = $('#pnyaml').value;
    if (!yaml.trim()) throw new Error('Plak eerst de YAML.');
    const r = await api('POST', `panels/${d.id}/yaml`, { yaml });
    SC.force = true;
    await refresh();
    const head = `${r.cards.length} ${r.cards.length === 1 ? 'kaart' : 'kaarten'} gemaakt en aan het paneel gehangen.`;
    if (!r.notes.length) { toast(head); return; }
    smallDialog(`<h3>YAML omgezet</h3><p class="sub">${esc(head)} Dit is niet meegekomen:</p><ul class="sub" style="padding-left:18px">${r.notes.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`, async () => {});
  },
  cditem: (d) => {
    const b = scBlock(d.k), items = scItems(b), i = +d.i;
    if (d.op === 'add') items.push({ ref: null, entity: null, name: '', icon: '' });
    else if (d.op === 'del') items.splice(i, 1);
    else {
      const to = d.op === 'up' ? i - 1 : i + 1;
      [items[i], items[to]] = [items[to], items[i]];
    }
    return scSaveCard(b, { items });
  },
  cdpow: (d) => {
    const b = scBlock(d.k), items = scPowItems(b);
    if (d.op === 'add') items.push({ entity: null, name: '', icon: '', speed: 0, levels: null });
    else items.splice(+d.i, 1);
    return scSaveCard(b, { items });
  },
  rtoggle: (d) => scRemoteCmd(scBlock(d.k), { op: 'toggle' }),
  rcmd: (d) => scRemoteCmd(scBlock(d.k), { op: d.op }),
  cdinclear: (d) => {
    const b = scBlock(d.k);
    return scSaveCard(b, { inputs: Object.fromEntries(b.ins.filter((x) => x.ref && x.name !== d.name).map((x) => [x.name, x.ref])) });
  },
  delaction: async (d) => {
    if (!confirm('Deze routine verwijderen? Wat ze al gezet heeft blijft zo staan.')) return;
    await api('DELETE', `actions/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  delcard: async (d) => {
    if (!confirm('Deze kaart verwijderen? Ze verdwijnt van de panelen en schermen, en de knoppen die erheen gingen verdwijnen ook.')) return;
    await api('DELETE', `cards/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  delpanel: async (d) => {
    if (!confirm('Dit paneel verwijderen? Zijn relais en temperatuur verdwijnen ook uit Home Assistant.')) return;
    await api('DELETE', `panels/${d.id}`);
    delete SC.panelDraft[d.id];
    SC.sel = null;
    await refresh();
  },
  delscreen: async (d) => {
    if (!confirm('Dit scherm verwijderen? Op het scherm zelf blijft het ontwerp staan tot het opnieuw opstart.')) return;
    await api('DELETE', `screens/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  tyfetch: async (d) => {
    try {
      await api('POST', `tuya/${d.id}/fetch`, {});
    } finally {
      SC.force = true;
      await refresh();
    }
  },
  awscan: async () => {
    await api('POST', 'airco/scan', {});
    toast('Gevraagd wie er is; een airco die antwoordt, staat binnen enkele seconden in de keuzelijst.', 5000);
    setTimeout(() => { SC.force = true; refresh(); }, 2500);
  },
  awbind: async (d) => {
    if (!confirm('Opnieuw een sleutel vragen aan deze airco? Dat is nodig als ze opnieuw aan de wifi gekoppeld is.')) return;
    await scSaveAirco(scBlock(`w:${d.id}`), { rebind: true });
  },
  solook: async (d) => {
    const b = scBlock(`o:${d.id}`), user = document.getElementById('solu'), pass = document.getElementById('solp');
    const r = await api('POST', 'solar/lookup', { host: b.host, user: user ? user.value : 'admin', password: pass ? pass.value : 'admin' });
    await scSaveSolar(b, { serial: r.serial });
    toast(`Serienummer ${r.serial} gevonden.`, 4000);
  },
  delsolar: async (d) => {
    if (!confirm('Deze omvormer verwijderen? Zijn sensoren verdwijnen uit Home Assistant.')) return;
    await api('DELETE', `solar/${d.id}`);
    SC.sel = null;
    await refresh();
  },
  delairco: async (d) => {
    if (!confirm('Deze airco verwijderen? Haar entiteiten verdwijnen uit Home Assistant.')) return;
    await api('DELETE', `airco/${d.id}`);
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

const scPortOf = (el) => (el && el.dataset && el.dataset.role ? { b: el.dataset.b, role: el.dataset.role === 'touch' || el.dataset.role === 'relay' ? null : el.dataset.role, swRole: el.dataset.role, g: el.dataset.g, i: el.dataset.i, dp: el.dataset.dp } : null);

function scFinish(target) {
  const from = SC.link;
  const toPort = target && target.closest && target.closest('.pt');
  const toBlock = target && target.closest && target.closest('[data-block]');
  const norm = (p) => ({ b: p.b, role: scBlock(p.b).kind === 'lamp' ? p.lampRole : p.swRole, g: p.g, i: p.i, dp: p.dp });
  let dest = null;
  if (toPort) {
    const p = scPortOf(toPort);
    dest = { b: p.b, lampRole: p.role, swRole: p.swRole, g: p.g, i: p.i, dp: p.dp };
  } else if (toBlock && toBlock.dataset.block !== from.b) {
    const tb = scBlock(toBlock.dataset.block);
    if (tb.kind === 'lamp' || (tb.kind === 'remote' && from.swRole === 'citem')) dest = { b: tb.key, lampRole: null }; // losgelaten op de lamp zelf: de poort waar je begon beslist
    else if ((tb.kind === 'panel' || tb.kind === 'screen') && from.swRole === 'cout') dest = { b: tb.key, swRole: 'pcard', i: 'new' }; // een kaart op een paneel of scherm: achteraan
    else if (tb.kind === 'card' && tb.type === 'grid' && from.swRole !== 'citem') dest = { b: tb.key, swRole: 'citem', i: 'new' };
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
    SC.link = { b: p.b, lampRole: p.role, swRole: p.swRole, role: p.role || p.swRole, g: p.g, i: p.i, dp: p.dp, x: pt.x, y: pt.y, startX: ev.clientX, startY: ev.clientY };
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
        else if (b.kind === 'airco') await api('PUT', `airco/${b.id}`, { name });
        else if (b.kind === 'solar') await api('PUT', `solar/${b.id}`, { name });
        else if (b.kind === 'card') await api('PUT', `cards/${b.id}`, { name });
        else if (b.kind === 'action') await api('PUT', `actions/${b.id}`, { name });
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
  const action = SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'action' ? scBlock(SC.sel) : null;
  if (t.dataset && t.dataset.acfield && action) {
    const f = t.dataset.acfield;
    const patch = f === 'enabled' ? { enabled: t.checked } : f === 'trigger' ? { inputs: { trigger: t.value || null } } : f === 'interval' ? { interval: +t.value }
      : f === 'device' ? { device: t.value, dp: '', on_value: '', off_value: '' } : f === 'dp' ? { dp: t.value, on_value: '', off_value: '' } : { [f]: t.value.trim() };
    t.blur();
    act(async () => { try { await api('PUT', `actions/${action.id}`, patch); } finally { SC.force = true; await refresh(); } })();
  }
  if (t.dataset && t.dataset.mfield) {
    const b = scBlock(t.dataset.k), f = t.dataset.mfield;
    const patch = f === 'value' ? { value: +t.value } : f === 'device' ? { device: t.value, dp: '' } : { dp: t.value };
    t.blur();
    act(async () => { try { await api('PUT', `modules/${b.id}`, patch); } finally { SC.force = true; await refresh(); } })();
  }
  const remote = SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'remote' ? scBlock(SC.sel) : null;
  if (t.dataset && t.dataset.rset && remote) {
    const dp = t.dataset.rset, value = t.value;
    t.blur();
    act(() => scRemoteCmd(remote, { dp, value }))();
  }
  const card = SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'card' ? scBlock(SC.sel) : null;
  if (t.dataset && t.dataset.cdfield && card) {
    const f = t.dataset.cdfield, raw = t.value.trim();
    const value = f === 'price' ? (raw === '' ? 0 : +raw) : ['weather', 'forecast', 'temperature', 'status1', 'status2', 'price_entity'].includes(f) ? (raw || null) : f === 'password' ? t.value : raw;
    t.blur();
    act(() => scSaveCard(card, { [f]: value }))();
  }
  if (t.dataset && t.dataset.cdtarget && card) {
    const f = t.dataset.cdtarget, v = t.value.trim();
    const target = f === 'device' ? { device: v, dp: '', on: '', off: '' } : f === 'dp' ? { ...card.target, dp: v, on: '', off: '' } : { ...card.target, [f]: v };
    t.blur();
    act(() => scSaveCard(card, { target }))();
  }
  if (t.dataset && t.dataset.cdwhen && card) {
    const f = t.dataset.cdwhen, value = t.value.split(',').map((x) => x.trim()).filter(Boolean);
    t.blur();
    act(() => scSaveCard(card, { [f]: value }))();
  }
  if (t.dataset && t.dataset.cdstat != null && card) { // één statuswaarde: niets, Start of Stop
    const o = t.dataset.cdstat.toLowerCase(), drop = (xs) => (xs || []).filter((x) => x.toLowerCase() !== o);
    const start_when = drop(card.start_when), stop_when = drop(card.stop_when);
    if (t.value === 'start') start_when.push(t.dataset.cdstat);
    if (t.value === 'stop') stop_when.push(t.dataset.cdstat);
    t.blur();
    act(() => scSaveCard(card, { start_when, stop_when }))();
  }
  if (t.dataset && t.dataset.cditem && card) {
    const f = t.dataset.cditem, i = +t.dataset.i, items = scItems(card);
    if (f === 'entity') {
      const entity = t.value.trim() || null;
      if (!entity && items[i].ref) return; // "zoals hierboven": de lamp of subpagina blijft
      const known = (SC.haEnts || []).find((e) => e.entity_id === entity);
      items[i] = { ...items[i], entity, ref: null, dp: '', name: items[i].name || (known ? known.name.slice(0, 60) : '') };
    } else if (f === 'dpdev') {
      if (!t.value) { items[i] = { ...items[i], ref: null, dp: '' }; } else {
        const dev = scDevices(SC_HERE_Y).all.find((x) => x.value === t.value), first = dev && dev.points[0];
        if (!first) { toast('Dit toestel meldt nog geen waarden.'); return; }
        items[i] = { ...items[i], ref: t.value, entity: null, dp: String(first.dp) };
      }
    } else items[i] = { ...items[i], [f]: t.value.trim() };
    t.blur();
    act(() => scSaveCard(card, { items }))();
  }
  if (t.dataset && t.dataset.cdpow && card) {
    const f = t.dataset.cdpow, i = +t.dataset.i, items = scPowItems(card), it = { ...items[i] };
    if (f === 'lo' || f === 'hi') {
      const box = t.closest('.row'), lo = box.querySelector('[data-cdpow=lo]').value, hi = box.querySelector('[data-cdpow=hi]').value;
      if ((lo === '') !== (hi === '')) return; // wacht op de tweede grens
      it.levels = lo === '' ? null : [+lo, +hi];
    } else if (f === 'speed') it.speed = +t.value || 0;
    else if (f === 'entity') { it.entity = t.value.trim() || null; if (it.entity) { it.tuya = ''; it.dp = ''; } }
    else if (f === 'tuya') {
      it.tuya = t.value;
      const dev = scDevices(SC_HERE_Y).all.find((x) => x.value === t.value), first = dev && dev.points[0];
      it.dp = t.value && first ? String(first.dp) : '';
      if (t.value && !first) { toast('Dit toestel meldt nog geen waarden.'); return; }
      if (t.value) it.entity = null;
    }
    else it[f] = t.value.trim();
    items[i] = it;
    t.blur();
    act(() => scSaveCard(card, { items }))();
  }
  if (t.dataset && t.dataset.pnset) {
    const f = t.dataset.pnset, value = f === 'locale' ? t.value : +t.value;
    t.blur();
    act(() => scSavePanel(t.dataset.id, { [f]: value }))();
  }
  if (t.dataset && t.dataset.pnadd && t.value) {
    const b = scBlock(t.dataset.pnadd), cid = t.value;
    t.blur();
    act(() => scSaveCards(b, [...b.cards.map((c) => c.id), cid]))();
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
  if (t.dataset && (t.dataset.awset || t.dataset.awin || t.dataset.awfield || t.dataset.awfound) && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'airco') {
    const b = scBlock(SC.sel), ds = t.dataset, value = ds.awfield === 'key' ? t.value : t.value.trim();
    t.blur();
    if (ds.awset && value !== '') {
      act(async () => {
        try {
          await api('POST', `airco/${b.id}/set`, { field: ds.awset, value });
        } finally {
          SC.force = true;
          await refresh();
        }
      })();
    } else if (ds.awin) act(() => scSaveAirco(b, { inputs: { [ds.awin]: value || null } }))();
    else if (ds.awfound && value) {
      const [mac, host] = value.split('|');
      act(() => scSaveAirco(b, { mac, host }))();
    } else if (ds.awfield && (value || ds.awfield !== 'key')) act(() => scSaveAirco(b, { [ds.awfield]: value }))();
  }
  if (t.dataset && t.dataset.sofield && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'solar') {
    const b = scBlock(SC.sel), value = t.value.trim();
    t.blur();
    if (value) act(() => scSaveSolar(b, { [t.dataset.sofield]: value }))();
  }
  if (t.dataset && t.dataset.tyfield && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'tuya') {
    const b = scBlock(SC.sel);
    const value = t.dataset.tyfield === 'key' ? t.value : t.value.trim();
    t.blur();
    if (value || t.dataset.tyfield !== 'model') act(() => scSaveTuya(b, { [t.dataset.tyfield]: value }))();
  }
  if (t.dataset && t.dataset.tyfound && t.value && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'tuya') {
    const [deviceId, host] = t.value.split('|');
    t.blur();
    act(() => scSaveTuya(scBlock(SC.sel), { device_id: deviceId, host }))();
  }
  if (t.dataset && t.dataset.tycloud) {
    const value = t.value.trim();
    t.blur();
    if (value || t.dataset.tycloud !== 'secret') {
      act(async () => {
        try {
          await api('PUT', 'tuya-cloud', { [t.dataset.tycloud]: value });
        } finally {
          SC.force = true;
          await refresh();
        }
      })();
    }
  }
  if (t.dataset && t.dataset.hsfield && SC.sel && scBlock(SC.sel) && scBlock(SC.sel).kind === 'screen') {
    const b = scBlock(SC.sel), f = t.dataset.hsfield;
    t.blur();
    act(() => scSaveScreen(b, { [f]: f === 'timeout' ? +t.value : t.value.trim() }))();
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
