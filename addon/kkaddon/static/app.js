'use strict';
// Eenvoudige pagina zonder build-stap. Alle adressen zijn relatief, zodat het ook achter HA-ingress werkt.

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const MODE_NL = { toggle: 'wisselen', on: 'aan', off: 'uit' };
const CMD_BTN = 255; // LIGHT_STATE-oorzaak "commando" (CMD_LIGHT), dus Home Assistant of deze UI

let S = null; // laatste momentopname van de add-on
let tab = 'lights';
let L = null; // open licht-dialoog: {node, relay, picks, learning, saved}
let tokenRefresh = 0;

async function api(method, path, body) {
  const r = await fetch('api/' + path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let j = null;
  try { j = await r.json(); } catch { /* geen JSON */ }
  if (!r.ok) throw new Error((j && j.error) || `Fout ${r.status}`);
  return j;
}

function toast(msg, ms = 4000) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.hidden = true), ms);
}
const act = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message); } };

// ---------------------------------------------------------------- namen en teksten

const nodeName = (id) => (S && S.names[id]) || `Node ${id}`;
const nodeOf = (id) => S && S.nodes.find((n) => n.id === id);
function lightName(n, r) {
  const l = S && S.lights.find((x) => x.node === n && x.relay === r);
  return (l && l.name) || `${nodeName(n)} relais ${r + 1}`;
}
const time = (ts) => new Date(ts * 1000).toLocaleTimeString('nl-NL');
function ago(ts) {
  if (!ts) return 'nooit';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return 'net nu';
  if (s < 5400) return `${Math.round(s / 60)} min geleden`;
  if (s < 172800) return `${Math.round(s / 3600)} u geleden`;
  return `${Math.round(s / 86400)} d geleden`;
}

function causeText(e) {
  if (!e.cause) return e.via === 'heartbeat' ? ' (via heartbeat)' : '';
  if (e.cause.btn === CMD_BTN) return ' (door Home Assistant)';
  return ` (door ${nodeName(e.cause.src)}, knop ${e.cause.btn + 1})`;
}

function logText(e) {
  switch (e.t) {
    case 'btn': return `${nodeName(e.node)}: knop ${e.btn + 1} ingedrukt`;
    case 'light': return `${lightName(e.node, e.relay)} → ${e.on ? 'aan' : 'uit'}${causeText(e)}`;
    case 'node': return `${nodeName(e.node)}: ${{ new: 'voor het eerst gezien', online: 'online', offline: 'offline', info: 'gegevens bijgewerkt' }[e.what] || e.what}`;
    case 'cfg': return `${nodeName(e.node)}: configuratie ${{ ok: 'klopt', applied: 'verstuurd en bevestigd', pushing: 'wordt verstuurd', drift: 'wijkt af, wordt opnieuw verstuurd', failed: 'versturen mislukt' }[e.status] || e.status}`;
    case 'cmd': if (e.result === 'unknown') return `${lightName(e.node, e.relay)}: stand onbekend, wisselen niet mogelijk`;
      return e.result === 'ok'
      ? `${lightName(e.node, e.relay)} ${e.target ? 'aan' : 'uit'} bevestigd (${e.ms} ms)`
      : `${lightName(e.node, e.relay)} ${e.target ? 'aan' : 'uit'}: geen bevestiging, opdracht opgegeven`;
    case 'learn': return { started: 'Leer-modus gestart', stopped: 'Leer-modus gestopt', saved: 'Schakelaars opgeslagen', detected: `Leer-modus: ${nodeName(e.node)} knop ${e.btn + 1} gezien` }[e.what] || e.what;
    default: return e.t;
  }
}

// ---------------------------------------------------------------- weergave

function chips() {
  const online = S.nodes.filter((n) => n.online).length;
  const c = (cls, txt) => `<span class="chip ${cls}">${txt}</span>`;
  const out = [c(online === S.nodes.length && S.nodes.length ? 'ok' : online ? 'warn' : 'bad', `${online}/${S.nodes.length} nodes online`)];
  out.push(c(S.peers.length ? 'ok' : 'warn', S.peers.length ? `${S.peers.length} bridge${S.peers.length > 1 ? 's' : ''} op het LAN` : 'nog geen bridge gehoord'));
  out.push(c(S.mqtt.connected ? 'ok' : 'warn', S.mqtt.connected ? 'MQTT verbonden' : S.mqtt.enabled ? 'MQTT verbindt…' : 'geen MQTT-broker'));
  if (S.nodes.some((n) => n.virtual)) out.push(c(S.ha.connected ? 'ok' : 'bad', S.ha.connected ? 'HA verbonden' : S.ha.enabled ? 'HA verbindt…' : 'geen HA-koppeling'));
  $('#chips').innerHTML = out.join('');
}

function banner() {
  const b = $('#banner');
  if (!S.learn) { b.hidden = true; return; }
  const t = S.learn.target;
  b.hidden = false;
  b.innerHTML = `<span><b>Leer-modus actief</b>${t ? ` voor ${esc(lightName(t[0], t[1]))}` : ''}. Er schakelt niets tot je opslaat of stopt.</span>
    <span class="row">${t && !L ? '<button class="btn small" data-act="resume">Openen</button>' : ''}<button class="btn small" data-act="learnstop">Stoppen</button></span>`;
}

function viewLights() {
  const cards = S.lights.map((l) => {
    const st = l.on === true ? 'Aan' : l.on === false ? 'Uit' : 'Onbekend';
    const cls = l.on === true ? 'on' : l.on === null ? 'unknown' : '';
    const trig = l.triggers.map((t) => `<span class="tag">${esc(nodeName(t.node))} · knop ${t.btn + 1}${t.mode !== 'toggle' ? ` (${MODE_NL[t.mode]})` : ''}</span>`).join('')
      || '<span class="sub">Nog geen schakelaars gekoppeld</span>';
    return `<div class="card">
      <div class="row sp"><h3>${esc(l.name || `Relais ${l.relay + 1}`)}</h3>${l.enabled ? '' : '<span class="badge">verborgen in HA</span>'}</div>
      <div class="sub"><span class="dot ${l.online ? 'on' : 'off'}"></span>${esc(nodeName(l.node))} · relais ${l.relay + 1}${l.online ? '' : ' · offline'}</div>
      <button class="power ${cls} ${l.pending ? 'pending' : ''}" data-act="toggle" data-n="${l.node}" data-r="${l.relay}">${st}</button>
      <div class="tags">${trig}</div>
      <div class="row"><button class="btn small primary" data-act="learn" data-n="${l.node}" data-r="${l.relay}">Schakelaars leren</button>
      <button class="btn small" data-act="edit" data-n="${l.node}" data-r="${l.relay}">Bewerken</button></div>
    </div>`;
  }).join('');
  const free = S.free_relays.map((f) => `<tr><td>${esc(nodeName(f.node))} · relais ${f.relay + 1}</td><td>${f.on === null ? '' : f.on ? 'aan' : 'uit'}</td>
      <td style="text-align:right"><button class="btn small" data-act="addlight" data-n="${f.node}" data-r="${f.relay}">Als licht toevoegen</button></td></tr>`).join('');
  return `${cards ? `<div class="grid" style="margin-top:12px">${cards}</div>` : '<p class="empty">Nog geen lichten. Voeg hieronder een relais toe als licht, of wacht tot er nodes verschijnen.</p>'}
    ${free ? `<h2>Relais zonder licht</h2><div class="scroll"><table><tbody>${free}</tbody></table></div>` : ''}`;
}

const CFG = {
  ok: ['ok', 'in orde'], applied: ['ok', 'verstuurd'], pushing: ['warn', 'bezig…'], drift: ['bad', 'afwijkend'],
  failed: ['bad', 'mislukt'], unmanaged: ['', 'niet beheerd'], unknown: ['', 'onbekend'], extern: ['', 'via HA'],
};

function viewNodes() {
  const add = '<div class="row" style="margin-top:12px"><button class="btn" data-act="vnew">Extern apparaat toevoegen (bv. Tuya)</button><button class="btn" data-act="vimport">Importeren uit Home Assistant (bv. ESPHome)</button></div>';
  if (!S.nodes.length) return `${add}<p class="empty">Nog geen nodes gehoord. Zodra een node een heartbeat stuurt (elke 40–80 s) verschijnt hij hier.</p>`;
  const rows = S.nodes.map((n) => {
    const [cls, txt] = CFG[n.cfg_status] || ['', n.cfg_status];
    if (n.virtual) {
      return `<tr>
      <td style="white-space:nowrap"><span class="dot ${n.online ? 'on' : 'off'}"></span>${n.id}</td>
      <td><b>${esc(n.name || `Extern ${n.id}`)}</b>${n.room ? `<div class="sub">${esc(n.room)}</div>` : ''} <span class="badge">extern</span></td>
      <td>–</td>
      <td>${n.gangs.length} gangen<div class="sub">${n.gangs.map((g) => esc(g.entity)).join(', ')}</div></td>
      <td><span class="badge ${n.online ? 'ok' : 'bad'}">${n.online ? 'via HA' : 'HA niet bereikbaar'}</span></td>
      <td class="sub">${n.online ? 'live' : ago(n.last_seen)}</td>
      <td><div class="row"><button class="btn small" data-act="vedit" data-n="${n.id}">Bewerken</button></div></td></tr>`;
    }
    return `<tr>
      <td style="white-space:nowrap"><span class="dot ${n.online ? 'on' : 'off'}"></span>${n.id}</td>
      <td><b>${esc(n.name || `Node ${n.id}`)}</b>${n.room ? `<div class="sub">${esc(n.room)}</div>` : ''}${n.bridge ? ' <span class="badge">bridge</span>' : ''}</td>
      <td>${esc(n.fw || '?')}</td>
      <td>${n.relays.length} relais${n.nbtn != null ? `, ${n.nbtn} knoppen` : ''}${n.nind ? `, ${n.nind} indicatoren` : ''}</td>
      <td><span class="badge ${cls}">${txt}</span></td>
      <td class="sub">${ago(n.last_seen)}</td>
      <td><div class="row">
        <button class="btn small" data-act="nodename" data-n="${n.id}">Naam</button>
        <button class="btn small" data-act="identify" data-n="${n.id}">Knipper</button>
        <button class="btn small" data-act="sync" data-n="${n.id}">Config sturen</button>
        ${n.nind ? `<button class="btn small" data-act="follow" data-n="${n.id}">Indicatoren</button>` : ''}
      </div></td></tr>`;
  }).join('');
  return `${add}<div class="scroll" style="margin-top:12px"><table><thead><tr><th>ID</th><th>Naam</th><th>Fw</th><th>Hardware</th><th>Config</th><th>Laatst gezien</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="sub">"Niet beheerd": de node heeft een andere configuratie dan het model, maar jij hebt hem nog niet aan een licht gekoppeld, dus wordt er niets overschreven.</p>`;
}

function viewLog() {
  const rows = [...S.log].reverse().map((e) => `<div><time>${time(e.ts)}</time><span>${esc(logText(e))}</span></div>`).join('');
  return rows ? `<div class="log" style="margin-top:12px">${rows}</div>` : '<p class="empty">Nog niets gebeurd.</p>';
}

function viewSettings() {
  const peers = S.peers.map((p) => `<tr><td>${esc(p.ip)}${p.configured ? ' <span class="badge">vast</span>' : ''}</td><td>${p.frames}</td><td class="sub">${ago(p.last)}</td></tr>`).join('')
    || '<tr><td colspan="3" class="sub">Nog geen frames van een bridge ontvangen.</td></tr>';
  return `<h2>Bridges op het LAN</h2>
    <div class="scroll"><table><thead><tr><th>Adres</th><th>Frames</th><th>Laatst</th></tr></thead><tbody>${peers}</tbody></table></div>
    <p class="sub">Peer-id van deze add-on: ${S.peer_id} (0x${S.peer_id.toString(16)}). ${S.mqtt.error ? `MQTT-fout: ${esc(S.mqtt.error)}` : ''}</p>
    <p class="sub">Home Assistant (externe apparaten): ${S.ha.connected ? 'verbonden' : S.ha.enabled ? `niet verbonden${S.ha.error ? ` (${esc(S.ha.error)})` : ''}` : 'niet ingesteld (nodig voor Tuya en andere HA-entiteiten)'}.</p>
    <h2>Netwerksleutel</h2>
    <p class="sub">Alle nodes van dit huis delen één sleutel, die bij het compileren in de firmware komt (<code>firmware/secrets.h</code>, staat in .gitignore).</p>
    <div class="row"><button class="btn" data-act="showkey">Toon secrets.h</button></div><div id="keybox"></div>
    <h2>Back-up</h2>
    <div class="row"><a class="btn" href="api/export" download="kruisschakelaars.json">Exporteer configuratie</a>
    <label class="btn" style="margin:0;color:var(--ink)">Importeer… <input id="imp" type="file" accept="application/json" hidden></label></div>
    <p class="sub">Versie ${esc(S.version)}.</p>`;
}

function render() {
  if (!S || (tab === 'plan' && PL.drag)) return; // tijdens slepen op de plattegrond niet opnieuw tekenen
  chips();
  banner();
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
  if (tab !== 'settings' || !$('#keybox')) $('#main').innerHTML = { lights: viewLights, plan: viewPlan, nodes: viewNodes, log: viewLog, settings: viewSettings }[tab]();
  if (L) refreshPicks();
}

// ---------------------------------------------------------------- licht-dialoog (bewerken en leren)

const dlg = () => $('#dlg');
const pkey = (n, b) => `${n}:${b}`;

function openLight(node, relay, learning, resume = false) {
  const l = S.lights.find((x) => x.node === node && x.relay === relay) || { name: '', enabled: true, triggers: [] };
  L = { node, relay, learning, saved: false, picks: new Map() };
  l.triggers.forEach((t) => L.picks.set(pkey(t.node, t.btn), { node: t.node, btn: t.btn, mode: t.mode, checked: true, seen: 0 }));
  const nodesWithButtons = S.nodes.filter((n) => n.nbtn !== 0);
  $('#dlgform').innerHTML = `<h3>${learning ? 'Schakelaars leren' : 'Licht bewerken'}</h3>
    <div class="sub">${esc(nodeName(node))} · relais ${relay + 1}</div>
    <label for="lname">Naam</label><input type="text" id="lname" maxlength="60" value="${esc(l.name)}" placeholder="bv. Plafond living">
    <label><input type="checkbox" id="lenabled" ${l.enabled ? 'checked' : ''}> Zichtbaar in Home Assistant</label>
    ${learning ? `<p style="margin:12px 0 4px"><b>Druk nu op elke schakelaar die dit licht moet bedienen.</b><br><span class="sub">Tijdens het leren schakelt er niets. Een gedetecteerde schakelaar verschijnt hieronder.</span></p>` : ''}
    <h2 style="margin-top:14px">Schakelaars</h2><div id="picks"></div>
    <div class="row" style="margin-top:10px"><select id="addnode">${nodesWithButtons.map((n) => `<option value="${n.id}">${esc(n.name || `Node ${n.id}`)} (${n.id})</option>`).join('')}</select>
      <select id="addbtn">${[0, 1, 2, 3, 4, 5, 6, 7].map((b) => `<option value="${b}">knop ${b + 1}</option>`).join('')}</select>
      <button class="btn small" type="button" data-act="addpick">Toevoegen</button></div>
    <div class="row sp" style="margin-top:18px">
      <span>${S.lights.some((x) => x.node === node && x.relay === relay) ? '<button class="btn danger" type="button" data-act="dellight">Licht verwijderen</button>' : ''}</span>
      <span class="row"><button class="btn" type="button" data-act="cancel">Annuleren</button><button class="btn primary" type="button" data-act="save">Opslaan</button></span></div>`;
  dlg().showModal();
  if (learning && !resume) act(() => api('POST', 'learn/start', { node, relay }))();
  refreshPicks();
}

function refreshPicks() {
  if (!L || !$('#picks')) return;
  if (S.learn && S.learn.target && S.learn.target[0] === L.node && S.learn.target[1] === L.relay) {
    for (const d of S.learn.detected) {
      const k = pkey(d.node, d.btn);
      const p = L.picks.get(k);
      if (p) p.seen = d.count;
      else L.picks.set(k, { node: d.node, btn: d.btn, mode: 'toggle', checked: true, seen: d.count });
    }
  }
  const rows = [...L.picks.values()].sort((a, b) => a.node - b.node || a.btn - b.btn).map((p) => `
    <div class="pick"><input type="checkbox" data-pick="${pkey(p.node, p.btn)}" ${p.checked ? 'checked' : ''}>
      <span>${esc(nodeName(p.node))} · knop ${p.btn + 1}${p.seen ? ` <span class="sub">· ${p.seen}× gezien</span>` : ''}</span>
      <select data-mode="${pkey(p.node, p.btn)}">${Object.entries(MODE_NL).map(([v, t]) => `<option value="${v}" ${p.mode === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div>`).join('');
  $('#picks').innerHTML = rows || '<div class="empty">Nog geen schakelaars.</div>';
}

async function closeLight(save) {
  const cur = L;
  if (!cur) return;
  if (save) {
    const triggers = [...cur.picks.values()].filter((p) => p.checked).map((p) => ({ node: p.node, btn: p.btn, mode: p.mode }));
    await api('PUT', `light/${cur.node}/${cur.relay}`, { name: $('#lname').value, enabled: $('#lenabled').checked, triggers });
    cur.saved = true;
  }
  if (S.learn) await api('POST', 'learn/stop', {});
  L = null;
  dlg().close();
  await refresh();
}

// ---------------------------------------------------------------- andere dialogen

function smallDialog(html, onOk) {
  $('#dlgform').innerHTML = `${html}<div class="row sp" style="margin-top:18px"><span></span><span class="row">
    <button class="btn" type="button" data-act="dlgcancel">Annuleren</button><button class="btn primary" type="button" data-act="dlgok">OK</button></span></div>`;
  smallDialog.ok = onOk;
  dlg().showModal();
}

// ---------------------------------------------------------------- extern apparaat (bv. Tuya via HA)

let V = null; // open dialoog: {id, gangs}

function readGangs() {
  return [...document.querySelectorAll('#gangs .gang')].map((r) => ({
    entity: r.querySelector('[data-f=entity]').value.trim(),
    name: r.querySelector('[data-f=name]').value.trim(),
    lamp: r.querySelector('[data-f=lamp]').checked,
  }));
}

function gangsHtml() {
  return V.gangs.map((g, i) => `<div class="gang" style="border-top:1px solid var(--line);padding:8px 0">
      <div class="row"><b>Gang ${i + 1}</b>${V.gangs.length > 1 ? `<button class="btn small danger" type="button" data-act="vdelgang" data-i="${i}">Weg</button>` : ''}</div>
      <label>HA-entiteit</label><input type="text" data-f="entity" list="ents" value="${esc(g.entity)}" placeholder="switch.gang_1" autocomplete="off">
      <label>Naam</label><input type="text" data-f="name" maxlength="60" value="${esc(g.name)}">
      <label><input type="checkbox" data-f="lamp" ${g.lamp ? 'checked' : ''}> Stuurt een lamp aan</label></div>`).join('');
}

async function openVirtual(id) {
  const n = id ? nodeOf(id) : null;
  V = { id, gangs: n ? n.gangs.map((g) => ({ ...g })) : [{ entity: '', name: 'Gang 1', lamp: true }] };
  let ents = [];
  try { ents = (await api('GET', 'ha/entities')).entities; } catch { /* zonder suggesties kan het ook */ }
  smallDialog(`<h3>${n ? 'Extern apparaat bewerken' : 'Extern apparaat toevoegen'}</h3>
    <p class="sub">Een schakelaar die niet te flashen is (bv. Tuya via local-tuya). Elke gang is een HA-entiteit. De touch van het apparaat schakelt zelf zijn relais; de andere schakelaars bedienen het via Home Assistant.</p>
    ${S.ha.connected ? '' : '<p class="sub" style="color:var(--bad)">Home Assistant is niet verbonden: je kunt de entiteiten niet kiezen uit een lijst en het apparaat blijft offline.</p>'}
    <label for="vname">Naam</label><input type="text" id="vname" maxlength="60" value="${esc(n ? n.name : '')}" placeholder="bv. Tuya gang">
    <label for="vroom">Kamer</label><input type="text" id="vroom" maxlength="60" value="${esc(n ? n.room : '')}">
    <datalist id="ents">${ents.map((e) => `<option value="${esc(e.entity_id)}">${esc(e.name)}</option>`).join('')}</datalist>
    <div id="gangs">${gangsHtml()}</div>
    <div class="row" style="margin-top:8px"><button class="btn small" type="button" data-act="vaddgang">Gang toevoegen</button>
    ${n ? '<button class="btn small danger" type="button" data-act="vdelete">Apparaat verwijderen</button>' : ''}</div>`,
  async () => {
    const body = { name: $('#vname').value, room: $('#vroom').value, gangs: readGangs() };
    if (V.id) await api('PUT', `virtual/${V.id}`, body);
    else await api('POST', 'virtual', body);
  });
}

// ---------------------------------------------------------------- importeren uit HA (bv. bestaande ESPHome-schakelaars)

const MAX_GANGS = 8; // een node heeft knopindex 0..7; een relaisbox met 16 relais wordt dus in delen van 8 gesplitst

async function openImport() {
  let r = { connected: false, registry: false, devices: [] };
  try { r = await api('GET', 'ha/devices'); } catch { /* hieronder uitgelegd */ }
  if (!r.registry) {
    smallDialog(`<h3>Importeren uit Home Assistant</h3>
      <p class="sub">${r.connected ? 'Home Assistant geeft de apparatenlijst niet vrij.' : 'Home Assistant is niet verbonden.'} Voeg het apparaat dan met <b>Extern apparaat toevoegen</b> toe en kies de entiteiten zelf.</p>`, async () => {});
    return;
  }
  const todo = r.devices.filter((d) => !d.imported).sort((a, b) => (b.platform === 'esphome') - (a.platform === 'esphome') || a.name.localeCompare(b.name));
  const rows = todo.map((d, i) => `<label class="imp" data-q="${esc(`${d.name} ${d.area} ${d.platform}`.toLowerCase())}" style="display:block;padding:6px 0;border-top:1px solid var(--line)">
      <input type="checkbox" data-i="${i}"> <b>${esc(d.name)}</b>${d.platform === 'esphome' ? ' <span class="badge ok">ESPHome</span>' : ''}${d.area ? ` <span class="sub">${esc(d.area)}</span>` : ''}
      <div class="sub">${d.entities.length} ${d.entities.length === 1 ? 'schakelaar' : 'schakelaars'}: ${d.entities.map((e) => esc(e.entity_id)).join(', ')}${d.entities.length > MAX_GANGS ? ` (wordt ${Math.ceil(d.entities.length / MAX_GANGS)} apparaten van maximaal ${MAX_GANGS})` : ''}</div></label>`).join('');
  smallDialog(`<h3>Importeren uit Home Assistant</h3>
    <p class="sub">Elk gekozen apparaat wordt een extern apparaat met zijn schakelaars als gangen (de volgorde is die van de entiteit-id's). Alle gangen staan op <i>stuurt een lamp aan</i>: zet dat uit voor een touch die alleen een ander licht bedient. Al geïmporteerde apparaten staan niet in de lijst.</p>
    ${todo.length ? `<input type="search" id="impq" style="width:100%" placeholder="Zoeken op naam, kamer of ESPHome" autocomplete="off"><div id="implist" style="max-height:50vh;overflow:auto;margin-top:8px">${rows}</div>`
      : '<p class="empty">Alle apparaten met schakelaars zijn al geïmporteerd.</p>'}`,
  async () => {
    const picked = [...document.querySelectorAll('#implist input:checked')].map((c) => todo[+c.dataset.i]);
    let n = 0;
    for (const d of picked) {
      const parts = Math.ceil(d.entities.length / MAX_GANGS);
      for (let p = 0; p < parts; p++) {
        const gangs = d.entities.slice(p * MAX_GANGS, (p + 1) * MAX_GANGS).map((e) => ({ entity: e.entity_id, name: e.name, lamp: true }));
        await api('POST', 'virtual', { name: parts > 1 ? `${d.name} (${p + 1}/${parts})` : d.name, room: d.area, gangs });
        n++;
      }
    }
    if (n) toast(`${n} extern ${n === 1 ? 'apparaat' : 'apparaten'} toegevoegd`);
  });
}

// ---------------------------------------------------------------- acties

const ACTIONS = {
  toggle: (d) => api('POST', `light/${d.n}/${d.r}/cmd`, { op: 'toggle' }),
  learn: (d) => openLight(+d.n, +d.r, true),
  resume: () => openLight(S.learn.target[0], S.learn.target[1], true, true),
  learnstop: () => api('POST', 'learn/stop', {}).then(() => { if (L) { L = null; dlg().close(); } }),
  edit: (d) => openLight(+d.n, +d.r, false),
  addlight: (d) => smallDialog(`<h3>Licht toevoegen</h3><label for="nm">Naam</label><input type="text" id="nm" maxlength="60" placeholder="bv. Gang plafond">`,
    () => api('PUT', `light/${d.n}/${d.r}`, { name: $('#nm').value || `Relais ${+d.r + 1}` })),
  addpick: () => {
    const node = +$('#addnode').value, btn = +$('#addbtn').value;
    const k = pkey(node, btn);
    if (!L.picks.has(k)) L.picks.set(k, { node, btn, mode: 'toggle', checked: true, seen: 0 });
    else L.picks.get(k).checked = true;
    refreshPicks();
  },
  save: () => closeLight(true),
  cancel: () => closeLight(false),
  dellight: async () => {
    if (!confirm('Dit licht verwijderen? De schakelaars blijven bestaan maar bedienen het niet meer.')) return;
    await api('DELETE', `light/${L.node}/${L.relay}`);
    if (S.learn) await api('POST', 'learn/stop', {});
    L = null; dlg().close(); await refresh();
  },
  nodename: (d) => {
    const n = nodeOf(+d.n);
    smallDialog(`<h3>Node ${d.n}</h3><label for="nm">Naam</label><input type="text" id="nm" maxlength="60" value="${esc(n.name)}">
      <label for="rm">Kamer</label><input type="text" id="rm" maxlength="60" value="${esc(n.room)}">`,
      () => api('PUT', `node/${d.n}`, { name: $('#nm').value, room: $('#rm').value }));
  },
  identify: (d) => api('POST', `node/${d.n}/identify`, {}).then(() => toast('Node knippert met zijn LED')),
  sync: (d) => api('POST', `node/${d.n}/sync`, { full: true }).then(() => toast('Configuratie wordt verstuurd')),
  follow: (d) => {
    const n = nodeOf(+d.n);
    const opts = (cur) => `<option value="auto" ${cur.manual ? '' : 'selected'}>automatisch (volgt de knop)</option><option value="none" ${cur.manual && !cur.light ? 'selected' : ''}>geen</option>`
      + S.lights.map((l) => `<option value="${l.node}:${l.relay}" ${cur.manual && cur.light && cur.light[0] === l.node && cur.light[1] === l.relay ? 'selected' : ''}>${esc(l.name || lightName(l.node, l.relay))}</option>`).join('');
    smallDialog(`<h3>Indicatoren van ${esc(n.name || `node ${n.id}`)}</h3><p class="sub">Indicator i toont de stand van het licht dat knop i bedient, tenzij je hier iets anders kiest.</p>`
      + n.follow.map((f) => `<label>Indicator ${f.ind + 1}${f.light ? ` (nu: ${esc(lightName(f.light[0], f.light[1]))})` : ''}</label><select data-ind="${f.ind}">${opts(f)}</select>`).join(''),
      async () => {
        for (const sel of document.querySelectorAll('#dlgform select[data-ind]')) {
          const v = sel.value;
          const light = v === 'auto' ? 'auto' : v === 'none' ? null : { node: +v.split(':')[0], relay: +v.split(':')[1] };
          await api('PUT', `follow/${d.n}/${sel.dataset.ind}`, { light });
        }
      });
  },
  showkey: async () => {
    const k = await api('GET', 'secrets');
    $('#keybox').innerHTML = `<pre>${esc(k.secrets_h)}</pre>${k.generated ? '<p class="sub">Deze sleutel is zojuist door de add-on gemaakt. Bewaar hem veilig: elke node moet hem krijgen.</p>' : ''}`;
  },
  vnew: () => openVirtual(null),
  vimport: () => openImport(),
  vedit: (d) => openVirtual(+d.n),
  vaddgang: () => {
    V.gangs = readGangs();
    V.gangs.push({ entity: '', name: `Gang ${V.gangs.length + 1}`, lamp: true });
    $('#gangs').innerHTML = gangsHtml();
  },
  vdelgang: (d) => {
    V.gangs = readGangs();
    V.gangs.splice(+d.i, 1);
    $('#gangs').innerHTML = gangsHtml();
  },
  vdelete: async () => {
    if (!confirm('Dit externe apparaat verwijderen? Zijn lichten en schakelaar-koppelingen verdwijnen uit het netwerk.')) return;
    await api('DELETE', `virtual/${V.id}`);
    dlg().close();
    await refresh();
  },
  dlgcancel: () => dlg().close(),
  dlgok: async () => { await smallDialog.ok(); dlg().close(); await refresh(); },
};

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-act]');
  if (el && ACTIONS[el.dataset.act]) {
    ev.preventDefault();
    act(ACTIONS[el.dataset.act])(el.dataset);
  }
  const t = ev.target.closest('#tabs button');
  if (t) { tab = t.dataset.tab; render(); }
});

document.addEventListener('input', (ev) => {
  if (ev.target.id !== 'impq') return;
  const q = ev.target.value.trim().toLowerCase();
  document.querySelectorAll('#implist .imp').forEach((r) => { r.hidden = !!q && !r.dataset.q.includes(q); });
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (t.dataset && t.dataset.pick && L) L.picks.get(t.dataset.pick).checked = t.checked;
  if (t.dataset && t.dataset.mode && L) L.picks.get(t.dataset.mode).mode = t.value;
  if (t.id === 'imp' && t.files[0]) {
    t.files[0].text().then((txt) => act(async () => { await api('POST', 'import', JSON.parse(txt)); toast('Configuratie geïmporteerd'); await refresh(); })());
  }
});

dlg().addEventListener('close', () => {
  if (L && !L.saved && S && S.learn) api('POST', 'learn/stop', {}).catch(() => {}); // Esc tijdens het leren: stoppen
  L = null;
});

// ---------------------------------------------------------------- data en live updates

let reqSeq = 0, appliedSeq = 0;
async function refresh() {
  const mine = ++reqSeq;
  try {
    const next = await api('GET', 'state');
    if (mine < appliedSeq) return; // een nieuwer antwoord was al binnen: dit is verouderd
    appliedSeq = mine;
    const keepPlan = (PL.busy || PL.drag) && S ? S.plan : null; // een eigen wijziging is onderweg: die wint
    S = next;
    if (keepPlan) S.plan = keepPlan;
    render();
  } catch (e) {
    $('#chips').innerHTML = '<span class="chip bad">geen verbinding met de add-on</span>';
  }
}

function connect() {
  const ws = new WebSocket(new URL('api/ws', location.href).href.replace(/^http/, 'ws'));
  ws.onmessage = (m) => {
    const e = JSON.parse(m.data).event;
    planEvent(e);
    if (S && e && ['btn', 'light', 'node', 'cfg', 'learn', 'cmd'].includes(e.t)) {
      S.log.push(e);
      if (S.log.length > 300) S.log.shift();
    }
    clearTimeout(tokenRefresh);
    tokenRefresh = setTimeout(refresh, 120); // gebundeld: een druk geeft meerdere events
  };
  ws.onclose = () => setTimeout(connect, 2000);
}

refresh();
connect();
setInterval(refresh, 20000); // vangnet als de websocket stil valt (bv. proxy)
