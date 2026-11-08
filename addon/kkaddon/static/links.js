'use strict';
// Instellingen → Andere installaties: de koppeling met de Huisconfig van een ander subnet (kkaddon/linkhub.py).
// Wordt vóór app.js geladen en gebruikt zijn globals (S, api, act, refresh, toast, esc) pas bij gebruik.

function lkHex() { // een nieuwe koppelsleutel: 32 willekeurige bytes
  const b = new Uint8Array(32);
  crypto.getRandomValues(b);
  return [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
}

function lkState(p) {
  if (p.state === 'verbonden') return `<span class="badge ok">verbonden${p.peer ? ` met ${esc((S.links.remote[p.peer] || {}).name || p.peer)}` : ''}</span>`;
  return `<span class="badge ${p.state === 'fout' ? 'bad' : ''}">${esc(p.state)}</span>${p.error ? ` <span class="sub">${esc(p.error)}</span>` : ''}`;
}

function viewLinks() {
  const lk = S.links, me = lk.me;
  const peers = lk.peers.map((p) => `<tr><td><code>${esc(p.host)}:${p.port}</code></td><td>${lkState(p)}</td>
      <td><button class="btn small danger" type="button" data-lk="rmpeer" data-host="${esc(p.host)}" data-port="${p.port}" title="Adres weghalen">✕</button></td></tr>`).join('')
    || '<tr><td colspan="3" class="sub">Nog geen adressen. Eén kant opgeven volstaat: de verbinding werkt in beide richtingen.</td></tr>';
  const kind = { lamp: 'Lamp', tuya: 'Tuya', gang: 'Gang (touch en relais)', charger: 'Laadpaal (met sessies)' };
  const known = Object.entries(lk.remote).map(([id, r]) => {
    const order = { charger: 0, tuya: 1, lamp: 2, gang: 3 };
    const offer = Object.entries(r.catalog || {}).sort(([, a], [, b]) => (order[a.kind] ?? 9) - (order[b.kind] ?? 9) || a.name.localeCompare(b.name, 'nl'));
    const used = offer.filter(([, o]) => o.used).length;
    const rows = offer.map(([obj, o]) => `<label style="display:flex;gap:8px;align-items:center;margin:2px 0;color:var(--ink);cursor:pointer">
        <input type="checkbox" data-lkuse="${esc(obj)}" data-peer="${esc(id)}" ${o.used ? 'checked' : ''}> ${esc(o.name)} <span class="sub">${kind[o.kind] || esc(o.kind)}${o.busy ? ' · bezet in een kring daar' : ''}</span></label>`).join('');
    return `<div class="tmd"><div class="row sp"><b>${esc(r.name)} <span class="sub">(${esc(id)})</span></b><span class="row">${r.online ? '<span class="badge ok">online</span>' : '<span class="badge">offline</span>'}
        ${r.online ? '' : `<button class="btn small danger" type="button" data-lk="forget" data-peer="${esc(id)}" title="Haar blokken weghalen">Vergeten</button>`}</span></div>
      <div class="sub">${offer.length ? `${used} van ${offer.length} in gebruik. Vink aan wat je hier wilt gebruiken: alleen dat wordt een blok, en alleen daarvan komt de stand over de lijn.` : 'Biedt nog niets aan.'}</div>
      ${rows}</div>`;
  }).join('');
  return `<h2>Andere installaties</h2>
    <p class="sub">Een Huisconfig in een ander subnet (met een eigen Home Assistant) biedt zijn lampen, gangen (touch en relais van een schakelaar), laadpalen en Tuya-toestellen aan, en omgekeerd. Je kiest per installatie wat je wilt gebruiken; dat verschijnt op het schema als blok dat je kunt bedienen, aan een kaart kunt hangen, of (een gang) op een lamp kunt aansluiten. Instellen doe je bij de installatie waar het toestel hangt.</p>
    <label for="lkname">Naam van deze installatie</label>
    <div class="row"><input type="text" id="lkname" maxlength="60" placeholder="bv. Hoofdhuis" value="${esc(me.name)}"><button class="btn small" type="button" data-lk="name">Opslaan</button></div>
    ${me.id ? `<div class="sub">Id: <code>${esc(me.id)}</code>. Die blijft, ook als je de naam verandert: de andere installaties verwijzen ermee naar deze.</div>` : '<div class="sub">Nodig om te koppelen. Elke installatie een andere naam.</div>'}
    <label for="lkkey">Koppelsleutel ${me.has_key ? '<span class="badge ok">ingesteld</span>' : '<span class="badge">niet ingesteld</span>'}</label>
    <div class="row"><input type="text" id="lkkey" spellcheck="false" autocomplete="off" placeholder="${me.has_key ? 'leeg laten om te behouden' : 'plak de sleutel van een andere installatie, of maak er een'}">
      <button class="btn small" type="button" data-lk="newkey">Nieuwe sleutel</button><button class="btn small" type="button" data-lk="key">Opslaan</button>
      ${me.has_key ? '<button class="btn small danger" type="button" data-lk="clearkey">Wissen</button>' : ''}</div>
    <div class="sub">Alle installaties van het huis gebruiken dezelfde sleutel. Maak hem op één installatie, kopieer hem vóór je opslaat, en plak hem bij de andere. De add-on toont hem daarna niet meer.</div>
    <p class="sub">Deze installatie luistert op TCP-poort <b>${me.port}</b>${me.listening ? '' : ` — <span style="color:var(--warn)">${esc(me.error || 'niet actief')}</span>`}. Die poort moet open staan tussen de subnetten; je kiest hem in de opties van de add-on.</p>
    <label>Adressen</label>
    <div class="scroll"><table><thead><tr><th>Home Assistant</th><th>Stand</th><th></th></tr></thead><tbody>${peers}</tbody></table></div>
    <div class="row" style="margin-top:6px"><input type="text" id="lkhost" spellcheck="false" autocomplete="off" placeholder="IP-adres, bv. 192.168.20.10" style="flex:1;min-width:180px"><input type="number" id="lkport" min="1" max="65535" value="47490" style="width:90px">
      <button class="btn small" type="button" data-lk="addpeer">Toevoegen</button></div>
    ${known ? `<label>Wat je van andere installaties gebruikt</label>${known}` : ''}`;
}

function linksUpdate() { // de stand van de verbindingen verandert: bijwerken, behalve terwijl je in een veld typt
  const box = $('#linkbox');
  if (!box || (document.activeElement && box.contains(document.activeElement) && /INPUT/.test(document.activeElement.tagName))) return;
  box.innerHTML = viewLinks();
}

const LK_ACTIONS = {
  name: () => api('PUT', 'link', { name: $('#lkname').value.trim() }),
  newkey: () => { $('#lkkey').value = lkHex(); $('#lkkey').select(); toast('Kopieer de sleutel nu: na het opslaan toont de add-on hem niet meer.', 6000); },
  key: async () => {
    const key = $('#lkkey').value.trim();
    if (!key) throw new Error('Plak of maak eerst een sleutel.');
    await api('PUT', 'link', { key });
    toast('Koppelsleutel opgeslagen');
  },
  clearkey: async () => {
    if (!confirm('De koppelsleutel wissen? Alle koppelingen met andere installaties stoppen.')) return;
    await api('PUT', 'link', { key: null });
  },
  addpeer: async () => {
    await api('POST', 'link/peers', { host: $('#lkhost').value.trim(), port: +$('#lkport').value || 47490 });
    $('#lkhost').value = '';
  },
  rmpeer: (d) => api('POST', 'link/peers/remove', { host: d.host, port: +d.port }),
  forget: async (d) => {
    if (!confirm('Deze installatie vergeten? Haar blokken verdwijnen van het schema; kaarten die haar toestellen gebruikten tonen ze als weg.')) return;
    await api('POST', 'link/forget', { peer: d.peer });
  },
};

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (!t.dataset || t.dataset.lkuse === undefined) return;
  const peer = t.dataset.peer, obj = t.dataset.lkuse, use = t.checked, ref = `r:${peer}:${obj}`;
  const cards = (S.schema.blocks || []).filter((c) => c.kind === 'card' && ((c.items || []).some((it) => it.ref === ref) || c.charger === ref));
  if (!use && cards.length && !confirm(`Niet meer gebruiken? ${cards.map((c) => c.name).join(', ')} ${cards.length === 1 ? 'toont' : 'tonen'} het dan als weg.`)) { t.checked = true; return; }
  t.blur();
  act(async () => {
    try { await api('POST', 'link/use', { peer, obj, use }); } finally { await refresh(); const box = $('#linkbox'); if (box) box.innerHTML = viewLinks(); }
  })();
});

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-lk]');
  if (!el || !LK_ACTIONS[el.dataset.lk]) return;
  ev.preventDefault();
  act(async () => {
    await LK_ACTIONS[el.dataset.lk](el.dataset);
    await refresh();
    const box = $('#linkbox');
    if (box) box.innerHTML = viewLinks();
  })();
});
