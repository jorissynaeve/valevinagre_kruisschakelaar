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
  const dir = s.dir === 'out' ? '<span class="badge">→ Loxone</span>' : '<span class="badge">← Loxone</span>';
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
  return `<details class="lxgroup" data-lxg="${esc(g.id)}" ${LX_OPEN.has(g.id) ? 'open' : ''}>
    <summary><input type="checkbox" data-lxpick="${esc(g.id)}" ${picked ? 'checked' : ''} title="Dit apparaat in de bestanden voor Loxone Config">
      <b>${esc(g.name)}</b> <span class="sub">${nOut} naar Loxone, ${nIn} van Loxone</span></summary>
    <div class="scroll"><table><thead><tr><th></th><th>Bericht</th><th>Bron</th><th>Waarde</th><th>Aan</th><th></th></tr></thead><tbody>${g.sigs.map(loxSigRow).join('')}</tbody></table></div>
  </details>`;
}

function viewLox() {
  if (!LX) return '<h2>Loxone</h2><p class="sub">Laden…</p>';
  if (LX.error) return `<h2>Loxone</h2><p class="sub">${esc(LX.error)}</p>`;
  const groups = loxGroups();
  const rows = groups.map(loxGroup).join('') || '<p class="sub">Nog geen signalen. Kies hieronder een apparaat.</p>';
  const none = LX_PICK && LX_PICK.size === 0;
  const q = loxQuery();
  const devs = (S.espdev || []).map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  const st = LX.stats;
  const ready = LX.miniserver && LX.me;
  return `<h2>Loxone</h2>
    <p class="sub">Vervangt JavaLox. Elk signaal is één kort bericht <code>kk.&lt;naam&gt;=&lt;getal&gt;</code>: een aan/uit is 1 of 0, een keuze (scene, stand) is het nummer ervan. Naar Loxone gaat het als UDP naar één virtuele UDP-ingang;
      van Loxone komt het binnen via één virtuele uitgang. Beide maakt deze pagina als bestand voor Loxone Config, dus je typt geen commando's over.</p>
    <div class="row"><div style="flex:1;min-width:170px"><label for="lxmini">IP van de Miniserver</label><input type="text" id="lxmini" placeholder="192.168.1.112" value="${esc(LX.miniserver)}"></div>
      <div style="width:150px"><label for="lxout">Poort van zijn UDP-ingang</label><input type="number" id="lxout" min="1" max="65535" value="${LX.out_port}"></div>
      <div style="flex:1;min-width:170px"><label for="lxhost">IP van deze add-on</label><input type="text" id="lxhost" placeholder="${esc(LX.me || 'bv. 192.168.1.5')}" value="${esc(LX.addon_host)}"></div>
      <div style="width:200px"><label for="lxtr">Loxone roept de add-on aan via</label><select id="lxtr"><option value="http" ${LX.transport === 'http' ? 'selected' : ''}>HTTP (met bevestiging)</option><option value="udp" ${LX.transport === 'udp' ? 'selected' : ''}>UDP</option></select></div>
      <button class="btn small" type="button" data-lx="save">Opslaan</button></div>
    <p class="sub">De add-on luistert op poort <b>${LX.port}</b> (UDP en HTTP)${LX.listen_error ? ` — <span style="color:var(--warn)">${esc(LX.listen_error)}</span>` : (LX.udp || LX.http ? '' : ' — <span style="color:var(--warn)">niet actief</span>')}. Alleen berichten van het IP van de Miniserver worden aangenomen.
      Verstuurd: ${st.sent}, ontvangen: ${st.received}, geweigerd: ${st.refused}, onbekend: ${st.unknown}${LX.queue ? `, in de rij: ${LX.queue}` : ''}.</p>
    <div class="row"><span class="sub">${groups.length} apparaten, ${LX.signals.length} signalen. Het vinkje bij een apparaat bepaalt wat in de bestanden voor Loxone Config komt.</span>
      <button class="btn small" type="button" data-lx="openall">Alles open</button><button class="btn small" type="button" data-lx="closeall">Alles dicht</button>
      <button class="btn small" type="button" data-lx="pickall">Alle apparaten kiezen</button><button class="btn small" type="button" data-lx="picknone">Geen kiezen</button></div>
    <div id="lxgroups">${rows}</div>
    <label>Signalen van een apparaat maken</label>
    <div class="row"><select id="lxdev">${devs || '<option value="">Nog geen apparaten</option>'}</select>
      <input type="text" id="lxpre" maxlength="24" placeholder="voorvoegsel, bv. tuin" style="width:200px">
      <button class="btn small" type="button" data-lx="adddev" ${devs ? '' : 'disabled'}>Signalen maken</button></div>
    <div class="sub">De punten die op het blok van het apparaat staan, gaan naar Loxone; wat instelbaar is kan Loxone ook zetten (<code>…_set</code>); <code>…_ok</code> meldt of het apparaat bereikbaar is.
      Een gevaarlijk punt (zoals een deur) kan Loxone lezen, nooit zetten. Wil je andere punten, zet ze eerst op het blok van het apparaat.</div>
    <label>Voor Loxone Config</label>
    <div class="sub">${LX_PICK ? `Gekozen: ${LX_PICK.size} van ${groups.length} apparaten.` : 'Alle apparaten.'}${none ? ' Kies eerst minstens één apparaat.' : ''}</div>
    <div class="row">${ready && !none ? `<a class="btn" href="api/loxone/viu.xml${q}" download>Virtuele ingang (VIU)</a><a class="btn" href="api/loxone/vo.xml${q}" download>Virtuele uitgang (VO)</a>
      <a class="btn" href="api/loxone/table.txt${q}" target="_blank" rel="noopener">Tabel om over te typen</a>` : `<span class="sub">${ready ? '' : 'Vul eerst het IP van de Miniserver in.'}</span>`}
      <button class="btn" type="button" data-lx="sync" title="Alle waarden meteen opnieuw naar Loxone sturen">Alles opnieuw sturen</button></div>
    <div class="sub">Importeer de bestanden in Loxone Config als sjabloon, op dezelfde plek als waar je er een exporteerde. Een sjabloon dat je al in je project hebt, werkt Config niet bij door het opnieuw te importeren.
      Maak je later signalen bij, kies dan alleen dat apparaat en voeg het bestand toe als aparte ingang en uitgang (de titel noemt het apparaat), of typ de nieuwe commando's over uit de tabel.
      Loxone kan zelf alles opnieuw opvragen: stuur <code>kk.sync=1</code>.</div>`;
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
    const r = await api('POST', 'loxone/device', { did, prefix: $('#lxpre').value.trim() || loxSlug(dev && dev.name) });
    toast(r.made.length ? `${r.made.length} signalen gemaakt` : 'Er waren al signalen met deze namen');
  },
  sync: async () => { await api('POST', 'loxone/sync'); toast('Alles wordt opnieuw gestuurd'); },
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
