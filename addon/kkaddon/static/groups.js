'use strict';
// Kringen als geheel, onder Instellingen: AppDaemon-YAML importeren, lamp-relais op naam kiezen, alles uitzetten.
// Op het schema is een kring een lamp-blok. Elke kring heeft een eigen aan/uit, zodat je ze één voor één van AppDaemon kunt verhuizen.
// Wordt vóór app.js geladen en gebruikt zijn globals (S, api, act, render, refresh, ...) pas bij gebruik.

const G = { parsed: null };

function groupName(id) {
  const g = ((S && S.groups) || []).find((x) => x.id === id);
  return g ? g.name : id;
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
    await api('POST', 'schema/convert', {}); // meteen op het schema; wat al staat blijft staan
    toast(`${picked.length} ${picked.length === 1 ? 'kring' : 'kringen'} toegevoegd (uitgeschakeld) en op het schema gezet`, 6000);
  });
}

const slug = (s) => s.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 60);

const GROUP_ACTIONS = {
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
  import: () => openGroupImport(),
  parse: async () => {
    const r = await api('POST', 'groups/parse', { yaml: $('#gyaml').value });
    G.parsed = r;
    $('#gpreview').innerHTML = parsedHtml(r);
  },
  alloff: async () => {
    if (!confirm('Alle kringen uitzetten? De add-on volgt dan niets meer: touches bedienen de lampen pas weer als de kringen actief zijn.')) return;
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
