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

function viewGroups() {
  const gs = S.groups || [];
  const active = gs.filter((g) => g.enabled).length;
  const cards = gs.map((g) => {
    const known = g.members.filter((m) => m.on !== null);
    const mixed = known.length > 1 && known.some((m) => m.on !== known[0].on);
    return `<div class="card">
      <div class="row sp"><h3>${esc(g.name)}</h3>
        <label style="margin:0;color:var(--ink);cursor:pointer"><input type="checkbox" data-grp-toggle="${esc(g.id)}" ${g.enabled ? 'checked' : ''}> ${g.enabled ? 'Actief' : 'Uit'}</label></div>
      <div class="sub">${g.members.length} entiteiten · ${g.enabled ? 'volgen elkaar' : 'doet niets (alleen meekijken)'}${mixed ? ' <span class="badge warn">staan niet gelijk</span>' : ''}</div>
      <div>${g.members.map((m) => `<div class="gm"><span class="gdot" style="background:${memberColor(m)}"></span><code>${esc(m.entity)}</code> <span class="sub">${memberText(m)}</span></div>`).join('')}</div>
      <div class="row"><button class="btn small danger" data-grp="del" data-id="${esc(g.id)}">Verwijderen</button></div></div>`;
  }).join('');
  return `<p class="sub" style="margin-top:12px">Kringen zoals in AppDaemon (<code>kruisschakelaar</code>): verandert er één entiteit van een kring doordat iemand op een touch drukt,
    dan krijgen de andere dezelfde stand. Een kring doet alleen iets als hij hier <b>Actief</b> is. <b>Zet dezelfde kring in AppDaemon uit</b>, anders werken ze elkaar tegen.</p>
    <div class="row"><button class="btn primary" data-grp="import">AppDaemon-YAML importeren</button>
    <button class="btn" data-grp="alloff" ${active ? '' : 'disabled'}>Alles uit${active ? ` (${active} actief)` : ''}</button></div>
    ${cards ? `<div class="grid" style="margin-top:12px">${cards}</div>` : '<p class="empty">Nog geen kringen. Plak je oude AppDaemon-configuratie bij <b>AppDaemon-YAML importeren</b>; alles komt uitgeschakeld binnen.</p>'}`;
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

const GROUP_ACTIONS = {
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
