'use strict';
// Tabblad Energie: de meters van de huisjes, de omvormers en hun prijzen invullen in het energiedashboard van HA (kkaddon/energyhub.py).
// Wordt vóór app.js geladen en gebruikt zijn globals ($, api, act, toast, esc, tab, render) pas bij gebruik.

let EN = null; // {rows, price_in, price_out, roles, candidates, prices, ha, applied}: wat je aan het bewerken bent
let ENERR = '';

async function loadEnergy() {
  try { EN = await api('GET', 'energy'); ENERR = ''; } catch (e) { EN = null; ENERR = e.message; }
  const box = $('#energybox');
  if (box) box.innerHTML = viewEnergyBody();
}

function enValue(ent) { // de stand van een meter in HA, voor in de rij
  const c = EN && EN.candidates.find((x) => x.entity === ent);
  return c ? `${c.state} ${c.unit}` : '';
}

function viewEnergy() {
  if (!$('#energybox')) setTimeout(loadEnergy, 0);
  return `<h2>Energie</h2>
    <p class="sub">Alle huisjes op één pagina: het ingebouwde energiedashboard van Home Assistant (<b>Energie</b> in de zijbalk van HA). Hier kies je welke meter wat is en wat de kWh kost; de add-on schrijft dat in dat dashboard. Wat je in HA zelf toevoegt blijft staan.</p>
    <div id="energybox">${viewEnergyBody()}</div>`;
}

function enRow(r, i) {
  const roles = Object.entries(EN.roles).map(([k, v]) => `<option value="${k}" ${r.role === k ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const priced = ['house', 'grid_in', 'grid_out'].includes(r.role);
  const batt = r.role === 'battery';
  return `<tr>
    <td><select data-en="role" data-i="${i}">${roles}</select></td>
    <td><input type="text" list="enlist" spellcheck="false" autocomplete="off" data-en="entity" data-i="${i}" value="${esc(r.entity)}" placeholder="${batt ? 'accu ontladen (kWh)' : r.role === 'grid_out' ? 'teruglevering (kWh)' : 'afname (kWh)'}">
      ${batt || ['house', 'grid_in'].includes(r.role) ? `<input type="text" list="enlist" spellcheck="false" autocomplete="off" data-en="entity_to" data-i="${i}" value="${esc(r.entity_to || '')}" placeholder="${batt ? 'accu laden (kWh)' : 'teruglevering (kWh), mag leeg'}" style="margin-top:4px">` : ''}
      <div class="sub">${esc(enValue(r.entity))}</div></td>
    <td>${['house', 'device'].includes(r.role) ? `<input type="text" data-en="name" data-i="${i}" maxlength="60" value="${esc(r.name)}" placeholder="bv. Huisje 1">` : ''}</td>
    <td>${priced ? `<input type="text" list="enprice" spellcheck="false" data-en="price" data-i="${i}" value="${esc(r.price ?? '')}" placeholder="standaard" style="width:110px">` : ''}</td>
    <td><button class="btn small danger" type="button" data-enact="del" data-i="${i}" title="Rij weghalen">✕</button></td></tr>`;
}

function viewEnergyBody() {
  if (!EN) return ENERR ? `<p class="empty">${esc(ENERR)}</p>` : '<p class="sub">Laden…</p>';
  const used = new Set(EN.rows.flatMap((r) => [r.entity, r.entity_to]));
  const free = EN.candidates.filter((c) => !used.has(c.entity));
  const quick = free.map((c) => `<div class="row" style="margin:2px 0"><span style="flex:1;min-width:220px">${esc(c.name)} <span class="sub">${esc(c.entity)} · ${esc(c.state)} ${esc(c.unit)}</span></span>
      <button class="btn small" type="button" data-enact="add" data-role="house" data-e="${esc(c.entity)}">Huis</button>
      <button class="btn small" type="button" data-enact="add" data-role="solar" data-e="${esc(c.entity)}">Zon</button>
      <button class="btn small" type="button" data-enact="add" data-role="device" data-e="${esc(c.entity)}">Apparaat</button></div>`).join('');
  const rows = EN.rows.map(enRow).join('') || '<tr><td colspan="5" class="sub">Nog niets gekozen. Kies hieronder een meter, of voeg een rij toe.</td></tr>';
  return `${EN.ha ? '' : '<p class="empty">Home Assistant is niet verbonden: opslaan kan, naar HA schrijven pas als de verbinding er is.</p>'}
    <datalist id="enlist">${EN.candidates.map((c) => `<option value="${esc(c.entity)}">${esc(c.name)}</option>`).join('')}</datalist>
    <datalist id="enprice">${EN.prices.map((c) => `<option value="${esc(c.entity)}">${esc(c.name)}</option>`).join('')}</datalist>
    <div class="scroll"><table><thead><tr><th>Rol</th><th>Meter in HA</th><th>Naam</th><th>€ per kWh</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-enact="add" data-role="house">+ Rij</button>
      <span class="sub"><b>Huis</b>: een eigen aansluiting met afname en eventueel teruglevering, apart in het overzicht, zo zie je alle huisjes naast elkaar. <b>Net</b>: afname of teruglevering zonder apart overzicht. <b>Accu</b>: ontladen en laden zijn twee meters.</span></div>
    <h3>Prijs</h3>
    <div class="row"><label style="margin:0">Afname</label><input type="text" list="enprice" data-en="price_in" value="${esc(EN.price_in ?? '')}" placeholder="bv. 0,31" style="width:140px">
      <label style="margin:0">Teruglevering</label><input type="text" list="enprice" data-en="price_out" value="${esc(EN.price_out ?? '')}" placeholder="bv. 0,05" style="width:140px"></div>
    <div class="sub">€ per kWh, of een sensor met de prijs (dynamisch tarief). Een prijs op een rij gaat voor. Zonder prijs rekent HA niet.</div>
    <div class="row" style="margin:12px 0"><button class="btn primary" type="button" data-enact="apply" ${EN.ha ? '' : 'disabled'}>Opslaan en naar HA schrijven</button>
      <button class="btn" type="button" data-enact="save">Alleen opslaan</button></div>
    ${EN.applied ? `<div class="sub">In HA gezet: ${esc(EN.applied)}.</div>` : ''}
    <h3>Meters die HA kent</h3>
    <div class="sub">Sensoren met een kWh-teller (klasse energie). ${free.length ? 'Kies wat ze zijn:' : 'Alle zijn gekozen.'}</div>${quick}`;
}

function enBody() {
  return { rows: EN.rows, price_in: EN.price_in, price_out: EN.price_out };
}

const EN_ACTIONS = {
  add: (d) => {
    const c = d.e && EN.candidates.find((x) => x.entity === d.e);
    EN.rows.push({ role: d.role, entity: d.e || '', entity_to: '', name: c ? c.name.replace(/\s*(energie|energy|totaal|total)\b.*$/i, '').trim() || c.name : '', price: '' });
    $('#energybox').innerHTML = viewEnergyBody();
  },
  del: (d) => { EN.rows.splice(+d.i, 1); $('#energybox').innerHTML = viewEnergyBody(); },
  save: async () => { await api('PUT', 'energy', enBody()); toast('Opgeslagen'); },
  apply: async () => {
    const r = await api('POST', 'energy/apply', enBody());
    EN.applied = r.summary;
    $('#energybox').innerHTML = viewEnergyBody();
    toast('Het energiedashboard in HA is bijgewerkt');
  },
};

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-enact]');
  if (!el || !EN_ACTIONS[el.dataset.enact]) return;
  ev.preventDefault();
  act(() => EN_ACTIONS[el.dataset.enact](el.dataset))();
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (!EN || !t.dataset || !t.dataset.en) return;
  const f = t.dataset.en;
  if (t.dataset.i === undefined) { EN[f] = t.value.trim(); return; } // de algemene prijzen
  const r = EN.rows[+t.dataset.i];
  r[f] = t.value.trim();
  if (f === 'role' || f === 'entity') $('#energybox').innerHTML = viewEnergyBody(); // andere velden of de stand van de meter
});
