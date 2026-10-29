'use strict';
// HA-kringen: de kruisschakeling zoals AppDaemon hem deed. Alle entiteiten van een kring volgen elkaar.
// Elke kring heeft een eigen aan/uit, zodat je ze één voor één van AppDaemon naar deze add-on kunt verhuizen.
// Wordt vóór app.js geladen en gebruikt zijn globals (S, api, act, render, refresh, ...) pas bij gebruik.

const G = { parsed: null };

function groupName(id) {
  const g = ((S && S.groups) || []).find((x) => x.id === id);
  return g ? g.name : id;
}

const memberText = (m) => (m.on === true ? 'aan' : m.on === false ? 'uit' : 'niet beschikbaar');
const memberColor = (m) => (m.on === true ? 'var(--on)' : m.on === false ? 'var(--mute)' : 'var(--bad)');

// Aan/uit-knop en leden van een kring; op het tabblad Lichten én op HA-kringen
function groupPower(g) {
  const lamps = g.members.filter((m) => m.lamp && m.on !== null);
  const basis = lamps.length ? lamps : g.members; // de lamp-relais bepalen de stand, anders iedereen
  const on = basis.some((m) => m.on === true);
  const unknown = basis.every((m) => m.on === null);
  return `<button class="power ${on ? 'on' : unknown ? 'unknown' : ''}" data-grp="gtoggle" data-id="${esc(g.id)}">${on ? 'Aan' : unknown ? 'Onbekend' : 'Uit'}</button>`;
}

function memberRow(g, m) {
  return `<div class="gm"><span class="gdot" style="background:${memberColor(m)}"></span>
    <a href="#" class="gml" data-grp="member" data-id="${esc(g.id)}" data-e="${esc(m.entity)}" title="Alleen deze entiteit wisselen"><code>${esc(m.entity)}</code></a> <span class="sub">${memberText(m)}</span>
    <a href="#" class="lamptag ${m.lamp ? 'on' : ''}" data-grp="lamp" data-id="${esc(g.id)}" data-e="${esc(m.entity)}" title="${m.lamp ? 'Dit relais schakelt de lamp (klik om te wissen)' : 'Markeer als relais dat de lamp schakelt'}">${m.lamp ? 'lamp-relais' : 'lamp?'}</a></div>`;
}

// Op het tabblad Lichten: elke kring als een licht dat je kunt bedienen
function groupLightCards() {
  const gs = S.groups || [];
  if (!gs.length) return '';
  return `<h2>Kringen via Home Assistant</h2><div class="grid">${gs.map((g) => `<div class="card">
      <div class="row sp"><h3>${esc(g.name)}</h3><span class="badge ${g.enabled ? 'ok' : ''}">${g.enabled ? 'volgt elkaar' : 'alleen bediening'}</span></div>
      <div class="sub">${g.members.length} entiteiten</div>${groupPower(g)}
      <details><summary class="sub" style="cursor:pointer">Entiteiten (klik om er één te wisselen)</summary>${g.members.map((m) => memberRow(g, m)).join('')}</details></div>`).join('')}</div>`;
}

function viewGroups() {
  const gs = S.groups || [];
  const active = gs.filter((g) => g.enabled).length;
  const cards = gs.map((g) => {
    const known = g.members.filter((m) => m.on !== null);
    const mixed = known.length > 1 && known.some((m) => m.on !== known[0].on);
    return `<div class="card">
      <div class="row sp"><h3>${esc(g.name)}</h3>
        <label style="margin:0;color:var(--ink);cursor:pointer"><input type="checkbox" data-grp-toggle="${esc(g.id)}" ${g.enabled ? 'checked' : ''}> ${g.enabled ? 'Actief' : 'Uit'}</label></div>
      <div class="sub">${g.members.length} entiteiten · ${g.enabled ? 'volgen elkaar' : 'volgen elkaar niet (alleen meekijken en bedienen)'}${mixed ? ' <span class="badge warn">staan niet gelijk</span>' : ''}</div>
      ${groupPower(g)}
      <div>${g.members.map((m) => memberRow(g, m)).join('')}</div>
      <div class="row"><button class="btn small" data-grp="edit" data-id="${esc(g.id)}">Bewerken</button>
        <button class="btn small danger" data-grp="del" data-id="${esc(g.id)}">Verwijderen</button></div></div>`;
  }).join('');
  return `<p class="sub" style="margin-top:12px">Kringen zoals in AppDaemon (<code>kruisschakelaar</code>): verandert er één entiteit van een kring doordat iemand op een touch drukt,
    dan krijgen de andere dezelfde stand. Een kring doet alleen iets als hij hier <b>Actief</b> is. <b>Zet dezelfde kring in AppDaemon uit</b>, anders werken ze elkaar tegen.</p>
    <div class="row"><button class="btn primary" data-grp="import">AppDaemon-YAML importeren</button>
    <button class="btn" data-grp="new">Nieuwe kring</button>
    <button class="btn" data-grp="lampmatch" ${gs.length ? '' : 'disabled'}>Lamp-relais kiezen op naam…</button>
    <button class="btn" data-grp="alloff" ${active ? '' : 'disabled'}>Alles uit${active ? ` (${active} actief)` : ''}</button></div>
    ${cards ? `<div class="grid" style="margin-top:12px">${cards}</div>` : '<p class="empty">Nog geen kringen. Plak je oude AppDaemon-configuratie bij <b>AppDaemon-YAML importeren</b> (alles komt uitgeschakeld binnen) of maak er zelf een met <b>Nieuwe kring</b>.</p>'}`;
}

function parsedHtml(r) {
  const rows = r.groups.map((g, i) => {
    const miss = g.missing === null ? '' : g.missing.length
      ? `<div class="sub" style="color:var(--warn)">${g.missing.length} onbekend in HA: ${esc(g.missing.slice(0, 3).join(', '))}${g.missing.length > 3 ? ', …' : ''}</div>`
      : '<div class="sub">alle entiteiten bekend in HA</div>';
    return `<label style="display:block;margin:0;padding:6px 0;border-top:1px solid var(--line);color:var(--ink)"><input type="checkbox" data-g="${i}" checked>
      <b>${esc(g.name)}</b> <span class="sub">${g.entities.length} entiteiten</span>${g.exists ? ' <span class="badge">bestaat al: wordt bijgewerkt</span>' : ''}${miss}
      ${g.warnings.map((w) => `<div class="sub">${esc(w)}</div>`).join('')}</label>`;
  }).join('');
  const sk = r.skipped.length ? `<p class="sub">Overgeslagen: ${r.skipped.map((s) => `${esc(s.key)} (${esc(s.reason)})`).join(', ')}</p>` : '';
  return `<p class="sub"><b>${r.groups.length} kringen gevonden.</b> Nieuwe kringen komen <b>uitgeschakeld</b> binnen; bestaande houden hun stand.${r.ha ? '' : ' Home Assistant is niet verbonden: kan niet controleren of de entiteiten bestaan.'}</p>
    <div style="max-height:40vh;overflow:auto">${rows || '<p class="empty">Geen kringen gevonden.</p>'}</div>${sk}`;
}

function openGroupImport() {
  G.parsed = null;
  smallDialog(`<h3>AppDaemon-kringen importeren</h3>
    <p class="sub">Plak de YAML van <code>kruisschakelaar</code> (een heel <code>apps.yaml</code> mag ook; wat geen kring is wordt overgeslagen) of kies het bestand.</p>
    <textarea id="gyaml" rows="9" spellcheck="false" placeholder="kruisschakelaar_lamp_keuken:&#10;  module: kruisschakelaar&#10;  class: Kruisschakelaar&#10;  entities:&#10;    - switch.…"></textarea>
    <div class="row" style="margin-top:6px"><input type="file" id="gfile" accept=".yaml,.yml,text/yaml,text/plain"><button class="btn small" type="button" data-grp="parse">Controleren</button></div>
    <div id="gpreview" style="margin-top:8px"></div>`,
  async () => {
    if (!G.parsed) throw new Error('Klik eerst op Controleren');
    const picked = [...document.querySelectorAll('#gpreview input[data-g]:checked')].map((c) => G.parsed.groups[+c.dataset.g]);
    if (!picked.length) throw new Error('Kies minstens één kring');
    await api('POST', 'groups', { groups: picked.map((g) => ({ id: g.id, name: g.name, entities: g.entities })) });
    toast(`${picked.length} ${picked.length === 1 ? 'kring' : 'kringen'} toegevoegd (uitgeschakeld)`);
  });
}

// ---------------------------------------------------------------- kring maken of bewerken

let GE = null; // open dialoog: {id (null = nieuw), name}

const slug = (s) => s.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 60);

function readGroupRows() {
  return [...document.querySelectorAll('#gents input[data-ge]')].map((i) => i.value.trim());
}

function groupRowsHtml(rows) {
  return rows.map((v, i) => `<div class="row" style="margin:3px 0;flex-wrap:nowrap"><input type="text" data-ge list="gentlist" value="${esc(v)}" placeholder="switch.… of light.…" autocomplete="off" spellcheck="false">
    <button class="btn small danger" type="button" data-grp="gerow" data-i="${i}" title="Deze entiteit uit de kring halen">✕</button></div>`).join('');
}

async function openGroupEdit(id) {
  const g = id ? S.groups.find((x) => x.id === id) : null;
  GE = { id: g ? g.id : null };
  let ents = [];
  try { ents = (await api('GET', 'ha/entities')).entities; } catch { /* zonder suggesties kan het ook */ }
  const rows = g ? g.members.map((m) => m.entity) : ['', ''];
  smallDialog(`<h3>${g ? 'Kring bewerken' : 'Nieuwe kring'}</h3>
    <p class="sub">Alle entiteiten van een kring volgen elkaar. Een kring heeft minstens 2 entiteiten.${g && g.enabled ? ' Deze kring is actief: wijzigingen gelden meteen.' : ''}</p>
    ${S.ha.connected ? '' : '<p class="sub" style="color:var(--bad)">Home Assistant is niet verbonden: typ de entiteit-id zelf, zonder suggesties.</p>'}
    <label for="gname">Naam</label><input type="text" id="gname" maxlength="60" value="${esc(g ? g.name : '')}" placeholder="bv. Keuken">
    <datalist id="gentlist">${ents.map((e) => `<option value="${esc(e.entity_id)}">${esc(e.name)}</option>`).join('')}</datalist>
    <label>Entiteiten</label><div id="gents">${groupRowsHtml(rows)}</div>
    <div class="row" style="margin-top:6px"><button class="btn small" type="button" data-grp="geadd">Entiteit toevoegen</button>
    ${g ? '<span class="sub">Een lamp-relais of volgorde wijzig je in de kaart zelf.</span>' : ''}</div>`,
  async () => {
    const name = $('#gname').value.trim();
    const entities = [...new Set(readGroupRows().filter(Boolean))];
    if (GE.id) {
      await api('PUT', `groups/${GE.id}`, { name: name || undefined, entities });
    } else {
      if (!name) throw new Error('Geef de kring een naam');
      const taken = new Set((S.groups || []).map((x) => x.id));
      let gid = slug(name) || 'kring', n = 2;
      while (taken.has(gid)) gid = `${slug(name) || 'kring'}_${n++}`;
      await api('POST', 'groups', { groups: [{ id: gid, name, entities }] });
      toast(`Kring ${name} toegevoegd (uitgeschakeld)`);
    }
  });
}

const GROUP_ACTIONS = {
  new: () => openGroupEdit(null),
  edit: (d) => openGroupEdit(d.id),
  geadd: () => {
    const rows = readGroupRows();
    rows.push('');
    $('#gents').innerHTML = groupRowsHtml(rows);
    const last = [...document.querySelectorAll('#gents input[data-ge]')].pop();
    if (last) last.focus();
  },
  gerow: (d) => {
    const rows = readGroupRows();
    rows.splice(+d.i, 1);
    $('#gents').innerHTML = groupRowsHtml(rows.length ? rows : ['']);
  },
  lamp: async (d) => {
    const g = S.groups.find((x) => x.id === d.id);
    const lamp = g.lamp.includes(d.e) ? g.lamp.filter((x) => x !== d.e) : [...g.lamp, d.e];
    await api('PUT', `groups/${d.id}`, { lamp });
    await refresh();
  },
  lampmatch: async () => {
    const word = (prompt('Welke entiteiten schakelen de lamp? Geef een deel van de naam, bv. dingtian. Alle kringen met zo\'n entiteit krijgen die als lamp-relais.', 'dingtian') || '').trim().toLowerCase();
    if (!word) return;
    const hits = S.groups.map((g) => ({ g, lamp: g.members.filter((m) => m.entity.toLowerCase().includes(word)).map((m) => m.entity) })).filter((x) => x.lamp.length);
    if (!hits.length) throw new Error(`Geen entiteit met "${word}" gevonden`);
    if (!confirm(`${hits.length} van ${S.groups.length} kringen hebben een entiteit met "${word}". Die worden hun lamp-relais (een eerdere keuze wordt vervangen). Doorgaan?`)) return;
    for (const h of hits) await api('PUT', `groups/${h.g.id}`, { lamp: h.lamp });
    toast(`Lamp-relais ingesteld bij ${hits.length} kringen`);
    await refresh();
  },
  gtoggle: async (d) => {
    await api('POST', `groups/${d.id}/cmd`, { op: 'toggle' }).catch((e) => { throw new Error(`${e.message}: geen enkel lid is bereikbaar?`); });
  },
  member: async (d) => {
    await api('POST', `groups/${d.id}/cmd`, { op: 'toggle', entity: d.e }).catch((e) => { throw new Error(`${e.message}: ${d.e} is niet bereikbaar?`); });
  },
  import: () => openGroupImport(),
  parse: async () => {
    const r = await api('POST', 'groups/parse', { yaml: $('#gyaml').value });
    G.parsed = r;
    $('#gpreview').innerHTML = parsedHtml(r);
  },
  del: async (d) => {
    if (!confirm(`Kring "${groupName(d.id)}" verwijderen? De lampen zelf blijven zoals ze zijn.`)) return;
    await api('DELETE', `groups/${d.id}`);
    await refresh();
  },
  alloff: async () => {
    const r = await api('POST', 'groups/all-off', {});
    toast(`${r.disabled} ${r.disabled === 1 ? 'kring' : 'kringen'} uitgezet`);
    await refresh();
  },
};

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-grp]');
  if (!el || !GROUP_ACTIONS[el.dataset.grp]) return;
  ev.preventDefault();
  act(GROUP_ACTIONS[el.dataset.grp])(el.dataset);
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (t.dataset && t.dataset.grpToggle) {
    act(async () => {
      try {
        await api('PUT', `groups/${t.dataset.grpToggle}`, { enabled: t.checked });
      } finally {
        await refresh(); // ook bij een weigering (conflict): het vinkje terug naar wat de add-on werkelijk doet
      }
      toast(t.checked ? `Kring ${groupName(t.dataset.grpToggle)} is actief: zet hem nu in AppDaemon uit` : `Kring ${groupName(t.dataset.grpToggle)} staat uit`);
    })();
  }
  if (t.id === 'gfile' && t.files[0]) t.files[0].text().then((txt) => { $('#gyaml').value = txt; });
});
