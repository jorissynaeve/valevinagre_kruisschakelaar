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

function viewLox() {
  if (!LX) return '<h2>Loxone</h2><p class="sub">Laden…</p>';
  if (LX.error) return `<h2>Loxone</h2><p class="sub">${esc(LX.error)}</p>`;
  const rows = LX.signals.map((s) => {
    const dir = s.dir === 'out' ? '<span class="badge">→ Loxone</span>' : '<span class="badge">← Loxone</span>';
    const last = s.dir === 'out'
      ? (s.last ? `verstuurd ${ago(s.last)}` : (LX.miniserver ? 'nog niet verstuurd' : ''))
      : (s.last ? `ontvangen ${ago(s.last)}: ${esc(String(s.got))} → ${esc(s.result || '')}` : 'nog niets ontvangen');
    return `<tr><td>${dir}</td><td><code>${esc(s.key)}</code></td><td>${esc(s.label || s.ref)}<div class="sub">${esc(s.ref)}</div></td>
      <td>${loxValue(s)}<div class="sub">${last}</div></td>
      <td><input type="checkbox" data-lxon="${esc(s.id)}" ${s.enabled ? 'checked' : ''} title="Aan of uit"></td>
      <td><button class="btn small danger" type="button" data-lx="rm" data-id="${esc(s.id)}" title="Signaal weghalen">✕</button></td></tr>`;
  }).join('') || '<tr><td colspan="6" class="sub">Nog geen signalen. Kies hieronder een apparaat.</td></tr>';
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
    <div class="scroll"><table><thead><tr><th></th><th>Bericht</th><th>Bron</th><th>Waarde</th><th>Aan</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <label>Signalen van een apparaat maken</label>
    <div class="row"><select id="lxdev">${devs || '<option value="">Nog geen apparaten</option>'}</select>
      <input type="text" id="lxpre" maxlength="24" placeholder="voorvoegsel, bv. tuin" style="width:200px">
      <button class="btn small" type="button" data-lx="adddev" ${devs ? '' : 'disabled'}>Signalen maken</button></div>
    <div class="sub">De punten die op het blok van het apparaat staan, gaan naar Loxone; wat instelbaar is kan Loxone ook zetten (<code>…_set</code>); <code>…_ok</code> meldt of het apparaat bereikbaar is.
      Een gevaarlijk punt (zoals een deur) kan Loxone lezen, nooit zetten. Wil je andere punten, zet ze eerst op het blok van het apparaat.</div>
    <label>Voor Loxone Config</label>
    <div class="row">${ready ? `<a class="btn" href="api/loxone/viu.xml" download>Virtuele ingang (VIU_Huisconfig.xml)</a><a class="btn" href="api/loxone/vo.xml" download>Virtuele uitgang (VO_Huisconfig.xml)</a>
      <a class="btn" href="api/loxone/table.txt" target="_blank" rel="noopener">Tabel om over te typen</a>` : '<span class="sub">Vul eerst het IP van de Miniserver in.</span>'}
      <button class="btn" type="button" data-lx="sync" title="Alle waarden meteen opnieuw naar Loxone sturen">Alles opnieuw sturen</button></div>
    <div class="sub">Importeer de twee bestanden in Loxone Config als sjabloon, op dezelfde plek als waar je er een exporteerde. Maak je later signalen bij, importeer dan opnieuw (een bestaande ingang vervang je).
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
};

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-lx]');
  if (!el || !LX_ACTIONS[el.dataset.lx]) return;
  ev.preventDefault();
  act(async () => { await LX_ACTIONS[el.dataset.lx](el.dataset); await loxLoad(); })();
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (!t.dataset || t.dataset.lxon === undefined) return;
  t.blur();
  act(async () => { try { await api('PUT', `loxone/signals/${encodeURIComponent(t.dataset.lxon)}`, { enabled: t.checked }); } finally { await loxLoad(); } })();
});

setInterval(() => { if ($('#loxbox')) loxLoad(); }, 5000); // de waarden en tellers volgen
