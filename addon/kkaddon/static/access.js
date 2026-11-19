'use strict';
// Tabblad Toegang: het logboek van detecties en deuren (kkaddon/accesshub.py). Een bron is een punt van een blok dat je zelf aanzet:
// een Reolink-camera, een PIR of bewegingsmelder, een Nuki-slot of een deurcontact.
// Wordt vóór app.js geladen en gebruikt zijn globals ($, api, act, toast, esc) pas bij gebruik.

let AC = null; // {events: [...], sources: [{key, device, driver, label, kind, on}], missing: [...]}
let ACERR = '';
let ACSET = false; // het paneel met bronnen staat open
let ACQ = ''; // zoekterm in dat paneel
const ACF = (() => { try { return { device: '', from: '', to: '', ...JSON.parse(localStorage.getItem('kk-access') || '{}') }; } catch { return { device: '', from: '', to: '' }; } })();
const acSave = () => { try { localStorage.setItem('kk-access', JSON.stringify(ACF)); } catch { /* geen opslag */ } };

async function loadAccess() {
  try { AC = await api('GET', 'access'); ACERR = ''; } catch (e) { AC = null; ACERR = e.message; }
  const box = $('#accessbox');
  if (box) box.innerHTML = viewAccessBody();
}

const acDay = (s, end) => { // 'YYYY-MM-DD' (lokale tijd) -> seconden; end = het einde van die dag
  if (!s) return null;
  const [y, m, d] = s.split('-').map(Number);
  return Math.floor(new Date(y, m - 1, d + (end ? 1 : 0)).getTime() / 1000);
};
const acDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const acStamp = (t) => new Date(t * 1000).toLocaleString('nl-BE', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
const acDur = (s) => (s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s` : `${Math.floor(s / 3600)}u${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}`);

function acRows() { // de regels die door het filter komen, nieuwste eerst
  if (!AC) return [];
  const from = acDay(ACF.from, false), to = acDay(ACF.to, true);
  return AC.events.filter((e) => (!ACF.device || e.name === ACF.device) && (from == null || e.start >= from) && (to == null || e.start < to))
    .sort((a, b) => b.start - a.start || b.id - a.id);
}

function viewAccess() {
  if (!$('#accessbox')) setTimeout(loadAccess, 0);
  return `<h2>Toegang</h2>
    <p class="sub">Alle detecties en deuren op één lijst: beweging, persoon en voertuig van een camera, een PIR of bewegingsmelder, de stand van een Nuki-slot. Je kiest zelf welke punten meetellen onder <b>Bronnen</b>.</p>
    <div id="accessbox">${viewAccessBody()}</div>`;
}

function acSources() {
  const on = AC.sources.filter((s) => s.on).length;
  const groups = new Map();
  for (const s of AC.sources) groups.set(s.device, [...(groups.get(s.device) || []), s]);
  const body = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0], 'nl')).map(([dev, pts]) => `<div class="acgroup"><div class="sub"><b>${esc(dev)}</b></div>
      ${pts.map((s) => `<label class="chk acpt" data-ack="${esc(`${dev} ${s.label}`.toLowerCase())}"><input type="checkbox" data-acsrc="${esc(s.key)}" ${s.on ? 'checked' : ''}> ${esc(s.label)} <span class="sub">${s.kind === 'bool' ? 'periode' : 'stand'}</span></label>`).join('')}</div>`).join('');
  return `<div class="card noprint"><div class="row sp"><h3 style="margin:0">Bronnen <span class="sub">${on} aan</span></h3><button class="btn small" type="button" data-acact="sources">Klaar</button></div>
    <p class="sub">Een <b>periode</b> (aan en uit, zoals beweging) krijgt een begin- en eindtijd. Een <b>stand</b> (zoals Ontgrendeld bij een slot) is één regel per verandering. Wat offline of onbekend is geeft nooit een regel.</p>
    ${AC.missing.length ? `<div class="sub" style="color:var(--warn)">${AC.missing.length} ${AC.missing.length === 1 ? 'bron staat' : 'bronnen staan'} aan maar het apparaat is weg: ${AC.missing.map(esc).join(', ')}.</div>` : ''}
    <label class="msearch">${ico('search', 15)}<input type="search" id="acq" placeholder="Zoek een apparaat of punt" autocomplete="off" value="${esc(ACQ)}"></label>
    <div id="acsrclist">${body || '<div class="sub">Geen apparaten met een bool- of enum-punt. Voeg eerst een camera, slot of melder toe via + Module.</div>'}</div></div>`;
}

function viewAccessBody() {
  if (!AC) return ACERR ? `<div class="sub" style="color:var(--bad)">${esc(ACERR)}</div>` : '<div class="sub">Laden…</div>';
  const rows = acRows(), names = [...new Set(AC.events.map((e) => e.name))].sort((a, b) => a.localeCompare(b, 'nl'));
  const opt = (v, cur, l) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(l)}</option>`;
  const detail = (e) => (e.text ? esc(e.text) : e.end == null ? '<span class="badge warn">bezig</span>' : `${acStamp(e.end).slice(-8)} · ${acDur(e.end - e.start)}`);
  const body = rows.map((e) => `<tr><td>${acStamp(e.start)}</td><td>${esc(e.name)}</td><td>${esc(e.label)}</td><td>${detail(e)}</td></tr>`).join('');
  const nsrc = AC.sources.filter((s) => s.on).length;
  return `<div class="card noprint"><div class="row" style="flex-wrap:wrap;gap:10px">
      <div><label>Apparaat</label><select data-acf="device"><option value="">alle</option>${names.map((n) => opt(n, ACF.device, n)).join('')}</select></div>
      <div><label>Van</label><input type="date" data-acf="from" value="${esc(ACF.from)}"></div>
      <div><label>Tot en met</label><input type="date" data-acf="to" value="${esc(ACF.to)}"></div></div>
    <div class="row"><button class="btn small" type="button" data-acact="today">Vandaag</button><button class="btn small" type="button" data-acact="clear">Alles</button>
      <button class="btn small" type="button" data-acact="reload">Vernieuwen</button><button class="btn small ${ACSET ? 'primary' : ''}" type="button" data-acact="sources">Bronnen (${nsrc})</button></div></div>
    ${ACSET ? acSources() : ''}
    <div class="noprint">${rows.length ? `<table class="sclog"><tr><th>Wanneer</th><th>Apparaat</th><th>Punt</th><th>Einde · duur</th></tr>${body}
      <tr><th colspan="4">${rows.length} ${rows.length === 1 ? 'regel' : 'regels'}</th></tr></table>`
    : `<div class="sub">${nsrc ? 'Nog geen regels in deze selectie.' : 'Nog geen bronnen aan: kies onder <b>Bronnen</b> welke punten meetellen.'}</div>`}</div>
    <div class="row noprint" style="margin:10px 0"><button class="btn" type="button" data-acact="csv" ${rows.length ? '' : 'disabled'}>CSV van deze selectie</button>
      <button class="btn small danger" type="button" data-acact="wipe" ${AC.events.length ? '' : 'disabled'}>Log wissen</button></div>`;
}

function acCsv(rows) {
  const q = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const lines = rows.map((e) => [q(e.name), q(e.label), q(acStamp(e.start)), q(e.end == null ? '' : acStamp(e.end)), e.end == null ? '' : e.end - e.start, q(e.text)].join(';'));
  return `﻿apparaat;punt;start;einde;duur (s);melding\n${lines.join('\n')}\n`;
}

const AC_ACTIONS = {
  reload: loadAccess,
  clear: () => { Object.assign(ACF, { from: '', to: '' }); acSave(); $('#accessbox').innerHTML = viewAccessBody(); },
  today: () => { const t = acDate(new Date()); Object.assign(ACF, { from: t, to: t }); acSave(); $('#accessbox').innerHTML = viewAccessBody(); },
  sources: () => { ACSET = !ACSET; $('#accessbox').innerHTML = viewAccessBody(); },
  csv: () => {
    const url = URL.createObjectURL(new Blob([acCsv(acRows())], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = 'toegang.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  wipe: async () => {
    if (!confirm('Het hele toegangslogboek wissen? Dit kan niet ongedaan worden. De bronnen blijven staan.')) return;
    await api('DELETE', 'access/events');
    await loadAccess();
  },
};

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-acact]');
  if (!el || !AC_ACTIONS[el.dataset.acact]) return;
  ev.preventDefault();
  act(() => AC_ACTIONS[el.dataset.acact](el.dataset))();
});

document.addEventListener('input', (ev) => {
  if (ev.target.id !== 'acq') return;
  ACQ = ev.target.value;
  const q = ACQ.trim().toLowerCase();
  document.querySelectorAll('#acsrclist .acgroup').forEach((g) => {
    let any = !q;
    g.querySelectorAll('.acpt').forEach((p) => { const hit = !q || p.dataset.ack.includes(q); p.hidden = !hit; if (hit) any = true; });
    g.hidden = !any;
  });
});

document.addEventListener('change', async (ev) => {
  const t = ev.target;
  if (!t.dataset || !AC) return;
  if (t.dataset.acf) {
    ACF[t.dataset.acf] = t.value.trim();
    acSave();
    $('#accessbox').innerHTML = viewAccessBody();
  } else if (t.dataset.acsrc) {
    const keys = [...document.querySelectorAll('#acsrclist [data-acsrc]')].filter((x) => x.checked).map((x) => x.dataset.acsrc);
    try { await api('PUT', 'access/sources', { keys }); } catch (e) { toast(e.message); }
    await loadAccess();
  }
});
