'use strict';
// Instellingen → Loxone: de brug naar een Loxone Miniserver (kkaddon/loxonehub.py), in plaats van JavaLox.
// Wordt vóór app.js geladen en gebruikt zijn globals ($, S, api, act, toast, esc, ago) pas bij gebruik.

let LX = null; // wat GET api/loxone gaf, of {error}

async function loxLoad() {
  try { LX = await api('GET', 'loxone'); } catch (e) { LX = { error: e.message }; }
  const box = $('#loxbox');
  if (box && !(document.activeElement && box.contains(document.activeElement) && /INPUT|SELECT/.test(document.activeElement.tagName))) box.innerHTML = viewLox();
}

function loxSlug(name) {
  return String(name || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 20) || 'apparaat';
}

function loxValue(s) {
  if (s.action) return s.danger ? '<span style="color:var(--warn)">opent de deur</span>' : '<span class="sub">actie</span>';
  if (s.value === null || s.value === undefined) return '<span class="sub">onbekend</span>';
  if (s.kind === 'bool') return s.value ? 'aan (1)' : 'uit (0)';
  return esc(String(s.kind === 'float' ? (+s.value).toFixed(s.decimals) : s.value));
}

// Welke groepen (apparaten) open staan en welke in het bestand komen. Open: onthouden in de browser. Bestand: null = alles (ook nieuwe apparaten), anders een Set.
let LX_OPEN = new Set();
let LX_PICK = null;
try { LX_OPEN = new Set(JSON.parse(localStorage.getItem('kk.lxopen') || '[]')); } catch (e) { /* zonder opslag: dicht */ }

function loxGroups() {
  const g = new Map();
  for (const s of LX.signals) {
    if (!g.has(s.group)) g.set(s.group, { id: s.group, name: s.group_name, sigs: [] });
    g.get(s.group).sigs.push(s);
  }
  return [...g.values()]; // de server stuurt ze al op naam van het apparaat
}

// De query voor de downloads: leeg (alles) of ?devices=a,b
function loxQuery() {
  return LX_PICK ? `?devices=${[...LX_PICK].map(encodeURIComponent).join(',')}` : '';
}

function loxSigRow(s) {
  const via = s.dir === 'out' ? 'UDP' : String(LX.transport || 'http').toUpperCase(); // naar Loxone altijd UDP; van Loxone volgt de instelling bovenaan
  const dir = `<span class="badge">${s.dir === 'out' ? '→' : '←'} Loxone</span><div class="sub">${via}</div>`;
  const last = s.dir === 'out'
    ? (s.last ? `verstuurd ${ago(s.last)}` : (LX.miniserver ? 'nog niet verstuurd' : ''))
    : (s.last ? `ontvangen ${ago(s.last)}: ${esc(String(s.got))} → ${esc(s.result || '')}` : 'nog niets ontvangen');
  return `<tr><td>${dir}</td><td><code>${esc(s.key)}</code></td><td>${esc(s.label || s.ref)}<div class="sub">${esc(s.ref)}</div></td>
      <td>${loxValue(s)}<div class="sub">${last}</div></td>
      <td><input type="checkbox" data-lxon="${esc(s.id)}" ${s.enabled ? 'checked' : ''} title="Aan of uit"></td>
      <td><button class="btn small danger" type="button" data-lx="rm" data-id="${esc(s.id)}" title="Signaal weghalen">✕</button></td></tr>`;
}

function loxGroup(g) {
  const nOut = g.sigs.filter((s) => s.dir === 'out').length;
  const nIn = g.sigs.length - nOut;
  const picked = !LX_PICK || LX_PICK.has(g.id);
  const off = g.sigs.filter((s) => !s.enabled).length;
  return `<details class="lxgroup" data-lxg="${esc(g.id)}" ${LX_OPEN.has(g.id) ? 'open' : ''}>
    <summary><input type="checkbox" data-lxpick="${esc(g.id)}" ${picked ? 'checked' : ''} title="Dit apparaat in de bestanden voor Loxone Config">
      <span class="nm">${esc(g.name)}</span><span class="sub">${nOut ? `${nOut} → Loxone` : ''}${nOut && nIn ? ' · ' : ''}${nIn ? `${nIn} ← Loxone` : ''}${off ? ` · ${off} uit` : ''}</span>
      ${LX.channels.length > 1 ? `<select data-lxch="${esc(g.id)}" title="In welk kanaal (virtuele ingang en uitgang in Loxone) dit apparaat zit" aria-label="Kanaal">${LX.channels.map((c) => `<option value="${esc(c.id)}" ${c.id === g.sigs[0].channel ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select>` : ''}</summary>
    <div class="scroll"><table><thead><tr><th>Richting</th><th>Bericht</th><th>Bron</th><th>Waarde</th><th>Aan</th><th></th></tr></thead><tbody>${g.sigs.map(loxSigRow).join('')}</tbody></table></div>
  </details>`;
}

function viewLox() {
  if (!LX) return '<div class="panel"><h3>Loxone</h3><p class="lead">Laden…</p></div>';
  if (LX.error) return `<div class="panel"><h3>Loxone</h3><p class="lead">${esc(LX.error)}</p></div>`;
  const groups = loxGroups();
  const rows = groups.map(loxGroup).join('') || '<p class="lead">Nog geen signalen. Kies hieronder een apparaat.</p>';
  const devs =(S.espdev || []).map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  const st = LX.stats;
  const listening = LX.listen_error ? `<span class="badge bad">${esc(LX.listen_error)}</span>`
    : LX.udp || LX.http ? `<span class="badge ok">luistert op poort ${LX.port}</span>` : '<span class="badge warn">luistert niet</span>';
  return `<div class="panel">
      <div class="phead"><h3>Verbinding met de Miniserver</h3>${listening}</div>
      <p class="lead">Vervangt JavaLox. Elk signaal is één kort bericht <code>kk.&lt;naam&gt;=&lt;getal&gt;</code>: een aan/uit is 1 of 0, een keuze (scene, stand) is het nummer ervan.
        Naar Loxone gaat het als UDP naar één virtuele UDP-ingang; van Loxone komt het binnen via één virtuele uitgang.</p>
      <div class="fgrid">
        <div><label for="lxmini">IP van de Miniserver</label><input type="text" id="lxmini" placeholder="192.168.1.112" value="${esc(LX.miniserver)}"></div>
        <div><label for="lxout">Poort van zijn UDP-ingang</label><input type="number" id="lxout" min="1" max="65535" value="${LX.out_port}"></div>
        <div><label for="lxhost">IP van deze add-on</label><input type="text" id="lxhost" placeholder="${esc(LX.me || 'bv. 192.168.1.5')}" value="${esc(LX.addon_host)}"></div>
        <div><label for="lxtr">Loxone roept de add-on aan via</label><select id="lxtr"><option value="http" ${LX.transport === 'http' ? 'selected' : ''}>HTTP (met bevestiging)</option><option value="udp" ${LX.transport === 'udp' ? 'selected' : ''}>UDP</option></select></div>
      </div>
      <div class="row sp"><div class="stats"><span class="stat">Verstuurd<b>${st.sent}</b></span><span class="stat">Ontvangen<b>${st.received}</b></span>
          <span class="stat ${st.refused ? 'bad' : ''}">Geweigerd<b>${st.refused}</b></span><span class="stat">Onbekend<b>${st.unknown}</b></span>${LX.queue ? `<span class="stat">In de rij<b>${LX.queue}</b></span>` : ''}</div>
        <button class="btn primary" type="button" data-lx="save">Opslaan</button></div>
      <div class="note"><span>ⓘ</span><span>De add-on luistert op poort <b>${LX.port}</b> (UDP en HTTP) en neemt alleen berichten aan van het IP van de Miniserver.</span></div>
    </div>

    ${viewLoxChannels(groups)}

    <div class="panel">
      <div class="phead"><h3>Signalen</h3><span class="sub">${groups.length} apparaten · ${LX.signals.length} signalen</span></div>
      <div class="toolbar"><span class="sub">Klap een apparaat open voor zijn signalen. Het vinkje bepaalt wat in de bestanden voor Loxone Config komt.</span>
        <button class="btn small" type="button" data-lx="openall">Alles open</button><button class="btn small" type="button" data-lx="closeall">Alles dicht</button></div>
      <div id="lxgroups">${rows}</div>
      <h4>Signalen van een apparaat maken</h4>
      <div class="row"><select id="lxdev">${devs || '<option value="">Nog geen apparaten</option>'}</select>
        <input type="text" id="lxpre" maxlength="24" placeholder="voorvoegsel, bv. tuin">
        ${LX.channels.length > 1 ? `<select id="lxdevch" aria-label="Kanaal">${LX.channels.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select>` : ''}
        <button class="btn" type="button" data-lx="adddev" ${devs ? '' : 'disabled'}>Signalen maken</button></div>
      <p class="sub" style="margin:8px 0 0">De punten die op het blok van het apparaat staan, gaan naar Loxone; wat instelbaar is kan Loxone ook zetten (<code>…_set</code>); <code>…_ok</code> meldt of het apparaat bereikbaar is.
        Een gevaarlijk punt (zoals een deur) kan Loxone lezen, nooit zetten. Wil je andere punten, zet ze eerst op het blok van het apparaat.</p>
    </div>

    ${viewLoxLock()}

    `;
}

// De downloads van een kanaal, met de keuze van apparaten (vinkjes bij Signalen) erbij.
function loxHref(file, cid) {
  const p = [`channel=${encodeURIComponent(cid)}`];
  if (LX_PICK) p.push(`devices=${[...LX_PICK].map(encodeURIComponent).join(',')}`);
  return `api/loxone/${file}?${p.join('&')}`;
}

// Kanalen: elk is één virtuele UDP-ingang plus één virtuele uitgang in Loxone, met een eigen poort. Een nieuw apparaat in een nieuw kanaal is een nieuw bestand om te importeren,
// zonder iets te veranderen aan wat er al in Loxone Config staat.
function viewLoxChannels(groups) {
  const ready = LX.miniserver && LX.me;
  const none = LX_PICK && LX_PICK.size === 0;
  const dl = (file, label, n, cid, cls = '') => (ready && !none && n ? `<a class="btn small ${cls}" href="${loxHref(file, cid)}" download>${label}</a>` : `<button class="btn small" type="button" disabled>${label}</button>`);
  const rows = LX.channels.map((c) => `<tr>
      <td>${c.default ? '<b>Standaard</b>' : `<input type="text" data-chname="${esc(c.id)}" maxlength="30" value="${esc(c.name)}" aria-label="Naam van het kanaal">`}</td>
      <td>${c.default ? `${c.port} <span class="sub">(zie hierboven)</span>` : `<input type="number" data-chport="${esc(c.id)}" min="1" max="65535" value="${c.port}" aria-label="Poort van de virtuele ingang">`}</td>
      <td>${c.devices.length ? esc(c.devices.join(', ')) : '<span class="sub">nog geen apparaten</span>'}<div class="sub">${c.n_out} → Loxone · ${c.n_in} ← Loxone</div></td>
      <td><div class="actions">${dl('viu.xml', 'VIU', c.n_out, c.id, 'primary')}${dl('vo.xml', 'VO', c.n_in, c.id, 'primary')}
        <a class="btn small" href="${loxHref('table.txt', c.id)}" target="_blank" rel="noopener">Tabel</a></div></td>
      <td>${c.default ? '' : `<div class="actions"><button class="btn small" type="button" data-lx="savech" data-id="${esc(c.id)}">Opslaan</button>
        <button class="btn small danger" type="button" data-lx="rmch" data-id="${esc(c.id)}" title="Kanaal weghalen">✕</button></div>`}</td></tr>`).join('');
  const nextPort = Math.max(LX.out_port, ...LX.channels.map((c) => c.port)) + 1;
  return `<div class="panel">
      <div class="phead"><h3>Kanalen</h3><span class="sub">${LX.channels.length} ${LX.channels.length === 1 ? 'kanaal' : 'kanalen'}</span></div>
      <p class="lead">Een kanaal is één virtuele UDP-ingang plus één virtuele uitgang in Loxone, met een eigen poort. Zet een apparaat bij Signalen in een kanaal, en importeer het bestand van dat kanaal in Loxone Config.
        Bij nieuwe apparaten maak je een nieuw kanaal: Config werkt een bestaand sjabloon niet bij en twee ingangen op dezelfde poort kunnen niet, maar een extra kanaal is gewoon een extra import.</p>
      <div class="scroll"><table><thead><tr><th>Kanaal</th><th>Poort van de ingang</th><th>Apparaten</th><th title="VIU: virtuele UDP-ingang (naar Loxone). VO: virtuele uitgang (van Loxone).">Bestanden (VIU · VO)</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <h4>Een kanaal toevoegen</h4>
      <div class="row"><input type="text" id="lxchname" maxlength="30" placeholder="naam, bv. Tuin"><input type="number" id="lxchport" min="1" max="65535" value="${nextPort}" aria-label="Poort van de nieuwe virtuele ingang">
        <button class="btn" type="button" data-lx="addch">Kanaal toevoegen</button></div>
      <div class="toolbar" style="margin:12px 0 0"><span class="sub">${LX_PICK ? `Alleen ${LX_PICK.size} van ${groups.length} apparaten (vinkjes bij Signalen) komen in de bestanden.` : 'Alle apparaten van het kanaal komen in zijn bestanden; haal een vinkje bij Signalen weg om er één weg te laten.'}</span>
        <button class="btn small" type="button" data-lx="pickall">Alle apparaten</button><button class="btn small" type="button" data-lx="picknone">Geen</button></div>
      <div class="note"><span>ⓘ</span><span>Een signaal dat je later bij een bestaand apparaat maakt, typ je over uit de tabel van dat kanaal: commando's toevoegen aan een bestaande ingang of uitgang via een bestand lukt niet.</span></div>
      <div class="row" style="margin-top:12px"><button class="btn" type="button" data-lx="sync" title="Alle waarden meteen opnieuw naar Loxone sturen">Alles opnieuw sturen</button>
        <span class="sub">Loxone kan dat zelf vragen met <code>kk.sync=1</code>.</span></div>
    </div>`;
}

// Een slot bedienen vanuit Loxone: een signaal per actie. Alleen een Nuki-slot, alleen een actie zonder argument.
function loxLockActions() {
  const out = [];
  for (const d of S.espdev || []) {
    if (d.driver !== 'nuki') continue;
    for (const a of d.actions || []) if (!(a.args && a.args.length)) out.push({ did: d.id, dev: d.name, aid: a.id, label: a.label, danger: !!a.danger });
  }
  return out;
}

function viewLoxLock() {
  const acts = loxLockActions();
  if (!acts.length) return '';
  const opts = acts.map((a) => `<option value="${esc(a.did)}|${esc(a.aid)}">${esc(a.dev)}: ${esc(a.label)}${a.danger ? ' (opent de deur)' : ''}</option>`).join('');
  return `<div class="panel">
      <div class="phead"><h3>Een slot bedienen vanuit Loxone</h3></div>
      <p class="lead">Een signaal per actie, bijvoorbeeld <code>voordeur_lock</code> en <code>voordeur_unlock</code>.</p>
      <div class="row"><select id="lxact">${opts}</select>
        <input type="text" id="lxactname" maxlength="30" placeholder="naam, bv. voordeur">
        <button class="btn" type="button" data-lx="addact">Signaal maken</button></div>
      <label class="chk" style="display:flex;gap:8px;align-items:flex-start;color:var(--ink);margin-top:12px"><input type="checkbox" id="lxactack" style="margin-top:3px"> <span>Ik begrijp dat Loxone hiermee de deur kan <b>ontgrendelen</b> (nodig voor een actie die de deur opent).</span></label>
      <div class="note warn"><span>⚠</span><span>Loxone moet een puls sturen: 1 en daarna weer 0 (de uitgang in het bestand doet dat bij een digitale uitgang). Een actie die de deur opent start alleen bij de overgang van 0 naar 1, alleen over HTTP,
        hoogstens eens per 5 s en alleen als het slot bereikbaar is; zonder die overgang (bv. na een herstart van de Miniserver) gebeurt er niets. Elke start staat in het logboek van de add-on.</span></div>
    </div>`;
}

const LX_ACTIONS = {
  save: () => api('PUT', 'loxone', { miniserver: $('#lxmini').value.trim(), out_port: +$('#lxout').value, addon_host: $('#lxhost').value.trim(), transport: $('#lxtr').value }),
  rm: async (d) => {
    if (!confirm(`Het signaal ${d.id} weghalen? Zet het ook weg in Loxone Config.`)) return;
    await api('DELETE', `loxone/signals/${encodeURIComponent(d.id)}`);
  },
  adddev: async () => {
    const did = $('#lxdev').value;
    const dev = (S.espdev || []).find((x) => x.id === did);
    const r = await api('POST', 'loxone/device', { did, prefix: $('#lxpre').value.trim() || loxSlug(dev && dev.name), channel: ($('#lxdevch') || {}).value || '' });
    toast(r.made.length ? `${r.made.length} signalen gemaakt` : 'Er waren al signalen met deze namen');
  },
  sync: async () => { await api('POST', 'loxone/sync'); toast('Alles wordt opnieuw gestuurd'); },
  addact: async () => {
    const [did, aid] = ($('#lxact').value || '|').split('|');
    const a = loxLockActions().find((x) => x.did === did && x.aid === aid);
    if (!a) return;
    const name = loxSlug($('#lxactname').value.trim() || a.dev);
    await api('POST', 'loxone/signals', { id: `${name}_${aid}`.slice(0, 40), dir: 'in', ref: `a:${did}:${aid}`, label: `${a.dev}: ${a.label}`, ack: $('#lxactack').checked });
    toast('Signaal gemaakt');
  },
  addch: async () => {
    await api('POST', 'loxone/channels', { name: $('#lxchname').value.trim(), port: +$('#lxchport').value });
    toast('Kanaal gemaakt: zet er een apparaat in en importeer zijn bestanden');
  },
  savech: async (d) => {
    const q = (a) => document.querySelector(`[data-${a}="${CSS.escape(d.id)}"]`);
    await api('PUT', `loxone/channels/${encodeURIComponent(d.id)}`, { name: q('chname').value.trim(), port: +q('chport').value });
    toast('Opgeslagen. Wijzigde de poort, pas hem dan ook aan in Loxone Config.');
  },
  rmch: async (d) => {
    if (!confirm('Dit kanaal weghalen? Haal ook zijn virtuele ingang en uitgang weg in Loxone Config.')) return;
    await api('DELETE', `loxone/channels/${encodeURIComponent(d.id)}`);
  },
  openall: () => loxOpen(loxGroups().map((g) => g.id)),
  closeall: () => loxOpen([]),
  pickall: () => { LX_PICK = null; },
  picknone: () => { LX_PICK = new Set(); },
};

function loxOpen(ids) {
  LX_OPEN = new Set(ids);
  try { localStorage.setItem('kk.lxopen', JSON.stringify([...LX_OPEN])); } catch (e) { /* zonder opslag: niet onthouden */ }
}

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-lx]');
  if (!el || !LX_ACTIONS[el.dataset.lx]) return;
  ev.preventDefault();
  act(async () => { await LX_ACTIONS[el.dataset.lx](el.dataset); await loxLoad(); })();
});

// Een groep open of dicht: onthouden, zodat de verversing hem niet terugzet.
document.addEventListener('toggle', (ev) => {
  const d = ev.target;
  if (!d.dataset || d.dataset.lxg === undefined) return;
  if (d.open) LX_OPEN.add(d.dataset.lxg); else LX_OPEN.delete(d.dataset.lxg);
  loxOpen([...LX_OPEN]);
}, true);

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (!t.dataset) return;
  if (t.dataset.lxch !== undefined) { // een apparaat naar een ander kanaal
    t.blur();
    act(async () => {
      try {
        await api('POST', 'loxone/assign', { group: t.dataset.lxch, channel: t.value });
        toast('Verhuisd. Importeer de bestanden van dat kanaal in Loxone Config (en haal de commando\'s bij het oude kanaal weg).');
      } finally { await loxLoad(); }
    })();
    return;
  }
  if (t.dataset.lxpick !== undefined) { // een apparaat in of uit de bestanden
    const all = loxGroups().map((g) => g.id);
    const cur = new Set(LX_PICK || all);
    if (t.checked) cur.add(t.dataset.lxpick); else cur.delete(t.dataset.lxpick);
    LX_PICK = all.every((id) => cur.has(id)) ? null : cur; // alles gekozen = "alles", dus ook nieuwe apparaten
    t.blur();
    const box = $('#loxbox');
    if (box) box.innerHTML = viewLox();
    return;
  }
  if (t.dataset.lxon === undefined) return;
  t.blur();
  act(async () => { try { await api('PUT', `loxone/signals/${encodeURIComponent(t.dataset.lxon)}`, { enabled: t.checked }); } finally { await loxLoad(); } })();
});

setInterval(() => { if ($('#loxbox')) loxLoad(); }, 5000); // de waarden en tellers volgen
