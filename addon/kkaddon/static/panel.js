'use strict';
// Tabblad "Paneel": de NSPanel(s) van het huis. Config in het YAML-formaat van nspanel-lovelace-ui, plus status en relais.

let PN = null; // {panels: [...], udp_port}

async function panelLoad() {
  try { PN = await api('GET', 'panels'); } catch (e) { PN = { panels: [], udp_port: 0, error: e.message }; }
  if (tab === 'panel') panelDraw();
}

function viewPanel() {
  if (!PN) panelLoad();
  const hub = esc(location.hostname);
  return `<div id="panelview">
    <h2>NSPanel</h2>
    <p class="sub">Het scherm van de panel blijft zoals het is (de TFT van nspanel-lovelace-ui). Wat er op moet staan komt hier, uit dezelfde YAML als in AppDaemon,
    en de panel praat rechtstreeks met deze add-on in plaats van via MQTT en AppDaemon. De twee relais en de temperatuur komen als objecten in Home Assistant.</p>
    <div id="pnlist"></div>
    <details id="pnnew"><summary class="btn">Paneel toevoegen…</summary>
      <div class="card">
        <label>Naam <input type="text" id="pn-name" placeholder="NSPanel 1"></label>
        <label>IP-adres van de panel <input type="text" id="pn-host" placeholder="192.168.0.105"></label>
        <label>Configuratie (YAML) <textarea id="pn-yaml" rows="14" spellcheck="false" placeholder="cards:&#10;  - type: cardGrid&#10;    title: Licht&#10;    entities: ..."></textarea></label>
        <div class="row"><button class="btn" data-pn="add">Toevoegen</button></div>
      </div>
    </details>
    <h2>Op de panel zetten</h2>
    <ol class="sub">
      <li>Zet de AppDaemon-app <code>nspanel-lovelace-ui</code> uit, anders sturen twee programma's het scherm aan.</li>
      <li>IP-adres van Home Assistant: <input type="text" id="pn-hub" value="${hub}" style="width:14em"> en download <a id="pn-dl" class="btn" href="#">huisconfig.be</a></li>
      <li>Open de webpagina van de panel → <b>Consoles → Manage File system</b> en upload het bestand.</li>
      <li>Zet in <code>autoexec.be</code> (zelfde pagina, ⬇ bewerken/upload) als laatste regel: <code>load("huisconfig.be")</code> en herstart de panel (Console: <code>Restart 1</code>).</li>
    </ol>
    <p class="sub">Terugdraaien: haal die regel weg en herstart; zet AppDaemon weer aan. UDP-poort van de add-on: ${PN ? PN.udp_port : '…'}.
    Het verkeer tussen panel en add-on is niet versleuteld: het blijft op het eigen LAN.</p>
  </div>`;
}

function panelCard(p) {
  const d = (s) => esc(String(s ?? ''));
  return `<div class="card" data-pid="${d(p.id)}">
    <div class="row sp"><h3>${d(p.name)}</h3><span class="pstat"></span></div>
    <label>Naam <input type="text" class="pf-name" value="${d(p.name)}"></label>
    <label>IP-adres <input type="text" class="pf-host" value="${d(p.host)}"></label>
    <label>Configuratie (YAML) <textarea class="pf-yaml" rows="14" spellcheck="false">${d(p.yaml)}</textarea></label>
    <div class="row"><button class="btn" data-pn="save">Opslaan</button><button class="btn danger" data-pn="del">Verwijderen</button>
    <span class="pr"></span></div>
    <div class="sub pfail" ${p.error ? '' : 'hidden'}></div>
  </div>`;
}

function panelStatus(card, p) {
  const ago = p.last_seen == null ? 'nog niet gezien' : p.last_seen < 5 ? 'nu' : `${p.last_seen} s geleden`;
  card.querySelector('.pstat').innerHTML = p.online
    ? `<span class="chip ok">online</span> <span class="sub">${p.temperature != null ? p.temperature.toFixed(1) + ' °C · ' : ''}${p.rssi != null ? p.rssi + ' dBm · ' : ''}${p.greeted ? 'scherm gestart' : 'wacht op HA'}</span>`
    : `<span class="chip bad">offline</span> <span class="sub">${esc(ago)}</span>`;
  card.querySelector('.pr').innerHTML = [1, 2].map((n) => {
    const on = p.relays[n - 1];
    return `<button class="btn" data-pn="relay" data-n="${n}" data-on="${on ? 0 : 1}" ${on == null ? 'disabled' : ''}>Relais ${n}: ${on == null ? '?' : on ? 'aan' : 'uit'}</button>`;
  }).join('');
  const f = card.querySelector('.pfail');
  f.hidden = !p.error;
  f.textContent = p.error ? `Config niet te gebruiken: ${p.error}` : '';
}

function panelDraw() {
  const list = $('#pnlist');
  if (!list || !PN) return;
  const ids = PN.panels.map((p) => p.id).join(',');
  if (list.dataset.ids !== ids) { // alleen de kaarten opnieuw maken als er panelen bij of af zijn: anders verlies je wat je aan het typen bent
    list.dataset.ids = ids;
    list.innerHTML = PN.panels.map(panelCard).join('') || '<p class="empty">Nog geen paneel.</p>';
  }
  for (const p of PN.panels) {
    const card = list.querySelector(`[data-pid="${CSS.escape(p.id)}"]`);
    if (card) panelStatus(card, p);
  }
  const dl = $('#pn-dl');
  if (dl) dl.href = `api/panels/script?hub=${encodeURIComponent(($('#pn-hub') || {}).value || location.hostname)}`;
}

document.addEventListener('input', (e) => { if (e.target.id === 'pn-hub') panelDraw(); });

document.addEventListener('click', act(async (e) => {
  const b = e.target.closest('[data-pn]');
  if (!b) return;
  const card = b.closest('[data-pid]');
  const pid = card && card.dataset.pid;
  const val = (sel) => card.querySelector(sel).value;
  switch (b.dataset.pn) {
    case 'add':
      await api('POST', 'panels', { name: $('#pn-name').value, host: $('#pn-host').value, yaml: $('#pn-yaml').value });
      $('#pn-name').value = $('#pn-host').value = $('#pn-yaml').value = '';
      $('#pnnew').open = false;
      toast('Paneel toegevoegd');
      break;
    case 'save':
      await api('PUT', `panels/${pid}`, { name: val('.pf-name'), host: val('.pf-host'), yaml: val('.pf-yaml') });
      toast('Opgeslagen');
      break;
    case 'del':
      if (!confirm('Dit paneel verwijderen? De relais verdwijnen dan ook uit Home Assistant.')) return;
      await api('DELETE', `panels/${pid}`);
      break;
    case 'relay':
      await api('POST', `panels/${pid}/relay/${b.dataset.n}`, { on: b.dataset.on === '1' });
      break;
  }
  await panelLoad();
}));
