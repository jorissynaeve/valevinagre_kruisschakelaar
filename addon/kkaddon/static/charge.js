'use strict';
// Tabblad Laden: alle laadsessies van alle laadpalen van deze installatie, met filter, CSV en een factuur per huis (kkaddon/chargehub.py).
// Wordt vóór app.js geladen en gebruikt zijn globals ($, api, act, toast, esc) pas bij gebruik.

let CH = null; // {me, chargers: {id: naam}, sessions: [...], active: [...]}
let CHERR = '';
let CHINV = false; // de factuur staat open
const CHF = (() => { try { return { house: '', charger: '', from: '', to: '', number: '', note: '', ...JSON.parse(localStorage.getItem('kk-charge') || '{}') }; } catch { return { house: '', charger: '', from: '', to: '', number: '', note: '' }; } })();
const chSave = () => { try { localStorage.setItem('kk-charge', JSON.stringify(CHF)); } catch { /* geen opslag */ } };

async function loadCharge() {
  try { CH = await api('GET', 'charge/sessions'); CHERR = ''; } catch (e) { CH = null; CHERR = e.message; }
  const box = $('#chargebox');
  if (box) box.innerHTML = viewChargeBody();
}

const chDay = (s, end) => { // 'YYYY-MM-DD' (lokale tijd) -> seconden; end = het einde van die dag
  if (!s) return null;
  const [y, m, d] = s.split('-').map(Number);
  return Math.floor(new Date(y, m - 1, d + (end ? 1 : 0)).getTime() / 1000);
};
const chDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const chStamp = (t) => new Date(t * 1000).toLocaleString('nl-BE', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
const chDur = (s) => `${Math.floor(s / 3600)}u${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}`;
const chKwh = (v) => (v == null ? '—' : v.toLocaleString('nl-BE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const chEur = (v) => (v == null ? '—' : `€ ${v.toLocaleString('nl-BE', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const chTariff = (v) => `€ ${v.toLocaleString('nl-BE', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;

function chRows() { // de sessies die door het filter komen, nieuwste eerst
  if (!CH) return [];
  const from = chDay(CHF.from, false), to = chDay(CHF.to, true);
  return CH.sessions.filter((e) => (!CHF.house || e.by === CHF.house) && (!CHF.charger || e.charger === CHF.charger)
    && (from == null || e.start >= from) && (to == null || e.start < to)).sort((a, b) => b.start - a.start);
}

function chHouses() { // elk huis dat ooit geladen heeft: id -> naam van de laatste sessie
  const out = new Map();
  for (const e of CH.sessions) out.set(e.by, e.name);
  return [...out.entries()].sort((a, b) => a[1].localeCompare(b[1], 'nl'));
}

const chTotals = (rows) => ({ n: rows.length, seconds: rows.reduce((a, e) => a + e.seconds, 0), kwh: rows.reduce((a, e) => a + (e.kwh || 0), 0),
  cost: Math.round(rows.reduce((a, e) => a + (e.cost || 0), 0) * 100) / 100 }); // de som van de bedragen zoals ze in de log staan

function viewCharge() {
  if (!$('#chargebox')) setTimeout(loadCharge, 0);
  return `<h2>Laadsessies</h2>
    <p class="sub">Alle sessies van de laadpalen van deze installatie: wie (welk huis), wanneer, hoeveel en wat het kost. Kies een huis en een periode en maak er een factuur van.</p>
    <div id="chargebox">${viewChargeBody()}</div>`;
}

function viewChargeBody() {
  if (!CH) return CHERR ? `<div class="sub" style="color:var(--bad)">${esc(CHERR)}</div>` : '<div class="sub">Laden…</div>';
  const rows = chRows(), t = chTotals(rows), houses = chHouses(), many = Object.keys(CH.chargers).length > 1;
  const opt = (v, cur, l) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(l)}</option>`;
  const lokaal = CHF.house === '-';
  const body = rows.map((e) => `<tr>${many ? `<td>${esc(CH.chargers[e.charger] || e.charger)}</td>` : ''}<td>${esc(e.name)}</td><td>${chStamp(e.start)}</td><td>${chStamp(e.end)}</td><td>${chDur(e.seconds)}</td>
      <td class="num">${chKwh(e.kwh)}</td><td class="num">${chTariff(e.tariff)}</td><td class="num">${chEur(e.cost)}</td><td>${esc(e.reason)}</td></tr>`).join('');
  const running = (CH.active || []).map((a) => `<div class="sub">Nu bezig: <b>${esc(a.name)}</b> op ${esc(CH.chargers[a.charger] || a.charger)} sinds ${chStamp(a.start)} (komt in de lijst als ze eindigt)</div>`).join('');
  return `<div class="card noprint" id="lgfilter"><div class="row" style="flex-wrap:wrap;gap:10px">
      <div><label>Huis</label><select data-lg="house"><option value="">alle huizen</option>${houses.map(([id, n]) => opt(id, CHF.house, id === '-' ? n : n)).join('')}</select></div>
      ${many ? `<div><label>Laadpaal</label><select data-lg="charger"><option value="">alle</option>${Object.entries(CH.chargers).map(([id, n]) => opt(id, CHF.charger, n)).join('')}</select></div>` : ''}
      <div><label>Van</label><input type="date" data-lg="from" value="${esc(CHF.from)}"></div>
      <div><label>Tot en met</label><input type="date" data-lg="to" value="${esc(CHF.to)}"></div></div>
    <div class="row"><button class="btn small" type="button" data-lgact="month" data-m="0">Deze maand</button><button class="btn small" type="button" data-lgact="month" data-m="1">Vorige maand</button>
      <button class="btn small" type="button" data-lgact="clear">Alles</button><button class="btn small" type="button" data-lgact="reload">Vernieuwen</button></div></div>
    ${running}
    <div class="noprint" id="lgtable">${rows.length ? `<table class="sclog"><tr>${many ? '<th>Laadpaal</th>' : ''}<th>Huis</th><th>Start</th><th>Einde</th><th>Duur</th><th>kWh</th><th>Tarief</th><th>Kosten</th><th>Einde door</th></tr>${body}
      <tr><th colspan="${many ? 4 : 3}">${t.n} ${t.n === 1 ? 'sessie' : 'sessies'}</th><th>${chDur(t.seconds)}</th><th class="num">${chKwh(t.kwh)}</th><th></th><th class="num">${chEur(t.cost)}</th><th></th></tr></table>` : '<div class="sub">Geen sessies in deze selectie.</div>'}</div>
    <div class="row noprint" style="margin:10px 0"><button class="btn" type="button" data-lgact="csv" ${rows.length ? '' : 'disabled'}>CSV van deze selectie</button>
      <button class="btn primary" type="button" data-lgact="invoice" ${rows.length && CHF.house && !lokaal ? '' : 'disabled'} title="${CHF.house ? '' : 'Kies eerst een huis'}">Factuur maken</button></div>
    ${CHF.house ? '' : '<div class="sub noprint">Kies een huis om er een factuur voor te maken.</div>'}
    ${CHINV && CHF.house && !lokaal ? chInvoice(rows, t) : ''}`;
}

function chInvoice(rows, t) {
  const house = (chHouses().find(([id]) => id === CHF.house) || [CHF.house, CHF.house])[1];
  const period = CHF.from || CHF.to ? `${CHF.from ? new Date(chDay(CHF.from) * 1000).toLocaleDateString('nl-BE') : 'het begin'} t.e.m. ${CHF.to ? new Date(chDay(CHF.to) * 1000).toLocaleDateString('nl-BE') : 'nu'}` : 'alle sessies';
  const lines = [...rows].sort((a, b) => a.start - b.start).map((e) => `<tr><td>${chStamp(e.start)}</td><td>${chDur(e.seconds)}</td><td class="num">${chKwh(e.kwh)}</td><td class="num">${chTariff(e.tariff)}</td><td class="num">${chEur(e.cost)}</td></tr>`).join('');
  const missing = rows.filter((e) => e.cost == null).length;
  return `<div id="chinvoice" class="card"><div class="row sp noprint"><h2 style="margin:0">Factuur</h2><span class="row"><button class="btn small" type="button" data-lgact="print">Afdrukken of opslaan als PDF</button><button class="btn small" type="button" data-lgact="close">Sluiten</button></span></div>
    <div class="row noprint" style="flex-wrap:wrap;gap:10px"><div><label>Factuurnummer</label><input type="text" maxlength="40" data-lg="number" value="${esc(CHF.number)}" placeholder="bv. 2026-11-01"></div>
      <div style="flex:1;min-width:220px"><label>Opmerking (bv. rekeningnummer)</label><input type="text" maxlength="200" data-lg="note" value="${esc(CHF.note)}"></div></div>
    <div class="invoice">
      <h1 style="margin:0">Factuur laden${CHF.number ? ` ${esc(CHF.number)}` : ''}</h1>
      <p>Datum: ${new Date().toLocaleDateString('nl-BE')}<br>Van: <b>${esc(CH.me)}</b><br>Aan: <b>${esc(house)}</b><br>Periode: ${esc(period)}</p>
      <table><tr><th>Start</th><th>Duur</th><th>kWh</th><th>Tarief</th><th>Bedrag</th></tr>${lines}
        <tr><th>Totaal (${t.n} ${t.n === 1 ? 'sessie' : 'sessies'})</th><th>${chDur(t.seconds)}</th><th class="num">${chKwh(t.kwh)}</th><th></th><th class="num">${chEur(t.cost)}</th></tr></table>
      ${missing ? `<p class="sub">Bij ${missing} ${missing === 1 ? 'sessie' : 'sessies'} is de kWh niet gemeten: die tellen voor € 0,00 mee.</p>` : ''}
      ${CHF.note ? `<p>${esc(CHF.note)}</p>` : ''}</div></div>`;
}

function chCsv(rows) {
  const q = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const num = (v) => (v == null ? '' : String(v).replace('.', ','));
  const head = ['laadpaal', 'huis', 'start', 'einde', 'duur (s)', 'kWh', 'tarief (€/kWh)', 'kosten (€)', 'reden'];
  const lines = rows.map((e) => [q(CH.chargers[e.charger] || e.charger), q(e.name), q(chStamp(e.start)), q(chStamp(e.end)), e.seconds, num(e.kwh), num(e.tariff), num(e.cost), q(e.reason)].join(';'));
  return `﻿${head.join(';')}\n${lines.join('\n')}\n`;
}

const CH_ACTIONS = {
  reload: loadCharge,
  clear: () => { Object.assign(CHF, { from: '', to: '' }); CHINV = false; chSave(); $('#chargebox').innerHTML = viewChargeBody(); },
  month: (d) => {
    const now = new Date(), a = new Date(now.getFullYear(), now.getMonth() - +d.m, 1), b = new Date(a.getFullYear(), a.getMonth() + 1, 0);
    Object.assign(CHF, { from: chDate(a), to: chDate(b) });
    chSave();
    $('#chargebox').innerHTML = viewChargeBody();
  },
  csv: () => {
    const url = URL.createObjectURL(new Blob([chCsv(chRows())], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = 'laadsessies.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  invoice: () => { CHINV = true; $('#chargebox').innerHTML = viewChargeBody(); $('#chinvoice').scrollIntoView(); },
  close: () => { CHINV = false; $('#chargebox').innerHTML = viewChargeBody(); },
  print: () => { document.body.classList.add('printing-invoice'); window.print(); document.body.classList.remove('printing-invoice'); },
};

document.addEventListener('click', (ev) => {
  const el = ev.target.closest('[data-lgact]');
  if (!el || !CH_ACTIONS[el.dataset.lgact]) return;
  ev.preventDefault();
  act(() => CH_ACTIONS[el.dataset.lgact](el.dataset))();
});

document.addEventListener('change', (ev) => {
  const t = ev.target;
  if (!t.dataset || !t.dataset.lg || !CH) return;
  CHF[t.dataset.lg] = t.value.trim();
  if (t.dataset.lg === 'house') CHINV = false;
  chSave();
  $('#chargebox').innerHTML = viewChargeBody();
});
